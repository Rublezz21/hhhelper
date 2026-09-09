"""Тесты авторизации и хранения токена."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hhhelper.auth import (
    AuthError,
    Token,
    TokenStore,
    build_authorize_url,
    exchange_code,
    refresh_token,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("нет тела")
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class TestAuthorizeUrl(unittest.TestCase):
    def test_contains_required_params(self):
        url = build_authorize_url("cid", "http://localhost:8765/callback", "st")
        self.assertIn("response_type=code", url)
        self.assertIn("client_id=cid", url)
        self.assertIn("state=st", url)


class TestTokenExchange(unittest.TestCase):
    def test_exchange_code(self):
        session = FakeSession(FakeResponse(200, {"access_token": "a", "refresh_token": "r", "expires_in": 3600}))
        token = exchange_code("code", "cid", "secret", session=session)
        self.assertEqual(token.access_token, "a")
        self.assertEqual(token.client_secret, "secret")
        self.assertGreater(token.expires_at, time.time())
        self.assertEqual(session.calls[0]["data"]["grant_type"], "authorization_code")

    def test_error_response_raises(self):
        session = FakeSession(FakeResponse(400, {"error_description": "код просрочен"}))
        with self.assertRaises(AuthError) as ctx:
            exchange_code("code", "cid", "secret", session=session)
        self.assertIn("код просрочен", str(ctx.exception))

    def test_refresh_keeps_client_credentials(self):
        old = Token(access_token="a", refresh_token="r", client_id="cid", client_secret="s")
        session = FakeSession(FakeResponse(200, {"access_token": "b", "refresh_token": "r2", "expires_in": 100}))
        new = refresh_token(old, session=session)
        self.assertEqual(new.access_token, "b")
        self.assertEqual(new.client_id, "cid")
        self.assertEqual(session.calls[0]["data"]["grant_type"], "refresh_token")

    def test_refresh_without_token(self):
        with self.assertRaises(AuthError):
            refresh_token(Token(access_token="a"))


class TestTokenStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "token.json"

    def test_save_and_load(self):
        store = TokenStore(path=self.path)
        store.save(Token(access_token="a", refresh_token="r", expires_at=time.time() + 3600))
        self.assertEqual(json.loads(self.path.read_text())["access_token"], "a")
        self.assertEqual(TokenStore(path=self.path).access_token(), "a")

    def test_file_permissions(self):
        store = TokenStore(path=self.path)
        store.save(Token(access_token="a"))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_missing_token_message(self):
        with self.assertRaises(AuthError) as ctx:
            TokenStore(path=self.path).access_token()
        self.assertIn("auth login", str(ctx.exception))

    def test_env_token_wins(self):
        with mock.patch.dict("os.environ", {"HH_TOKEN": "из-окружения"}):
            self.assertEqual(TokenStore(path=self.path).access_token(), "из-окружения")

    def test_expired_token_refreshed(self):
        store = TokenStore(path=self.path)
        store.save(Token(access_token="старый", refresh_token="r", expires_at=time.time() - 10))
        fresh = Token(access_token="новый", refresh_token="r2", expires_at=time.time() + 3600)
        with mock.patch("hhhelper.auth.refresh_token", return_value=fresh) as refresher:
            self.assertEqual(TokenStore(path=self.path).access_token(), "новый")
        refresher.assert_called_once()
        self.assertEqual(json.loads(self.path.read_text())["access_token"], "новый")

    def test_expired_without_refresh_token(self):
        store = TokenStore(path=self.path)
        store.save(Token(access_token="старый", expires_at=time.time() - 10))
        with self.assertRaises(AuthError):
            TokenStore(path=self.path).access_token()

    def test_clear(self):
        store = TokenStore(path=self.path)
        store.save(Token(access_token="a"))
        store.clear()
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
