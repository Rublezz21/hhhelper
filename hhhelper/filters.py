"""Движок фильтрации вакансий.

Фильтр состоит из двух частей:

1. `search` — параметры, которые уходят прямо в поиск hh.ru (GET /vacancies).
2. `rules` — локальные правила, которые применяются к найденным вакансиям
   (стоп-слова, зарплатный порог, чёрный список компаний и т. д.).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import FilterRules, VacancyFilter
from .models import Vacancy

#: Префикс, включающий режим регулярного выражения в шаблоне.
REGEX_PREFIX = "re:"


@dataclass(frozen=True)
class MatchResult:
    """Результат проверки вакансии фильтром."""

    passed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.passed


def normalize(text: str) -> str:
    """Приводит текст к виду, удобному для поиска подстрок."""
    return (text or "").replace("ё", "е").replace("Ё", "Е").lower()


def pattern_matches(pattern: str, text: str) -> bool:
    """Проверяет шаблон: подстрока без учёта регистра либо `re:<regexp>`."""
    if not pattern:
        return False
    if pattern.startswith(REGEX_PREFIX):
        expression = pattern[len(REGEX_PREFIX):]
        try:
            return re.search(expression, text or "", re.IGNORECASE | re.MULTILINE) is not None
        except re.error:
            # Битую регулярку не считаем совпадением, но и не роняем запуск.
            return False
    return normalize(pattern) in normalize(text)


def any_matches(patterns: Sequence[str], text: str) -> Optional[str]:
    """Возвращает первый сработавший шаблон или None."""
    for pattern in patterns:
        if pattern_matches(pattern, text):
            return pattern
    return None


def build_search_params(flt: VacancyFilter) -> Dict[str, Any]:
    """Готовит query-параметры для GET /vacancies из фильтра."""
    params: Dict[str, Any] = {}
    for key, value in flt.search.items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            params[key] = "true" if value else "false"
        elif isinstance(value, (list, tuple, set)):
            values = [str(item) for item in value if item is not None and item != ""]
            if values:
                params[key] = values
        else:
            params[key] = str(value)
    params.setdefault("per_page", "50")
    return params


class FilterEngine:
    """Применяет локальные правила фильтра к вакансиям."""

    def __init__(
        self,
        flt: VacancyFilter,
        *,
        currency_rates: Optional[Dict[str, float]] = None,
        is_applied: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self.filter = flt
        self.rules: FilterRules = flt.rules
        self.currency_rates = currency_rates or {}
        self._is_applied = is_applied or (lambda vacancy_id: False)

    # -- публичный интерфейс ------------------------------------------------

    def matches(self, vacancy: Vacancy, *, include_text: bool = True) -> MatchResult:
        """Проверяет вакансию по правилам фильтра.

        `include_text=False` пропускает проверки по описанию — это дешёвый
        предварительный прогон до загрузки полного текста вакансии.
        """
        checks = [
            self._check_state,
            self._check_history,
            self._check_employer,
            self._check_title,
            self._check_salary,
        ]
        if include_text:
            checks.append(self._check_text)
        for check in checks:
            result = check(vacancy)
            if not result.passed:
                return result
        return MatchResult(True, "подходит")

    def split(self, vacancies: Iterable[Vacancy]) -> Tuple[List[Vacancy], List[Tuple[Vacancy, str]]]:
        """Делит вакансии на подходящие и отсеянные (с причиной отсева)."""
        passed: List[Vacancy] = []
        rejected: List[Tuple[Vacancy, str]] = []
        for vacancy in vacancies:
            result = self.matches(vacancy)
            if result.passed:
                passed.append(vacancy)
            else:
                rejected.append((vacancy, result.reason))
        return passed, rejected

    # -- отдельные проверки -------------------------------------------------

    def _check_state(self, vacancy: Vacancy) -> MatchResult:
        if self.rules.skip_archived and vacancy.archived:
            return MatchResult(False, "вакансия в архиве")
        if self.rules.skip_with_test and vacancy.has_test:
            return MatchResult(False, "требуется тестовое задание hh")
        if self.rules.skip_letter_required and vacancy.response_letter_required:
            return MatchResult(False, "обязательно сопроводительное письмо")
        return MatchResult(True)

    def _check_history(self, vacancy: Vacancy) -> MatchResult:
        if not self.rules.skip_already_applied:
            return MatchResult(True)
        if vacancy.already_responded:
            return MatchResult(False, "отклик уже есть на hh.ru")
        if self._is_applied(vacancy.id):
            return MatchResult(False, "отклик уже отправлен ранее")
        return MatchResult(True)

    def _check_employer(self, vacancy: Vacancy) -> MatchResult:
        employer = vacancy.employer_name or ""
        if vacancy.employer_id and str(vacancy.employer_id) in set(self.rules.employer_exclude_ids):
            return MatchResult(False, f"компания в чёрном списке (id {vacancy.employer_id})")
        hit = any_matches(self.rules.employer_exclude, employer)
        if hit:
            return MatchResult(False, f"компания в чёрном списке: «{hit}»")
        if self.rules.employer_include and not any_matches(self.rules.employer_include, employer):
            return MatchResult(False, "компания не входит в белый список")
        return MatchResult(True)

    def _check_title(self, vacancy: Vacancy) -> MatchResult:
        hit = any_matches(self.rules.title_exclude, vacancy.name)
        if hit:
            return MatchResult(False, f"стоп-слово в названии: «{hit}»")
        if self.rules.title_include and not any_matches(self.rules.title_include, vacancy.name):
            return MatchResult(False, "в названии нет ни одного нужного слова")
        return MatchResult(True)

    def _check_text(self, vacancy: Vacancy) -> MatchResult:
        if not (self.rules.text_exclude or self.rules.text_include):
            return MatchResult(True)
        blob = vacancy.text_blob
        hit = any_matches(self.rules.text_exclude, blob)
        if hit:
            return MatchResult(False, f"стоп-слово в описании: «{hit}»")
        if self.rules.text_include and not any_matches(self.rules.text_include, blob):
            return MatchResult(False, "в описании нет ни одного нужного слова")
        return MatchResult(True)

    def _check_salary(self, vacancy: Vacancy) -> MatchResult:
        rules = self.rules
        salary = vacancy.salary
        has_salary = salary is not None and not salary.is_empty
        if rules.require_salary and not has_salary:
            return MatchResult(False, "зарплата не указана")
        if not has_salary or (rules.salary_min is None and rules.salary_max is None):
            return MatchResult(True)
        assert salary is not None
        value = salary.in_rub(self.currency_rates)
        if value is None:
            # Неизвестная валюта — не отсеиваем, чтобы не терять вакансии молча.
            return MatchResult(True)
        if rules.salary_min is not None and value < rules.salary_min:
            return MatchResult(False, f"зарплата ниже порога ({salary})")
        if rules.salary_max is not None and value > rules.salary_max:
            return MatchResult(False, f"зарплата выше верхней границы ({salary})")
        return MatchResult(True)
