"""Тесты истории откликов."""

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from hhhelper.models import Vacancy
from hhhelper.storage import History

from .fakes import make_vacancy


class TestHistory(unittest.TestCase):
    def setUp(self):
        self.history = History()
        self.vacancy = Vacancy.parse(make_vacancy("1"))

    def tearDown(self):
        self.history.close()

    def test_record_and_lookup(self):
        self.assertFalse(self.history.has_applied("1"))
        self.history.record(self.vacancy, status="applied", filter_name="f", letter_name="l")
        self.assertTrue(self.history.has_applied("1"))
        self.assertEqual(self.history.applied_ids(), ["1"])

    def test_failed_does_not_override_applied(self):
        self.history.record(self.vacancy, status="applied")
        self.history.record(self.vacancy, status="failed", reason="403")
        self.assertTrue(self.history.has_applied("1"))

    def test_applied_overrides_failed(self):
        self.history.record(self.vacancy, status="failed", reason="сеть")
        self.assertFalse(self.history.has_applied("1"))
        self.history.record(self.vacancy, status="applied")
        self.assertTrue(self.history.has_applied("1"))

    def test_daily_counter(self):
        self.history.record(self.vacancy, status="applied")
        self.history.record(Vacancy.parse(make_vacancy("2")), status="applied")
        old = dt.datetime.now() - dt.timedelta(days=3)
        self.history.record(Vacancy.parse(make_vacancy("3")), status="applied", when=old)
        self.assertEqual(self.history.count_applied_today(), 2)
        self.assertEqual(self.history.count_applied_since(dt.datetime.now() - dt.timedelta(days=7)), 3)

    def test_stats_grouping(self):
        self.history.record(self.vacancy, status="applied", letter_name="a", filter_name="f1")
        self.history.record(Vacancy.parse(make_vacancy("2")), status="applied", letter_name="a", filter_name="f2")
        self.history.record(Vacancy.parse(make_vacancy("3")), status="failed", reason="х")
        stats = self.history.stats()
        self.assertEqual(stats["applied"], 2)
        self.assertEqual(stats["by_letter"]["a"], 2)
        self.assertEqual(set(stats["by_filter"]), {"f1", "f2"})
        self.assertEqual(stats["by_status"]["failed"], 1)

    def test_import_ids_is_idempotent(self):
        self.assertEqual(self.history.import_ids(["10", "11"]), 2)
        self.assertEqual(self.history.import_ids(["10", "12"]), 1)
        self.assertTrue(self.history.has_applied("10"))

    def test_recent_filtered_by_status(self):
        self.history.record(self.vacancy, status="applied")
        self.history.record(Vacancy.parse(make_vacancy("2")), status="failed")
        self.assertEqual(len(self.history.recent(status="applied")), 1)
        self.assertEqual(len(self.history.recent()), 2)

    def test_persisted_to_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "history.db"
            with History(path) as history:
                history.record(self.vacancy, status="applied")
            with History(path) as history:
                self.assertTrue(history.has_applied("1"))

    def test_mark_seen(self):
        self.history.mark_seen(self.vacancy, "стоп-слово", "f")
        self.history.mark_seen(self.vacancy, "другая причина", "f")
        row = self.history.conn.execute("SELECT * FROM seen").fetchone()
        self.assertEqual(row["reason"], "другая причина")


if __name__ == "__main__":
    unittest.main()
