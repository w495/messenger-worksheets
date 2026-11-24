from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import base64
import unittest
from unittest.mock import Mock, patch
import uuid

from messenger_codex.bridge import Bridge, message_prompt, strip_prompt_comments

os.environ.setdefault("WORKSHEET_MESSENGER_CHAT_ID", "0/0/00000000-0000-0000-0000-000000000000")
os.environ.setdefault("WORKSHEET_MESSENGER_EMAIL", "user@example.test")

from messenger_worksheets.cloud_gateway import CloudBridge
from messenger_worksheets.bot_gateway import BotCloudBridge


class PromptCommentsTest(unittest.TestCase):
    def download_image(self, media, directory):
        """Создать в разрешённом временном каталоге bytes изображения для RPC-double."""
        image = directory / "attachment.png"
        image.write_bytes(b"image-bytes")
        return (image,)

    def bridge(self, gateway=Bridge):
        """Создать мост с SQLite в памяти и проверяемыми дублями внешних действий."""
        database = sqlite3.connect(":memory:")
        self.addCleanup(database.close)
        messenger = Mock()
        messenger.chat = "0/0/00000000-0000-0000-0000-000000000000"
        messenger.identity = {"guid": "service-account"}
        messenger.send.return_value = 777
        bridge = object.__new__(gateway)
        Bridge.__init__(bridge, database, messenger, Path("/tmp/codex"))
        bridge._draft = Mock(return_value=("Ответ", "session"))
        bridge._task = Mock(return_value=("Ответ", ()))
        return bridge

    def test_line_comments_allow_indentation_and_preserve_regular_text(self):
        """Удалять полные строки # и // с отступами, не меняя текст и URL внутри строк."""
        source = (
            "# HUMAN_ONLY\n"
            " \t// HUMAN_ONLY\r\n"
            "Текст # тег\n"
            "https://example.test/a#section\n"
            "Текст // часть строки"
        )
        self.assertEqual(
            strip_prompt_comments(source),
            "\n \t\r\nТекст # тег\nhttps://example.test/a#section\nТекст // часть строки",
        )

    def test_inline_multiline_and_nested_blocks_are_removed(self):
        """Удалять inline/multiline блоки, включая вложенные, без склейки соседних слов."""
        self.assertEqual(strip_prompt_comments("до/* HUMAN_ONLY */после"), "до после")
        self.assertEqual(
            strip_prompt_comments("до/* HUMAN_ONLY\n/* вложенный */\r\nконец */после"),
            "до \n\r\nпосле",
        )

    def test_unicode_line_separators_also_end_comment_lines(self):
        """Не пропускать комментарии после Unicode LINE/PARAGRAPH SEPARATOR или NEL."""
        for separator in ("\n", "\r", "\r\n", "\u0085", "\u2028", "\u2029"):
            for prefix in ("#", "//"):
                with self.subTest(separator=repr(separator), prefix=prefix):
                    source = "Ответь OK" + separator + prefix + " HUMAN_ONLY"
                    prompt = json.loads(message_prompt({"text": source}, 0))
                    self.assertEqual(prompt["text"], "Ответь OK" + separator)
                    self.assertEqual(
                        strip_prompt_comments(prefix + " HUMAN_ONLY" + separator + "OK"),
                        separator + "OK",
                    )

    def test_comment_markers_in_line_comments_do_not_open_blocks(self):
        """Не переносить /* из удалённой строки на следующее реальное поручение."""
        self.assertEqual(strip_prompt_comments("# /*\nОтветь OK"), "\nОтветь OK")
        self.assertEqual(strip_prompt_comments("// /*\nОтветь OK"), "\nОтветь OK")
        self.assertEqual(strip_prompt_comments("/* заметка */# HUMAN_ONLY\nOK"), " \nOK")

    def test_unclosed_block_drops_tail_only_in_current_message(self):
        """Незакрытый блок скрывает остаток сообщения, не влияя на следующее сообщение."""
        self.assertEqual(strip_prompt_comments("OK /* HUMAN_ONLY\nещё заметка"), "OK  \n")
        self.assertEqual(strip_prompt_comments("Новый запрос"), "Новый запрос")

    def test_comment_only_prompt_is_ignored_even_with_attachments_and_metadata(self):
        """Не отправлять изображения и неизвестные поля сообщения, адресованного только людям."""
        for text in ("# HUMAN_ONLY", "// HUMAN_ONLY\n", "/* HUMAN_ONLY */", " /* незакрытый"):
            for metadata in ({}, {"unknown_content": True}, {
                "media": [{"type": "gallery", "file_id": "image-id"}],
            }):
                with self.subTest(text=text, metadata=metadata):
                    self.assertIsNone(message_prompt({"text": text, **metadata}, 0))

    def test_attachment_only_messages_keep_existing_behavior(self):
        """Сохранять обработку изображений без подписи, включая подпись из пробелов."""
        for text in ("", "  \n"):
            prompt = message_prompt({
                "text": text, "media": [{"type": "gallery", "file_id": "image-id"}],
            }, 123, image_count=1)
            self.assertIsNotNone(prompt)
            self.assertEqual(json.loads(prompt)["attached_image_count"], 1)

    def test_comment_only_respond_has_no_model_or_messenger_side_effect(self):
        """Игнорировать комментарий до вызовов модели, реакций, статусов и сохранения ответа."""
        for gateway in (Bridge, CloudBridge, BotCloudBridge):
            for parent in (0, 123):
                with self.subTest(gateway=gateway.__name__, parent=parent):
                    bridge = self.bridge(gateway)
                    result = bridge._respond(
                        parent, 456, "UNFILTERED_PROMPT", {
                            "text": "# HUMAN_ONLY", "unknown_content": True,
                            "media": [{"type": "gallery", "file_id": "image-id"}],
                        }, "previous-session",
                    )
                    self.assertEqual(result, "previous-session")
                    bridge._draft.assert_not_called()
                    bridge._task.assert_not_called()
                    bridge.messenger.send.assert_not_called()
                    bridge.messenger.edit.assert_not_called()
                    bridge.messenger.react.assert_not_called()
                    self.assertEqual(bridge.db.execute("SELECT COUNT(*) FROM replies").fetchone()[0], 0)

    def test_comments_do_not_select_task_route(self):
        """Не запускать инструменты из-за URL или команды /task в комментарии."""
        bridge = self.bridge()
        with patch.dict(os.environ, {"WORKSHEET_ALLOW_TASKS_FROM_ANY_PARTICIPANT": "true"}):
            for text in (
                "Привет\n# /task создай файл https://example.test/secret",
                "Привет\n// создай файл https://example.test/secret",
                "Привет /* создай файл https://example.test/secret */",
            ):
                with self.subTest(text=text):
                    self.assertFalse(bridge._is_owner_task({"text": text}))

    def test_respond_passes_only_clean_text_to_chat_and_task(self):
        """Перестраивать переданный prompt и удалять комментарии также из message для task."""
        for text, task in (("Ответь OK", False), ("/task Ответь OK", True)):
            with self.subTest(task=task):
                bridge = self.bridge()
                with patch.dict(os.environ, {"WORKSHEET_ALLOW_TASKS_FROM_ANY_PARTICIPANT": "true"}):
                    bridge._respond(0, 123, "HUMAN_ONLY", {
                        "text": "# HUMAN_ONLY\n" + text + " /* HUMAN_ONLY */",
                    }, None)
                if task:
                    bridge._draft.assert_not_called()
                    sent_message = bridge._task.call_args.args[2]
                else:
                    bridge._task.assert_not_called()
                    prompt = bridge._draft.call_args.args[1]
                    self.assertNotIn("HUMAN_ONLY", prompt)
                    sent_message = bridge._draft.call_args.args[2]
                self.assertNotIn("HUMAN_ONLY", sent_message["text"])
                self.assertIn(text, sent_message["text"])

    def test_transfer_filters_actual_rpc_for_both_gateways_and_modes(self):
        """Проверять тело реального RPC construction: комментарии не уходят с текстом или изображением."""
        for gateway, module in (
            (CloudBridge, "messenger_worksheets.cloud_gateway"),
            (BotCloudBridge, "messenger_worksheets.bot_gateway"),
        ):
            for task in (False, True):
                with self.subTest(gateway=gateway.__name__, task=task):
                    bridge = self.bridge(gateway)
                    bridge.messenger.download_images.side_effect = self.download_image
                    expected = {
                        "text": "Ответ", "session": None if task else str(uuid.uuid4()), "files": [],
                    }
                    with patch(module + ".submit", return_value=expected) as submit:
                        result = bridge.transfer(0, 123, {
                            "text": "# HUMAN_ONLY\nОтветь OK\n/* HUMAN_ONLY */",
                            "chat_id": bridge.messenger.chat,
                            "media": [{"type": "gallery", "file_id": "image-id"}],
                        }, task=task, session=None,
                            worker_url="https://127.0.0.1:8445/v1/codex-worksheet-jobs",
                            transport_token="test-token", ca_file="/test/ca.crt",
                        )
                    self.assertEqual(result, expected)
                    wire = submit.call_args.args[2]
                    self.assertNotIn("HUMAN_ONLY", json.dumps(wire))
                    self.assertIn("Ответь OK", json.loads(wire["text"])["text"])
                    self.assertEqual(json.loads(wire["text"])["attached_image_count"], 1)
                    self.assertEqual(wire["images"], [{
                        "format": "png", "data": base64.b64encode(b"image-bytes").decode("ascii"),
                    }])

    def test_transfer_refuses_comment_only_before_rpc_or_download(self):
        """Защитить прямой вызов transfer от обхода фильтра на уровне приёма сообщений."""
        for gateway, module in (
            (CloudBridge, "messenger_worksheets.cloud_gateway"),
            (BotCloudBridge, "messenger_worksheets.bot_gateway"),
        ):
            bridge = self.bridge(gateway)
            with patch(module + ".submit") as submit:
                with self.assertRaisesRegex(ValueError, "message has no model content"):
                    bridge.transfer(0, 123, {"text": "// HUMAN_ONLY"}, task=True, session=None,
                        worker_url="https://127.0.0.1:8445/v1/codex-worksheet-jobs",
                        transport_token="test-token", ca_file="/test/ca.crt",
                    )
            submit.assert_not_called()
            bridge.messenger.download_images.assert_not_called()

    def test_ignored_messages_advance_poll_cursor_without_model_call(self):
        """Продвигать курсор через комментарии, чтобы polling не зацикливался на них."""
        bridge = self.bridge()
        started = bridge.started
        bridge.messenger.new_messages.return_value = [
            (started + 1, {"text": "# HUMAN_ONLY", "unknown_content": True}),
            (started + 2, {"text": "/* HUMAN_ONLY */"}),
        ]
        bridge._poll_feed(0, started, "session")
        self.assertEqual(
            bridge.db.execute("SELECT cursor,session FROM feeds WHERE parent=0").fetchone(),
            (started + 2, "session"),
        )
        bridge._draft.assert_not_called()
        bridge._task.assert_not_called()
        bridge.messenger.send.assert_not_called()

    def test_bot_update_comment_only_does_not_start_response(self):
        """Игнорировать Bot update с комментарием, даже если он содержит file metadata."""
        bridge = self.bridge(BotCloudBridge)
        bridge._respond = Mock()
        bridge.handle_update({
            "message_id": 123,
            "from": {"id": "participant"},
            "text": "# HUMAN_ONLY",
            "file": {"file_id": "file-id"},
        })
        bridge._respond.assert_not_called()


if __name__ == "__main__":
    unittest.main()
