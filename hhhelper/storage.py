"""История откликов: SQLite-база рядом с конфигом.

Нужна, чтобы:
    * не откликаться дважды на одну вакансию;
    * соблюдать дневной лимит откликов;
    * показывать статистику и историю.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import ApplicationResult, Vacancy

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    vacancy_id    TEXT PRIMARY KEY,
    vacancy_name  TEXT NOT NULL DEFAULT '',
    employer_id   TEXT,
    employer_name TEXT NOT NULL DEFAULT '',
    area          TEXT NOT NULL DEFAULT '',
    url           TEXT NOT NULL DEFAULT '',
    salary        TEXT NOT NULL DEFAULT '',
    filter_name   TEXT NOT NULL DEFAULT '',
    letter_name   TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'applied',
    reason        TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_applications_created ON applications (created_at);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications (status);

CREATE TABLE IF NOT EXISTS seen (
    vacancy_id  TEXT PRIMARY KEY,
    reason      TEXT NOT NULL DEFAULT '',
    filter_name TEXT NOT NULL DEFAULT '',
    seen_at     TEXT NOT NULL
);
"""

#: Статусы, которые считаются реально отправленным откликом.
APPLIED_STATUSES = ("applied",)


class History:
    """Обёртка над SQLite с историей откликов."""

    def __init__(self, path: Path | str = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self.path = str(Path(self.path).expanduser())
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # -- запись -------------------------------------------------------------

    def record(
        self,
        vacancy: Vacancy,
        *,
        status: str,
        filter_name: str = "",
        letter_name: str = "",
        reason: str = "",
        when: Optional[_dt.datetime] = None,
    ) -> None:
        """Сохраняет отклик (или его неудачную попытку)."""
        timestamp = (when or _dt.datetime.now()).isoformat(timespec="seconds")
        self.conn.execute(
            """
            INSERT INTO applications (vacancy_id, vacancy_name, employer_id, employer_name, area,
                                      url, salary, filter_name, letter_name, status, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(vacancy_id) DO UPDATE SET
                status      = excluded.status,
                reason      = excluded.reason,
                letter_name = excluded.letter_name,
                filter_name = excluded.filter_name,
                created_at  = excluded.created_at
            WHERE applications.status <> 'applied' OR excluded.status = 'applied'
            """,
            (
                vacancy.id,
                vacancy.name,
                vacancy.employer_id,
                vacancy.employer_name,
                vacancy.area_name,
                vacancy.url,
                str(vacancy.salary) if vacancy.salary else "",
                filter_name,
                letter_name,
                status,
                reason,
                timestamp,
            ),
        )
        self.conn.commit()

    def record_result(self, result: ApplicationResult) -> None:
        self.record(
            result.vacancy,
            status=result.status,
            filter_name=result.filter_name,
            letter_name=result.letter_name,
            reason=result.reason,
        )

    def mark_seen(self, vacancy: Vacancy, reason: str = "", filter_name: str = "") -> None:
        """Запоминает отсеянную вакансию, чтобы не разбирать её повторно."""
        self.conn.execute(
            """
            INSERT INTO seen (vacancy_id, reason, filter_name, seen_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(vacancy_id) DO UPDATE SET reason = excluded.reason, seen_at = excluded.seen_at
            """,
            (vacancy.id, reason, filter_name, _dt.datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    # -- чтение -------------------------------------------------------------

    def has_applied(self, vacancy_id: str) -> bool:
        """Был ли успешный отклик на вакансию."""
        row = self.conn.execute(
            "SELECT 1 FROM applications WHERE vacancy_id = ? AND status IN (%s)"
            % ",".join("?" * len(APPLIED_STATUSES)),
            (str(vacancy_id), *APPLIED_STATUSES),
        ).fetchone()
        return row is not None

    def applied_ids(self) -> List[str]:
        rows = self.conn.execute(
            "SELECT vacancy_id FROM applications WHERE status = 'applied'"
        ).fetchall()
        return [row["vacancy_id"] for row in rows]

    def count_applied_since(self, since: _dt.datetime) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM applications WHERE status = 'applied' AND created_at >= ?",
            (since.isoformat(timespec="seconds"),),
        ).fetchone()
        return int(row["n"])

    def count_applied_today(self, now: Optional[_dt.datetime] = None) -> int:
        now = now or _dt.datetime.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return self.count_applied_since(start)

    def recent(self, limit: int = 20, status: Optional[str] = None) -> List[sqlite3.Row]:
        if status:
            query = "SELECT * FROM applications WHERE status = ? ORDER BY created_at DESC LIMIT ?"
            return list(self.conn.execute(query, (status, limit)).fetchall())
        query = "SELECT * FROM applications ORDER BY created_at DESC LIMIT ?"
        return list(self.conn.execute(query, (limit,)).fetchall())

    def stats(self) -> Dict[str, Any]:
        """Сводка: всего, по статусам, по письмам, по фильтрам, за сегодня и неделю."""
        total = self.conn.execute("SELECT COUNT(*) AS n FROM applications").fetchone()["n"]
        by_status = {
            row["status"]: row["n"]
            for row in self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM applications GROUP BY status ORDER BY n DESC"
            )
        }
        by_letter = {
            row["letter_name"] or "—": row["n"]
            for row in self.conn.execute(
                "SELECT letter_name, COUNT(*) AS n FROM applications "
                "WHERE status = 'applied' GROUP BY letter_name ORDER BY n DESC"
            )
        }
        by_filter = {
            row["filter_name"] or "—": row["n"]
            for row in self.conn.execute(
                "SELECT filter_name, COUNT(*) AS n FROM applications "
                "WHERE status = 'applied' GROUP BY filter_name ORDER BY n DESC"
            )
        }
        now = _dt.datetime.now()
        return {
            "total": int(total),
            "applied": int(by_status.get("applied", 0)),
            "by_status": by_status,
            "by_letter": by_letter,
            "by_filter": by_filter,
            "today": self.count_applied_today(now),
            "week": self.count_applied_since(now - _dt.timedelta(days=7)),
        }

    def import_ids(self, vacancy_ids: Iterable[str], status: str = "applied") -> int:
        """Помечает вакансии как уже отправленные (например, импорт из hh.ru)."""
        added = 0
        now = _dt.datetime.now().isoformat(timespec="seconds")
        for vacancy_id in vacancy_ids:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO applications (vacancy_id, status, reason, created_at) "
                "VALUES (?, ?, 'импорт с hh.ru', ?)",
                (str(vacancy_id), status, now),
            )
            added += cursor.rowcount or 0
        self.conn.commit()
        return added

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "History":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
