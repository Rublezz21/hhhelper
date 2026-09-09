"""Минимальный клиент Telegram Bot API.

Используется long polling (`getUpdates`) и инлайн-клавиатуры — этого хватает
для панели управления и не тянет за собой лишних зависимостей.
"""

from __future__ import annotations

import html
import logging
import time
from typing import Any, Callable, Dict, List, Optional

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://api.telegram.org"
#: Telegram обрезает сообщения длиннее 4096 символов.
MAX_MESSAGE_LENGTH = 4096
MAX_RETRIES = 4


class TelegramError(Exception):
    """Ошибка обращения к Telegram Bot API."""

    def __init__(self, message: str, code: Optional[int] = None, payload: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


def escape(text: str) -> str:
    """Экранирует текст для parse_mode=HTML."""
    return html.escape(str(text or ""), quote=False)


def trim(text: str, limit: int = MAX_MESSAGE_LENGTH) -> str:
    """Обрезает сообщение до допустимой длины."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


class TelegramApi:
    """Тонкая обёртка над HTTP API Telegram."""

    def __init__(
        self,
        token: str,
        *,
        session: Optional[requests.Session] = None,
        base_url: str = BASE_URL,
        timeout: int = 65,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not token:
            raise TelegramError(
                "Не задан токен Telegram-бота. Получите его у @BotFather и укажите "
                "в settings.telegram.token или в переменной HH_TELEGRAM_TOKEN."
            )
        self.token = token
        self.session = session or requests.Session()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._sleep = sleep

    # -- низкий уровень -----------------------------------------------------

    def call(self, method: str, **params: Any) -> Any:
        """Вызывает метод Bot API с повторами при 429 и сетевых сбоях."""
        url = f"{self.base_url}/bot{self.token}/{method}"
        payload = {key: value for key, value in params.items() if value is not None}
        last_error: Optional[TelegramError] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.session.post(url, json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = TelegramError(f"Сетевая ошибка при вызове {method}: {exc}")
                self._sleep(min(2 ** attempt, 15))
                continue
            try:
                data = response.json()
            except ValueError:
                data = {}
            if data.get("ok"):
                return data.get("result")
            description = data.get("description") or response.text[:200]
            code = data.get("error_code") or response.status_code
            if code == 429:
                retry_after = float((data.get("parameters") or {}).get("retry_after") or 1)
                log.debug("Telegram просит подождать %s с", retry_after)
                self._sleep(retry_after)
                last_error = TelegramError(f"Telegram 429: {description}", code, data)
                continue
            if code and code >= 500:
                last_error = TelegramError(f"Telegram {code}: {description}", code, data)
                self._sleep(min(2 ** attempt, 15))
                continue
            raise TelegramError(f"Telegram {code}: {description}", code, data)
        assert last_error is not None
        raise last_error

    # -- методы -------------------------------------------------------------

    def get_me(self) -> Dict[str, Any]:
        return self.call("getMe")

    def get_updates(self, offset: Optional[int] = None, timeout: int = 30) -> List[Dict[str, Any]]:
        """Забирает новые события (long polling)."""
        return self.call(
            "getUpdates",
            offset=offset,
            timeout=timeout,
            allowed_updates=["message", "callback_query"],
        ) or []

    def send_message(
        self,
        chat_id: int,
        text: str,
        keyboard: Optional[Dict[str, Any]] = None,
        *,
        preview: bool = False,
    ) -> Dict[str, Any]:
        return self.call(
            "sendMessage",
            chat_id=chat_id,
            text=trim(text),
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=not preview,
        )

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        keyboard: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Меняет текст сообщения; «сообщение не изменилось» — не ошибка."""
        try:
            return self.call(
                "editMessageText",
                chat_id=chat_id,
                message_id=message_id,
                text=trim(text),
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        except TelegramError as exc:
            if "not modified" in str(exc).lower():
                return None
            raise

    def edit_keyboard(self, chat_id: int, message_id: int, keyboard: Optional[Dict[str, Any]] = None) -> Any:
        try:
            return self.call(
                "editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup=keyboard
            )
        except TelegramError as exc:
            if "not modified" in str(exc).lower():
                return None
            raise

    def answer_callback(self, callback_id: str, text: str = "", alert: bool = False) -> Any:
        return self.call("answerCallbackQuery", callback_query_id=callback_id, text=text or None, show_alert=alert)

    def send_action(self, chat_id: int, action: str = "typing") -> Any:
        try:
            return self.call("sendChatAction", chat_id=chat_id, action=action)
        except TelegramError:  # индикатор набора — не повод падать
            return None
