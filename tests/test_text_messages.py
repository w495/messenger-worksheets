"""Проверить лимит сообщений, точность текста и доставку после частичного сбоя."""

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock
import uuid

from messenger_codex.bridge import Bridge
from messenger_worksheets.text_messages import split_messages, utf16_length


class TextMessagesTest(unittest.TestCase):
    """Проверить пользовательские границы сообщений без сети и артефактов."""

    def test_utf16_limit_counts_emoji_and_preserves_long_code_and_links(self):
        """Emoji занимают две единицы; код, длинные URL и весь исходный текст сохраняются."""
        self.assertEqual(utf16_length("A🐅Б"), 4)
        text = "Вступление.\n\n```python\n" + "print('🐅')\n" * 800 + "```\nhttps://example.test/" + "a" * 7000
        parts = split_messages(text)
        self.assertGreater(len(parts), 2)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(0 < utf16_length(part) <= 4096 for part in parts))
        self.assertEqual(sum(part.count("🐅") for part in parts), 800)
        self.assertEqual(split_messages("🐅" * 2048), ("🐅" * 2048,))
        self.assertEqual(tuple(map(utf16_length, split_messages("🐅" * 2049))), (4096, 2))

    def test_split_prefers_paragraph_then_line_then_word_without_dropping_spaces(self):
        """Части заканчиваются на читаемой границе, а разделительные пробелы не теряются."""
        for text, limit, first in (
            ("one\n\ntwo three four", 12, "one\n\n"),
            ("one\ntwo three four", 12, "one\n"),
            ("one two three", 8, "one two "),
            ("abcdefghij", 4, "abcd"),
        ):
            with self.subTest(text=text):
                parts = split_messages(text, limit)
                self.assertEqual(parts[0], first)
                self.assertEqual("".join(parts), text)
                self.assertTrue(all(utf16_length(part) <= limit for part in parts))
        self.assertEqual(split_messages(""), ())
        self.assertEqual(split_messages("Текст\n"), ("Текст\n",))

    def test_invalid_limits_never_allow_oversized_messages(self):
        """Некорректный лимит и невозможный односимвольный бюджет явно отклоняются."""
        for limit in (0, -1, 4097, True, "4096", None):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                split_messages("text", limit)
        with self.assertRaises(ValueError):
            split_messages("🐅", 1)
        for function in (split_messages, utf16_length):
            with self.assertRaises(TypeError):
                function(None)


class ReplyTextDeliveryTest(unittest.TestCase):
    """Проверить настоящий Bridge._respond с SQLite и идемпотентным Messenger-дублем."""

    def setUp(self):
        """Создать приватное состояние и заменить модель/сеть, сохранив delivery-код."""
        self.enterContext(redirect_stdout(StringIO()))
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.database = sqlite3.connect(":memory:")
        self.addCleanup(self.database.close)
        self.messenger = Mock()
        self.messenger.chat = "0/0/00000000-0000-0000-0000-000000000000"
        self.messenger.identity = {"guid": "service"}
        self.messenger.new_messages.return_value = []
        self.messenger.send.return_value = 777
        self.bridge = self.make_bridge()

    def make_bridge(self):
        """Создать свежий процессный объект поверх того же durable SQLite-состояния."""
        bridge = Bridge(self.database, self.messenger, self.directory)
        bridge.progress = Mock()
        bridge._draft = Mock(return_value=("OK", "session"))
        bridge._task = Mock(return_value=("OK", ()))
        return bridge

    def test_partial_delivery_restarts_without_model_rerun_or_changing_payloads(self):
        """Неизвестный исход второй части повторяется тем же UUID; первая не отправляется снова."""
        text = "Первый абзац.\n\n" + "🐅" * 2500 + "\n" + "z" * 4500
        self.bridge._draft.return_value = (text, "session")
        attempts, received = [], {}
        failed = False
        def send(parent, part, payload):
            """Имитировать принятие сервером до потери ACK и проверить durable plan до send."""
            nonlocal failed
            stored = self.database.execute("SELECT parts_json FROM reply_text_plans WHERE incoming=123").fetchone()[0]
            planned = json.loads(stored)
            self.assertEqual("".join(planned), text)
            self.assertEqual(self.database.execute("SELECT COUNT(*) FROM updates WHERE stage LIKE 'answer-part-%'").fetchone()[0], len(planned))
            self.assertLessEqual(utf16_length(part), 4096)
            self.assertTrue(payload not in received or received[payload] == part)
            received[payload] = part
            attempts.append((part, payload))
            if len(attempts) == 2 and not failed:
                failed = True
                raise RuntimeError("synthetic lost ACK")
            return 777
        self.messenger.send.side_effect = send
        with self.assertRaisesRegex(RuntimeError, "lost ACK"):
            self.bridge._respond(0, 123, "", {"text": "hello"}, None)
        self.bridge._draft.assert_called_once()
        self.assertEqual(self.database.execute("SELECT draft FROM replies WHERE incoming=123").fetchone()[0], text)
        self.assertEqual(self.database.execute("SELECT sent FROM updates WHERE stage='answer-part-0'").fetchone()[0], 1)
        resumed = self.make_bridge()
        resumed._draft.side_effect = AssertionError("model must not run on delivery retry")
        resumed._respond(0, 123, "", {"text": "hello"}, "session")
        resumed._draft.assert_not_called()
        self.assertEqual(attempts[1], attempts[2])
        self.assertEqual(sum(payload == attempts[0][1] for _, payload in attempts), 1)
        self.assertEqual("".join(received.values()), text)
        self.assertEqual(self.database.execute("SELECT draft FROM replies WHERE incoming=123").fetchone(), (None,))
        self.assertEqual(self.database.execute("SELECT parts_json FROM reply_text_plans WHERE incoming=123").fetchone(), (None,))
        self.assertEqual(self.database.execute("SELECT cursor FROM feeds WHERE parent=0").fetchone(), (123,))

    def test_short_reply_keeps_original_payload_and_does_not_read_history(self):
        """Короткий ответ сохраняет прежний контракт одной отправки с replies.payload."""
        self.bridge._draft.return_value = ("Короткий ответ 🐅", "session")
        self.bridge._respond(0, 123, "", {"text": "hello"}, None)
        payload = self.database.execute("SELECT payload FROM replies WHERE incoming=123").fetchone()[0]
        self.messenger.send.assert_called_once_with(0, "Короткий ответ 🐅", payload)
        self.messenger.new_messages.assert_not_called()
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM updates WHERE stage LIKE 'answer-part-%'").fetchone(), (0,))

    def insert_legacy_reply(self, text):
        """Сохранить receipt прежнего формата без маркера нового chunk plan."""
        payload = str(uuid.uuid4())
        with self.database:
            self.database.execute("INSERT INTO replies(parent,incoming,payload,draft) VALUES (0,123,?,?)", (payload, text))
        return payload

    def test_legacy_delivered_long_reply_is_reconciled_before_changing_format(self):
        """Найденный собственный исходный payload не превращается в повторную отправку частей."""
        text = "старый ответ " * 500
        payload = self.insert_legacy_reply(text)
        self.messenger.new_messages.return_value = [(200, {"payload_id": payload, "author_guid": "service", "text": text})]
        self.bridge._respond(0, 123, "", {"text": "hello"}, None)
        self.messenger.new_messages.assert_called_once_with(parent=0, after=123)
        self.messenger.send.assert_not_called()
        self.bridge._draft.assert_not_called()
        self.assertEqual(self.database.execute("SELECT draft FROM replies").fetchone(), (None,))

    def test_legacy_absent_reply_is_chunked_only_after_successful_history_read(self):
        """Полная успешная сверка отсутствующего payload разрешает новый ограниченный план."""
        text = "старый недоставленный ответ " * 300
        self.insert_legacy_reply(text)
        events = []
        self.messenger.new_messages.side_effect = lambda **kwargs: events.append("history") or []
        self.messenger.send.side_effect = lambda parent, body, payload: events.append(body) or 777
        self.bridge._respond(0, 123, "", {"text": "hello"}, None)
        self.assertEqual(events[0], "history")
        self.assertEqual("".join(events[1:]), text)
        self.assertTrue(all(utf16_length(part) <= 4096 for part in events[1:]))
        self.bridge._draft.assert_not_called()

    def test_legacy_unknown_outcome_does_not_rerun_or_blindly_duplicate(self):
        """Недоступная история сохраняет draft и не создаёт внешних отправок."""
        text = "старый ответ " * 500
        self.insert_legacy_reply(text)
        self.messenger.new_messages.side_effect = RuntimeError("synthetic unavailable history")
        with self.assertRaisesRegex(RuntimeError, "legacy_reply_delivery_unverified"):
            self.bridge._respond(0, 123, "", {"text": "hello"}, None)
        self.messenger.send.assert_not_called()
        self.bridge._draft.assert_not_called()
        self.assertEqual(self.database.execute("SELECT draft FROM replies").fetchone(), (text,))
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM reply_text_plans").fetchone(), (0,))

    def test_answer_parts_remain_assistant_history_while_statuses_are_excluded(self):
        """Bootstrap памяти включает содержательные answer-part и исключает обычные updates."""
        answer_payload, status_payload = str(uuid.uuid4()), str(uuid.uuid4())
        with self.database:
            self.database.executemany("INSERT INTO updates(parent,incoming,stage,payload,sent) VALUES (100,123,?,?,1)",
                                      [("answer-part-0", answer_payload), ("command", status_payload)])
        self.messenger.context_messages.return_value = [
            (200, {"payload_id": answer_payload, "author_guid": "service", "text": "Содержательная часть ответа"}),
            (201, {"payload_id": status_payload, "author_guid": "service", "text": "Служебная справка"}),
        ]
        store = Mock()
        store.is_initialized.return_value = False
        store.snapshot.return_value = {}
        self.bridge._context_store = Mock(return_value=store)
        self.bridge._prepare_thread_context(100, 300, {"text": "followup"})
        remembered = [call.args for call in store.remember.call_args_list]
        self.assertIn((100, 200, "assistant", "Содержательная часть ответа"), remembered)
        self.assertFalse(any("Служебная справка" in call for call in remembered))


if __name__ == "__main__":
    unittest.main()
