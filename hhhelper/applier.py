"""Сценарий автоматических откликов.

Порядок работы для каждого включённого фильтра:

1. Поиск вакансий через API hh.ru по параметрам фильтра.
2. Отсев по локальным правилам (стоп-слова, зарплата, чёрные списки, история).
3. Подбор сопроводительного письма (или выбор кандидатом вручную).
4. Отправка отклика с паузой между запросами и соблюдением лимитов.
"""

from __future__ import annotations

import datetime as _dt
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence

from .api import HHApiError, HHClient
from .config import AppConfig, Letter, VacancyFilter
from .filters import FilterEngine, build_search_params
from .letters import LetterBook, LetterChoice, render
from .models import ApplicationResult, Vacancy
from .storage import History

log = logging.getLogger(__name__)

#: Коды hh.ru, после которых продолжать запуск бессмысленно.
FATAL_CODES = {"limit_exceeded", "resume_not_found", "resume_visibility", "incomplete_resume"}


@dataclass
class RunOptions:
    """Параметры одного запуска откликов."""

    filters: Optional[Sequence[str]] = None
    dry_run: bool = False
    interactive: bool = True
    limit: Optional[int] = None
    letter_override: Optional[str] = None
    resume_id: Optional[str] = None
    ignore_daily_limit: bool = False


@dataclass
class PromptDecision:
    """Решение кандидата по конкретной вакансии."""

    action: str  # apply | skip | quit | apply_all
    letter: Optional[Letter] = None


class Prompt:
    """Базовый диалог: по умолчанию соглашается на всё (неинтерактивный режим)."""

    def ask(
        self,
        vacancy: Vacancy,
        choice: LetterChoice,
        text: str,
        book: LetterBook,
    ) -> PromptDecision:
        return PromptDecision("apply", choice.letter)


class RunObserver:
    """Хуки для вывода прогресса. CLI подменяет их печатью в консоль."""

    def filter_started(self, flt: VacancyFilter) -> None: ...

    def vacancy_found(self, vacancy: Vacancy) -> None: ...

    def vacancy_rejected(self, vacancy: Vacancy, reason: str) -> None: ...

    def result(self, result: ApplicationResult) -> None: ...

    def limit_reached(self, message: str) -> None: ...

    def waiting(self, seconds: float) -> None: ...


@dataclass
class RunReport:
    """Итоги запуска."""

    results: List[ApplicationResult] = field(default_factory=list)
    scanned: int = 0
    rejected: int = 0
    stopped_reason: str = ""

    @property
    def applied(self) -> List[ApplicationResult]:
        return [r for r in self.results if r.status == "applied"]

    @property
    def dry_run(self) -> List[ApplicationResult]:
        return [r for r in self.results if r.status == "dry-run"]

    @property
    def failed(self) -> List[ApplicationResult]:
        return [r for r in self.results if r.status == "failed"]

    @property
    def skipped(self) -> List[ApplicationResult]:
        return [r for r in self.results if r.status == "skipped"]

    def summary(self) -> str:
        parts = [
            f"просмотрено {self.scanned}",
            f"отсеяно {self.rejected}",
            f"откликов {len(self.applied)}",
        ]
        if self.dry_run:
            parts.append(f"в режиме проверки {len(self.dry_run)}")
        if self.failed:
            parts.append(f"ошибок {len(self.failed)}")
        if self.skipped:
            parts.append(f"пропущено вручную {len(self.skipped)}")
        return ", ".join(parts)


class Applier:
    """Оркестратор откликов."""

    def __init__(
        self,
        config: AppConfig,
        client: HHClient,
        history: History,
        *,
        prompt: Optional[Prompt] = None,
        observer: Optional[RunObserver] = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: Optional[random.Random] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.config = config
        self.client = client
        self.history = history
        self.prompt = prompt or Prompt()
        self.observer = observer or RunObserver()
        # Внешний сигнал «остановись» — например кнопка «Стоп» в Telegram.
        self.should_stop = should_stop or (lambda: False)
        self.book = LetterBook(config)
        self._sleep = sleep
        self._rng = rng or random.Random()

    # -- запуск -------------------------------------------------------------

    def run(self, options: Optional[RunOptions] = None) -> RunReport:
        options = options or RunOptions()
        report = RunReport()
        filters = self._select_filters(options.filters)
        if not filters:
            report.stopped_reason = "нет включённых фильтров"
            return report

        resume_id = options.resume_id or self.config.settings.resume_id
        budget = self._run_budget(options)
        if budget <= 0:
            report.stopped_reason = "дневной лимит откликов уже исчерпан"
            self.observer.limit_reached(report.stopped_reason)
            return report

        interactive = options.interactive
        for flt in filters:
            if self.should_stop():
                report.stopped_reason = "остановлено кандидатом"
                return report
            if budget <= 0:
                report.stopped_reason = report.stopped_reason or "достигнут лимит откликов на запуск"
                break
            self.observer.filter_started(flt)
            filter_budget = min(budget, flt.rules.max_applications or budget)
            applied_by_filter = 0
            engine = FilterEngine(
                flt,
                currency_rates=self.config.settings.currency_rates,
                is_applied=self.history.has_applied,
            )
            for vacancy in self._search(flt):
                if self.should_stop():
                    report.stopped_reason = "остановлено кандидатом"
                    return report
                if budget <= 0 or applied_by_filter >= filter_budget:
                    break
                report.scanned += 1
                self.observer.vacancy_found(vacancy)

                vacancy, verdict = self._evaluate(vacancy, flt, engine)
                if not verdict.passed:
                    report.rejected += 1
                    self.observer.vacancy_rejected(vacancy, verdict.reason)
                    self.history.mark_seen(vacancy, verdict.reason, flt.name)
                    continue

                if not resume_id:
                    report.stopped_reason = (
                        "не указано резюме: задайте `settings.resume_id` в конфиге "
                        "или передайте --resume (список: `hhhelper resumes`)"
                    )
                    self.observer.limit_reached(report.stopped_reason)
                    return report

                choice = self.book.choose(vacancy, flt, options.letter_override)
                if choice.letter is None:
                    report.stopped_reason = "не настроено ни одного сопроводительного письма"
                    self.observer.limit_reached(report.stopped_reason)
                    return report

                text = render(choice.letter, vacancy, self.config, flt.name)
                if interactive:
                    decision = self.prompt.ask(vacancy, choice, text, self.book)
                    if decision.action == "quit":
                        report.stopped_reason = "остановлено кандидатом"
                        return report
                    if decision.action == "skip":
                        result = ApplicationResult(vacancy, "skipped", "пропущено вручную",
                                                   choice.name, flt.name)
                        report.results.append(result)
                        self.observer.result(result)
                        self.history.mark_seen(vacancy, "пропущено вручную", flt.name)
                        continue
                    if decision.action == "apply_all":
                        interactive = False
                    if decision.letter is not None and decision.letter is not choice.letter:
                        choice = LetterChoice(decision.letter, "выбрано вручную")
                        text = render(choice.letter, vacancy, self.config, flt.name)

                result = self._send(vacancy, resume_id, text, choice, flt, options.dry_run)
                report.results.append(result)
                self.observer.result(result)

                if result.status in ("applied", "dry-run"):
                    budget -= 1
                    applied_by_filter += 1
                    if not options.dry_run and budget > 0:
                        self._pause()
                if result.status == "failed" and result.reason.startswith("fatal:"):
                    report.stopped_reason = result.reason[len("fatal:"):].strip()
                    self.observer.limit_reached(report.stopped_reason)
                    return report

        if budget <= 0 and not report.stopped_reason:
            report.stopped_reason = "достигнут лимит откликов"
            self.observer.limit_reached(report.stopped_reason)
        return report

    # -- вспомогательное ----------------------------------------------------

    def _select_filters(self, names: Optional[Sequence[str]]) -> List[VacancyFilter]:
        if names:
            return [self.config.get_filter(name) for name in names]
        return self.config.enabled_filters()

    def _run_budget(self, options: RunOptions) -> int:
        """Сколько откликов ещё можно отправить с учётом всех лимитов."""
        settings = self.config.settings
        budget = options.limit if options.limit is not None else settings.max_per_run
        if not options.ignore_daily_limit and not options.dry_run and settings.max_per_day:
            left_today = settings.max_per_day - self.history.count_applied_today()
            budget = min(budget, max(0, left_today))
        return max(0, int(budget))

    def _search(self, flt: VacancyFilter) -> Iterable[Vacancy]:
        params = build_search_params(flt)
        return self.client.search_vacancies(params, pages=flt.pages)

    def _evaluate(self, vacancy: Vacancy, flt: VacancyFilter, engine: FilterEngine):
        """Проверяет вакансию, при необходимости догружая полное описание."""
        verdict = engine.matches(vacancy, include_text=False)
        if not verdict.passed:
            return vacancy, verdict
        if flt.rules.needs_description and not vacancy.description:
            try:
                vacancy = self.client.get_vacancy(vacancy.id)
            except HHApiError as exc:
                log.debug("Не удалось загрузить вакансию %s: %s", vacancy.id, exc)
        return vacancy, engine.matches(vacancy)

    def _send(
        self,
        vacancy: Vacancy,
        resume_id: str,
        text: str,
        choice: LetterChoice,
        flt: VacancyFilter,
        dry_run: bool,
    ) -> ApplicationResult:
        if dry_run:
            return ApplicationResult(vacancy, "dry-run", "проверочный запуск, отклик не отправлен",
                                     choice.name, flt.name)
        resume = flt.resume_id or resume_id
        try:
            self.client.apply(vacancy.id, resume, text)
        except HHApiError as exc:
            codes = set(exc.codes)
            if "already_applied" in codes:
                # На hh.ru отклик уже есть — фиксируем, чтобы не пробовать снова.
                result = ApplicationResult(vacancy, "applied", "отклик уже был на hh.ru",
                                           choice.name, flt.name)
                self.history.record_result(result)
                return result
            reason = str(exc)
            if codes & FATAL_CODES:
                reason = f"fatal: {reason}"
            result = ApplicationResult(vacancy, "failed", reason, choice.name, flt.name)
            self.history.record_result(result)
            return result
        result = ApplicationResult(vacancy, "applied", choice.reason, choice.name, flt.name)
        self.history.record_result(result)
        return result

    def _pause(self) -> None:
        settings = self.config.settings
        delay = self._rng.uniform(settings.delay_min, settings.delay_max)
        if delay > 0:
            self.observer.waiting(delay)
            self._sleep(delay)


def preview(
    config: AppConfig,
    client: HHClient,
    history: History,
    flt: VacancyFilter,
    *,
    limit: int = 20,
    show_rejected: bool = False,
) -> Dict[str, List]:
    """Прогон фильтра без откликов: что прошло и что отсеяно и почему."""
    engine = FilterEngine(
        flt,
        currency_rates=config.settings.currency_rates,
        is_applied=history.has_applied,
    )
    book = LetterBook(config)
    passed: List[Dict] = []
    rejected: List[Dict] = []
    for vacancy in client.search_vacancies(build_search_params(flt), pages=flt.pages):
        verdict = engine.matches(vacancy, include_text=False)
        if verdict.passed and flt.rules.needs_description and not vacancy.description:
            try:
                vacancy = client.get_vacancy(vacancy.id)
            except HHApiError:
                pass
            verdict = engine.matches(vacancy)
        if verdict.passed:
            choice = book.choose(vacancy, flt)
            passed.append({"vacancy": vacancy, "letter": choice.name, "reason": choice.reason})
        else:
            rejected.append({"vacancy": vacancy, "reason": verdict.reason})
        if len(passed) >= limit and not show_rejected:
            break
    return {"passed": passed, "rejected": rejected}
