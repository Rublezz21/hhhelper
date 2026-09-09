"""Тесты подбора и рендеринга сопроводительных писем."""

import unittest

from hhhelper.config import AppConfig
from hhhelper.letters import MAX_LETTER_LENGTH, LetterBook, render
from hhhelper.models import Vacancy

from .fakes import make_vacancy

CONFIG = AppConfig.from_dict(
    {
        "settings": {"candidate_name": "Иван Иванов", "email": "ivan@example.com"},
        "letters": [
            {
                "name": "backend",
                "text": "Здравствуйте! Вакансия «{vacancy_name}» в {employer}. {candidate_name}",
                "match": {"keywords": ["python", "django"]},
            },
            {"name": "data", "text": "Аналитика: {vacancy_name}", "match": {"filters": ["analytics"]}},
            {"name": "generic", "text": "Универсальное письмо", "default": True},
        ],
        "filters": [
            {"name": "analytics", "search": {"text": "аналитик"}},
            {"name": "py", "search": {"text": "python"}, "letter": "backend"},
        ],
    }
)


class TestLetterBook(unittest.TestCase):
    def setUp(self):
        self.book = LetterBook(CONFIG)

    def test_filter_letter_wins(self):
        vacancy = Vacancy.parse(make_vacancy("1", name="Аналитик"))
        choice = self.book.choose(vacancy, CONFIG.get_filter("py"))
        self.assertEqual(choice.name, "backend")

    def test_letter_bound_to_filter(self):
        vacancy = Vacancy.parse(make_vacancy("1", name="Аналитик данных"))
        choice = self.book.choose(vacancy, CONFIG.get_filter("analytics"))
        self.assertEqual(choice.name, "data")

    def test_keyword_match(self):
        vacancy = Vacancy.parse(make_vacancy("1", name="Django-разработчик"))
        choice = self.book.choose(vacancy)
        self.assertEqual(choice.name, "backend")
        self.assertIn("ключев", choice.reason)

    def test_falls_back_to_default(self):
        vacancy = Vacancy.parse(make_vacancy("1", name="Дизайнер интерфейсов"))
        raw = vacancy.raw
        raw["snippet"] = {}
        choice = self.book.choose(Vacancy.parse(raw))
        self.assertEqual(choice.name, "generic")

    def test_override(self):
        vacancy = Vacancy.parse(make_vacancy("1"))
        self.assertEqual(self.book.choose(vacancy, override="data").name, "data")

    def test_no_letters_configured(self):
        empty = AppConfig.from_dict({"filters": []})
        self.assertIsNone(LetterBook(empty).choose(Vacancy.parse(make_vacancy("1"))).letter)

    def test_disabled_letter_is_ignored(self):
        config = AppConfig.from_dict(
            {"letters": [{"name": "off", "text": "x", "enabled": False}, {"name": "on", "text": "y"}]}
        )
        self.assertEqual(LetterBook(config).names, ["on"])


class TestRender(unittest.TestCase):
    def test_placeholders_substituted(self):
        vacancy = Vacancy.parse(make_vacancy("1", name="Python-разработчик", employer="ООО Ромашка"))
        text = render(CONFIG.get_letter("backend"), vacancy, CONFIG, "py")
        self.assertIn("«Python-разработчик»", text)
        self.assertIn("ООО Ромашка", text)
        self.assertIn("Иван Иванов", text)

    def test_unknown_placeholder_kept(self):
        config = AppConfig.from_dict({"letters": [{"name": "l", "text": "Привет {неизвестно} {vacancy_id}"}]})
        text = render(config.get_letter("l"), Vacancy.parse(make_vacancy("42")), config)
        self.assertIn("{неизвестно}", text)
        self.assertIn("42", text)

    def test_long_letter_trimmed(self):
        config = AppConfig.from_dict({"letters": [{"name": "l", "text": "я" * (MAX_LETTER_LENGTH + 500)}]})
        text = render(config.get_letter("l"), Vacancy.parse(make_vacancy("1")), config)
        self.assertLessEqual(len(text), MAX_LETTER_LENGTH)

    def test_validate_warns_about_length_and_unknown(self):
        config = AppConfig.from_dict(
            {
                "letters": [
                    {"name": "a", "text": "{опечатка}"},
                    {"name": "b", "text": "я" * (MAX_LETTER_LENGTH + 1)},
                ]
            }
        )
        warnings = " ".join(LetterBook(config).validate())
        self.assertIn("опечатка", warnings)
        self.assertIn("длиннее", warnings)
        self.assertIn("default", warnings)


if __name__ == "__main__":
    unittest.main()
