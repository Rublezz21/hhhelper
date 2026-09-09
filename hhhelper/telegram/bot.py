"""Панель управления помощником в Telegram.

Бот работает на long polling и предоставляет то же, что и CLI:
просмотр и создание фильтров и писем, проверочный поиск, запуск откликов
(в том числе с подтверждением каждой вакансии прямо в чате), статистику,
историю и базовые настройки.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..api import HHApiError, HHClient
from ..applier import Applier, Prompt, PromptDecision, RunObserver, RunOptions, preview
from ..auth import AuthError
from ..config import AppConfig, ConfigError, Letter, VacancyFilter
from ..letters import LetterBook
from ..models import ApplicationResult, Vacancy
from ..storage import History
from . import views
from .api import TelegramApi, TelegramError
from .wizard import WizardReply, WizardState, advance as wizard_advance, start as wizard_start

log = logging.getLogger(__name__)

#: Сколько ждём ответа кандидата по вакансии, прежде чем остановить прогон.
ANSWER_TIMEOUT = 900.0


@dataclass
class TelegramSettings:
    """Настройки панели: токен бота и список тех, кому разрешено управление."""

    token: str = ""
    allowed_users: List[int] = field(default_factory=list)
    poll_timeout: int = 30
    #: Разрешить первому написавшему стать владельцем панели.
    claim: bool = False

    @classmethod
    def from_config(cls, config: AppConfig, *, token: str = "", claim: bool = False) -> "TelegramSettings":
        raw = config.settings.telegram or {}
        users = raw.get("allowed_users") or []
        return cls(
            token=token or os.environ.get("HH_TELEGRAM_TOKEN") or str(raw.get("token") or ""),
            allowed_users=[int(user) for user in users],
            poll_timeout=int(raw.get("poll_timeout") or 30),
            claim=claim,
        )


@dataclass
class RunSession:
    """Идущий прогон откликов в конкретном чате."""

    chat_id: int
    dry_run: bool = False
    interactive: bool = False
    answers: "queue.Queue[str]" = field(default_factory=queue.Queue)
    stop: threading.Event = field(default_factory=threading.Event)
    current_filter: str = ""
    #: Сообщение с карточкой вакансии, которое редактируется по итогу.
    card_message_id: Optional[int] = None
    progress_message_id: Optional[int] = None
    scanned: int = 0
    rejected: int = 0
    applied: int = 0


class TelegramPrompt(Prompt):
    """Спрашивает подтверждение по каждой вакансии в чате."""

    def __init__(
        self,
        api: TelegramApi,
        session: RunSession,
        config: AppConfig,
        *,
        timeout: float = ANSWER_TIMEOUT,
    ) -> None:
        self.api = api
        self.session = session
        self.config = config
        self.timeout = timeout

    def ask(self, vacancy: Vacancy, choice, text: str, book: LetterBook) -> PromptDecision:
        from ..letters import render  # локальный импорт: избегаем цикла на уровне модуля

        letter = choice.letter
        reason = choice.reason
        message = self.api.send_message(
            self.session.chat_id,
            views.vacancy_message(vacancy, choice.name, text, reason),
            views.vacancy_keyboard(book.letters),
        )
        self.session.card_message_id = message.get("message_id")
        while True:
            answer = self._wait()
            if answer is None:
                self._finish("⌛️ Ответа не было — прогон остановлен.")
                return PromptDecision("quit")
            if answer == "r:yes":
                self._keep_card()
                return PromptDecision("apply", letter)
            if answer == "r:no":
                return PromptDecision("skip")
            if answer == "r:stop":
                self._finish("⏹ Прогон остановлен.")
                return PromptDecision("quit")
            if answer == "r:letter":
                self._set_keyboard(views.letter_choice_keyboard(book.letters))
                continue
            if answer == "r:back":
                self._set_keyboard(views.vacancy_keyboard(book.letters))
                continue
            if answer.startswith("r:l:"):
                index = int(answer.rsplit(":", 1)[1])
                if 0 <= index < len(book.letters):
                    letter = book.letters[index]
                    reason = "выбрано вручную"
                    text = render(letter, vacancy, self.config, self.session.current_filter)
                    self._update_card(views.vacancy_message(vacancy, letter.name, text, reason),
                                      views.vacancy_keyboard(book.letters))
                continue

    # -- работа с сообщением ------------------------------------------------

    def _wait(self) -> Optional[str]:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self.session.stop.is_set():
                return "r:stop"
            try:
                return self.session.answers.get(timeout=0.5)
            except queue.Empty:
                continue
        return None

    def _keep_card(self) -> None:
        """Карточку оставляем на месте: итог допишет наблюдатель прогона."""
        self._set_keyboard(None)

    def _set_keyboard(self, keyboard: Optional[Dict[str, Any]]) -> None:
        if self.session.card_message_id:
            self.api.edit_keyboard(self.session.chat_id, self.session.card_message_id, keyboard)

    def _update_card(self, text: str, keyboard: Optional[Dict[str, Any]]) -> None:
        if self.session.card_message_id:
            self.api.edit_message(self.session.chat_id, self.session.card_message_id, text, keyboard)

    def _finish(self, text: str) -> None:
        if self.session.card_message_id:
            self.api.edit_message(self.session.chat_id, self.session.card_message_id, text, None)
            self.session.card_message_id = None


class TelegramObserver(RunObserver):
    """Показывает ход прогона: одно обновляемое сообщение + итог по вакансии."""

    def __init__(self, api: TelegramApi, session: RunSession) -> None:
        self.api = api
        self.session = session

    def filter_started(self, flt: VacancyFilter) -> None:
        self.session.current_filter = flt.name

    def vacancy_found(self, vacancy: Vacancy) -> None:
        self.session.scanned += 1
        self._progress(current=vacancy.name)

    def vacancy_rejected(self, vacancy: Vacancy, reason: str) -> None:
        self.session.rejected += 1

    def result(self, result: ApplicationResult) -> None:
        if result.status in ("applied", "dry-run"):
            self.session.applied += 1
        if self.session.card_message_id:
            # Интерактивный режим: превращаем карточку в строку с итогом.
            self.api.edit_message(
                self.session.chat_id, self.session.card_message_id, views.result_line(result), None
            )
            self.session.card_message_id = None
        elif result.status in ("applied", "dry-run", "failed"):
            self.api.send_message(self.session.chat_id, views.result_line(result))
        self._progress()

    def limit_reached(self, message: str) -> None:
        self.api.send_message(self.session.chat_id, views.escape(f"Остановка: {message}"))

    def _progress(self, current: str = "", finished: bool = False) -> None:
        text = views.progress_text(
            applied=self.session.applied,
            rejected=self.session.rejected,
            scanned=self.session.scanned,
            current=current,
            dry_run=self.session.dry_run,
            finished=finished,
        )
        if self.session.progress_message_id is None:
            message = self.api.send_message(self.session.chat_id, text)
            self.session.progress_message_id = message.get("message_id")
        else:
            self.api.edit_message(self.session.chat_id, self.session.progress_message_id, text)


class TelegramBot:
    """Маршрутизация команд и кнопок панели."""

    def __init__(
        self,
        config: AppConfig,
        api: TelegramApi,
        settings: TelegramSettings,
        *,
        history_factory: Callable[[], History],
        client_factory: Callable[[], HHClient],
        executor: Optional[Callable[[Callable[[], None]], None]] = None,
    ) -> None:
        self.config = config
        self.api = api
        self.settings = settings
        self._history_factory = history_factory
        self._client_factory = client_factory
        self._executor = executor or _thread_executor
        self._history: Optional[History] = None
        self._client: Optional[HHClient] = None
        self.wizards: Dict[int, WizardState] = {}
        self.runs: Dict[int, RunSession] = {}
        self.offset: Optional[int] = None
        self.running = False

    # -- ресурсы ------------------------------------------------------------

    @property
    def history(self) -> History:
        if self._history is None:
            self._history = self._history_factory()
        return self._history

    def client(self) -> HHClient:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    # -- цикл ---------------------------------------------------------------

    def run_forever(self) -> None:
        """Основной цикл long polling."""
        self.running = True
        while self.running:
            try:
                updates = self.api.get_updates(self.offset, timeout=self.settings.poll_timeout)
            except TelegramError as exc:
                log.warning("Telegram недоступен: %s", exc)
                time.sleep(5)
                continue
            for update in updates:
                self.offset = int(update.get("update_id", 0)) + 1
                try:
                    self.handle_update(update)
                except Exception as exc:  # бот не должен падать из-за одной ошибки
                    log.exception("Ошибка обработки обновления: %s", exc)
                    chat_id = _chat_id(update)
                    if chat_id:
                        self._safe_send(chat_id, views.escape(f"Ошибка: {exc}"))

    def stop(self) -> None:
        self.running = False

    # -- маршрутизация ------------------------------------------------------

    def handle_update(self, update: Dict[str, Any]) -> None:
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
        elif "message" in update:
            self._handle_message(update["message"])

    def _handle_message(self, message: Dict[str, Any]) -> None:
        chat_id = int((message.get("chat") or {}).get("id", 0))
        user_id = int((message.get("from") or {}).get("id", 0))
        text = (message.get("text") or "").strip()
        if not self._authorize(chat_id, user_id):
            return
        if text.startswith("/"):
            self._handle_command(chat_id, text)
            return
        wizard = self.wizards.get(chat_id)
        if wizard is not None:
            self._continue_wizard(chat_id, wizard, text)
            return
        if chat_id in self.runs:
            self.api.send_message(chat_id, "Идёт прогон откликов. Отвечайте кнопками или командой /stop.")
            return
        self._show(chat_id, *views.main_menu(self.config, self._applied_today()))

    def _handle_command(self, chat_id: int, text: str) -> None:
        command = text.split()[0].lstrip("/").split("@")[0].lower()
        if command in ("start", "menu"):
            self.wizards.pop(chat_id, None)
            self._show(chat_id, *views.main_menu(self.config, self._applied_today()))
        elif command == "help":
            self.api.send_message(chat_id, views.help_text())
        elif command == "filters":
            self._show(chat_id, *views.filters_list(self.config))
        elif command == "letters":
            self._show(chat_id, *views.letters_list(self.config))
        elif command == "apply":
            self._show(chat_id, *views.pick_filter(self.config, "run:pick", "Выберите, по какому фильтру откликаться:"))
        elif command == "search":
            self._show(chat_id, *views.pick_filter(self.config, "srch", "Выберите фильтр для проверки:"))
        elif command == "stats":
            self._show(chat_id, *views.stats_view(self.history.stats()))
        elif command == "stop":
            self._stop_run(chat_id)
        elif command == "cancel":
            if self.wizards.pop(chat_id, None) is not None:
                self.api.send_message(chat_id, "Создание отменено.")
            else:
                self.api.send_message(chat_id, "Нечего отменять.")
        elif command == "id":
            self.api.send_message(chat_id, f"Ваш Telegram ID: <code>{chat_id}</code>")
        else:
            self.api.send_message(chat_id, views.help_text())

    def _handle_callback(self, callback: Dict[str, Any]) -> None:
        data = callback.get("data") or ""
        message = callback.get("message") or {}
        chat_id = int((message.get("chat") or {}).get("id", 0))
        user_id = int((callback.get("from") or {}).get("id", 0))
        callback_id = callback.get("id", "")
        self.message_id = message.get("message_id")
        if not self._authorize(chat_id, user_id, callback_id=callback_id):
            return
        self.api.answer_callback(callback_id)

        session = self.runs.get(chat_id)
        if data.startswith("r:"):
            if session is not None:
                session.answers.put(data)
            return
        if session is not None:
            self.api.send_message(chat_id, "Сейчас идёт прогон откликов — остановите его кнопкой или /stop.")
            return
        if data.startswith("w:"):
            wizard = self.wizards.get(chat_id)
            if wizard is not None:
                self._continue_wizard(chat_id, wizard, data)
            return
        self._route(chat_id, data)

    def _route(self, chat_id: int, data: str) -> None:
        parts = data.split(":")
        head = parts[0]
        if data == "nop":
            return
        if head == "menu":
            self._show(chat_id, *views.main_menu(self.config, self._applied_today()))
        elif head == "f":
            self._route_filters(chat_id, parts)
        elif head == "l":
            self._route_letters(chat_id, parts)
        elif head == "srch":
            self._route_search(chat_id, parts)
        elif head == "run":
            self._route_run(chat_id, parts)
        elif head == "st":
            self._show(chat_id, *views.stats_view(self.history.stats()))
        elif head == "h":
            self._show(chat_id, *views.history_view(self.history.recent(limit=15)))
        elif head == "cfg":
            self._route_settings(chat_id, parts)

    # -- фильтры ------------------------------------------------------------

    def _route_filters(self, chat_id: int, parts: List[str]) -> None:
        if len(parts) == 1:
            self._show(chat_id, *views.filters_list(self.config))
            return
        action = parts[1]
        if action == "new":
            self._start_wizard(chat_id, "filter")
            return
        index = self._index(parts[2], len(self.config.filters))
        if index is None:
            self._show(chat_id, *views.filters_list(self.config))
            return
        if action == "s":
            self._show(chat_id, *views.filter_card(self.config, index))
        elif action == "t":
            flt = self.config.filters[index]
            flt.enabled = not flt.enabled
            self._save()
            self._show(chat_id, *views.filter_card(self.config, index))
        elif action == "d":
            self._show(chat_id, *views.filter_delete_confirm(self.config, index))
        elif action == "dc":
            flt = self.config.filters.pop(index)
            for letter in self.config.letters:
                if flt.name in letter.filters:
                    letter.filters.remove(flt.name)
            self._save()
            self.api.send_message(chat_id, f"Фильтр «{views.escape(flt.name)}» удалён.")
            self._show(chat_id, *views.filters_list(self.config))

    # -- письма -------------------------------------------------------------

    def _route_letters(self, chat_id: int, parts: List[str]) -> None:
        if len(parts) == 1:
            self._show(chat_id, *views.letters_list(self.config))
            return
        action = parts[1]
        if action == "new":
            self._start_wizard(chat_id, "letter")
            return
        index = self._index(parts[2], len(self.config.letters))
        if index is None:
            self._show(chat_id, *views.letters_list(self.config))
            return
        if action == "s":
            self._show(chat_id, *views.letter_card(self.config, index))
        elif action == "def":
            for position, letter in enumerate(self.config.letters):
                letter.default = position == index
            self._save()
            self._show(chat_id, *views.letter_card(self.config, index))
        elif action == "d":
            self._show(chat_id, *views.letter_delete_confirm(self.config, index))
        elif action == "dc":
            letter = self.config.letters.pop(index)
            for flt in self.config.filters:
                if flt.letter == letter.name:
                    flt.letter = None
            self._save()
            self.api.send_message(chat_id, f"Письмо «{views.escape(letter.name)}» удалено.")
            self._show(chat_id, *views.letters_list(self.config))

    # -- мастер создания ----------------------------------------------------

    def _start_wizard(self, chat_id: int, kind: str) -> None:
        context = {"letters": [letter.name for letter in self.config.letters]} if kind == "filter" else {}
        state, reply = wizard_start(kind, context)
        self.wizards[chat_id] = state
        self.api.send_message(chat_id, reply.text, reply.keyboard)

    def _continue_wizard(self, chat_id: int, state: WizardState, answer: str) -> None:
        reply = wizard_advance(state, answer)
        if reply.cancelled:
            self.wizards.pop(chat_id, None)
            self.api.send_message(chat_id, reply.text)
            self._show(chat_id, *views.main_menu(self.config, self._applied_today()))
            return
        if not reply.done:
            self.api.send_message(chat_id, reply.text, reply.keyboard)
            return
        self.wizards.pop(chat_id, None)
        try:
            self._save_wizard_result(state, reply)
        except ConfigError as exc:
            self.api.send_message(chat_id, views.escape(f"Не сохранилось: {exc}"))
            return
        self.api.send_message(chat_id, reply.text)
        if state.kind == "filter":
            self._show(chat_id, *views.filters_list(self.config))
        else:
            self._show(chat_id, *views.letters_list(self.config))

    def _save_wizard_result(self, state: WizardState, reply: WizardReply) -> None:
        data = dict(reply.result or {})
        name = str(data.get("name") or "")
        if state.kind == "filter":
            if any(flt.name == name for flt in self.config.filters):
                raise ConfigError(f"фильтр «{name}» уже существует")
            self.config.filters.append(VacancyFilter.from_dict(data))
        else:
            if any(letter.name == name for letter in self.config.letters):
                raise ConfigError(f"письмо «{name}» уже существует")
            self.config.letters.append(Letter.from_dict(data))
        self.config.validate()
        self._save()

    # -- поиск --------------------------------------------------------------

    def _route_search(self, chat_id: int, parts: List[str]) -> None:
        if len(parts) == 1:
            self._show(chat_id, *views.pick_filter(self.config, "srch", "Выберите фильтр для проверки:"))
            return
        filters = self._filters_for(parts[1])
        if not filters:
            self.api.send_message(chat_id, "Нет подходящих фильтров.")
            return
        self.api.send_message(chat_id, "Ищу вакансии…")
        self._executor(lambda: self._run_preview(chat_id, filters))

    def _run_preview(self, chat_id: int, filters: Sequence[VacancyFilter]) -> None:
        history = self._history_factory()
        try:
            client = self.client()
            for flt in filters:
                result = preview(self.config, client, history, flt, limit=10, show_rejected=True)
                self.api.send_message(
                    chat_id, views.preview_result(flt.name, result["passed"], result["rejected"])
                )
        except (HHApiError, AuthError) as exc:
            self.api.send_message(chat_id, views.escape(f"hh.ru: {exc}"))
        finally:
            history.close()

    # -- отклики ------------------------------------------------------------

    def _route_run(self, chat_id: int, parts: List[str]) -> None:
        if len(parts) == 1:
            self._show(chat_id, *views.pick_filter(self.config, "run:pick", "По какому фильтру откликаться?"))
            return
        if parts[1] == "pick":
            target = parts[2]
            if target != "all" and self._index(target, len(self.config.filters)) is None:
                self._show(chat_id, *views.pick_filter(self.config, "run:pick", "По какому фильтру откликаться?"))
                return
            self._show(chat_id, *views.run_modes(self.config, target))
        elif parts[1] == "mode":
            self._start_run(chat_id, mode=parts[2], target=parts[3])

    def _start_run(self, chat_id: int, *, mode: str, target: str) -> None:
        if chat_id in self.runs:
            self.api.send_message(chat_id, "Прогон уже идёт. Остановить — /stop.")
            return
        filters = self._filters_for(target)
        if not filters:
            self.api.send_message(chat_id, "Нет включённых фильтров.")
            return
        if not self.config.letters:
            self.api.send_message(chat_id, "Сначала добавьте хотя бы одно сопроводительное письмо.")
            return
        if not self.config.settings.resume_id and mode != "dry":
            self.api.send_message(chat_id, "Не выбрано резюме — откройте «Настройки».")
            return

        session = RunSession(chat_id=chat_id, dry_run=(mode == "dry"), interactive=(mode == "ask"))
        self.runs[chat_id] = session
        options = RunOptions(
            filters=[flt.name for flt in filters],
            dry_run=session.dry_run,
            interactive=session.interactive,
        )
        self._executor(lambda: self._run_applier(session, options))

    def _run_applier(self, session: RunSession, options: RunOptions) -> None:
        history = self._history_factory()
        observer = TelegramObserver(self.api, session)
        prompt = TelegramPrompt(self.api, session, self.config) if session.interactive else Prompt()
        try:
            applier = Applier(
                self.config,
                self.client(),
                history,
                prompt=prompt,
                observer=observer,
                should_stop=session.stop.is_set,
            )
            report = applier.run(options)
            self.api.send_message(
                session.chat_id,
                views.run_summary(report, session.dry_run),
                views.keyboard([[("‹ Меню", "menu")]]),
            )
        except (HHApiError, AuthError) as exc:
            self.api.send_message(session.chat_id, views.escape(f"hh.ru: {exc}"))
        except Exception as exc:  # noqa: BLE001 - сообщаем и не роняем бота
            log.exception("Прогон завершился ошибкой")
            self.api.send_message(session.chat_id, views.escape(f"Ошибка прогона: {exc}"))
        finally:
            history.close()
            self.runs.pop(session.chat_id, None)

    def _stop_run(self, chat_id: int) -> None:
        session = self.runs.get(chat_id)
        if session is None:
            self.api.send_message(chat_id, "Сейчас ничего не выполняется.")
            return
        session.stop.set()
        session.answers.put("r:stop")
        self.api.send_message(chat_id, "Останавливаю прогон…")

    # -- настройки ----------------------------------------------------------

    def _route_settings(self, chat_id: int, parts: List[str]) -> None:
        if len(parts) == 1:
            self._show(chat_id, *views.settings_view(self.config))
            return
        action = parts[1]
        settings = self.config.settings
        if action == "resume":
            try:
                resumes = self.client().resumes()
            except (HHApiError, AuthError) as exc:
                self.api.send_message(chat_id, views.escape(f"hh.ru: {exc}"))
                return
            self._show(chat_id, *views.resume_choice(resumes, settings.resume_id))
        elif action == "res":
            settings.resume_id = parts[2]
            self._save()
            self.api.send_message(chat_id, "Резюме выбрано.")
            self._show(chat_id, *views.settings_view(self.config))
        elif action in ("run", "day"):
            delta = int(parts[2])
            if action == "run":
                settings.max_per_run = max(1, settings.max_per_run + delta)
            else:
                settings.max_per_day = max(1, settings.max_per_day + delta)
            self._save()
            self._show(chat_id, *views.settings_view(self.config))

    # -- вспомогательное ----------------------------------------------------

    def _authorize(self, chat_id: int, user_id: int, callback_id: str = "") -> bool:
        allowed = self.settings.allowed_users
        if not allowed and self.settings.claim:
            self.settings.allowed_users.append(user_id)
            telegram = dict(self.config.settings.telegram)
            telegram["allowed_users"] = list(self.settings.allowed_users)
            self.config.settings.telegram = telegram
            self._save()
            self.api.send_message(chat_id, "Панель привязана к вашему аккаунту.")
            return True
        if user_id in allowed:
            return True
        if callback_id:
            self.api.answer_callback(callback_id, "Доступ закрыт", alert=True)
        else:
            self.api.send_message(chat_id, views.access_denied(user_id))
        return False

    def _filters_for(self, target: str) -> List[VacancyFilter]:
        if target == "all":
            return self.config.enabled_filters()
        index = self._index(target, len(self.config.filters))
        return [self.config.filters[index]] if index is not None else []

    @staticmethod
    def _index(value: str, size: int) -> Optional[int]:
        try:
            index = int(value)
        except (TypeError, ValueError):
            return None
        return index if 0 <= index < size else None

    def _applied_today(self) -> int:
        return self.history.count_applied_today()

    def _show(self, chat_id: int, text: str, keyboard: Optional[Dict[str, Any]] = None) -> None:
        self.api.send_message(chat_id, text, keyboard)

    def _safe_send(self, chat_id: int, text: str) -> None:
        try:
            self.api.send_message(chat_id, text)
        except TelegramError:
            log.warning("Не удалось отправить сообщение в чат %s", chat_id)

    def _save(self) -> None:
        try:
            self.config.save()
        except ConfigError as exc:
            log.warning("Конфиг не сохранён: %s", exc)


def _thread_executor(job: Callable[[], None]) -> None:
    threading.Thread(target=job, daemon=True).start()


def _chat_id(update: Dict[str, Any]) -> Optional[int]:
    message = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
    chat = message.get("chat") or {}
    return int(chat["id"]) if chat.get("id") is not None else None
