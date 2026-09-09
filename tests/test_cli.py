"""Сквозные тесты командной строки (сеть подменена фейковым клиентом)."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import yaml

from hhhelper import cli
from hhhelper.storage import History

from .fakes import FakeClient, make_vacancy

CONFIG = {
    "settings": {
        "resume_id": "resume-1",
        "candidate_name": "Иван",
        "delay_seconds": [0, 0],
        "max_per_run": 5,
        "interactive": False,
    },
    "letters": [
        {"name": "backend", "text": "Письмо про {vacancy_name}", "default": True},
        {"name": "data", "text": "Данные"},
    ],
    "filters": [{"name": "py", "search": {"text": "python"}, "rules": {"salary_min": 150000}}],
}


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text(yaml.safe_dump(CONFIG, allow_unicode=True), encoding="utf-8")
        self.db_path = self.root / "history.db"
        self.client = FakeClient([make_vacancy("1"), make_vacancy("2", name="Python инженер")])
        patcher = mock.patch.object(
            cli.Context, "client", new_callable=mock.PropertyMock, return_value=self.client
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *args, stdin: str = ""):
        """Запускает CLI и возвращает (код возврата, вывод)."""
        argv = ["--config", str(self.config_path), "--db", str(self.db_path), *args]
        buffer = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin)), redirect_stdout(buffer):
            code = cli.main(argv)
        return code, buffer.getvalue()

    def read_config(self):
        return yaml.safe_load(self.config_path.read_text(encoding="utf-8"))


class TestInspectionCommands(CliTestCase):
    def test_letters_list(self):
        code, out = self.run_cli("letters")
        self.assertEqual(code, 0)
        self.assertIn("backend", out)
        self.assertIn("data", out)

    def test_letter_show(self):
        code, out = self.run_cli("letters", "show", "backend")
        self.assertEqual(code, 0)
        self.assertIn("Письмо про", out)

    def test_filters_list(self):
        code, out = self.run_cli("filters")
        self.assertEqual(code, 0)
        self.assertIn("py", out)

    def test_filter_show_is_json(self):
        code, out = self.run_cli("filters", "show", "py")
        self.assertEqual(code, 0)
        payload = json.loads(out[out.index("{"):])
        self.assertEqual(payload["search"]["text"], "python")

    def test_unknown_filter_reports_error(self):
        code, out = self.run_cli("filters", "show", "нет-такого")
        self.assertEqual(code, 1)

    def test_resumes(self):
        code, out = self.run_cli("resumes")
        self.assertEqual(code, 0)
        self.assertIn("resume-1", out)

    def test_search_preview(self):
        code, out = self.run_cli("search")
        self.assertEqual(code, 0)
        self.assertIn("Python инженер", out)
        self.assertEqual(self.client.applications, [])


class TestApplyCommand(CliTestCase):
    def test_dry_run_sends_nothing(self):
        code, out = self.run_cli("apply", "--dry-run", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(self.client.applications, [])
        self.assertIn("Проверочный запуск", out)

    def test_apply_sends_and_records(self):
        code, out = self.run_cli("apply", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.client.applications), 2)
        with History(self.db_path) as history:
            self.assertTrue(history.has_applied("1"))
            self.assertEqual(history.stats()["applied"], 2)
        self.assertIn("Отклик отправлен", out)

    def test_limit_and_letter_override(self):
        code, _ = self.run_cli("apply", "--yes", "--limit", "1", "--letter", "data")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.client.applications), 1)
        self.assertEqual(self.client.applications[0]["message"], "Данные")

    def test_second_run_skips_applied(self):
        self.run_cli("apply", "--yes")
        self.client.applications.clear()
        code, _ = self.run_cli("apply", "--yes")
        self.assertEqual(self.client.applications, [])

    def test_stats_and_history_after_apply(self):
        self.run_cli("apply", "--yes")
        code, out = self.run_cli("stats")
        self.assertEqual(code, 0)
        self.assertIn("Откликов:       2", out)
        code, out = self.run_cli("history")
        self.assertEqual(code, 0)
        self.assertIn("Python инженер", out)

    def test_sync_imports_negotiations(self):
        code, out = self.run_cli("sync")
        self.assertEqual(code, 0)
        self.assertIn("Добавлено в локальную историю: 1", out)
        with History(self.db_path) as history:
            self.assertTrue(history.has_applied("1"))


class TestFilterManagement(CliTestCase):
    def test_add_filter_from_arguments(self):
        code, out = self.run_cli(
            "filters", "add", "go-remote",
            "--text", "Golang", "--area", "1", "--area", "2",
            "--schedule", "remote", "--salary-min", "250000",
            "--exclude-title", "1С", "--exclude-employer", "Агентство",
            "--max-applications", "5", "--letter", "backend", "--pages", "3",
        )
        self.assertEqual(code, 0)
        data = self.read_config()
        added = [f for f in data["filters"] if f["name"] == "go-remote"][0]
        self.assertEqual(added["search"]["text"], "Golang")
        self.assertEqual(added["search"]["area"], ["1", "2"])
        self.assertEqual(added["search"]["pages"], 3)
        self.assertEqual(added["rules"]["salary_min"], 250000)
        self.assertEqual(added["rules"]["title_exclude"], ["1С"])
        self.assertEqual(added["letter"], "backend")

    def test_added_filter_is_used_by_apply(self):
        self.run_cli("filters", "add", "go", "--text", "Golang")
        self.run_cli("filters", "disable", "py")
        self.run_cli("apply", "--yes", "--dry-run")
        self.assertEqual(self.client.search_calls[-1]["params"]["text"], "Golang")

    def test_duplicate_filter_rejected(self):
        code, out = self.run_cli("filters", "add", "py", "--text", "x")
        self.assertEqual(code, 1)
        self.assertIn("уже существует", out)

    def test_enable_disable_and_remove(self):
        self.run_cli("filters", "disable", "py")
        self.assertFalse(self.read_config()["filters"][0]["enabled"])
        self.run_cli("filters", "enable", "py")
        self.assertNotIn("enabled", self.read_config()["filters"][0])
        code, _ = self.run_cli("filters", "remove", "py")
        self.assertEqual(code, 0)
        self.assertEqual(self.read_config()["filters"], [])

    def test_wizard_creates_filter(self):
        answers = "\n".join(
            ["ml-remote", "Удалённый ML", "Machine Learning", "1,2", "between1And3",
             "remote", "14", "2", "300000", "1С,стажер", "казино", "Кадровое", "7", "data", "y"]
        ) + "\n"
        with mock.patch("builtins.input", side_effect=answers.splitlines()):
            code, out = self.run_cli("filters", "add")
        self.assertEqual(code, 0)
        added = [f for f in self.read_config()["filters"] if f["name"] == "ml-remote"][0]
        self.assertEqual(added["search"]["text"], "Machine Learning")
        self.assertEqual(added["search"]["area"], ["1", "2"])
        self.assertEqual(added["rules"]["salary_min"], 300000)
        self.assertEqual(added["rules"]["title_exclude"], ["1С", "стажер"])
        self.assertEqual(added["letter"], "data")


class TestLetterManagement(CliTestCase):
    def test_add_letter_with_text(self):
        code, _ = self.run_cli(
            "letters", "add", "startup", "--text", "Привет, {employer}!",
            "--keyword", "стартап", "--for-filter", "py",
        )
        self.assertEqual(code, 0)
        data = self.read_config()
        added = [l for l in data["letters"] if l["name"] == "startup"][0]
        self.assertEqual(added["match"]["keywords"], ["стартап"])
        self.assertEqual(added["match"]["filters"], ["py"])

    def test_add_letter_from_file(self):
        path = self.root / "letter.txt"
        path.write_text("Текст из файла", encoding="utf-8")
        code, _ = self.run_cli("letters", "add", "fromfile", "--file", str(path))
        self.assertEqual(code, 0)
        self.assertIn("Текст из файла", self.config_path.read_text(encoding="utf-8"))

    def test_remove_letter_unlinks_filters(self):
        self.run_cli("filters", "add", "tmp", "--text", "x", "--letter", "data")
        code, _ = self.run_cli("letters", "remove", "data")
        self.assertEqual(code, 0)
        data = self.read_config()
        self.assertNotIn("data", [l["name"] for l in data["letters"]])
        self.assertNotIn("letter", [f for f in data["filters"] if f["name"] == "tmp"][0])

    def test_duplicate_letter_rejected(self):
        code, out = self.run_cli("letters", "add", "backend", "--text", "x")
        self.assertEqual(code, 1)


class TestInit(unittest.TestCase):
    def test_init_creates_valid_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = cli.main(["init", "--path", str(path)])
            self.assertEqual(code, 0)
            from hhhelper.config import load_config

            config = load_config(str(path))
            self.assertTrue(config.filters)
            self.assertTrue(config.letters)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["init", "--path", str(path)]), 1)
                self.assertEqual(cli.main(["init", "--path", str(path), "--force"]), 0)


if __name__ == "__main__":
    unittest.main()


class TestConsolePrompt(unittest.TestCase):
    """Диалог подтверждения отклика."""

    def setUp(self):
        from hhhelper.config import AppConfig
        from hhhelper.letters import LetterBook
        from hhhelper.models import Vacancy

        self.config = AppConfig.from_dict(CONFIG)
        self.book = LetterBook(self.config)
        self.vacancy = Vacancy.parse(make_vacancy("1"))
        self.choice = self.book.choose(self.vacancy)
        self.prompt = cli.ConsolePrompt()

    def ask(self, answers):
        with mock.patch("builtins.input", side_effect=answers), redirect_stdout(io.StringIO()) as out:
            decision = self.prompt.ask(self.vacancy, self.choice, "текст письма", self.book)
        return decision, out.getvalue()

    def test_enter_means_apply(self):
        decision, _ = self.ask([""])
        self.assertEqual(decision.action, "apply")
        self.assertEqual(decision.letter.name, "backend")

    def test_skip(self):
        self.assertEqual(self.ask(["n"])[0].action, "skip")

    def test_quit(self):
        self.assertEqual(self.ask(["q"])[0].action, "quit")

    def test_apply_all(self):
        self.assertEqual(self.ask(["a"])[0].action, "apply_all")

    def test_preview_then_apply(self):
        decision, out = self.ask(["p", ""])
        self.assertEqual(decision.action, "apply")
        self.assertIn("текст письма", out)

    def test_change_letter_by_number(self):
        decision, _ = self.ask(["l", "2"])
        self.assertEqual(decision.letter.name, "data")

    def test_change_letter_by_name(self):
        decision, _ = self.ask(["l", "data"])
        self.assertEqual(decision.letter.name, "data")

    def test_cancel_letter_choice_then_apply(self):
        decision, _ = self.ask(["l", "", ""])
        self.assertEqual(decision.letter.name, "backend")

    def test_unknown_letter_asks_again(self):
        decision, out = self.ask(["l", "нет-такого", ""])
        self.assertEqual(decision.action, "apply")
        self.assertIn("не найдено", out)

    def test_open_in_browser(self):
        with mock.patch("hhhelper.cli.webbrowser.open") as opener:
            decision, _ = self.ask(["o", ""])
        opener.assert_called_once_with(self.vacancy.url)
        self.assertEqual(decision.action, "apply")

    def test_unrecognized_answer_repeats_question(self):
        decision, out = self.ask(["что?", ""])
        self.assertEqual(decision.action, "apply")
        self.assertIn("Не понял ответ", out)


class TestVacancyCard(unittest.TestCase):
    def test_card_contains_key_fields(self):
        from hhhelper.models import Vacancy

        card = cli.vacancy_card(Vacancy.parse(make_vacancy("1", name="Python-разработчик")))
        self.assertIn("Python-разработчик", card)
        self.assertIn("ООО Ромашка", card)
        self.assertIn("200 000 RUR", card)
        self.assertIn("hh.ru/vacancy/1", card)
