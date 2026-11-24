from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from messenger_codex.bridge import (
    ARTIFACT_RETRY_LIMIT,
    ArtifactDeliveryError,
    ArtifactDeliveryRetry,
    Bridge,
    Messenger,
)


class FakeClient:
    def __init__(self):
        """Создаёт дубль клиента, записывающий вызовы отправки артефактов."""
        self.operations = []

    def send_image_with_caption(self, **kwargs):
        """Имитирует подтверждённую отправку изображения и сохраняет параметры вызова."""
        self.operations.append(("image", kwargs))
        if "confirm_fingerprint" not in kwargs:
            return {"fingerprint": "image-fingerprint"}
        return {"outcome": "confirmed"}

    def send_document(self, **kwargs):
        """Имитирует подтверждённую отправку документа и сохраняет параметры вызова."""
        self.operations.append(("document", kwargs))
        if "confirm_fingerprint" not in kwargs:
            return {"fingerprint": "document-fingerprint"}
        return {"outcome": "confirmed"}


class RejectingImageClient(FakeClient):
    def send_image_with_caption(self, **kwargs):
        """Имитирует отказ preview изображения для проверки fallback на документ."""
        self.operations.append(("image", kwargs))
        raise ValueError("invalid test PNG")


class FailingDocumentClient(FakeClient):
    def send_document(self, **kwargs):
        """Имитирует исключение при отправке документа после успешного preview."""
        self.operations.append(("document", kwargs))
        if "confirm_fingerprint" not in kwargs:
            return {"fingerprint": "document-fingerprint"}
        raise RuntimeError("raw client detail must not escape")


class FailingArtifactMessenger:
    chat = "0/0/00000000-0000-0000-0000-000000000000"
    identity = {"guid": "00000000-0000-0000-0000-000000000099"}

    def __init__(self):
        """Создаёт Messenger-дубль, считающий попытки доставки артефакта."""
        self.calls = 0

    def send_file(self, parent, path, payload, receipts):
        """Всегда отклоняет доставку файла безопасным кодом ошибки."""
        self.calls += 1
        raise ArtifactDeliveryError("document_send_failed")


class MissingTimestampClient:
    guid = "00000000-0000-0000-0000-000000000099"

    def __init__(self):
        """Создаёт дубль ответа без timestamp и с историей для reconciliation."""
        self.history_calls = 0

    def send_message(self, text, **kwargs):
        """Имитирует preview и подтверждение отправки без серверного timestamp."""
        if "confirm_fingerprint" not in kwargs:
            return {"fingerprint": "message-fingerprint"}
        return {"outcome": "confirmed", "chat_id": kwargs["chat_id"], "message_info": None}

    def identity(self):
        """Возвращает фиксированную идентичность автора для поиска сообщения в истории."""
        return {"guid": self.guid}

    def read_history(self, chat_id, limit):
        """Возвращает историю с совпадающим payload и подтверждённым timestamp."""
        self.history_calls += 1
        if self.history_calls < 3:
            return {"messages": []}
        return {"messages": [{
            "payload_id": "00000000-0000-0000-0000-000000000004",
            "author_guid": self.guid,
            "ts": 987654,
        }]}


class NestedTimestampClient(MissingTimestampClient):
    def send_message(self, text, **kwargs):
        """Возвращает timestamp во вложенном ServerMessageInfo."""
        if "confirm_fingerprint" not in kwargs:
            return {"fingerprint": "message-fingerprint"}
        return {
            "outcome": "confirmed",
            "chat_id": kwargs["chat_id"],
            "message_info": {"ServerMessageInfo": {"Timestamp": 123456}},
        }

    def read_history(self, chat_id, limit):
        """Запрещает fallback к истории, если вложенный timestamp уже найден."""
        raise AssertionError("history fallback must not run")


class NormalizedTimestampClient(NestedTimestampClient):
    def send_message(self, text, **kwargs):
        """Возвращает timestamp в нормализованном поле ts cleanroom-клиента."""
        if "confirm_fingerprint" not in kwargs:
            return {"fingerprint": "message-fingerprint"}
        return {
            "outcome": "confirmed",
            "chat_id": kwargs["chat_id"],
            "message_info": {"ts": 654321},
        }


class UnknownOutcomeClient(MissingTimestampClient):
    def send_message(self, text, **kwargs):
        """Имитирует неизвестный outcome без timestamp для последующей сверки с историей."""
        if "confirm_fingerprint" not in kwargs:
            return {"fingerprint": "message-fingerprint"}
        return {"outcome": "unknown", "chat_id": kwargs["chat_id"], "message_info": None}


class ArtifactDeliveryTest(unittest.TestCase):
    def setUp(self):
        """Создаёт временную SQLite-базу и рабочее окружение сценариев доставки."""
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.messenger = object.__new__(Messenger)
        self.messenger.chat = "0/0/00000000-0000-0000-0000-000000000000"
        self.messenger.thread_chat_id = lambda chat, parent: chat
        self.messenger.client = FakeClient()

    def tearDown(self):
        """Закрывает SQLite-соединение после каждого сценария доставки."""
        self.temp.cleanup()

    def test_png_uses_native_image(self):
        """Проверяет, что валидный PNG отправляется нативным изображением и документом."""
        image = self.root / "result.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nrest")
        self.messenger.send_file(0, image, "00000000-0000-0000-0000-000000000001", self.root)
        self.assertEqual(
            [kind for kind, _ in self.messenger.client.operations],
            ["image", "image", "document", "document"],
        )

    def test_non_png_uses_document(self):
        """Проверяет, что не-PNG файл отправляется только документом."""
        document = self.root / "result.txt"
        document.write_text("ok", encoding="utf-8")
        self.messenger.send_file(0, document, "00000000-0000-0000-0000-000000000002", self.root)
        self.assertEqual([kind for kind, _ in self.messenger.client.operations], ["document", "document"])

    def test_invalid_png_falls_back_to_document(self):
        """Проверяет fallback повреждённого PNG на безопасную отправку документом."""
        image = self.root / "broken.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nnot-a-real-png")
        self.messenger.client = RejectingImageClient()
        self.messenger.send_file(0, image, "00000000-0000-0000-0000-000000000003", self.root)
        self.assertEqual(
            [kind for kind, _ in self.messenger.client.operations],
            ["image", "document", "document"],
        )

    def test_document_exception_is_replaced_with_safe_stage_code(self):
        """Проверяет замену исключения клиента безопасным кодом этапа доставки."""
        document = self.root / "result.txt"
        document.write_text("ok", encoding="utf-8")
        self.messenger.client = FailingDocumentClient()
        with self.assertRaisesRegex(ArtifactDeliveryError, "^document_send_failed$"):
            self.messenger.send_file(
                0, document, "00000000-0000-0000-0000-000000000006", self.root,
            )

    def test_artifact_delivery_becomes_terminal_after_bounded_retries(self):
        """Проверяет остановку доставки после ограниченного числа повторов и фиксацию отказа."""
        database = sqlite3.connect(":memory:")
        messenger = FailingArtifactMessenger()
        bridge = Bridge(database, messenger, self.root / "codex")
        result_directory = self.root / "task-results/result-test"
        result_directory.mkdir(parents=True)
        document = result_directory / "result.txt"
        document.write_text("ok", encoding="utf-8")
        with database:
            database.execute(
                "INSERT INTO artifacts (parent,incoming,path) VALUES (?,?,?)",
                (0, 123, str(document)),
            )

        for attempt in range(1, ARTIFACT_RETRY_LIMIT):
            with self.assertRaises(ArtifactDeliveryRetry) as caught:
                bridge._send_artifacts(0, 123, delivery_parent=123)
            self.assertEqual(caught.exception.code, "document_send_failed")
            self.assertEqual(caught.exception.attempt, attempt)

        self.assertEqual(bridge._send_artifacts(0, 123, delivery_parent=123), 1)
        self.assertEqual(messenger.calls, ARTIFACT_RETRY_LIMIT)
        self.assertEqual(
            database.execute(
                "SELECT attempts,failed,error_code FROM artifacts"
            ).fetchone(),
            (ARTIFACT_RETRY_LIMIT, 1, "document_send_failed"),
        )
        self.assertFalse(document.exists())
        self.assertEqual(bridge._send_artifacts(0, 123, delivery_parent=123), 1)
        self.assertEqual(messenger.calls, ARTIFACT_RETRY_LIMIT)

    def test_send_resolves_missing_timestamp_from_history(self):
        """Проверяет восстановление отсутствующего timestamp по payload в истории."""
        self.messenger.client = MissingTimestampClient()
        timestamp = self.messenger.send(
            0,
            "status",
            "00000000-0000-0000-0000-000000000004",
        )
        self.assertEqual(timestamp, 987654)
        self.assertEqual(self.messenger.client.history_calls, 3)

    def test_artifact_cleanup_refuses_path_outside_task_results(self):
        """Проверяет component containment перед удалением сохранённого файла."""
        database = sqlite3.connect(":memory:")
        bridge = Bridge(database, FailingArtifactMessenger(), self.root / "codex")
        outside = self.root / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        bridge._discard_artifact_file(str(outside))
        self.assertTrue(outside.exists())

    def test_send_reads_nested_server_message_timestamp(self):
        """Проверяет чтение timestamp из вложенного ServerMessageInfo без истории."""
        self.messenger.client = NestedTimestampClient()
        timestamp = self.messenger.send(
            0,
            "status",
            "00000000-0000-0000-0000-000000000005",
        )
        self.assertEqual(timestamp, 123456)

    def test_send_reads_cleanroom_normalized_timestamp(self):
        """Проверяет чтение нормализованного cleanroom-поля ts."""
        self.messenger.client = NormalizedTimestampClient()
        timestamp = self.messenger.send(
            0,
            "status",
            "00000000-0000-0000-0000-000000000007",
        )
        self.assertEqual(timestamp, 654321)

    def test_unknown_outcome_is_reconciled_by_history(self):
        """Проверяет reconciliation неизвестного outcome через подтверждённую историю."""
        self.messenger.client = UnknownOutcomeClient()
        timestamp = self.messenger.send(
            0,
            "status",
            "00000000-0000-0000-0000-000000000004",
        )
        self.assertEqual(timestamp, 987654)


if __name__ == "__main__":
    unittest.main()
