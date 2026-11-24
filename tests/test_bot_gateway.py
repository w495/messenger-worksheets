from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from messenger_worksheets.bot_api import BotApiClient
from messenger_worksheets.bot_gateway import BotCloudBridge, BotMessenger


class Response(BytesIO):
    def __enter__(self):
        """Возвращает HTTP-дубль как контекстный менеджер."""
        return self

    def __exit__(self, *args):
        """Завершает контекст HTTP-дубля без подавления исключений."""
        self.close()


class Opener:
    def __init__(self, responses):
        """Создаёт opener-дубль с очередью заранее заданных ответов."""
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        """Записывает HTTP-запрос и возвращает следующий подготовленный ответ."""
        self.requests.append((request, timeout))
        return Response(self.responses.pop(0))


class FakeBotClient:
    def send_text(self, *args, **kwargs):
        """Записывает отправку текста и возвращает фиксированный message_id."""
        return kwargs.get("message_id") or 777

    def send_reaction(self, *args, **kwargs):
        """Записывает применение или удаление реакции."""
        return None


class BotApiTest(unittest.TestCase):
    def test_identity_updates_and_text_use_public_api_contract(self):
        """Проверяет публичные Bot API контракты identity, updates и sendText."""
        opener = Opener([
            json.dumps({"ok": True, "id": "bot-id", "login": "lang-env-bot"}).encode(),
            json.dumps({"ok": True, "updates": [{"update_id": 12}]}).encode(),
            json.dumps({"ok": True, "message_id": 12345}).encode(),
        ])
        client = BotApiClient("test-token")
        client._opener = opener
        self.assertEqual(client.identity()["login"], "lang-env-bot")
        self.assertEqual(client.get_updates(12), [{"update_id": 12}])
        message_id = client.send_text(
            "0/0/00000000-0000-0000-0000-000000000000",
            "hello", payload_id="00000000-0000-0000-0000-000000000001",
            thread_id=99,
        )
        self.assertEqual(message_id, 12345)
        text_request = opener.requests[-1][0]
        self.assertTrue(text_request.full_url.endswith("messages/sendText"))
        body = json.loads(text_request.data)
        self.assertEqual(body["thread_id"], 99)
        self.assertEqual(body["text"], "hello")

    def test_media_upload_is_multipart(self):
        """Проверяет multipart-кодирование файла при отправке медиа."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.txt"
            path.write_text("OK", encoding="utf-8")
            opener = Opener([json.dumps({
                "ok": True, "message_id": 456, "file_id": "file-id",
            }).encode()])
            client = BotApiClient("test-token")
            client._opener = opener
            self.assertEqual(client.send_media("chat", path, image=False), 456)
            request = opener.requests[0][0]
            self.assertIn("multipart/form-data", request.headers["Content-type"])
            self.assertIn(b'name="document"', request.data)
            self.assertIn(b"OK", request.data)

    def test_corporate_team_bot_response_uses_data_field(self):
        """Проверяет поддержку корпоративного ответа Bot API со списком в поле data."""
        opener = Opener([
            json.dumps({"ok": True, "data": [{"id": "chat-1"}]}).encode(),
            json.dumps({"ok": True, "data": [{"update_id": 7}]}).encode(),
        ])
        client = BotApiClient(
            "test-token", auth_scheme="OAuthTeam",
            base_url="https://bp.mssngr.yandex.net/public/bot/v1/",
        )
        client._opener = opener
        self.assertEqual(client.chats(), [{"id": "chat-1"}])
        self.assertEqual(client.get_updates(7), [{"update_id": 7}])


class BotGatewayTest(unittest.TestCase):
    def setUp(self):
        """Создаёт SQLite-мост и BotMessenger-дубль для маршрутизации update."""
        self.temp = tempfile.TemporaryDirectory()
        self.database = sqlite3.connect(":memory:")
        self.messenger = BotMessenger(
            FakeBotClient(),
            "0/0/00000000-0000-0000-0000-000000000000",
            {"id": "bot-guid", "login": "lang-env-bot"},
        )
        self.bridge = BotCloudBridge(
            self.database, self.messenger, Path(self.temp.name),
            worker_url="https://127.0.0.1:8443/jobs", worker_token="worker-token",
            worker_ca="/test/ca-api.crt",
        )

    def tearDown(self):
        """Закрывает SQLite-соединение тестового Bot gateway."""
        self.database.close()
        self.temp.cleanup()

    def test_update_is_routed_to_existing_thread(self):
        """Проверяет маршрутизацию update в уже существующий тред и сохранение сессии."""
        captured = []

        def respond(parent, stamp, prompt, message, session):
            """Имитирует обработку update и возвращает новую сессию треда."""
            captured.append((parent, stamp, prompt, message, session))
            return "00000000-0000-0000-0000-000000000001"

        self.bridge._respond = respond
        self.bridge.handle_update({
            "update_id": 10,
            "message_id": 200,
            "thread_id": 100,
            "chat": {"type": "group", "id": self.messenger.chat},
            "from": {"id": "human-guid", "login": "human", "robot": False},
            "text": "hello",
            "images": [[{"file_id": "small"}, {"file_id": "original", "size": 5}]],
        })
        self.assertEqual(len(captured), 1)
        parent, stamp, _, message, _ = captured[0]
        self.assertEqual((parent, stamp), (100, 200))
        self.assertEqual(message["media"][0]["file_id"], "original")

    def test_own_bot_update_is_ignored(self):
        """Проверяет игнорирование update, отправленного самим ботом."""
        self.bridge._respond = lambda *args: self.fail("own update must be ignored")
        self.bridge.handle_update({
            "update_id": 11,
            "message_id": 201,
            "chat": {"type": "group", "id": self.messenger.chat},
            "from": {"id": "bot-guid", "robot": True},
            "text": "self reply",
        })


if __name__ == "__main__":
    unittest.main()
