"""Сопроводительные письма: хранение, подстановки и автоматический выбор.

Кандидат заводит несколько писем (например «бэкенд», «аналитика», «стажировка»),
а помощник для каждой вакансии подбирает подходящее: по привязке к фильтру,
по ключевым словам вакансии или по письму «по умолчанию».
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from .config import AppConfig, Letter, VacancyFilter
from .filters import any_matches
from .models import Vacancy

#: hh.ru не принимает слишком длинные сопроводительные письма.
MAX_LETTER_LENGTH = 5000

# \w с флагом UNICODE — чтобы ловить и кириллические опечатки вида {имя}.
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}", re.UNICODE)


@dataclass(frozen=True)
class LetterChoice:
    """Выбранное письмо и объяснение, почему именно оно."""

    letter: Optional[Letter]
    reason: str = ""
    alternatives: Sequence[Letter] = ()

    @property
    def name(self) -> str:
        return self.letter.name if self.letter else ""


def placeholders(vacancy: Vacancy, config: AppConfig, filter_name: str = "") -> Dict[str, str]:
    """Значения подстановок, доступных в тексте письма."""
    today = _dt.date.today()
    return {
        "vacancy_name": vacancy.name,
        "vacancy_id": vacancy.id,
        "vacancy_url": vacancy.url,
        "employer": vacancy.employer_name or "вашей компании",
        "employer_name": vacancy.employer_name or "вашей компании",
        "area": vacancy.area_name,
        "salary": str(vacancy.salary) if vacancy.salary else "не указана",
        "schedule": vacancy.schedule_name or vacancy.schedule,
        "experience": vacancy.experience_name or vacancy.experience,
        "key_skills": ", ".join(vacancy.key_skills),
        "candidate_name": config.settings.candidate_name,
        "email": config.settings.email,
        "filter": filter_name,
        "date": today.strftime("%d.%m.%Y"),
        "today": today.strftime("%d.%m.%Y"),
    }


def render(letter: Letter, vacancy: Vacancy, config: AppConfig, filter_name: str = "") -> str:
    """Подставляет значения в шаблон письма.

    Неизвестные плейсхолдеры остаются в тексте как есть — так опечатка
    в шаблоне заметна в предпросмотре и не роняет отправку.
    """
    values = placeholders(vacancy, config, filter_name)

    def replace(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key in values:
            return values[key]
        return match.group(0)

    text = _PLACEHOLDER_RE.sub(replace, letter.text).strip()
    if len(text) > MAX_LETTER_LENGTH:
        text = text[:MAX_LETTER_LENGTH].rstrip()
    return text


class LetterBook:
    """Набор писем кандидата и логика подбора письма под вакансию."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.letters: List[Letter] = [letter for letter in config.letters if letter.enabled]

    def __len__(self) -> int:
        return len(self.letters)

    @property
    def names(self) -> List[str]:
        return [letter.name for letter in self.letters]

    def get(self, name: str) -> Letter:
        for letter in self.letters:
            if letter.name == name:
                return letter
        return self.config.get_letter(name)  # бросит понятную ошибку со списком

    def default(self) -> Optional[Letter]:
        for letter in self.letters:
            if letter.default:
                return letter
        return self.letters[0] if self.letters else None

    def candidates(self, vacancy: Vacancy, flt: Optional[VacancyFilter] = None) -> List[Letter]:
        """Письма, подходящие вакансии, в порядке убывания релевантности."""
        by_filter: List[Letter] = []
        by_keyword: List[Letter] = []
        if flt is not None:
            for letter in self.letters:
                if flt.name in letter.filters:
                    by_filter.append(letter)
        blob = vacancy.text_blob
        for letter in self.letters:
            if letter in by_filter:
                continue
            if letter.keywords and any_matches(letter.keywords, blob):
                by_keyword.append(letter)
        return by_filter + by_keyword

    def choose(
        self,
        vacancy: Vacancy,
        flt: Optional[VacancyFilter] = None,
        override: Optional[str] = None,
    ) -> LetterChoice:
        """Подбирает письмо: явное указание → фильтр → ключевые слова → по умолчанию."""
        if override:
            return LetterChoice(self.get(override), "выбрано вручную")
        if flt is not None and flt.letter:
            return LetterChoice(self.get(flt.letter), f"письмо фильтра «{flt.name}»")
        matched = self.candidates(vacancy, flt)
        if matched:
            reason = "совпадение по ключевым словам" if not (flt and flt.name in matched[0].filters) \
                else f"письмо привязано к фильтру «{flt.name}»"
            return LetterChoice(matched[0], reason, alternatives=matched[1:])
        fallback = self.default()
        if fallback is not None:
            return LetterChoice(fallback, "письмо по умолчанию")
        return LetterChoice(None, "письма не настроены")

    def validate(self) -> List[str]:
        """Возвращает список предупреждений по письмам (длина, дубли, пустой текст)."""
        warnings: List[str] = []
        for letter in self.letters:
            if len(letter.text) > MAX_LETTER_LENGTH:
                warnings.append(
                    f"Письмо «{letter.name}» длиннее {MAX_LETTER_LENGTH} символов — hh.ru его обрежет"
                )
            unknown = {
                key
                for key in _PLACEHOLDER_RE.findall(letter.text)
                if key not in placeholders(Vacancy(id="0", name=""), self.config)
            }
            if unknown:
                warnings.append(
                    f"Письмо «{letter.name}»: неизвестные подстановки {', '.join(sorted(unknown))}"
                )
        if len(self.letters) > 1 and not any(letter.default for letter in self.letters):
            warnings.append("Ни одно письмо не помечено `default: true` — будет использовано первое")
        return warnings
