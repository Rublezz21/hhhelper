"""Модели данных: вакансия, зарплата, резюме, результат отклика."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Курсы нужны только для сравнения зарплат в разных валютах с порогом фильтра.
# Значения приблизительные и настраиваются в конфиге (`currency_rates`).
DEFAULT_CURRENCY_RATES: Dict[str, float] = {
    "RUR": 1.0,
    "RUB": 1.0,
    "USD": 90.0,
    "EUR": 100.0,
    "KZT": 0.2,
    "BYR": 28.0,
    "BYN": 28.0,
    "UAH": 2.3,
    "KGS": 1.0,
    "UZS": 0.0073,
    "AZN": 53.0,
    "GEL": 33.0,
}


@dataclass(frozen=True)
class Salary:
    """Зарплатная вилка вакансии."""

    from_: Optional[int] = None
    to: Optional[int] = None
    currency: str = "RUR"
    gross: Optional[bool] = None

    @classmethod
    def parse(cls, raw: Optional[Dict[str, Any]]) -> Optional["Salary"]:
        if not raw:
            return None
        return cls(
            from_=raw.get("from"),
            to=raw.get("to"),
            currency=(raw.get("currency") or "RUR").upper(),
            gross=raw.get("gross"),
        )

    @property
    def is_empty(self) -> bool:
        return self.from_ is None and self.to is None

    def amount(self, prefer: str = "from") -> Optional[int]:
        """Число для сравнений: нижняя граница, если есть, иначе верхняя."""
        if prefer == "to":
            return self.to if self.to is not None else self.from_
        return self.from_ if self.from_ is not None else self.to

    def in_rub(self, rates: Optional[Dict[str, float]] = None, prefer: str = "from") -> Optional[float]:
        value = self.amount(prefer)
        if value is None:
            return None
        table = dict(DEFAULT_CURRENCY_RATES)
        if rates:
            table.update({k.upper(): float(v) for k, v in rates.items()})
        rate = table.get(self.currency.upper())
        if rate is None:
            # Неизвестная валюта — сравнивать некорректно, считаем «нет данных».
            return None
        return value * rate

    def __str__(self) -> str:
        if self.is_empty:
            return "з/п не указана"
        parts = []
        if self.from_ is not None:
            parts.append(f"от {self.from_:,}".replace(",", " "))
        if self.to is not None:
            parts.append(f"до {self.to:,}".replace(",", " "))
        suffix = " (до вычета налогов)" if self.gross else ""
        return f"{' '.join(parts)} {self.currency}{suffix}"


@dataclass
class Vacancy:
    """Вакансия hh.ru в удобном для фильтров виде."""

    id: str
    name: str
    employer_id: Optional[str] = None
    employer_name: str = ""
    area_name: str = ""
    salary: Optional[Salary] = None
    url: str = ""
    schedule: str = ""
    schedule_name: str = ""
    experience: str = ""
    experience_name: str = ""
    employment: str = ""
    published_at: str = ""
    has_test: bool = False
    response_letter_required: bool = False
    archived: bool = False
    already_responded: bool = False
    professional_roles: List[str] = field(default_factory=list)
    snippet_requirement: str = ""
    snippet_responsibility: str = ""
    description: str = ""
    key_skills: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Dict[str, Any]) -> "Vacancy":
        employer = raw.get("employer") or {}
        area = raw.get("area") or {}
        snippet = raw.get("snippet") or {}
        schedule = raw.get("schedule") or {}
        experience = raw.get("experience") or {}
        employment = raw.get("employment") or {}
        relations = raw.get("relations") or []
        return cls(
            id=str(raw.get("id", "")),
            name=raw.get("name") or "",
            employer_id=str(employer["id"]) if employer.get("id") is not None else None,
            employer_name=employer.get("name") or "",
            area_name=area.get("name") or "",
            salary=Salary.parse(raw.get("salary") or raw.get("salary_range")),
            url=raw.get("alternate_url") or raw.get("url") or "",
            schedule=(schedule.get("id") or ""),
            schedule_name=(schedule.get("name") or ""),
            experience=(experience.get("id") or ""),
            experience_name=(experience.get("name") or ""),
            employment=(employment.get("id") or ""),
            published_at=raw.get("published_at") or "",
            has_test=bool(raw.get("has_test")),
            response_letter_required=bool(raw.get("response_letter_required")),
            archived=bool(raw.get("archived")),
            already_responded=any("got_response" in str(r) or "got_invitation" in str(r) for r in relations),
            professional_roles=[r.get("name", "") for r in (raw.get("professional_roles") or [])],
            snippet_requirement=_strip_tags(snippet.get("requirement") or ""),
            snippet_responsibility=_strip_tags(snippet.get("responsibility") or ""),
            description=_strip_tags(raw.get("description") or ""),
            key_skills=[s.get("name", "") for s in (raw.get("key_skills") or [])],
            raw=raw,
        )

    @property
    def text_blob(self) -> str:
        """Весь текст вакансии одной строкой — по нему ищут ключевые слова."""
        parts = [
            self.name,
            self.employer_name,
            self.snippet_requirement,
            self.snippet_responsibility,
            self.description,
            " ".join(self.key_skills),
            " ".join(self.professional_roles),
        ]
        return "\n".join(p for p in parts if p)

    def short(self) -> str:
        salary = str(self.salary) if self.salary else "з/п не указана"
        return f"[{self.id}] {self.name} — {self.employer_name or 'без компании'} ({self.area_name}), {salary}"


@dataclass(frozen=True)
class Resume:
    """Резюме кандидата."""

    id: str
    title: str
    status: str = ""
    updated_at: str = ""
    url: str = ""

    @classmethod
    def parse(cls, raw: Dict[str, Any]) -> "Resume":
        status = raw.get("status") or {}
        return cls(
            id=str(raw.get("id", "")),
            title=raw.get("title") or "Без названия",
            status=status.get("name") or "",
            updated_at=raw.get("updated_at") or "",
            url=raw.get("alternate_url") or "",
        )


@dataclass
class ApplicationResult:
    """Итог попытки отклика на одну вакансию."""

    vacancy: Vacancy
    status: str  # applied | skipped | failed | dry-run
    reason: str = ""
    letter_name: str = ""
    filter_name: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("applied", "dry-run")


def _strip_tags(text: str) -> str:
    """Убирает HTML-теги и подсветку из текстов hh.ru."""
    import html
    import re

    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|li|div|ul|ol)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
