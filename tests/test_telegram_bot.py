"""Сквозные тесты панели управления в Telegram."""

import tempfile
import unittest
from pathlib import Path

import yaml

from hhhelper.api import HHApiError
from hhhelper.config import load_config
from hhhelper.storage import History
from hhhelper.telegram.bot import TelegramBot, TelegramSettings

from .fakes import FakeClient, FakeTelegramApi, callback_update, make_vacancy, message_update

OWNER = 555
STRANGER = 999

CONFIG = {
    "settings": {
        "resume_id": "resume-1",
        "candidate_name": "Иван",
        "delay_seconds": [0, 0],
        "max_per_run": 5,
        "telegram": {"allowed_users": [OWNER]},
    },
    "letters": [
        {"name": "backend", "text": "Письмо про {vacancy_name}", "default": True},
        {"name": "data", "text": "Данные: {vacancy_name}"},
    ],
    "filters": [{"name": "py", "search": {"text": "python"}, "rules": {"salary_min": 150000}}],
}


class BotTestCase(unittest.TestCase):
    """Бот с фейковым Telegram, фейковым hh.ru и синхронным запуском задач."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = Path(self.tmp.name) / "config.yaml"
        self.config_path.write_text(yaml.safe_dump(CONFIG, allow_unicode=True), encoding="utf-8")
        self.config = load_config(str(self.config_path))
        self.api = FakeTelegramApi()
        self.client = FakeClient([make_vacancy("1"), make_vacancy("2", name="Python инженер")])
        # Каждый прогон открывает своё соединение с общей базой — как в бою.
        self.db_path = Path(self.tmp.name) / "history.db"
        self.bot = TelegramBot(
            self.config,
            self.api,
            TelegramSettings(token="t", allowed_users=[OWNER]),
            history_factory=lambda: History(self.db_path),
            client_factory=lambda: self.client,
            executor=lambda job: job(),  # без потоков — сразу выполняем
        )

    def send(self, text, user_id=OWNER):
        self.bot.handle_update(message_update(text, user_id=user_id, chat_id=user_id))

    def press(self, data, user_id=OWNER):
        self.bot.handle_update(callback_update(data, user_id=user_id, chat_id=user_id))

    def saved_config(self):
        return yaml.safe_load(self.config_path.read_text(encoding="utf-8"))


class TestAccess(BotTestCase):
    def test_owner_sees_menu(self):
        self.send("/start")
        self.assertIn("Помощник кандидата hh.ru", self.api.last_text)
        self.assertIn("f", self.api.last_keyboard)

    def test_stranger_is_rejected(self):
        self.send("/start", user_id=STRANGER)
        self.assertIn("Доступ к панели закрыт", self.api.last_text)
        self.assertIn(str(STRANGER), self.api.last_text)

    def test_stranger_callback_rejected(self):
        self.press("f", user_id=STRANGER)
        self.assertTrue(self.api.answers[-1]["alert"])
        self.assertEqual(self.api.sent, [])

    def test_claim_binds_first_user(self):
        settings = TelegramSettings(token="t", allowed_users=[], claim=True)
        bot = TelegramBot(
            self.config,
            self.api,
            settings,
            history_factory=lambda: History(self.db_path),
            client_factory=lambda: self.client,
            executor=lambda job: job(),
        )
        bot.handle_update(message_update("/start", user_id=777, chat_id=777))
        self.assertEqual(settings.allowed_users, [777])
        self.assertEqual(self.saved_config()["settings"]["telegram"]["allowed_users"], [777])

    def test_id_command(self):
        self.send("/id")
        self.assertIn(str(OWNER), self.api.last_text)


class TestNavigation(BotTestCase):
    def test_filters_list_and_card(self):
        self.press("f")
        self.assertIn("Фильтры вакансий", self.api.last_text)
        self.press("f:s:0")
        self.assertIn("Фильтр «py»", self.api.last_text)
        self.assertIn("зарплата от: 150000", self.api.last_text)

    def test_toggle_filter_persists(self):
        self.press("f:t:0")
        self.assertIn("⚪️ выключен", self.api.last_text)
        self.assertFalse(self.saved_config()["filters"][0]["enabled"])
        self.press("f:t:0")
        self.assertIn("🟢 включён", self.api.last_text)

    def test_delete_filter_asks_confirmation(self):
        self.press("f:d:0")
        self.assertIn("Удалить фильтр", self.api.last_text)
        self.assertIn("f:dc:0", self.api.last_keyboard)
        self.press("f:dc:0")
        self.assertEqual(self.saved_config()["filters"], [])

    def test_letters_list_and_card(self):
        self.press("l")
        self.assertIn("Сопроводительные письма", self.api.last_text)
        self.press("l:s:0")
        self.assertIn("Письмо про", self.api.last_text)

    def test_make_letter_default(self):
        self.press("l:def:1")
        letters = {letter["name"]: letter.get("default", False) for letter in self.saved_config()["letters"]}
        self.assertEqual(letters, {"backend": False, "data": True})

    def test_delete_letter_unlinks_filter(self):
        self.config.get_filter("py").letter = "data"
        self.press("l:dc:1")
        saved = self.saved_config()
        self.assertNotIn("data", [letter["name"] for letter in saved["letters"]])
        self.assertNotIn("letter", saved["filters"][0])

    def test_stats_and_history(self):
        self.press("st")
        self.assertIn("Статистика откликов", self.api.last_text)
        self.press("h")
        self.assertIn("История пока пуста", self.api.last_text)

    def test_bad_index_falls_back_to_list(self):
        self.press("f:s:42")
        self.assertIn("Фильтры вакансий", self.api.last_text)

    def test_unknown_command_shows_help(self):
        self.send("/whatever")
        self.assertIn("Панель управления", self.api.last_text)


class TestSettings(BotTestCase):
    def test_resume_choice(self):
        self.press("cfg")
        self.assertIn("Настройки", self.api.last_text)
        self.press("cfg:resume")
        self.assertIn("resume-1", self.api.last_keyboard[0])
        self.press("cfg:res:resume-2")
        self.assertEqual(self.saved_config()["settings"]["resume_id"], "resume-2")

    def test_limits_adjustable(self):
        self.press("cfg:run:5")
        self.assertEqual(self.saved_config()["settings"]["max_per_run"], 10)
        self.press("cfg:day:-10")
        self.assertEqual(self.saved_config()["settings"]["max_per_day"], 180)

    def test_resume_error_is_reported(self):
        def broken():
            raise HHApiError("hh.ru 401: токен недействителен", 401)

        self.bot._client = None
        self.bot._client_factory = broken
        self.press("cfg:resume")
        self.assertIn("токен недействителен", self.api.last_text)


class TestWizards(BotTestCase):
    def test_create_filter_through_chat(self):
        self.press("f:new")
        self.assertIn("Создание фильтра", self.api.last_text)
        for answer in ["go-remote", "Golang", "w:113", "w:between1And3", "w:remote", "300000", "1С"]:
            self.send(answer)
        self.press("w:backend")
        saved = [flt for flt in self.saved_config()["filters"] if flt["name"] == "go-remote"]
        self.assertTrue(saved)
        self.assertEqual(saved[0]["search"]["text"], "Golang")
        self.assertEqual(saved[0]["rules"]["salary_min"], 300000)
        self.assertEqual(saved[0]["letter"], "backend")
        self.assertIn("Фильтры вакансий", self.api.last_text)

    def test_created_filter_is_used_by_run(self):
        self.press("f:new")
        for answer in ["go", "Golang", "-", "-", "-", "-", "-"]:
            self.send(answer)
        self.press("w:backend")
        self.press("run:mode:dry:1")
        self.assertEqual(self.client.search_calls[-1]["params"]["text"], "Golang")

    def test_duplicate_name_reported(self):
        self.press("f:new")
        for answer in ["py", "python", "-", "-", "-", "-", "-"]:
            self.send(answer)
        self.press("w:backend")
        self.assertIn("уже существует", self.api.last_text)
        self.assertEqual(len(self.saved_config()["filters"]), 1)

    def test_create_letter_through_chat(self):
        self.press("l:new")
        self.send("startup")
        self.send("Здравствуйте! Меня заинтересовала вакансия {vacancy_name}.")
        self.send("стартап, финтех")
        self.press("w:нет")
        saved = [letter for letter in self.saved_config()["letters"] if letter["name"] == "startup"]
        self.assertTrue(saved)
        self.assertEqual(saved[0]["match"]["keywords"], ["стартап", "финтех"])

    def test_cancel_wizard(self):
        self.press("f:new")
        self.send("/cancel")
        self.assertIn("отменено", self.api.last_text.lower())
        self.send("просто текст")
        self.assertIn("Помощник кандидата", self.api.last_text)


class TestSearchAndRun(BotTestCase):
    def test_search_preview_does_not_apply(self):
        self.press("srch:0")
        self.assertIn("Подходящих вакансий: 2", "\n".join(self.api.texts))
        self.assertEqual(self.client.applications, [])

    def test_dry_run(self):
        self.press("run:mode:dry:all")
        self.assertEqual(self.client.applications, [])
        summary = self.api.last_text
        self.assertIn("Проверка завершена", summary)
        self.assertIn("Откликнулись бы на: 2", summary)

    def test_auto_run_applies_and_records(self):
        self.press("run:mode:auto:all")
        self.assertEqual(len(self.client.applications), 2)
        with History(self.db_path) as history:
            self.assertTrue(history.has_applied("1"))
        self.assertIn("Отправлено откликов: 2", self.api.last_text)

    def test_run_requires_resume(self):
        self.config.settings.resume_id = None
        self.press("run:mode:auto:all")
        self.assertIn("Не выбрано резюме", self.api.last_text)
        self.assertEqual(self.client.applications, [])

    def test_run_menu_shows_modes(self):
        self.press("run:pick:0")
        self.assertIn("run:mode:ask:0", self.api.last_keyboard)
        self.assertIn("run:mode:dry:0", self.api.last_keyboard)

    def test_hh_error_reported(self):
        def broken():
            raise HHApiError("hh.ru 401: токен недействителен", 401)

        self.bot._client = None
        self.bot._client_factory = broken
        self.press("run:mode:auto:all")
        self.assertIn("токен недействителен", self.api.last_text)

    def test_stop_without_run(self):
        self.send("/stop")
        self.assertIn("ничего не выполняется", self.api.last_text)


if __name__ == "__main__":
    unittest.main()


class TestInteractiveRun(BotTestCase):
    """Режим «с подтверждением»: бот спрашивает по каждой вакансии."""

    def setUp(self):
        super().setUp()
        self.jobs = []
        self.bot._executor = self.jobs.append  # запускаем прогон вручную

    def start(self, answers, target="all"):
        """Стартует прогон, кладёт ответы кандидата и выполняет его."""
        self.press(f"run:mode:ask:{target}")
        session = self.bot.runs[OWNER]
        for answer in answers:
            session.answers.put(answer)
        self.jobs[0]()
        return session

    def test_yes_and_skip(self):
        self.start(["r:yes", "r:no"])
        self.assertEqual([a["vacancy_id"] for a in self.client.applications], ["1"])
        summary = self.api.last_text
        self.assertIn("Отправлено откликов: 1", summary)
        self.assertIn("Пропущено вручную: 1", summary)

    def test_card_shows_vacancy_and_letter(self):
        self.start(["r:yes", "r:no"])
        cards = [m for m in self.api.sent if "Письмо про" in m["text"]]
        self.assertEqual(len(cards), 2)  # по карточке на каждую вакансию
        card = cards[0]
        self.assertIn("Python-разработчик", card["text"])
        self.assertIn("ООО Ромашка", card["text"])
        buttons = [b["callback_data"] for row in card["keyboard"]["inline_keyboard"] for b in row]
        self.assertEqual(buttons, ["r:yes", "r:no", "r:letter", "r:stop"])

    def test_progress_message_is_updated_not_duplicated(self):
        self.start(["r:yes", "r:no"])
        progress = [m for m in self.api.sent if m["text"].startswith("🚀")]
        self.assertEqual(len(progress), 1)
        self.assertTrue(any("Просмотрено" in edit["text"] for edit in self.api.edited))

    def test_card_replaced_with_result(self):
        self.start(["r:yes", "r:no"])
        texts = [edit["text"] for edit in self.api.edited]
        self.assertTrue(any("Отклик отправлен" in text for text in texts))

    def test_change_letter_before_sending(self):
        self.start(["r:letter", "r:l:1", "r:yes", "r:no"])
        self.assertEqual(self.client.applications[0]["message"], "Данные: Python-разработчик")
        self.assertTrue(any("Данные:" in edit["text"] for edit in self.api.edited))

    def test_stop_button_ends_run(self):
        self.start(["r:stop"])
        self.assertEqual(self.client.applications, [])
        self.assertIn("остановлено кандидатом", self.api.last_text)

    def test_stop_command_interrupts(self):
        self.press("run:mode:ask:all")
        session = self.bot.runs[OWNER]
        self.send("/stop")
        self.jobs[0]()
        self.assertEqual(self.client.applications, [])
        self.assertTrue(session.stop.is_set())

    def test_second_run_is_refused(self):
        self.press("run:mode:ask:all")
        # Кнопки во время прогона не выполняются — бот напоминает про /stop.
        self.press("run:mode:auto:all")
        self.assertIn("идёт прогон", self.api.last_text.lower())
        self.assertEqual(len(self.jobs), 1)
        # Даже прямой вызов не запустит второй прогон в том же чате.
        self.bot._start_run(OWNER, mode="auto", target="all")
        self.assertIn("Прогон уже идёт", self.api.last_text)
        self.assertEqual(len(self.jobs), 1)

    def test_navigation_blocked_during_run(self):
        self.press("run:mode:ask:all")
        self.press("f")
        self.assertIn("идёт прогон", self.api.last_text.lower())

    def test_timeout_stops_run(self):
        from hhhelper.telegram.bot import RunSession, TelegramPrompt
        from hhhelper.letters import LetterBook
        from hhhelper.models import Vacancy

        session = RunSession(chat_id=OWNER, interactive=True)
        prompt = TelegramPrompt(self.api, session, self.config, timeout=0.0)
        book = LetterBook(self.config)
        vacancy = Vacancy.parse(make_vacancy("1"))
        decision = prompt.ask(vacancy, book.choose(vacancy), "текст", book)
        self.assertEqual(decision.action, "quit")
        self.assertIn("Ответа не было", self.api.edited[-1]["text"])
