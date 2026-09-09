"""Тесты конфигурации."""

import json
import tempfile
import unittest
from pathlib import Path

from hhhelper.config import AppConfig, ConfigError, load_config


BASE = {
    "settings": {"resume_id": "r1", "delay_seconds": [1, 2], "max_per_run": 5},
    "letters": [{"name": "a", "text": "Привет", "default": True}],
    "filters": [{"name": "f", "search": {"text": "python"}, "letter": "a"}],
}


class TestConfig(unittest.TestCase):
    def test_loads_basic_structure(self):
        config = AppConfig.from_dict(BASE)
        self.assertEqual(config.settings.resume_id, "r1")
        self.assertEqual(config.settings.delay_min, 1)
        self.assertEqual(config.get_filter("f").search["text"], "python")
        self.assertTrue(config.get_letter("a").default)

    def test_unknown_search_param_rejected(self):
        data = {"filters": [{"name": "f", "search": {"whatever": 1}}]}
        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_dict(data)
        self.assertIn("whatever", str(ctx.exception))

    def test_unknown_rule_rejected(self):
        data = {"filters": [{"name": "f", "rules": {"salary_minimum": 1}}]}
        with self.assertRaises(ConfigError):
            AppConfig.from_dict(data)

    def test_filter_referencing_missing_letter(self):
        data = {"filters": [{"name": "f", "letter": "нет"}]}
        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_dict(data)
        self.assertIn("несуществующее письмо", str(ctx.exception))

    def test_letter_referencing_missing_filter(self):
        data = {"letters": [{"name": "l", "text": "t", "match": {"filters": ["нет"]}}]}
        with self.assertRaises(ConfigError):
            AppConfig.from_dict(data)

    def test_duplicate_names_rejected(self):
        data = {"letters": [{"name": "l", "text": "1"}, {"name": "l", "text": "2"}]}
        with self.assertRaises(ConfigError):
            AppConfig.from_dict(data)

    def test_letter_requires_text(self):
        with self.assertRaises(ConfigError):
            AppConfig.from_dict({"letters": [{"name": "l"}]})

    def test_letter_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "letter.txt"
            path.write_text("Из файла", encoding="utf-8")
            config = AppConfig.from_dict({"letters": [{"name": "l", "file": str(path)}]})
            self.assertEqual(config.get_letter("l").text, "Из файла")

    def test_round_trip_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = AppConfig.from_dict(BASE)
            config.save(path)
            loaded = load_config(str(path))
            self.assertEqual(loaded.get_filter("f").letter, "a")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["settings"]["max_per_run"], 5)

    def test_yaml_save_keeps_letters_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            config = AppConfig.from_dict(
                {"letters": [{"name": "l", "text": "Здравствуйте!\n\nОткликаюсь на «{vacancy_name}»."}]}
            )
            config.save(path)
            text = path.read_text(encoding="utf-8")
            self.assertIn("text: |", text)          # многострочный блок, а не кавычки
            self.assertIn("resume_id: ''", text)    # подсказка заполнить резюме
            self.assertEqual(load_config(str(path)).get_letter("l").text.count("\n"), 2)

    def test_missing_config_message(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config("/nonexistent/config.yaml")
        self.assertIn("не найден", str(ctx.exception))

    def test_needs_description_flag(self):
        config = AppConfig.from_dict(
            {"filters": [{"name": "f", "rules": {"text_exclude": ["php"]}}, {"name": "g"}]}
        )
        self.assertTrue(config.get_filter("f").rules.needs_description)
        self.assertFalse(config.get_filter("g").rules.needs_description)


if __name__ == "__main__":
    unittest.main()
