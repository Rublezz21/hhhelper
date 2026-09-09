"""OAuth-авторизация на hh.ru и хранение токенов.

Поддерживаются два способа:

1. **Полный OAuth** — приложение регистрируется на https://dev.hh.ru,
   пользователь один раз подтверждает доступ в браузере, помощник сам
   обновляет access_token по refresh_token.
2. **Готовый токен** — переменная окружения ``HH_TOKEN`` (удобно для
   личного приложения, где токен выдаётся сразу).
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from .config import config_dir

AUTHORIZE_URL = "https://hh.ru/oauth/authorize"
TOKEN_URL = "https://api.hh.ru/token"
DEFAULT_REDIRECT_PORT = 8765
DEFAULT_REDIRECT_URI = f"http://localhost:{DEFAULT_REDIRECT_PORT}/callback"
#: За сколько секунд до истечения обновляем токен заранее.
REFRESH_MARGIN = 120


class AuthError(Exception):
    """Ошибка авторизации."""


@dataclass
class Token:
    """Пара токенов с временем жизни."""

    access_token: str
    refresh_token: str = ""
    expires_at: float = 0.0
    token_type: str = "bearer"
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = DEFAULT_REDIRECT_URI

    @classmethod
    def from_response(cls, data: Dict[str, Any], **extra: Any) -> "Token":
        expires_in = float(data.get("expires_in") or 0)
        return cls(
            access_token=str(data.get("access_token") or ""),
            refresh_token=str(data.get("refresh_token") or ""),
            expires_at=time.time() + expires_in if expires_in else 0.0,
            token_type=str(data.get("token_type") or "bearer"),
            **extra,
        )

    @property
    def expired(self) -> bool:
        if not self.expires_at:
            return False  # бессрочный токен личного приложения
        return time.time() >= self.expires_at - REFRESH_MARGIN

    @property
    def expires_in(self) -> Optional[int]:
        if not self.expires_at:
            return None
        return max(0, int(self.expires_at - time.time()))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "token_type": self.token_type,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
        }


@dataclass
class TokenStore:
    """Читает/пишет токен в ``~/.config/hhhelper/token.json`` (права 600)."""

    path: Path = field(default_factory=lambda: config_dir() / "token.json")
    _token: Optional[Token] = None
    _loaded: bool = False

    def load(self) -> Optional[Token]:
        if self._loaded:
            return self._token
        self._loaded = True
        env_token = os.environ.get("HH_TOKEN")
        if env_token:
            self._token = Token(access_token=env_token.strip())
            return self._token
        if not self.path.is_file():
            self._token = None
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AuthError(f"Не удалось прочитать {self.path}: {exc}") from exc
        self._token = Token(
            access_token=str(data.get("access_token") or ""),
            refresh_token=str(data.get("refresh_token") or ""),
            expires_at=float(data.get("expires_at") or 0),
            token_type=str(data.get("token_type") or "bearer"),
            client_id=str(data.get("client_id") or ""),
            client_secret=str(data.get("client_secret") or ""),
            redirect_uri=str(data.get("redirect_uri") or DEFAULT_REDIRECT_URI),
        )
        return self._token

    def save(self, token: Token) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(token.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:  # pragma: no cover - файловая система без прав POSIX
            pass
        self._token = token
        self._loaded = True

    def clear(self) -> None:
        if self.path.is_file():
            self.path.unlink()
        self._token = None
        self._loaded = True

    # -- выдача действующего токена ----------------------------------------

    def access_token(self) -> str:
        """Возвращает действующий access_token, при необходимости обновив его."""
        token = self.load()
        if token is None or not token.access_token:
            raise AuthError(
                "Нет токена доступа. Выполните `hhhelper auth login` "
                "или задайте переменную окружения HH_TOKEN."
            )
        if token.expired and token.refresh_token:
            token = refresh_token(token)
            self.save(token)
        elif token.expired:
            raise AuthError("Токен истёк и нет refresh_token. Выполните `hhhelper auth login` заново.")
        return token.access_token


# -- OAuth-поток ------------------------------------------------------------


def build_authorize_url(client_id: str, redirect_uri: str = DEFAULT_REDIRECT_URI, state: str = "") -> str:
    """Ссылка, по которой кандидат подтверждает доступ приложению."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
    }
    if state:
        params["state"] = state
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    *,
    session: Optional[requests.Session] = None,
) -> Token:
    """Меняет authorization code на пару токенов."""
    payload = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    }
    data = _post_token(payload, session=session)
    return Token.from_response(
        data, client_id=client_id, client_secret=client_secret, redirect_uri=redirect_uri
    )


def refresh_token(token: Token, *, session: Optional[requests.Session] = None) -> Token:
    """Обновляет access_token по refresh_token."""
    if not token.refresh_token:
        raise AuthError("Нет refresh_token — требуется повторная авторизация")
    data = _post_token({"grant_type": "refresh_token", "refresh_token": token.refresh_token}, session=session)
    return Token.from_response(
        data,
        client_id=token.client_id,
        client_secret=token.client_secret,
        redirect_uri=token.redirect_uri,
    )


def _post_token(payload: Dict[str, str], *, session: Optional[requests.Session] = None) -> Dict[str, Any]:
    http = session or requests.Session()
    try:
        response = http.post(
            TOKEN_URL,
            data=payload,
            headers={"User-Agent": "hhhelper/1.0 (auth)"},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise AuthError(f"Сеть недоступна при обращении к {TOKEN_URL}: {exc}") from exc
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or "access_token" not in data:
        description = data.get("error_description") or data.get("error") or response.text[:300]
        raise AuthError(f"hh.ru отклонил запрос токена ({response.status_code}): {description}")
    return data


class _CallbackHandler(BaseHTTPRequestHandler):
    """Одноразовый обработчик редиректа с authorization code."""

    result: Dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 - имя задано BaseHTTPRequestHandler
        query = urllib.parse.urlparse(self.path).query
        params = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
        type(self).result.update(params)
        body = (
            "<html><head><meta charset='utf-8'><title>hhhelper</title></head>"
            "<body style='font-family:sans-serif;padding:40px'>"
            "<h2>Готово</h2><p>Можно вернуться в терминал и закрыть эту вкладку.</p>"
            "</body></html>"
        ).encode("utf-8")
        if "error" in params:
            body = (
                "<html><head><meta charset='utf-8'></head><body style='font-family:sans-serif;padding:40px'>"
                f"<h2>Ошибка авторизации</h2><p>{params.get('error_description') or params['error']}</p>"
                "</body></html>"
            ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # тишина в консоли
        return


def wait_for_code(
    client_id: str,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    *,
    timeout: int = 300,
    open_browser: bool = True,
) -> str:
    """Поднимает локальный сервер и ждёт authorization code от hh.ru."""
    parsed = urllib.parse.urlparse(redirect_uri)
    port = parsed.port or DEFAULT_REDIRECT_PORT
    state = secrets.token_urlsafe(16)
    url = build_authorize_url(client_id, redirect_uri, state)

    _CallbackHandler.result = {}
    try:
        server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    except OSError as exc:
        raise AuthError(
            f"Не удалось занять порт {port} для приёма ответа hh.ru: {exc}. "
            "Освободите порт или авторизуйтесь вручную (`hhhelper auth login --manual`)."
        ) from exc

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print("Откройте ссылку и подтвердите доступ:\n" + url)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # pragma: no cover - окружение без браузера
            pass

    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            result = _CallbackHandler.result
            if result:
                if result.get("error"):
                    raise AuthError(
                        f"hh.ru вернул ошибку: {result.get('error_description') or result['error']}"
                    )
                if result.get("state") and result["state"] != state:
                    raise AuthError("Не совпал параметр state — возможна подмена ответа")
                code = result.get("code")
                if code:
                    return code
            time.sleep(0.3)
    finally:
        server.shutdown()
        server.server_close()
    raise AuthError("Не дождались ответа от hh.ru — попробуйте ещё раз")
