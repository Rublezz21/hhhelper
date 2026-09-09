"""Тесты движка фильтрации."""

import unittest

from hhhelper.config import VacancyFilter
from hhhelper.filters import FilterEngine, build_search_params, pattern_matches
from hhhelper.models import Vacancy

from .fakes import make_vacancy


def engine(rules=None, **kwargs):
    flt = VacancyFilter.from_dict({"name": "f", "rules": rules or {}})
    return FilterEngine(flt, **kwargs)


class TestPatterns(unittest.TestCase):
    def test_substring_case_insensitive(self):
        self.assertTrue(pattern_matches("Python", "ищем python-разработчика"))

    def test_yo_normalized(self):
        self.assertTrue(pattern_matches("удалённая", "Удаленная работа"))
        self.assertTrue(pattern_matches("удаленная", "Удалённая работа"))

    def test_regex_prefix(self):
        self.assertTrue(pattern_matches(r"re:\b(lead|head)\b", "Team Lead Python"))
        self.assertFalse(pattern_matches(r"re:\b(lead|head)\b", "Leadership skills"))

    def test_broken_regex_is_not_a_match(self):
        self.assertFalse(pattern_matches("re:[unclosed", "любой текст"))


class TestSearchParams(unittest.TestCase):
    def test_lists_and_booleans(self):
        flt = VacancyFilter.from_dict(
            {"name": "f", "search": {"text": "python", "area": [1, 2], "only_with_salary": True, "period": 7}}
        )
        params = build_search_params(flt)
        self.assertEqual(params["area"], ["1", "2"])
        self.assertEqual(params["only_with_salary"], "true")
        self.assertEqual(params["period"], "7")
        self.assertEqual(params["per_page"], "50")

    def test_empty_values_dropped(self):
        flt = VacancyFilter.from_dict({"name": "f", "search": {"text": "", "area": []}})
        self.assertNotIn("text", build_search_params(flt))
        self.assertNotIn("area", build_search_params(flt))


class TestRules(unittest.TestCase):
    def test_title_exclude(self):
        eng = engine({"title_exclude": ["1С"]})
        result = eng.matches(Vacancy.parse(make_vacancy("1", name="Программист 1С")))
        self.assertFalse(result.passed)
        self.assertIn("стоп-слово", result.reason)

    def test_title_include_requires_any(self):
        eng = engine({"title_include": ["python", "backend"]})
        self.assertTrue(eng.matches(Vacancy.parse(make_vacancy("1", name="Backend-разработчик"))).passed)
        self.assertFalse(eng.matches(Vacancy.parse(make_vacancy("2", name="Frontend"))).passed)

    def test_salary_threshold(self):
        eng = engine({"salary_min": 150000})
        self.assertTrue(eng.matches(Vacancy.parse(make_vacancy("1", salary_from=200000))).passed)
        self.assertFalse(eng.matches(Vacancy.parse(make_vacancy("2", salary_from=100000))).passed)

    def test_salary_missing_passes_unless_required(self):
        vacancy = Vacancy.parse(make_vacancy("1", salary_from=None))
        self.assertTrue(engine({"salary_min": 150000}).matches(vacancy).passed)
        self.assertFalse(engine({"require_salary": True}).matches(vacancy).passed)

    def test_salary_currency_conversion(self):
        raw = make_vacancy("1", salary_from=3000)
        raw["salary"]["currency"] = "USD"
        vacancy = Vacancy.parse(raw)
        self.assertTrue(engine({"salary_min": 150000}).matches(vacancy).passed)
        self.assertFalse(engine({"salary_min": 400000}).matches(vacancy).passed)

    def test_custom_currency_rate(self):
        raw = make_vacancy("1", salary_from=1000)
        raw["salary"]["currency"] = "USD"
        vacancy = Vacancy.parse(raw)
        eng = engine({"salary_min": 150000}, currency_rates={"USD": 200})
        self.assertTrue(eng.matches(vacancy).passed)

    def test_unknown_currency_not_rejected(self):
        raw = make_vacancy("1", salary_from=1000)
        raw["salary"]["currency"] = "XXX"
        self.assertTrue(engine({"salary_min": 150000}).matches(Vacancy.parse(raw)).passed)

    def test_skip_with_test_and_archive(self):
        self.assertFalse(engine().matches(Vacancy.parse(make_vacancy("1", has_test=True))).passed)
        self.assertTrue(engine({"skip_with_test": False}).matches(Vacancy.parse(make_vacancy("1", has_test=True))).passed)
        self.assertFalse(engine().matches(Vacancy.parse(make_vacancy("2", archived=True))).passed)

    def test_letter_required_rule(self):
        vacancy = Vacancy.parse(make_vacancy("1", response_letter_required=True))
        self.assertTrue(engine().matches(vacancy).passed)
        self.assertFalse(engine({"skip_letter_required": True}).matches(vacancy).passed)

    def test_employer_black_and_white_lists(self):
        vacancy = Vacancy.parse(make_vacancy("1", employer="Кадровое агентство Икс"))
        self.assertFalse(engine({"employer_exclude": ["кадровое агентство"]}).matches(vacancy).passed)
        self.assertFalse(engine({"employer_include": ["Яндекс"]}).matches(vacancy).passed)
        self.assertFalse(engine({"employer_exclude_ids": ["100"]}).matches(vacancy).passed)

    def test_history_check(self):
        eng = engine(is_applied=lambda vacancy_id: vacancy_id == "1")
        self.assertFalse(eng.matches(Vacancy.parse(make_vacancy("1"))).passed)
        self.assertTrue(eng.matches(Vacancy.parse(make_vacancy("2"))).passed)

    def test_already_responded_on_hh(self):
        vacancy = Vacancy.parse(make_vacancy("1", relations=["got_response"]))
        self.assertFalse(engine().matches(vacancy).passed)

    def test_text_rules_use_description(self):
        raw = make_vacancy("1")
        raw["description"] = "Требуется опыт с PHP и Битрикс"
        vacancy = Vacancy.parse(raw)
        self.assertFalse(engine({"text_exclude": ["битрикс"]}).matches(vacancy).passed)
        self.assertTrue(engine({"text_exclude": ["битрикс"]}).matches(vacancy, include_text=False).passed)

    def test_split_returns_reasons(self):
        eng = engine({"title_exclude": ["1С"]})
        passed, rejected = eng.split(
            [Vacancy.parse(make_vacancy("1")), Vacancy.parse(make_vacancy("2", name="Эксперт 1С"))]
        )
        self.assertEqual([v.id for v in passed], ["1"])
        self.assertEqual(rejected[0][0].id, "2")


if __name__ == "__main__":
    unittest.main()
