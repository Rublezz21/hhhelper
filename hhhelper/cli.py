"""Командный интерфейс помощника кандидата hh.ru."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import textwrap
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import __version__
from .api import HHApiError, HHClient
from .applier import Applier, Prompt, PromptDecision, RunObserver, RunOptions, preview
from .auth import (
    DEFAULT_REDIRECT_URI,
    AuthError,
    Token,
    TokenStore,
    build_authorize_url,
    exchange_code,
    wait_for_code,
)
from .config import (
    CONFIG_SEARCH_PATHS,
    AppConfig,
    ConfigError,
    FilterRules,
    Letter,
    VacancyFilter,
    config_dir,
    load_config,
)
from .letters import LetterBook, render
from .models import Vacancy
from .storage import History

log = logging.getLogger("hhhelper")

EXAMPLE_CONFIG = """\
# Конфигурация помощника кандидата hh.ru.
# Полное описание параметров — в README.md.

settings:
  # ID резюме, которым откликаемся (посмотреть: hhhelper resumes)
  resume_id: ""
  candidate_name: "Иван Иванов"
  email: "ivan@example.com"
  # Пауза между откликами, секунды [минимум, максимум]
  delay_seconds: [5, 15]
  # Сколько откликов максимум за один запуск
  max_per_run: 25
  # Дневной потолок (у hh.ru свой лимит около 200 откликов в сутки)
  max_per_day: 190
  # Спрашивать подтверждение перед каждым откликом
  interactive: true

letters:
  - name: backend
    title: "Бэкенд-разработка"
    default: true
    match:
      keywords: ["python", "django", "fastapi", "backend"]
    text: |
      Здравствуйте!

      Меня заинтересовала вакансия «{vacancy_name}» в {employer}.
      Последние несколько лет занимаюсь бэкендом на Python: сервисы на FastAPI и Django,
      PostgreSQL, очереди, покрытие тестами и CI.

      Буду рад обсудить задачи команды.

      С уважением,
      {candidate_name}

  - name: data
    title: "Аналитика и данные"
    match:
      keywords: ["аналитик", "data", "sql", "etl"]
    text: |
      Здравствуйте!

      Откликаюсь на вакансию «{vacancy_name}».
      Работал с аналитическими задачами: SQL, Python (pandas), выгрузки и дашборды.

      С уважением,
      {candidate_name}

filters:
  - name: python-remote
    description: "Удалённый Python-бэкенд от 200к"
    enabled: true
    letter: backend
    search:
      text: "Python разработчик"
      # 1 — Москва, 2 — Санкт-Петербург, 113 — вся Россия
      area: [113]
      experience: between1And3
      schedule: remote
      employment: [full]
      period: 7
      order_by: publication_time
      per_page: 50
      pages: 2
    rules:
      title_exclude: ["1С", "стажер", "re:\\\\b(lead|head)\\\\b"]
      text_exclude: ["криптовалют", "ставки"]
      employer_exclude: ["Кадровое агентство"]
      salary_min: 200000
      require_salary: false
      skip_with_test: true
      max_applications: 15
"""


# ---------------------------------------------------------------------------
# Вспомогательное: цвета и вывод
# ---------------------------------------------------------------------------


class Style:
    """Минимальная ANSI-раскраска (отключается через NO_COLOR)."""

    enabled = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    @classmethod
    def _wrap(cls, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if cls.enabled else text

    @classmethod
    def bold(cls, text: str) -> str:
        return cls._wrap("1", text)

    @classmethod
    def dim(cls, text: str) -> str:
        return cls._wrap("2", text)

    @classmethod
    def green(cls, text: str) -> str:
        return cls._wrap("32", text)

    @classmethod
    def yellow(cls, text: str) -> str:
        return cls._wrap("33", text)

    @classmethod
    def red(cls, text: str) -> str:
        return cls._wrap("31", text)

    @classmethod
    def cyan(cls, text: str) -> str:
        return cls._wrap("36", text)


def vacancy_card(vacancy: Vacancy) -> str:
    """Карточка вакансии для консоли."""
    salary = str(vacancy.salary) if vacancy.salary else "з/п не указана"
    lines = [
        Style.bold(vacancy.name),
        f"  Компания: {vacancy.employer_name or '—'}",
        f"  Город:    {vacancy.area_name or '—'}",
        f"  Зарплата: {salary}",
    ]
    details = ", ".join(x for x in (vacancy.experience_name, vacancy.schedule_name) if x)
    if details:
        lines.append(f"  Условия:  {details}")
    if vacancy.snippet_requirement:
        lines.append("  Требования: " + textwrap.shorten(vacancy.snippet_requirement, 160))
    if vacancy.url:
        lines.append(Style.dim(f"  {vacancy.url}"))
    return "\n".join(lines)


class ConsoleObserver(RunObserver):
    """Печатает ход работы в консоль."""

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose

    def filter_started(self, flt: VacancyFilter) -> None:
        title = f"Фильтр «{flt.name}»"
        if flt.description:
            title += f" — {flt.description}"
        print("\n" + Style.bold(Style.cyan(title)))

    def vacancy_rejected(self, vacancy: Vacancy, reason: str) -> None:
        if self.verbose:
            print(Style.dim(f"  – {vacancy.name} — {reason}"))

    def result(self, result) -> None:
        vacancy = result.vacancy
        label = f"{vacancy.name} — {vacancy.employer_name or '—'}"
        if result.status == "applied":
            print(Style.green(f"  ✓ Отклик отправлен: {label} [письмо: {result.letter_name}]"))
        elif result.status == "dry-run":
            print(Style.yellow(f"  · Проверка: откликнулись бы на {label} [письмо: {result.letter_name}]"))
        elif result.status == "skipped":
            print(Style.dim(f"  – Пропущено: {label}"))
        else:
            print(Style.red(f"  ✗ Не удалось: {label} — {result.reason}"))

    def limit_reached(self, message: str) -> None:
        print(Style.yellow(f"\nОстановка: {message}"))

    def waiting(self, seconds: float) -> None:
        if self.verbose:
            print(Style.dim(f"    пауза {seconds:.1f} с"))


class ConsolePrompt(Prompt):
    """Интерактивное подтверждение отклика с возможностью сменить письмо."""

    def ask(self, vacancy: Vacancy, choice, text: str, book: LetterBook) -> PromptDecision:
        print()
        print(vacancy_card(vacancy))
        print(Style.cyan(f"  Письмо: «{choice.name}» ({choice.reason})"))
        print(Style.dim(textwrap.indent(textwrap.shorten(text.replace("\n", " "), 300), "    ")))
        while True:
            answer = input(
                "  Отправить? [Enter — да, n — пропустить, l — другое письмо, "
                "p — показать письмо целиком, o — открыть вакансию, a — дальше без вопросов, q — выход]: "
            ).strip().lower()
            if answer in ("", "y", "д", "да"):
                return PromptDecision("apply", choice.letter)
            if answer in ("n", "н", "нет"):
                return PromptDecision("skip")
            if answer in ("a", "а"):
                return PromptDecision("apply_all", choice.letter)
            if answer in ("q", "й", "выход"):
                return PromptDecision("quit")
            if answer == "p":
                print(textwrap.indent(text, "    "))
                continue
            if answer == "o":
                if vacancy.url:
                    try:
                        webbrowser.open(vacancy.url)
                    except Exception:
                        print(vacancy.url)
                continue
            if answer == "l":
                letter = self._choose_letter(book)
                if letter is not None:
                    return PromptDecision("apply", letter)
                continue
            print("  Не понял ответ, попробуйте ещё раз.")

    @staticmethod
    def _choose_letter(book: LetterBook) -> Optional[Letter]:
        print("  Доступные письма:")
        for index, letter in enumerate(book.letters, start=1):
            mark = " (по умолчанию)" if letter.default else ""
            print(f"    {index}. {letter.name} — {letter.title}{mark}")
        answer = input("  Номер письма (Enter — отмена): ").strip()
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(book.letters):
            return book.letters[int(answer) - 1]
        try:
            return book.get(answer)
        except ConfigError as exc:
            print(f"  {exc}")
            return None


# ---------------------------------------------------------------------------
# Контекст выполнения
# ---------------------------------------------------------------------------


class Context:
    """Ленивая сборка конфига, клиента и истории."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self._config: Optional[AppConfig] = None
        self._history: Optional[History] = None
        self._client: Optional[HHClient] = None
        self.token_store = TokenStore()

    @property
    def config(self) -> AppConfig:
        if self._config is None:
            self._config = load_config(getattr(self.args, "config", None))
        return self._config

    @property
    def history(self) -> History:
        if self._history is None:
            path = getattr(self.args, "db", None) or self.config.settings.database_path
            self._history = History(path)
        return self._history

    @property
    def client(self) -> HHClient:
        if self._client is None:
            self._client = HHClient(
                self.token_store.access_token,
                user_agent=self.config.settings.user_agent,
            )
        return self._client


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------


def cmd_init(ctx: Context) -> int:
    target = Path(ctx.args.path or CONFIG_SEARCH_PATHS[0]).expanduser()
    if target.exists() and not ctx.args.force:
        print(f"Файл {target} уже существует. Перезаписать: --force")
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    print(f"Создан конфиг: {target}")
    print("Дальше: 1) hhhelper auth login  2) hhhelper resumes  3) впишите resume_id  4) hhhelper apply --dry-run")
    return 0


def cmd_auth_login(ctx: Context) -> int:
    args = ctx.args
    if args.token:
        ctx.token_store.save(Token(access_token=args.token.strip()))
        print(f"Токен сохранён в {ctx.token_store.path}")
        return 0

    client_id = args.client_id or os.environ.get("HH_CLIENT_ID") or input("Client ID (с dev.hh.ru): ").strip()
    client_secret = (
        args.client_secret or os.environ.get("HH_CLIENT_SECRET") or input("Client Secret: ").strip()
    )
    redirect_uri = args.redirect_uri or os.environ.get("HH_REDIRECT_URI") or DEFAULT_REDIRECT_URI
    if not client_id or not client_secret:
        print("Нужны Client ID и Client Secret приложения (создаётся на https://dev.hh.ru).")
        return 1

    if args.manual:
        print("Откройте ссылку, подтвердите доступ и вставьте сюда параметр `code` из адресной строки:")
        print("  " + build_authorize_url(client_id, redirect_uri))
        code = input("code: ").strip()
    else:
        code = wait_for_code(client_id, redirect_uri, open_browser=not args.no_browser)

    token = exchange_code(code, client_id, client_secret, redirect_uri)
    ctx.token_store.save(token)
    print(Style.green(f"Авторизация прошла успешно, токен сохранён в {ctx.token_store.path}"))
    return 0


def cmd_auth_status(ctx: Context) -> int:
    token = ctx.token_store.load()
    if token is None:
        print("Токена нет. Выполните `hhhelper auth login`.")
        return 1
    expires = token.expires_in
    when = f"истекает через {expires // 3600} ч {expires % 3600 // 60} мин" if expires else "без срока"
    print(f"Токен найден ({when}), файл: {ctx.token_store.path}")
    try:
        me = ctx.client.me()
    except (AuthError, HHApiError) as exc:
        print(Style.red(f"Токен не работает: {exc}"))
        return 1
    name = " ".join(x for x in (me.get("first_name"), me.get("last_name")) if x)
    print(Style.green(f"hh.ru отвечает: {name or me.get('email') or 'пользователь опознан'}"))
    return 0


def cmd_auth_logout(ctx: Context) -> int:
    ctx.token_store.clear()
    print("Токен удалён.")
    return 0


def cmd_resumes(ctx: Context) -> int:
    resumes = ctx.client.resumes()
    if not resumes:
        print("Резюме не найдены. Создайте резюме на hh.ru.")
        return 1
    print(Style.bold("Ваши резюме:"))
    for resume in resumes:
        print(f"  {Style.cyan(resume.id)}  {resume.title}  {Style.dim(resume.status)}")
    print(Style.dim("\nВпишите нужный id в settings.resume_id вашего конфига."))
    return 0


def cmd_letters(ctx: Context) -> int:
    book = LetterBook(ctx.config)
    if not book.letters:
        print("Письма не настроены. Добавьте их в раздел `letters` конфига или `hhhelper letters add`.")
        return 1
    print(Style.bold("Сопроводительные письма:"))
    for letter in book.letters:
        marks = []
        if letter.default:
            marks.append("по умолчанию")
        if letter.keywords:
            marks.append("ключевые слова: " + ", ".join(letter.keywords))
        if letter.filters:
            marks.append("фильтры: " + ", ".join(letter.filters))
        suffix = Style.dim("  (" + "; ".join(marks) + ")") if marks else ""
        print(f"  {Style.cyan(letter.name)} — {letter.title}{suffix}")
    for warning in book.validate():
        print(Style.yellow(f"  ! {warning}"))
    return 0


def cmd_letter_show(ctx: Context) -> int:
    letter = ctx.config.get_letter(ctx.args.name)
    print(Style.bold(f"{letter.name} — {letter.title}"))
    print()
    print(letter.text)
    return 0


def cmd_letter_add(ctx: Context) -> int:
    args = ctx.args
    config = ctx.config
    text = args.text
    if args.file:
        text = Path(args.file).expanduser().read_text(encoding="utf-8")
    if not text:
        print("Введите текст письма. Пустая строка на новой строке завершает ввод.")
        lines: List[str] = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line == "" and lines:
                break
            lines.append(line)
        text = "\n".join(lines)
    if not text.strip():
        print("Пустое письмо не сохранено.")
        return 1
    if any(letter.name == args.name for letter in config.letters):
        print(f"Письмо «{args.name}» уже существует. Удалите его (`hhhelper letters remove`) или выберите другое имя.")
        return 1
    config.letters.append(
        Letter(
            name=args.name,
            text=text.strip(),
            title=args.title or args.name,
            keywords=args.keyword or [],
            filters=args.for_filter or [],
            default=args.default,
        )
    )
    config.validate()
    path = config.save()
    print(Style.green(f"Письмо «{args.name}» добавлено в {path}"))
    return 0


def cmd_letter_remove(ctx: Context) -> int:
    config = ctx.config
    letter = config.get_letter(ctx.args.name)
    config.letters.remove(letter)
    for flt in config.filters:
        if flt.letter == letter.name:
            flt.letter = None
    config.validate()
    path = config.save()
    print(f"Письмо «{letter.name}» удалено из {path}")
    return 0


def cmd_filters(ctx: Context) -> int:
    config = ctx.config
    if not config.filters:
        print("Фильтры не настроены. Создайте: `hhhelper filters add` (мастер) или правкой конфига.")
        return 1
    print(Style.bold("Фильтры вакансий:"))
    for flt in config.filters:
        state = Style.green("вкл") if flt.enabled else Style.dim("выкл")
        query = flt.search.get("text") or "—"
        letter = f", письмо: {flt.letter}" if flt.letter else ""
        print(f"  [{state}] {Style.cyan(flt.name)} — запрос: {query}{letter}")
        if flt.description:
            print(Style.dim(f"        {flt.description}"))
    return 0


def cmd_filter_show(ctx: Context) -> int:
    flt = ctx.config.get_filter(ctx.args.name)
    print(Style.bold(f"Фильтр «{flt.name}»"))
    if flt.description:
        print(flt.description)
    print(json.dumps(flt.to_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_filter_add(ctx: Context) -> int:
    args = ctx.args
    config = ctx.config
    data = _filter_data_from_args(args) if args.name else _filter_wizard(config)
    if data is None:
        return 1
    if any(flt.name == data["name"] for flt in config.filters):
        print(f"Фильтр «{data['name']}» уже существует.")
        return 1
    flt = VacancyFilter.from_dict(data)
    config.filters.append(flt)
    config.validate()
    path = config.save()
    print(Style.green(f"Фильтр «{flt.name}» добавлен в {path}"))
    print(Style.dim("Проверить, что он находит: hhhelper search --filter " + flt.name))
    return 0


def cmd_filter_remove(ctx: Context) -> int:
    config = ctx.config
    flt = config.get_filter(ctx.args.name)
    config.filters.remove(flt)
    for letter in config.letters:
        if flt.name in letter.filters:
            letter.filters.remove(flt.name)
    config.validate()
    path = config.save()
    print(f"Фильтр «{flt.name}» удалён из {path}")
    return 0


def cmd_filter_toggle(ctx: Context, enabled: bool) -> int:
    config = ctx.config
    flt = config.get_filter(ctx.args.name)
    flt.enabled = enabled
    config.save()
    print(f"Фильтр «{flt.name}» {'включён' if enabled else 'выключен'}")
    return 0


def cmd_search(ctx: Context) -> int:
    config = ctx.config
    filters = [config.get_filter(name) for name in ctx.args.filter] if ctx.args.filter else config.enabled_filters()
    if not filters:
        print("Нет фильтров для поиска.")
        return 1
    total = 0
    for flt in filters:
        print("\n" + Style.bold(Style.cyan(f"Фильтр «{flt.name}»")))
        result = preview(
            config, ctx.client, ctx.history, flt,
            limit=ctx.args.limit, show_rejected=ctx.args.show_rejected,
        )
        for item in result["passed"]:
            vacancy = item["vacancy"]
            print(f"  {Style.green('+')} {vacancy.short()}  {Style.dim('письмо: ' + item['letter'])}")
            total += 1
        if ctx.args.show_rejected:
            for item in result["rejected"]:
                print(Style.dim(f"  - {item['vacancy'].short()} — {item['reason']}"))
        if not result["passed"]:
            print(Style.yellow("  Подходящих вакансий не найдено"))
    print(f"\nИтого подходящих: {total}")
    return 0


def cmd_apply(ctx: Context) -> int:
    config = ctx.config
    args = ctx.args
    interactive = config.settings.interactive
    if args.yes:
        interactive = False
    if args.interactive:
        interactive = True
    options = RunOptions(
        filters=args.filter or None,
        dry_run=args.dry_run,
        interactive=interactive and sys.stdin.isatty(),
        limit=args.limit,
        letter_override=args.letter,
        resume_id=args.resume,
        ignore_daily_limit=args.ignore_daily_limit,
    )
    if args.dry_run:
        print(Style.yellow("Проверочный запуск: отклики отправляться не будут."))
    applier = Applier(
        config,
        ctx.client,
        ctx.history,
        prompt=ConsolePrompt(),
        observer=ConsoleObserver(verbose=args.verbose),
    )
    report = applier.run(options)
    print("\n" + Style.bold("Итог: ") + report.summary())
    if report.stopped_reason:
        print(Style.dim(f"Причина остановки: {report.stopped_reason}"))
    if not args.dry_run and report.applied:
        left = config.settings.max_per_day - ctx.history.count_applied_today()
        print(Style.dim(f"Сегодня отправлено {ctx.history.count_applied_today()}, осталось по лимиту: {max(0, left)}"))
    return 0


def cmd_stats(ctx: Context) -> int:
    stats = ctx.history.stats()
    print(Style.bold("Статистика откликов"))
    print(f"  Всего записей:  {stats['total']}")
    print(f"  Откликов:       {stats['applied']}")
    print(f"  Сегодня:        {stats['today']}")
    print(f"  За 7 дней:      {stats['week']}")
    if stats["by_letter"]:
        print("  По письмам:")
        for name, count in stats["by_letter"].items():
            print(f"    {name}: {count}")
    if stats["by_filter"]:
        print("  По фильтрам:")
        for name, count in stats["by_filter"].items():
            print(f"    {name}: {count}")
    if stats["by_status"]:
        print("  По статусам:")
        for name, count in stats["by_status"].items():
            print(f"    {name}: {count}")
    return 0


def cmd_history(ctx: Context) -> int:
    rows = ctx.history.recent(limit=ctx.args.limit, status=ctx.args.status)
    if not rows:
        print("История пуста.")
        return 0
    for row in rows:
        when = row["created_at"].replace("T", " ")
        status = row["status"]
        painted = {
            "applied": Style.green("отклик"),
            "failed": Style.red("ошибка"),
            "skipped": Style.dim("пропуск"),
            "dry-run": Style.yellow("проверка"),
        }.get(status, status)
        print(f"{Style.dim(when)}  {painted}  {row['vacancy_name']} — {row['employer_name']}"
              f"{Style.dim('  [' + (row['letter_name'] or '—') + ']')}")
        if row["reason"] and status == "failed":
            print(Style.dim(f"       {row['reason']}"))
    return 0


def cmd_sync(ctx: Context) -> int:
    """Импортирует отклики с hh.ru, чтобы не дублировать их локально."""
    ids = ctx.client.negotiations_vacancy_ids()
    added = ctx.history.import_ids(ids)
    print(f"Найдено откликов на hh.ru: {len(ids)}. Добавлено в локальную историю: {added}.")
    return 0


# ---------------------------------------------------------------------------
# Мастер создания фильтра
# ---------------------------------------------------------------------------


def _filter_data_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    search: Dict[str, Any] = {}
    for key, value in (
        ("text", args.text),
        ("area", args.area),
        ("experience", args.experience),
        ("schedule", args.schedule),
        ("employment", args.employment),
        ("professional_role", args.professional_role),
        ("period", args.period),
        ("salary", args.salary),
        ("order_by", args.order_by),
        ("per_page", args.per_page),
        ("only_with_salary", args.only_with_salary or None),
    ):
        if value:
            search[key] = value
    rules: Dict[str, Any] = {}
    for key, value in (
        ("title_include", args.include_title),
        ("title_exclude", args.exclude_title),
        ("text_include", args.include_text),
        ("text_exclude", args.exclude_text),
        ("employer_exclude", args.exclude_employer),
        ("salary_min", args.salary_min),
        ("salary_max", args.salary_max),
        ("max_applications", args.max_applications),
    ):
        if value:
            rules[key] = value
    if args.require_salary:
        rules["require_salary"] = True
    if args.allow_tests:
        rules["skip_with_test"] = False
    data: Dict[str, Any] = {"name": args.name, "search": search, "rules": rules, "pages": args.pages}
    if args.description:
        data["description"] = args.description
    if args.letter:
        data["letter"] = args.letter
    return data


def _filter_wizard(config: AppConfig) -> Optional[Dict[str, Any]]:
    """Пошаговое создание фильтра в диалоге."""
    print(Style.bold("Создание фильтра. Пустой ответ — пропустить пункт.\n"))

    def ask(question: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        answer = input(f"  {question}{suffix}: ").strip()
        return answer or default

    def ask_list(question: str) -> List[str]:
        answer = ask(question + " (через запятую)")
        return [part.strip() for part in answer.split(",") if part.strip()]

    name = ask("Название фильтра (латиницей, например python-remote)")
    if not name:
        print("Без имени фильтр не создать.")
        return None
    search: Dict[str, Any] = {}
    description = ask("Описание (для себя)")
    text = ask("Поисковый запрос, как в строке поиска hh.ru")
    if text:
        search["text"] = text
    areas = ask_list("ID регионов (1 — Москва, 2 — СПб, 113 — Россия)")
    if areas:
        search["area"] = areas
    experience = ask(
        "Опыт: noExperience / between1And3 / between3And6 / moreThan6"
    )
    if experience:
        search["experience"] = experience
    schedule = ask("График: remote / fullDay / flexible / shift")
    if schedule:
        search["schedule"] = schedule
    period = ask("За сколько последних дней искать", "7")
    if period:
        search["period"] = period
    pages = ask("Сколько страниц выдачи забирать (по 50 вакансий)", "2")

    rules: Dict[str, Any] = {}
    salary_min = ask("Минимальная зарплата, ₽ (пусто — без порога)")
    if salary_min:
        rules["salary_min"] = int(salary_min)
    exclude_title = ask_list("Стоп-слова в названии (например: 1С, стажер, senior)")
    if exclude_title:
        rules["title_exclude"] = exclude_title
    exclude_text = ask_list("Стоп-слова в описании")
    if exclude_text:
        rules["text_exclude"] = exclude_text
    exclude_employer = ask_list("Компании, которым не откликаться")
    if exclude_employer:
        rules["employer_exclude"] = exclude_employer
    max_applications = ask("Максимум откликов по этому фильтру за запуск", "15")
    if max_applications:
        rules["max_applications"] = int(max_applications)

    letter = ""
    if config.letters:
        print("  Письма: " + ", ".join(letter.name for letter in config.letters))
        letter = ask("Каким письмом откликаться (пусто — подберётся автоматически)")

    data: Dict[str, Any] = {"name": name, "search": search, "rules": rules, "pages": int(pages or 1)}
    if description:
        data["description"] = description
    if letter:
        data["letter"] = letter
    print()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    if ask("Сохранить фильтр? (y/n)", "y").lower() not in ("y", "д", "да", ""):
        print("Отменено.")
        return None
    return data


# ---------------------------------------------------------------------------
# Разбор аргументов
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hhhelper",
        description="Помощник кандидата hh.ru: автоматические отклики по фильтрам "
                    "с несколькими сопроводительными письмами.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Быстрый старт:
              hhhelper init                 создать конфиг
              hhhelper auth login           авторизоваться на hh.ru
              hhhelper resumes              узнать id резюме
              hhhelper filters add          создать фильтр (мастер)
              hhhelper search               посмотреть, что находится
              hhhelper apply --dry-run      прогон без отправки
              hhhelper apply                отклики с подтверждением
            """
        ),
    )
    parser.add_argument("--config", help="путь к файлу конфигурации")
    parser.add_argument("--db", help="путь к базе истории откликов")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный вывод")
    parser.add_argument("--version", action="version", version=f"hhhelper {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="создать файл конфигурации с примером")
    p_init.add_argument("--path", help="куда сохранить конфиг")
    p_init.add_argument("--force", action="store_true", help="перезаписать существующий файл")
    p_init.set_defaults(func=cmd_init)

    p_auth = sub.add_parser("auth", help="авторизация на hh.ru")
    auth_sub = p_auth.add_subparsers(dest="auth_command")
    p_login = auth_sub.add_parser("login", help="получить и сохранить токен")
    p_login.add_argument("--client-id", help="Client ID приложения с dev.hh.ru")
    p_login.add_argument("--client-secret", help="Client Secret приложения")
    p_login.add_argument("--redirect-uri", help=f"redirect_uri приложения (по умолчанию {DEFAULT_REDIRECT_URI})")
    p_login.add_argument("--token", help="сохранить готовый access_token без OAuth")
    p_login.add_argument("--manual", action="store_true", help="без локального сервера: вставить code вручную")
    p_login.add_argument("--no-browser", action="store_true", help="не открывать браузер автоматически")
    p_login.set_defaults(func=cmd_auth_login)
    auth_sub.add_parser("status", help="проверить токен").set_defaults(func=cmd_auth_status)
    auth_sub.add_parser("logout", help="удалить сохранённый токен").set_defaults(func=cmd_auth_logout)
    p_auth.set_defaults(func=cmd_auth_status)

    sub.add_parser("resumes", help="показать резюме и их id").set_defaults(func=cmd_resumes)

    p_letters = sub.add_parser("letters", help="сопроводительные письма")
    letters_sub = p_letters.add_subparsers(dest="letters_command")
    letters_sub.add_parser("list", help="список писем").set_defaults(func=cmd_letters)
    p_letter_show = letters_sub.add_parser("show", help="показать текст письма")
    p_letter_show.add_argument("name")
    p_letter_show.set_defaults(func=cmd_letter_show)
    p_letter_add = letters_sub.add_parser("add", help="добавить письмо")
    p_letter_add.add_argument("name")
    p_letter_add.add_argument("--text", help="текст письма")
    p_letter_add.add_argument("--file", help="файл с текстом письма")
    p_letter_add.add_argument("--title", help="человекочитаемое название")
    p_letter_add.add_argument("--keyword", action="append", help="ключевое слово для автоподбора (можно несколько)")
    p_letter_add.add_argument("--for-filter", action="append", help="привязать к фильтру (можно несколько)")
    p_letter_add.add_argument("--default", action="store_true", help="сделать письмом по умолчанию")
    p_letter_add.set_defaults(func=cmd_letter_add)
    p_letter_rm = letters_sub.add_parser("remove", help="удалить письмо")
    p_letter_rm.add_argument("name")
    p_letter_rm.set_defaults(func=cmd_letter_remove)
    p_letters.set_defaults(func=cmd_letters)

    p_filters = sub.add_parser("filters", help="фильтры вакансий")
    filters_sub = p_filters.add_subparsers(dest="filters_command")
    filters_sub.add_parser("list", help="список фильтров").set_defaults(func=cmd_filters)
    p_filter_show = filters_sub.add_parser("show", help="показать фильтр целиком")
    p_filter_show.add_argument("name")
    p_filter_show.set_defaults(func=cmd_filter_show)
    p_filter_add = filters_sub.add_parser(
        "add", help="создать фильтр (без аргументов запускается мастер)"
    )
    p_filter_add.add_argument("name", nargs="?", help="имя фильтра")
    p_filter_add.add_argument("--description", help="описание фильтра")
    p_filter_add.add_argument("--text", help="поисковый запрос")
    p_filter_add.add_argument("--area", action="append", help="ID региона (можно несколько)")
    p_filter_add.add_argument("--experience", help="noExperience|between1And3|between3And6|moreThan6")
    p_filter_add.add_argument("--schedule", help="remote|fullDay|flexible|shift|flyInFlyOut")
    p_filter_add.add_argument("--employment", action="append", help="full|part|project|probation")
    p_filter_add.add_argument("--professional-role", action="append", help="ID профессиональной роли")
    p_filter_add.add_argument("--period", type=int, help="за сколько дней искать")
    p_filter_add.add_argument("--salary", type=int, help="ожидаемая зарплата для поиска hh.ru")
    p_filter_add.add_argument("--only-with-salary", action="store_true", help="только с указанной зарплатой")
    p_filter_add.add_argument("--order-by", help="publication_time|salary_desc|relevance")
    p_filter_add.add_argument("--per-page", type=int, help="вакансий на странице (до 100)")
    p_filter_add.add_argument("--pages", type=int, default=1, help="сколько страниц выдачи забирать")
    p_filter_add.add_argument("--include-title", action="append", help="нужное слово в названии")
    p_filter_add.add_argument("--exclude-title", action="append", help="стоп-слово в названии")
    p_filter_add.add_argument("--include-text", action="append", help="нужное слово в описании")
    p_filter_add.add_argument("--exclude-text", action="append", help="стоп-слово в описании")
    p_filter_add.add_argument("--exclude-employer", action="append", help="компания в чёрный список")
    p_filter_add.add_argument("--salary-min", type=int, help="минимальная зарплата, ₽")
    p_filter_add.add_argument("--salary-max", type=int, help="максимальная зарплата, ₽")
    p_filter_add.add_argument("--require-salary", action="store_true", help="пропускать вакансии без зарплаты")
    p_filter_add.add_argument("--allow-tests", action="store_true", help="не пропускать вакансии с тестами hh")
    p_filter_add.add_argument("--max-applications", type=int, help="максимум откликов по фильтру за запуск")
    p_filter_add.add_argument("--letter", help="имя письма для этого фильтра")
    p_filter_add.set_defaults(func=cmd_filter_add)
    p_filter_rm = filters_sub.add_parser("remove", help="удалить фильтр")
    p_filter_rm.add_argument("name")
    p_filter_rm.set_defaults(func=cmd_filter_remove)
    p_filter_on = filters_sub.add_parser("enable", help="включить фильтр")
    p_filter_on.add_argument("name")
    p_filter_on.set_defaults(func=lambda ctx: cmd_filter_toggle(ctx, True))
    p_filter_off = filters_sub.add_parser("disable", help="выключить фильтр")
    p_filter_off.add_argument("name")
    p_filter_off.set_defaults(func=lambda ctx: cmd_filter_toggle(ctx, False))
    p_filters.set_defaults(func=cmd_filters)

    p_search = sub.add_parser("search", help="показать вакансии по фильтрам без откликов")
    p_search.add_argument("--filter", action="append", help="имя фильтра (можно несколько)")
    p_search.add_argument("--limit", type=int, default=20, help="сколько подходящих показать")
    p_search.add_argument("--show-rejected", action="store_true", help="показать отсеянные и причины")
    p_search.set_defaults(func=cmd_search)

    p_apply = sub.add_parser("apply", help="откликнуться на подходящие вакансии")
    p_apply.add_argument("--filter", action="append", help="имя фильтра (по умолчанию — все включённые)")
    p_apply.add_argument("--dry-run", action="store_true", help="прогон без отправки откликов")
    p_apply.add_argument("--yes", "-y", action="store_true", help="не спрашивать подтверждение")
    p_apply.add_argument("--interactive", action="store_true", help="спрашивать по каждой вакансии")
    p_apply.add_argument("--limit", type=int, help="максимум откликов за запуск")
    p_apply.add_argument("--letter", help="принудительно использовать это письмо")
    p_apply.add_argument("--resume", help="id резюме (перекрывает конфиг)")
    p_apply.add_argument("--ignore-daily-limit", action="store_true", help="не учитывать дневной лимит")
    p_apply.set_defaults(func=cmd_apply)

    sub.add_parser("stats", help="статистика откликов").set_defaults(func=cmd_stats)

    p_history = sub.add_parser("history", help="история откликов")
    p_history.add_argument("--limit", type=int, default=20)
    p_history.add_argument("--status", choices=["applied", "failed", "skipped", "dry-run"])
    p_history.set_defaults(func=cmd_history)

    sub.add_parser("sync", help="импортировать отклики с hh.ru в локальную историю").set_defaults(func=cmd_sync)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    ctx = Context(args)
    try:
        return int(args.func(ctx) or 0)
    except (ConfigError, AuthError, HHApiError) as exc:
        print(Style.red(f"Ошибка: {exc}"), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        return 130
    finally:
        if ctx._history is not None:
            ctx._history.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
