"""Тесты сценария откликов."""

import datetime as dt
import random
import unittest

from hhhelper.api import HHApiError
from hhhelper.applier import Applier, Prompt, PromptDecision, RunOptions, preview
from hhhelper.config import AppConfig
from hhhelper.models import Vacancy
from hhhelper.storage import History

from .fakes import FakeClient, make_vacancy


def build_config(**overrides):
    data = {
        "settings": {
            "resume_id": "resume-1",
            "candidate_name": "Иван",
            "delay_seconds": [0, 0],
            "max_per_run": 10,
            "max_per_day": 100,
            "interactive": False,
        },
        "letters": [
            {"name": "backend", "text": "Письмо про {vacancy_name}", "match": {"keywords": ["python"]}},
            {"name": "generic", "text": "Универсальное", "default": True},
        ],
        "filters": [
            {
                "name": "py",
                "search": {"text": "python"},
                "rules": {"title_exclude": ["1С"], "salary_min": 150000},
            }
        ],
    }
    data.update(overrides)
    return AppConfig.from_dict(data)


class AlwaysSkip(Prompt):
    def ask(self, vacancy, choice, text, book):
        return PromptDecision("skip")


class QuitAfterFirst(Prompt):
    def __init__(self):
        self.calls = 0

    def ask(self, vacancy, choice, text, book):
        self.calls += 1
        return PromptDecision("apply", choice.letter) if self.calls == 1 else PromptDecision("quit")


class SwitchLetter(Prompt):
    def ask(self, vacancy, choice, text, book):
        return PromptDecision("apply", book.get("generic"))


class ApplyAll(Prompt):
    def __init__(self):
        self.calls = 0

    def ask(self, vacancy, choice, text, book):
        self.calls += 1
        return PromptDecision("apply_all", choice.letter)


class ApplierTestCase(unittest.TestCase):
    def setUp(self):
        self.history = History()
        self.config = build_config()

    def tearDown(self):
        self.history.close()

    def make(self, client, prompt=None, config=None):
        return Applier(
            config or self.config,
            client,
            self.history,
            prompt=prompt,
            sleep=lambda seconds: None,
            rng=random.Random(0),
        )


class TestApplyFlow(ApplierTestCase):
    def test_applies_and_records(self):
        client = FakeClient([make_vacancy("1"), make_vacancy("2", name="Python инженер")])
        report = self.make(client).run(RunOptions(interactive=False))
        self.assertEqual(len(report.applied), 2)
        self.assertEqual([a["vacancy_id"] for a in client.applications], ["1", "2"])
        self.assertIn("Письмо про", client.applications[0]["message"])
        self.assertTrue(self.history.has_applied("1"))
        self.assertEqual(report.scanned, 2)

    def test_rejected_vacancies_are_not_applied(self):
        client = FakeClient(
            [
                make_vacancy("1", name="Программист 1С"),
                make_vacancy("2", salary_from=90000),
                make_vacancy("3"),
            ]
        )
        report = self.make(client).run(RunOptions(interactive=False))
        self.assertEqual([a["vacancy_id"] for a in client.applications], ["3"])
        self.assertEqual(report.rejected, 2)

    def test_dry_run_sends_nothing(self):
        client = FakeClient([make_vacancy("1")])
        report = self.make(client).run(RunOptions(interactive=False, dry_run=True))
        self.assertEqual(client.applications, [])
        self.assertEqual(len(report.dry_run), 1)
        self.assertFalse(self.history.has_applied("1"))

    def test_letter_override(self):
        client = FakeClient([make_vacancy("1")])
        self.make(client).run(RunOptions(interactive=False, letter_override="generic"))
        self.assertEqual(client.applications[0]["message"], "Универсальное")

    def test_history_prevents_duplicates(self):
        self.history.record(Vacancy.parse(make_vacancy("1")), status="applied")
        client = FakeClient([make_vacancy("1"), make_vacancy("2")])
        self.make(client).run(RunOptions(interactive=False))
        self.assertEqual([a["vacancy_id"] for a in client.applications], ["2"])

    def test_run_limit(self):
        client = FakeClient([make_vacancy(str(i)) for i in range(5)])
        report = self.make(client).run(RunOptions(interactive=False, limit=2))
        self.assertEqual(len(client.applications), 2)
        self.assertIn("лимит", report.stopped_reason)

    def test_daily_limit(self):
        config = build_config()
        config.settings.max_per_day = 2
        self.history.record(Vacancy.parse(make_vacancy("90")), status="applied")
        client = FakeClient([make_vacancy("1"), make_vacancy("2"), make_vacancy("3")])
        self.make(client, config=config).run(RunOptions(interactive=False))
        self.assertEqual(len(client.applications), 1)

    def test_daily_limit_already_exhausted(self):
        config = build_config()
        config.settings.max_per_day = 1
        self.history.record(Vacancy.parse(make_vacancy("90")), status="applied")
        client = FakeClient([make_vacancy("1")])
        report = self.make(client, config=config).run(RunOptions(interactive=False))
        self.assertEqual(client.applications, [])
        self.assertIn("дневной лимит", report.stopped_reason)

    def test_filter_max_applications(self):
        config = build_config()
        config.get_filter("py").rules.max_applications = 1
        client = FakeClient([make_vacancy("1"), make_vacancy("2")])
        self.make(client, config=config).run(RunOptions(interactive=False))
        self.assertEqual(len(client.applications), 1)

    def test_only_selected_filters(self):
        config = build_config()
        config.filters.append(config.get_filter("py").__class__.from_dict({"name": "other", "search": {"text": "go"}}))
        client = FakeClient([make_vacancy("1")])
        applier = self.make(client, config=config)
        applier.run(RunOptions(filters=["other"], interactive=False, dry_run=True))
        self.assertEqual(client.search_calls[0]["params"]["text"], "go")

    def test_disabled_filter_skipped(self):
        config = build_config()
        config.get_filter("py").enabled = False
        client = FakeClient([make_vacancy("1")])
        report = self.make(client, config=config).run(RunOptions(interactive=False))
        self.assertEqual(report.stopped_reason, "нет включённых фильтров")
        self.assertEqual(client.search_calls, [])

    def test_missing_resume_id_stops_run(self):
        config = build_config()
        config.settings.resume_id = None
        client = FakeClient([make_vacancy("1")])
        report = self.make(client, config=config).run(RunOptions(interactive=False))
        self.assertIn("резюме", report.stopped_reason)
        self.assertEqual(client.applications, [])


class TestErrors(ApplierTestCase):
    def test_already_applied_recorded_as_applied(self):
        error = HHApiError("уже откликались", 403, {"errors": [{"value": "already_applied"}]})
        client = FakeClient([make_vacancy("1")], fail_with={"1": error})
        report = self.make(client).run(RunOptions(interactive=False))
        self.assertEqual(len(report.applied), 1)
        self.assertTrue(self.history.has_applied("1"))

    def test_fatal_error_stops_run(self):
        error = HHApiError("hh.ru 403: исчерпан дневной лимит откликов [limit_exceeded]", 403,
                           {"errors": [{"value": "limit_exceeded"}]})
        client = FakeClient([make_vacancy("1"), make_vacancy("2")], fail_with={"1": error})
        report = self.make(client).run(RunOptions(interactive=False))
        self.assertEqual(len(report.failed), 1)
        self.assertIn("limit_exceeded", report.stopped_reason)
        self.assertEqual(client.applications, [])

    def test_non_fatal_error_continues(self):
        error = HHApiError("вакансия в архиве", 403, {"errors": [{"value": "archived"}]})
        client = FakeClient([make_vacancy("1"), make_vacancy("2")], fail_with={"1": error})
        report = self.make(client).run(RunOptions(interactive=False))
        self.assertEqual(len(report.failed), 1)
        self.assertEqual([a["vacancy_id"] for a in client.applications], ["2"])


class TestPrompts(ApplierTestCase):
    def test_skip_decision(self):
        client = FakeClient([make_vacancy("1")])
        report = self.make(client, prompt=AlwaysSkip()).run(RunOptions(interactive=True))
        self.assertEqual(client.applications, [])
        self.assertEqual(len(report.skipped), 1)

    def test_quit_stops_run(self):
        client = FakeClient([make_vacancy("1"), make_vacancy("2")])
        report = self.make(client, prompt=QuitAfterFirst()).run(RunOptions(interactive=True))
        self.assertEqual(len(client.applications), 1)
        self.assertEqual(report.stopped_reason, "остановлено кандидатом")

    def test_letter_can_be_changed(self):
        client = FakeClient([make_vacancy("1")])
        self.make(client, prompt=SwitchLetter()).run(RunOptions(interactive=True))
        self.assertEqual(client.applications[0]["message"], "Универсальное")

    def test_apply_all_stops_asking(self):
        prompt = ApplyAll()
        client = FakeClient([make_vacancy("1"), make_vacancy("2"), make_vacancy("3")])
        self.make(client, prompt=prompt).run(RunOptions(interactive=True))
        self.assertEqual(prompt.calls, 1)
        self.assertEqual(len(client.applications), 3)


class TestDescriptionFetching(ApplierTestCase):
    def test_full_description_loaded_when_rules_need_it(self):
        config = build_config()
        config.get_filter("py").rules.text_exclude = ["битрикс"]
        client = FakeClient(
            [make_vacancy("1"), make_vacancy("2")],
            descriptions={"1": "Работа с Битрикс24", "2": "Чистый Python"},
        )
        self.make(client, config=config).run(RunOptions(interactive=False))
        self.assertEqual(client.fetched, ["1", "2"])
        self.assertEqual([a["vacancy_id"] for a in client.applications], ["2"])

    def test_description_not_loaded_without_text_rules(self):
        client = FakeClient([make_vacancy("1")])
        self.make(client).run(RunOptions(interactive=False))
        self.assertEqual(client.fetched, [])

    def test_description_not_loaded_for_rejected_vacancy(self):
        config = build_config()
        config.get_filter("py").rules.text_exclude = ["битрикс"]
        client = FakeClient([make_vacancy("1", name="Разработчик 1С")])
        self.make(client, config=config).run(RunOptions(interactive=False))
        self.assertEqual(client.fetched, [])


class TestPreview(ApplierTestCase):
    def test_preview_reports_reasons_without_applying(self):
        client = FakeClient([make_vacancy("1"), make_vacancy("2", name="Инженер 1С")])
        result = preview(self.config, client, self.history, self.config.get_filter("py"), show_rejected=True)
        self.assertEqual(len(result["passed"]), 1)
        self.assertEqual(result["passed"][0]["letter"], "backend")
        self.assertIn("стоп-слово", result["rejected"][0]["reason"])
        self.assertEqual(client.applications, [])


if __name__ == "__main__":
    unittest.main()
