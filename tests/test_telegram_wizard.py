"""Тесты пошаговых диалогов создания фильтра и письма."""

import unittest

from hhhelper.config import Letter, VacancyFilter
from hhhelper.telegram import wizard


def walk(kind, answers, context=None):
    """Прогоняет диалог по списку ответов и возвращает последний ответ бота."""
    state, reply = wizard.start(kind, context or {})
    for answer in answers:
        reply = wizard.advance(state, answer)
    return state, reply


class TestFilterWizard(unittest.TestCase):
    def test_full_pass(self):
        state, reply = walk(
            "filter",
            ["python-remote", "Python разработчик", "1,2", "between1And3", "remote", "250000", "1С, стажер"],
        )
        self.assertTrue(reply.done)
        data = reply.result
        self.assertEqual(data["name"], "python-remote")
        self.assertEqual(data["search"]["text"], "Python разработчик")
        self.assertEqual(data["search"]["area"], ["1", "2"])
        self.assertEqual(data["search"]["experience"], "between1And3")
        self.assertEqual(data["search"]["schedule"], "remote")
        self.assertEqual(data["rules"]["salary_min"], 250000)
        self.assertEqual(data["rules"]["title_exclude"], ["1С", "стажер"])
        # результат должен приниматься конфигом без правок
        flt = VacancyFilter.from_dict(data)
        self.assertEqual(flt.name, "python-remote")
        self.assertEqual(flt.rules.max_applications, 15)

    def test_skipping_optional_steps(self):
        state, reply = walk("filter", ["only-name", "-", "-", "-", "-", "-", "-"])
        self.assertTrue(reply.done)
        self.assertEqual(reply.result["name"], "only-name")
        self.assertEqual(reply.result["search"].get("text"), None)
        VacancyFilter.from_dict(reply.result)

    def test_buttons_are_prefixed(self):
        state, reply = walk("filter", ["f", "python", "w:113", "w:noExperience"])
        self.assertEqual(state.data["search"]["area"], ["113"])
        self.assertEqual(state.data["search"]["experience"], "noExperience")

    def test_name_is_required(self):
        state, reply = walk("filter", ["-"])
        self.assertFalse(reply.done)
        self.assertIn("нельзя", reply.text)
        self.assertEqual(state.step, 0)

    def test_bad_area_reports_error(self):
        state, reply = walk("filter", ["f", "python", "Москва"])
        self.assertIn("ID региона", reply.text)
        self.assertEqual(state.step, 2)

    def test_bad_salary_reports_error(self):
        state, reply = walk("filter", ["f", "python", "-", "-", "-", "много"])
        self.assertIn("число", reply.text)

    def test_letter_step_added_when_letters_exist(self):
        state, reply = walk(
            "filter",
            ["f", "python", "-", "-", "-", "-", "-", "backend"],
            context={"letters": ["backend", "data"]},
        )
        self.assertTrue(reply.done)
        self.assertEqual(reply.result["letter"], "backend")

    def test_cancel_at_any_step(self):
        state, reply = walk("filter", ["f", "w:cancel"])
        self.assertTrue(reply.cancelled)
        self.assertFalse(reply.done)


class TestLetterWizard(unittest.TestCase):
    def test_full_pass(self):
        text = "Здравствуйте! Откликаюсь на вакансию «{vacancy_name}» в {employer}."
        state, reply = walk("letter", ["backend", text, "python, django", "да"])
        self.assertTrue(reply.done)
        data = reply.result
        self.assertEqual(data["name"], "backend")
        self.assertEqual(data["match"]["keywords"], ["python", "django"])
        self.assertTrue(data["default"])
        letter = Letter.from_dict(data)
        self.assertTrue(letter.default)
        self.assertIn("{vacancy_name}", letter.text)

    def test_text_is_required_and_checked(self):
        state, reply = walk("letter", ["backend", "коротко"])
        self.assertIn("короткое", reply.text)
        self.assertEqual(state.step, 1)

    def test_not_default(self):
        state, reply = walk("letter", ["l", "Здравствуйте, меня заинтересовала вакансия.", "-", "нет"])
        self.assertFalse(reply.result["default"])


if __name__ == "__main__":
    unittest.main()
