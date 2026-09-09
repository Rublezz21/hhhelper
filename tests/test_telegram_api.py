"""Тесты транспорта Telegram Bot API (без сети)."""

import unittest
from typing import Any, Dict, List

import requests

from hhhelper.telegram.api import MAX_MESSAGE_LENGTH, TelegramApi, TelegramError, escape, trim


class FakeResponse:
    def __init__(self, payload: Any = None, status_code: int = 200, text: str = ""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("нет тела")
        return self._payload


class FakeSession:
    def __init__(self, responses: List[Any]):
        self.responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def build(responses):
    session = FakeSession(responses)
    return TelegramApi("123:ABC", session=session, sleep=lambda seconds: None), session


class TestHelpers(unittest.TestCase):
    def test_escape(self):
        self.assertEqual(escape("<b> & </b>"), "&lt;b&gt; &amp; &lt;/b&gt;")

    def test_trim(self):
        self.assertEqual(len(trim("я" * (MAX_MESSAGE_LENGTH + 100))), MAX_MESSAGE_LENGTH)
        self.assertEqual(trim("коротко"), "коротко")

    def test_token_required(self):
        with self.assertRaises(TelegramError) as ctx:
            TelegramApi("")
        self.assertIn("BotFather", str(ctx.exception))


class TestCalls(unittest.TestCase):
    def test_send_message_payload(self):
        api, session = build([FakeResponse({"ok": True, "result": {"message_id": 5}})])
        result = api.send_message(42, "привет", {"inline_keyboard": []})
        self.assertEqual(result["message_id"], 5)
        payload = session.calls[0]["json"]
        self.assertEqual(payload["chat_id"], 42)
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertTrue(session.calls[0]["url"].endswith("/bot123:ABC/sendMessage"))

    def test_get_updates_passes_offset(self):
        api, session = build([FakeResponse({"ok": True, "result": [{"update_id": 7}]})])
        updates = api.get_updates(offset=3, timeout=10)
        self.assertEqual(updates[0]["update_id"], 7)
        self.assertEqual(session.calls[0]["json"]["offset"], 3)

    def test_error_raises(self):
        api, _ = build([FakeResponse({"ok": False, "error_code": 400, "description": "chat not found"})])
        with self.assertRaises(TelegramError) as ctx:
            api.send_message(1, "текст")
        self.assertIn("chat not found", str(ctx.exception))

    def test_retry_after_429(self):
        api, session = build(
            [
                FakeResponse({"ok": False, "error_code": 429, "description": "Too Many Requests",
                              "parameters": {"retry_after": 0}}),
                FakeResponse({"ok": True, "result": {"message_id": 1}}),
            ]
        )
        api.send_message(1, "текст")
        self.assertEqual(len(session.calls), 2)

    def test_retry_on_network_error(self):
        api, session = build([requests.RequestException("обрыв"), FakeResponse({"ok": True, "result": []})])
        self.assertEqual(api.get_updates(), [])
        self.assertEqual(len(session.calls), 2)

    def test_not_modified_is_ignored(self):
        api, _ = build([FakeResponse({"ok": False, "error_code": 400, "description": "message is not modified"})])
        self.assertIsNone(api.edit_message(1, 2, "тот же текст"))

    def test_edit_error_is_raised(self):
        api, _ = build([FakeResponse({"ok": False, "error_code": 400, "description": "message to edit not found"})])
        with self.assertRaises(TelegramError):
            api.edit_message(1, 2, "текст")

    def test_send_action_never_raises(self):
        api, _ = build([FakeResponse({"ok": False, "error_code": 400, "description": "bad"})])
        self.assertIsNone(api.send_action(1))


if __name__ == "__main__":
    unittest.main()
