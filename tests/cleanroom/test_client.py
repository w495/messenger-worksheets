from __future__ import annotations

import json
import os
import ssl
import struct
import sys
import tempfile
import types
import unittest
import urllib.error
import urllib.parse
import uuid
import zlib
from pathlib import Path
from unittest import mock

from messenger_user_client import (
    AuthenticationError,
    ConfirmationError,
    MessengerError,
    MessengerUserClient,
    OperationUncertain,
    PermissionDenied,
    render_message,
    thread_chat_id,
)
from messenger_user_client.client import (
    _decode_data_frame,
    _encode_data_frame,
    _is_yandex_host,
    _read_secure_file,
    _validate_png,
    _validate_media_id,
)


class FakeClient(MessengerUserClient):
    def __init__(self, *, account="work", environment="prod", **kwargs):
        """Создать изолированный клиент с предсказуемыми identity и Fanout."""
        super().__init__(
            "unit-test-token",
            account=account,
            environment=environment,
            **kwargs,
        )
        self.registry_data = {
            "uid": "uid-test",
            "guid": "guid-test",
            "login": "login-test",
            "email": "user@example.invalid",
            "organization_id": "org-test" if account == "work" else None,
        }
        self.fanout_calls = []
        self.fanout_response = {"FullyCommitted": 1}

    def _registry(self, method, params):
        """Имитировать только разрешённые тестом методы Registry."""
        if method == "request_user":
            return dict(self.registry_data)
        if method == "get_chats_info":
            self.last_registry = (method, params)
            return {"Chats": [{"ChatId": "1/2/test", "Title": "Safe"}]}
        raise AssertionError(method)

    def _fanout_once(self, path, payload):
        """Записать Fanout-вызов и вернуть настраиваемый фиктивный ответ."""
        self.fanout_calls.append((path, payload))
        return dict(self.fanout_response)


def make_png(width=1, height=1, extra_chunks=()):
    """Собрать минимальный PNG с заданными размерами и дополнительными chunk."""

    def chunk(kind, data):
        """Закодировать один PNG chunk с корректной длиной и CRC."""
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    raw_pixel = b"\x00\x00\x00\x00\x00"
    chunks = [chunk(b"IHDR", ihdr)]
    chunks.extend(chunk(kind, data) for kind, data in extra_chunks)
    chunks.extend([chunk(b"IDAT", zlib.compress(raw_pixel)), chunk(b"IEND", b"")])
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks)


class PublicHelpersTest(unittest.TestCase):
    def test_thread_chat_id(self):
        """Проверить построение тредового id и отказ от путей и неверного сегмента."""
        self.assertEqual(thread_chat_id("10/20/abc", 123), "110/20/abc_123")
        with self.assertRaises(ValueError):
            thread_chat_id("../../etc/passwd", 1)
        with self.assertRaises(ValueError):
            thread_chat_id("x/20/abc", 1)

    def test_render_message_drops_unknown_wire_fields(self):
        """Проверить, что renderer не раскрывает неизвестные wire-поля и секреты."""
        raw = {
            "ServerMessage": {
                "ClientMessage": {
                    "Plain": {
                        "Text": {"MessageText": "hello"},
                        "PayloadId": "p",
                        "FutureSecret": {"Url": "https://credentials.invalid/"},
                    }
                },
                "ServerMessageInfo": {
                    "Timestamp": "101",
                    "SeqNo": 5,
                    "Version": 2,
                    "From": {"DisplayName": "A", "Guid": "g"},
                    "Deleted": False,
                },
            },
            "Authorization": "OAuth secret",
        }
        rendered = render_message(raw)
        self.assertEqual(rendered["text"], "hello")
        self.assertEqual(rendered["seq"], 5)
        self.assertEqual(rendered["author_guid"], "g")
        self.assertTrue(rendered["unknown_content"])
        serialized = json.dumps(rendered)
        self.assertNotIn("Authorization", serialized)
        self.assertNotIn("credentials", serialized)
        self.assertNotIn("FutureSecret", serialized)

    def test_render_media_only_uses_allowlisted_metadata(self):
        """Проверить allowlist media-метаданных без DownloadUrl из ответа."""
        rendered = render_message(
            {
                "ServerMessage": {
                    "ClientMessage": {
                        "Plain": {
                            "MiscFile": {
                                "FileInfo": {
                                    "Id2": "safe-id",
                                    "Name": "report.bin",
                                    "Size": 42,
                                    "DownloadUrl": "https://secret.invalid",
                                }
                            }
                        }
                    },
                    "ServerMessageInfo": {"Timestamp": "1"},
                }
            }
        )
        self.assertEqual(
            rendered["media"],
            [{"type": "document", "file_id": "safe-id", "name": "report.bin", "size": 42}],
        )
        self.assertNotIn("secret", json.dumps(rendered))


class IdentityAndRedactionTest(unittest.TestCase):
    def test_repr_and_transport_error_do_not_disclose_token_or_body(self):
        """Проверить маскирование токена в repr и тела HTTP-ошибки."""
        token = "very-secret-token"
        client = MessengerUserClient(token)
        self.assertNotIn(token, repr(client))
        error = urllib.error.HTTPError(
            "https://messenger.360.yandex.ru/api/",
            500,
            "server leaked body",
            {},
            None,
        )
        with mock.patch(
            "messenger_user_client.client._NO_REDIRECT_OPENER.open",
            side_effect=error,
        ):
            with self.assertRaises(MessengerError) as caught:
                client.identity()
        message = str(caught.exception)
        self.assertNotIn(token, message)
        self.assertNotIn("leaked body", message)

    def test_identity_expectations_and_account_class_fail_closed(self):
        """Проверить fail-closed отказ при mismatch email и класса аккаунта."""
        client = FakeClient(expected_email="other@example.invalid")
        with self.assertRaises(AuthenticationError):
            client.identity()
        client = FakeClient()
        client.registry_data["organization_id"] = None
        with self.assertRaises(AuthenticationError):
            client.identity()
        client = FakeClient(account="personal")
        client.registry_data["organization_id"] = "unexpected-org"
        with self.assertRaises(AuthenticationError):
            client.identity()

    def test_identity_requires_uid_and_guid_and_does_not_build_email(self):
        """Проверить обязательность UID/GUID и запрет синтеза email из login."""
        client = FakeClient(expected_email="login-test@example.invalid")
        client.registry_data.pop("email")
        with self.assertRaises(AuthenticationError):
            client.identity()
        client = FakeClient()
        client.registry_data.pop("guid")
        with self.assertRaises(AuthenticationError):
            client.identity()

    def test_resolve_invite_sanitizes_result_and_host(self):
        """Проверить очистку invite и отклонение URL вне host/fragment allowlist."""
        client = FakeClient()
        self.assertEqual(
            client.resolve_invite("https://messenger.360.yandex.ru/#/join/abc"),
            {
                "invite_hash": "abc",
                "found": True,
                "chat": {"ChatId": "1/2/test", "Title": "Safe"},
            },
        )
        self.assertEqual(client.last_registry, ("get_chats_info", {"invite_hash": "abc"}))
        self.assertTrue(client.resolve_invite("abc")["found"])
        with self.assertRaises(ValueError):
            client.resolve_invite("https://evil.example/join/abc")
        with self.assertRaises(ValueError):
            client.resolve_invite("https://messenger.360.yandex.ru/#/wrong/abc")

    def test_identity_exposes_compatibility_org_flag(self):
        """Проверить совместимый флаг организационного аккаунта для обоих классов."""
        self.assertTrue(FakeClient().identity()["is_org_account"])
        self.assertFalse(FakeClient(account="personal").identity()["is_org_account"])
        explicit = FakeClient()
        explicit.registry_data["organization_id"] = None
        explicit.registry_data["is_org_account"] = True
        self.assertTrue(explicit.identity()["is_org_account"])

    def test_nested_user_organizations_and_contact_email(self):
        """Проверить identity во вложенном user с организациями и contact email."""
        client = FakeClient(
            expected_email="contact@example.invalid",
            expected_organization_id="org-expected",
        )
        client.registry_data = {
            "user": {
                "uid": "nested-uid",
                "guid": "nested-guid",
                "nickname": "nested-login",
                "organizations": [
                    {"id": "org-other"},
                    {"id": "org-expected"},
                ],
                "contacts": [
                    {"type": "phone", "value": "not-an-email"},
                    {"type": "email", "value": "contact@example.invalid"},
                ],
            }
        }
        identity = client.identity()
        self.assertEqual(identity["email"], "contact@example.invalid")
        self.assertEqual(identity["login"], "nested-login")
        self.assertTrue(identity["is_org_account"])

        personal = FakeClient(account="personal")
        personal.registry_data = {
            "user": {
                "uid": "personal-uid",
                "guid": "personal-guid",
                "nickname": "personal-login",
                "organizations": [],
                "contacts": [],
            }
        }
        self.assertFalse(personal.identity()["is_org_account"])

    def test_corporate_login_suffix_is_normalized_but_email_is_not(self):
        """Проверить нормализацию корпоративного login без изменения email."""
        client = FakeClient(
            expected_login="compat-user",
            expected_email="compat-user@yandex-team.ru",
        )
        client.registry_data = {
            "user": {
                "uid": "compat-uid",
                "guid": "compat-guid",
                "nickname": "compat-user@yandex-team.ru",
                "organizations": [{"id": "compat-org"}],
                "contacts": [
                    {"type": "email", "value": "compat-user@yandex-team.ru"}
                ],
            }
        }
        identity = client.identity()
        self.assertEqual(identity["login"], "compat-user")
        self.assertEqual(identity["email"], "compat-user@yandex-team.ru")

        other_domain = FakeClient(expected_login="compat-user@example.invalid")
        other_domain.registry_data["login"] = "compat-user@example.invalid"
        self.assertEqual(
            other_domain.identity()["login"], "compat-user@example.invalid"
        )


class PreviewTest(unittest.TestCase):
    def test_send_is_two_phase_and_binds_exact_request(self):
        """Проверить двухфазную отправку и привязку fingerprint к точному тексту."""
        client = FakeClient()
        preview = client.send_message("hello", "1/2/chat")
        self.assertEqual(preview["status"], "preview_not_applied")
        self.assertEqual(client.fanout_calls, [])
        with self.assertRaises(ConfirmationError):
            client.send_message(
                "changed",
                "1/2/chat",
                payload_id=preview["payload_id"],
                confirm_fingerprint=preview["fingerprint"],
            )
        result = client.send_message(
            "hello", "1/2/chat", confirm_fingerprint=preview["fingerprint"]
        )
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(result["chat_id"], "1/2/chat")
        self.assertEqual(result["payload_id"], preview["payload_id"])
        self.assertIsNone(result["message_info"])
        self.assertEqual(client.fanout_calls[0][0], "push")
        self.assertEqual(client.fanout_calls[0][1]["UserIp"], "::1")
        plain = client.fanout_calls[0][1]["ClientMessage"]["Plain"]
        self.assertEqual(plain["Text"], {"MessageText": "hello"})

    def test_fingerprint_binds_actor_account_and_environment(self):
        """Проверить, что fingerprint нельзя перенести между actor и контурами."""
        first = FakeClient()
        preview = first.send_message("hello", "1/2/chat")
        second = FakeClient(environment="alpha")
        with self.assertRaises(ConfirmationError):
            second.send_message(
                "hello",
                "1/2/chat",
                payload_id=preview["payload_id"],
                confirm_fingerprint=preview["fingerprint"],
            )
        third = FakeClient()
        third.registry_data["uid"] = "other-uid"
        with self.assertRaises(ConfirmationError):
            third.send_message(
                "hello",
                "1/2/chat",
                payload_id=preview["payload_id"],
                confirm_fingerprint=preview["fingerprint"],
            )

    def test_confirmation_format_rejected(self):
        """Проверить отказ от confirm fingerprint неверного формата."""
        client = FakeClient()
        with self.assertRaises(ConfirmationError):
            client.set_reaction("1/2/chat", 10, "✅", confirm_fingerprint="bad")

    def test_reaction_allowlist(self):
        """Проверить integer timestamp, action-коды реакций и allowlist emoji."""
        client = FakeClient()
        for operation, action in (("add", 0), ("remove", 1), ("replace", 2)):
            with self.subTest(operation=operation):
                preview = client.set_reaction("1/2/chat", "10", "👀", operation=operation)
                result = client.set_reaction(
                    "1/2/chat", "10", "👀", operation=operation,
                    confirm_fingerprint=preview["fingerprint"],
                )
                payload = client.fanout_calls[-1][1]["ClientMessage"]["Reaction"]
                self.assertEqual(result["outcome"], "confirmed")
                self.assertEqual(payload["Timestamp"], 10)
                self.assertIs(type(payload["Timestamp"]), int)
                self.assertEqual(payload["Type"], 100110)
                self.assertIs(type(payload["Type"]), int)
                self.assertEqual(payload["Action"], action)
        with self.assertRaises(ValueError):
            client.set_reaction("1/2/chat", 10, "🔥")

    def test_broken_heart_replaces_processing_reaction(self):
        """Проверяет preview/confirm 💔 с integer Type=100116 и replace Action=2."""
        client = FakeClient()
        preview = client.set_reaction("1/2/chat", 10, "💔", operation="replace")
        result = client.set_reaction(
            "1/2/chat", 10, "💔", operation="replace",
            confirm_fingerprint=preview["fingerprint"],
        )
        self.assertEqual(result["outcome"], "confirmed")
        wire = client.fanout_calls[-1][1]["ClientMessage"]["Reaction"]
        self.assertEqual((wire["Type"], wire["Action"]), (100116, 2))

    def test_edit_rechecks_owner_and_reuses_payload(self):
        """Проверить повторную проверку автора и reuse исходного PayloadId."""
        client = FakeClient()
        client.fanout_response = {
            "ServerMessage": {
                "ClientMessage": {
                    "Plain": {
                        "PayloadId": "original-payload",
                        "Text": {"MessageText": "old"},
                    }
                },
                "ServerMessageInfo": {
                    "Timestamp": "10",
                    "From": {"Guid": "guid-test"},
                },
            }
        }
        preview = client.edit_message("1/2/chat", 10, "new")
        client.edit_message(
            "1/2/chat", 10, "new", confirm_fingerprint=preview["fingerprint"]
        )
        pushed = client.fanout_calls[-1][1]["ClientMessage"]["Plain"]
        self.assertEqual(pushed["PayloadId"], "original-payload")
        self.assertEqual(pushed["Timestamp"], 10)
        self.assertIs(type(pushed["Timestamp"]), int)
        message_info_calls = [
            payload for path, payload in client.fanout_calls if path == "message_info"
        ]
        self.assertEqual(len(message_info_calls), 2)
        self.assertTrue(all(call["Timestamp"] == 10 for call in message_info_calls))

    def test_timestamp_rejects_negative_and_non_numeric_values(self):
        """Проверить fail-closed валидацию timestamp до Fanout transport."""
        client = FakeClient()
        for timestamp in (-1, "-1", "not-a-timestamp", True):
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                client.set_reaction("1/2/chat", timestamp, "👀")

    def test_edit_rejects_foreign_author(self):
        """Проверить запрет редактирования сообщения другого автора."""
        client = FakeClient()
        client.fanout_response = {
            "ServerMessage": {
                "ClientMessage": {"Plain": {"PayloadId": "p"}},
                "ServerMessageInfo": {"From": {"Guid": "somebody-else"}},
            }
        }
        with self.assertRaises(PermissionDenied):
            client.edit_message("1/2/chat", 10, "new")

    def test_mutation_outcome_classification(self):
        """Проверить классификацию известных, отклонённых и неизвестных исходов."""
        cases = [
            ({"FullyCommitted": {}}, "confirmed"),
            ({"Duplicate": {}}, "confirmed"),
            ({"Result": 2}, "confirmed"),
            ({"Status": "FullyCommitted"}, "confirmed"),
            ({"Details": {"Code": "denied"}}, "rejected"),
            ({"Details": ""}, "unknown"),
            ({"Result": {"Code": 500}}, "unknown"),
            ({"Result": 3, "Metadata": {"Timestamp": 1}}, "unknown"),
            ({"unexpected": True}, "unknown"),
        ]
        for response, expected in cases:
            with self.subTest(response=response):
                client = FakeClient()
                client.fanout_response = response
                preview = client.send_message("hello", "1/2/chat")
                result = client.send_message(
                    "hello", "1/2/chat", confirm_fingerprint=preview["fingerprint"]
                )
                self.assertEqual(result["outcome"], expected)

    def test_successful_message_info_is_confirmed_and_rendered(self):
        """Проверить confirmed по MessageInfo и безопасный рендер ответа."""
        client = FakeClient()
        client.fanout_response = {
            "MessageInfo": {
                "ServerMessage": {
                    "ClientMessage": {
                        "Plain": {
                            "PayloadId": "server-payload",
                            "Text": {"MessageText": "delivered"},
                        }
                    },
                    "ServerMessageInfo": {"Timestamp": "42"},
                }
            }
        }
        preview = client.send_message("hello", "1/2/chat")
        result = client.send_message(
            "hello", "1/2/chat", confirm_fingerprint=preview["fingerprint"]
        )
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(result["message_info"]["text"], "delivered")


class FanoutAndWireTest(unittest.TestCase):
    def test_wire_fixture_and_decode(self):
        """Проверить байтовый fixture DATA frame и строгий JSON decode."""
        frame = _encode_data_frame(7, "history", {"ChatId": "1/2/test"})
        self.assertTrue(frame.startswith(b"\x01\x93\x00\x07\xa7history\x05\x00\x00\x00"))
        self.assertEqual(frame[-21:], b'{"ChatId":"1/2/test"}')
        response = b"\x01\x93\x00\x01\xa7history\x05\x00noise" + b'{"ok":true}'
        self.assertEqual(_decode_data_frame(response), {"ok": True})
        with self.assertRaises(MessengerError):
            _decode_data_frame(b"\x01junk{not-json}")

    def test_operation_ping_sends_data_and_query_uses_user_not_uid(self):
        """Проверить отправку после ping и query-поле user с подтверждённым UID."""
        client = FakeClient()
        captured = {}

        class FakeWebSocket:
            def __init__(self):
                """Подготовить очередь ping/DATA событий и журнал отправок."""
                self.events = ['{"operation":"ping"}', b"\x01header{}"]
                self.sent = []
                self.closed = False

            def recv(self):
                """Вернуть следующее заранее подготовленное WebSocket-событие."""
                return self.events.pop(0)

            def send_binary(self, value):
                """Записать отправленный бинарный DATA frame без сети."""
                self.sent.append(value)

            def close(self):
                """Отметить закрытие тестового WebSocket-соединения."""
                self.closed = True

        socket = FakeWebSocket()

        def create_connection(url, **kwargs):
            """Перехватить параметры подключения и вернуть WebSocket-double."""
            captured["url"] = url
            captured["kwargs"] = kwargs
            return socket

        fake_module = types.SimpleNamespace(create_connection=create_connection)
        with mock.patch.dict(sys.modules, {"websocket": fake_module}):
            result = MessengerUserClient._fanout_once(
                client,
                "history", {"ChatId": "1/2/chat", "Limit": 1}
            )
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(captured["url"]).query))
        self.assertEqual(query["user"], "uid-test")
        self.assertNotIn("uid", query)
        self.assertTrue(captured["kwargs"]["suppress_origin"])
        self.assertEqual(result, {})
        self.assertEqual(len(socket.sent), 1)
        self.assertTrue(socket.closed)

    def test_handshake_error_is_redacted_to_safe_http_category(self):
        """Проверить редактирование опасного handshake-текста до HTTP-категории."""
        client = FakeClient()

        class UnsafeHandshakeError(Exception):
            status_code = 403
            headers = {"Authorization": "OAuth reflected-secret"}

            def __str__(self):
                """Имитировать небезопасный текст исключения с секретами."""
                return "response body with unit-test-token and reflected-secret"

        def create_connection(url, **kwargs):
            """Имитировать отказ WebSocket handshake до создания соединения."""
            raise UnsafeHandshakeError()

        client._fanout_once = types.MethodType(MessengerUserClient._fanout_once, client)
        fake_module = types.SimpleNamespace(create_connection=create_connection)
        with mock.patch.dict(sys.modules, {"websocket": fake_module}):
            with self.assertRaises(MessengerError) as caught:
                client.fanout("history", {"ChatId": "1/2/chat", "Limit": 1})
        self.assertEqual(str(caught.exception), "fanout transport error: http_403")
        self.assertNotIn("unit-test-token", str(caught.exception))
        self.assertNotIn("reflected-secret", str(caught.exception))

    def test_transport_categories_timeout_tls_and_connection(self):
        """Проверить безопасные категории timeout, TLS и connection."""
        cases = [
            (TimeoutError(), "timeout"),
            (ssl.SSLError("unsafe tls details"), "tls"),
            (ConnectionError("unsafe connection details"), "connection"),
        ]
        for error, category in cases:
            with self.subTest(category=category):
                client = FakeClient()
                client._fanout_once = mock.Mock(side_effect=error)
                with self.assertRaises(MessengerError) as caught:
                    client.fanout(
                        "history", {"ChatId": "1/2/chat", "Limit": 1}
                    )
                self.assertEqual(
                    str(caught.exception), f"fanout transport error: {category}"
                )

    def test_read_only_retry_but_push_unknown_is_not_retried(self):
        """Проверить один retry чтения и отсутствие retry для push."""
        client = FakeClient()
        calls = []

        def flaky(path, payload):
            """Уронить первую попытку чтения и подтвердить вторую."""
            calls.append(path)
            if len(calls) == 1:
                raise TimeoutError()
            return {"ok": True}

        client._fanout_once = flaky
        history_payload = {"ChatId": "1/2/chat", "Limit": 1}
        self.assertEqual(client.fanout("history", history_payload), {"ok": True})
        self.assertEqual(calls, ["history", "history"])

        calls.clear()

        def uncertain(path, payload):
            """Имитировать неопределённый транспортный исход push."""
            calls.append(path)
            raise TimeoutError()

        client._fanout_once = uncertain
        with self.assertRaises(OperationUncertain):
            client._fanout_request(
                "push",
                {
                    "ClientMessage": {"Plain": {"ChatId": "1/2/chat"}},
                    "UserIp": "::1",
                },
            )
        self.assertEqual(calls, ["push"])

    def test_public_push_cannot_bypass_confirmation(self):
        """Проверить запрет прямого публичного push без preview."""
        client = FakeClient()
        with self.assertRaises(ConfirmationError):
            client.fanout("push", {"Plain": {"Text": "bypass"}})
        self.assertEqual(client.fanout_calls, [])

    def test_confirmed_message_reports_unknown_outcome_once(self):
        """Проверить unknown после единственной неуспешной push-попытки."""
        client = FakeClient()
        preview = client.send_message("hello", "1/2/chat")
        calls = []

        def uncertain(path, payload):
            """Имитировать timeout подтверждённой мутации и считать вызовы."""
            calls.append(path)
            raise TimeoutError()

        client._fanout_once = uncertain
        result = client.send_message(
            "hello", "1/2/chat", confirm_fingerprint=preview["fingerprint"]
        )
        self.assertEqual(result["outcome"], "unknown")
        self.assertIsNone(result["message_info"])
        self.assertEqual(calls, ["push"])

    def test_history_contract_and_max_timestamp(self):
        """Проверить контракт истории, MaxTimestamp и next_before_ts."""
        client = FakeClient()
        client.fanout_response = {
            "Chats": [
                {
                    "ChatId": "1/2/chat",
                    "Messages": [
                        {
                            "ServerMessage": {
                                "ClientMessage": {
                                    "Plain": {
                                        "PayloadId": "p2",
                                        "Text": {"MessageText": "new"},
                                    }
                                },
                                "ServerMessageInfo": {"Timestamp": "20"},
                            }
                        },
                        {
                            "ServerMessage": {
                                "ClientMessage": {
                                    "Plain": {
                                        "PayloadId": "p1",
                                        "Text": {"MessageText": "old"},
                                    }
                                },
                                "ServerMessageInfo": {"Timestamp": "10"},
                            }
                        },
                    ],
                }
            ]
        }
        result = client.read_history("1/2/chat", limit=2, before_ts=30)
        self.assertEqual(
            result,
            {
                "chat_id": "1/2/chat",
                "messages": result["messages"],
                "count": 2,
                "next_before_ts": "10",
            },
        )
        self.assertEqual(
            client.fanout_calls[-1][1],
            {"ChatId": "1/2/chat", "Limit": 2, "MaxTimestamp": "30"},
        )

    def test_history_rejects_mismatching_room(self):
        """Проверить fail-closed отказ при несовпадающем ChatId ответа."""
        client = FakeClient()
        client.fanout_response = {
            "Chats": [{"ChatId": "1/2/other", "Messages": []}]
        }
        with self.assertRaises(MessengerError):
            client.read_history("1/2/chat")

    def test_history_allows_min_timestamp_for_incremental_polling(self):
        """Проверить разрешённое MinTimestamp для инкрементального чтения."""
        client = FakeClient()
        payload = {"ChatId": "1/2/chat", "Limit": 100, "MinTimestamp": 31}
        self.assertEqual(client.fanout("history", payload), client.fanout_response)
        self.assertEqual(client.fanout_calls[-1], ("history", payload))

    def test_path_allowlist(self):
        """Проверить отклонение Fanout path вне фиксированного allowlist."""
        client = FakeClient()
        with self.assertRaises(ValueError):
            client.fanout("../../admin", {})


class FileSafetyTest(unittest.TestCase):
    def test_absolute_regular_file_and_symlinks(self):
        """Проверить абсолютный regular file и отказ от всех symlink-вариантов."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "file.bin"
            source.write_bytes(b"safe")
            self.assertEqual(_read_secure_file(source)["data"], b"safe")
            with self.assertRaises(ValueError):
                _read_secure_file(Path("relative.bin"))
            link = root / "link.bin"
            link.symlink_to(source)
            with self.assertRaises(ValueError):
                _read_secure_file(link)
            linked_dir = root / "linked-dir"
            linked_dir.symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                _read_secure_file(linked_dir / "file.bin")

    def test_size_limit_before_open(self):
        """Проверить отклонение файла свыше 50 MiB до чтения содержимого."""
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "large.bin"
            with source.open("wb") as stream:
                stream.truncate(50 * 1024 * 1024 + 1)
            with self.assertRaises(ValueError):
                _read_secure_file(source)

    def test_png_crc_dimensions_and_apng(self):
        """Проверить CRC, размеры и запрет APNG при валидации изображения."""
        self.assertEqual(_validate_png(make_png()), (1, 1))
        corrupted = bytearray(make_png())
        corrupted[-1] ^= 1
        with self.assertRaises(ValueError):
            _validate_png(bytes(corrupted))
        with self.assertRaises(ValueError):
            _validate_png(make_png(0, 1))
        with self.assertRaises(ValueError):
            _validate_png(make_png(extra_chunks=((b"acTL", struct.pack(">II", 1, 0)),)))

    def test_document_receipt_permissions_and_no_upload_on_preview(self):
        """Проверить отсутствие upload в preview и приватные mode receipt."""
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "doc.bin"
            source.write_bytes(b"document")
            receipt_dir = root / "receipts"
            with mock.patch.object(client, "_upload_media") as upload:
                preview = client.send_document(
                    "1/2/chat", source, "story.test", receipt_dir=receipt_dir
                )
                upload.assert_not_called()
                with mock.patch.object(
                    client, "_upload_media", return_value={"file_id": "uploaded-id"}
                ):
                    result = client.send_document(
                        "1/2/chat",
                        source,
                        "story.test",
                        confirm_fingerprint=preview["fingerprint"],
                        receipt_dir=receipt_dir,
                    )
            self.assertEqual(result["outcome"], "confirmed")
            self.assertEqual(result["file_id"], "uploaded-id")
            plain = client.fanout_calls[-1][1]["ClientMessage"]["Plain"]
            self.assertEqual(
                plain["MiscFile"]["FileInfo"],
                {
                    "Id2": "uploaded-id",
                    "Name": "doc.bin",
                    "Size": 8,
                    "Source": 0,
                },
            )
            receipts = list(receipt_dir.glob("*.json"))
            self.assertEqual(len(receipts), 1)
            self.assertEqual(os.stat(receipt_dir).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(receipts[0]).st_mode & 0o777, 0o600)
            receipt_text = receipts[0].read_text()
            self.assertNotIn("unit-test-token", receipt_text)
            self.assertEqual(json.loads(receipt_text)["stage"], "confirmed")

    def test_image_gallery_wire_contains_dimensions_and_file_info(self):
        """Проверить Gallery wire-поля, размеры PNG и FileInfo."""
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "image.png"
            source.write_bytes(make_png(1, 1))
            preview = client.send_image_with_caption(
                "1/2/chat", source, "caption", "story.image", receipt_dir=root / "receipts"
            )
            with mock.patch.object(
                client, "_upload_media", return_value={"file_id": "image-id"}
            ):
                result = client.send_image_with_caption(
                    "1/2/chat",
                    source,
                    "caption",
                    "story.image",
                    confirm_fingerprint=preview["fingerprint"],
                    receipt_dir=root / "receipts",
                )
        self.assertEqual(result["outcome"], "confirmed")
        gallery = client.fanout_calls[-1][1]["ClientMessage"]["Plain"]["Gallery"]
        self.assertEqual(gallery["Text"], "caption")
        image = gallery["Items"][0]["Image"]
        self.assertEqual((image["Width"], image["Height"]), (1, 1))
        self.assertFalse(image["Animated"])
        self.assertEqual(image["FileInfo"]["Id2"], "image-id")
        self.assertIs(type(image["FileInfo"]["Source"]), int)
        self.assertEqual(image["FileInfo"]["Source"], 0)

    def test_upload_uses_exact_url_raw_body_and_content_type(self):
        """Проверить точный upload URL, raw body и определённый Content-Type."""
        client = FakeClient()
        payload_id = str(uuid.uuid4())
        captured = {}

        class Response:
            def __enter__(self):
                """Вернуть HTTP-response double из контекстного менеджера."""
                return self

            def __exit__(self, *args):
                """Не подавлять исключения при выходе из контекста."""
                return False

            def read(self, size=-1):
                """Вернуть успешный JSON upload-ответ."""
                return b'{"file_id":"namespace/12345/uploaded-id"}'

        def open_request(request, timeout):
            """Перехватить исходящий upload request и вернуть response-double."""
            captured["request"] = request
            return Response()

        with mock.patch(
            "messenger_user_client.client._NO_REDIRECT_OPENER.open",
            side_effect=open_request,
        ):
            result = client._upload_media(
                b"raw-bytes", "report name.txt", "document", "1/2/chat", payload_id
            )
        request = captured["request"]
        self.assertEqual(
            request.full_url,
            "https://files.messenger.yandex.ru/media_upload/"
            f"1/2/chat/{payload_id}/report%20name.txt",
        )
        self.assertEqual(request.data, b"raw-bytes")
        self.assertEqual(request.get_header("Content-type"), "application/octet-stream")
        self.assertEqual(result, {"file_id": "namespace/12345/uploaded-id"})

    def test_media_id_allows_namespaces_and_rejects_unsafe_segments(self):
        """Принимает реальные slash namespaces, отклоняя URL/traversal/control."""
        _validate_media_id("namespace/12345/file-id_2.png")
        for value in (None, "", "/a", "a/", "a//b", "a/../b", "a/./b",
                      "https://example.test/file", "a%2fb", "a?token=x", "a\nB"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _validate_media_id(value)

    def test_png_document_upload_uses_binary_mime_and_original_bytes(self):
        """Не включает image-оптимизацию для PNG, отправляемого как документ."""
        client = FakeClient()
        data = make_png()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = (
            b'{"file_id":"namespace/123/png-original"}'
        )
        with mock.patch(
            "messenger_user_client.client._NO_REDIRECT_OPENER.open",
            return_value=response,
        ) as opened:
            result = client._upload_media(
                data, "result.png", "document", "1/2/chat", str(uuid.uuid4()),
            )
        request = opened.call_args.args[0]
        self.assertEqual(request.get_header("Content-type"), "application/octet-stream")
        self.assertEqual(request.data, data)
        self.assertEqual(result["file_id"], "namespace/123/png-original")


class DownloadSafetyTest(unittest.TestCase):
    def test_host_allowlist_rejects_lookalikes(self):
        """Проверить принятие Yandex-поддомена и отказ от DNS-lookalike."""
        self.assertTrue(_is_yandex_host("files.yandex.net"))
        self.assertFalse(_is_yandex_host("yandex.net.evil.example"))
        self.assertFalse(_is_yandex_host("evilyandex.net"))

    def test_redirect_strips_oauth_and_writes_exclusive_target(self):
        """Проверить снятие OAuth на redirect и эксклюзивное создание файла."""
        client = FakeClient()
        preview_target_dir = tempfile.TemporaryDirectory()
        self.addCleanup(preview_target_dir.cleanup)
        target = Path(preview_target_dir.name) / "download.bin"
        preview = client.download_media("namespace/123/file-id", target, 10)
        seen_headers = []
        seen_urls = []

        class Response:
            status = 200

            def __enter__(self):
                """Вернуть download-response double из контекстного менеджера."""
                return self

            def __exit__(self, *args):
                """Не подавлять исключения при выходе из download-контекста."""
                return False

            def read(self, size=-1):
                """Вернуть один chunk, а затем EOF."""
                if getattr(self, "done", False):
                    return b""
                self.done = True
                return b"abc"

        def open_side_effect(request, timeout):
            """Сначала вернуть redirect, затем разрешённый content response."""
            seen_headers.append(dict(request.header_items()))
            seen_urls.append(request.full_url)
            if len(seen_headers) == 1:
                raise urllib.error.HTTPError(
                    request.full_url,
                    302,
                    "redirect",
                    {"Location": "https://cdn.yandex.net/object"},
                    None,
                )
            return Response()

        with mock.patch(
            "messenger_user_client.client._NO_REDIRECT_OPENER.open",
            side_effect=open_side_effect,
        ):
            result = client.download_media(
                "namespace/123/file-id", target, 10, confirm_fingerprint=preview["fingerprint"]
            )
        self.assertEqual(result["size"], 3)
        self.assertEqual(
            seen_urls[0],
            "https://files.messenger.yandex.ru/file_shortterm/namespace/123/file-id",
        )
        self.assertTrue(any(k.lower() == "authorization" for k in seen_headers[0]))
        self.assertFalse(any(k.lower() == "authorization" for k in seen_headers[1]))
        self.assertEqual(target.read_bytes(), b"abc")
        with self.assertRaises(FileExistsError):
            os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL)

    def test_redirect_outside_yandex_removes_partial(self):
        """Проверить отказ от внешнего redirect и удаление partial-файла."""
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "download.bin"
            preview = client.download_media("file-id", target, 10)
            error = urllib.error.HTTPError(
                "https://api.messenger.yandex.net/download/file-id",
                302,
                "redirect",
                {"Location": "https://evil.example/object"},
                None,
            )
            with mock.patch(
                "messenger_user_client.client._NO_REDIRECT_OPENER.open",
                side_effect=error,
            ):
                with self.assertRaises(MessengerError):
                    client.download_media(
                        "file-id", target, 10, confirm_fingerprint=preview["fingerprint"]
                    )
            self.assertFalse(target.exists())

    def test_size_limit_removes_partial(self):
        """Проверить удаление partial при превышении заданного max_bytes."""
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "download.bin"
            preview = client.download_media("file-id", target, 2)

            class Response:
                def __enter__(self):
                    """Вернуть oversized response-double из контекста."""
                    return self

                def __exit__(self, *args):
                    """Не подавлять ошибку превышения размера."""
                    return False

                def read(self, size=-1):
                    """Вернуть chunk больше лимита, затем EOF."""
                    if getattr(self, "done", False):
                        return b""
                    self.done = True
                    return b"abc"

            with mock.patch(
                "messenger_user_client.client._NO_REDIRECT_OPENER.open",
                return_value=Response(),
            ):
                with self.assertRaises(MessengerError):
                    client.download_media(
                        "file-id", target, 2, confirm_fingerprint=preview["fingerprint"]
                    )
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
