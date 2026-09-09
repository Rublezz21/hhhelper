"""Тексты сообщений и инлайн-клавиатуры панели управления.

Функции здесь чистые: получают конфиг/данные и возвращают пару
``(текст, клавиатура)`` — их удобно проверять тестами без сети.
"""

from __future__ import annotations

import textwrap
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..config import AppConfig, Letter, VacancyFilter
from ..models import Vacancy
from .api import escape

Keyboard = Dict[str, Any]
View = Tuple[str, Optional[Keyboard]]


def keyboard(rows: Sequence[Sequence[Tuple[str, str]]]) -> Keyboard:
    """Собирает инлайн-клавиатуру из пар (подпись, callback_data)."""
    return {
        "inline_keyboard": [
            [{"text": title, "callback_data": data} for title, data in row] for row in rows if row
        ]
    }


BACK_ROW = [("‹ Меню", "menu")]


def main_menu(config: AppConfig, applied_today: int = 0) -> View:
    """Главный экран панели."""
    enabled = len(config.enabled_filters())
    resume = config.settings.resume_id
    lines = [
        "<b>Помощник кандидата hh.ru</b>",
        "",
        f"Фильтров: {len(config.filters)} (включено {enabled})",
        f"Писем: {len(config.letters)}",
        f"Откликов сегодня: {applied_today} из {config.settings.max_per_day}",
    ]
    if not resume:
        lines.append("\n⚠️ Не выбрано резюме — откройте «Настройки».")
    if not config.letters:
        lines.append("\n⚠️ Нет ни одного письма — добавьте в разделе «Письма».")
    return "\n".join(lines), keyboard(
        [
            [("🔍 Поиск", "srch"), ("🚀 Откликнуться", "run")],
            [("📋 Фильтры", "f"), ("✉️ Письма", "l")],
            [("📊 Статистика", "st"), ("🕓 История", "h")],
            [("⚙️ Настройки", "cfg")],
        ]
    )


# -- фильтры ----------------------------------------------------------------


def filters_list(config: AppConfig) -> View:
    if not config.filters:
        text = "Фильтров пока нет.\n\nФильтр — это поисковый запрос плюс правила отсева: " \
               "стоп-слова, порог зарплаты, чёрный список компаний."
    else:
        lines = ["<b>Фильтры вакансий</b>", ""]
        for flt in config.filters:
            mark = "🟢" if flt.enabled else "⚪️"
            query = escape(str(flt.search.get("text") or "без запроса"))
            lines.append(f"{mark} <b>{escape(flt.name)}</b> — {query}")
        text = "\n".join(lines)
    rows = [
        [(f"{'🟢' if flt.enabled else '⚪️'} {flt.name}", f"f:s:{index}")]
        for index, flt in enumerate(config.filters)
    ]
    rows.append([("➕ Создать фильтр", "f:new")])
    rows.append(BACK_ROW)
    return text, keyboard(rows)


def filter_card(config: AppConfig, index: int) -> View:
    flt = config.filters[index]
    rules = flt.rules
    lines = [f"<b>Фильтр «{escape(flt.name)}»</b>"]
    if flt.description:
        lines.append(escape(flt.description))
    lines.append("")
    lines.append("🟢 включён" if flt.enabled else "⚪️ выключен")
    lines.append("")
    lines.append("<b>Поиск</b>")
    for key, value in flt.search.items():
        lines.append(f"  {escape(key)}: {escape(_join(value))}")
    lines.append(f"  страниц выдачи: {flt.pages}")
    active_rules = rules.to_dict()
    if active_rules:
        lines.append("")
        lines.append("<b>Правила отсева</b>")
        for key, value in active_rules.items():
            lines.append(f"  {escape(_RULE_TITLES.get(key, key))}: {escape(_join(value))}")
    lines.append("")
    lines.append(f"Письмо: {escape(flt.letter) if flt.letter else 'подбирается автоматически'}")
    rows = [
        [("⚪️ Выключить" if flt.enabled else "🟢 Включить", f"f:t:{index}")],
        [("🔍 Проверить поиск", f"srch:{index}"), ("🚀 Откликнуться", f"run:pick:{index}")],
        [("🗑 Удалить", f"f:d:{index}")],
        [("‹ К фильтрам", "f")],
    ]
    return "\n".join(lines), keyboard(rows)


def filter_delete_confirm(config: AppConfig, index: int) -> View:
    flt = config.filters[index]
    text = f"Удалить фильтр «{escape(flt.name)}»? Отменить будет нельзя."
    return text, keyboard([[("🗑 Да, удалить", f"f:dc:{index}"), ("Отмена", f"f:s:{index}")]])


# -- письма -----------------------------------------------------------------


def letters_list(config: AppConfig) -> View:
    if not config.letters:
        text = "Писем пока нет.\n\nДобавьте хотя бы одно — без него отклик отправить не получится."
    else:
        lines = ["<b>Сопроводительные письма</b>", ""]
        for letter in config.letters:
            marks = []
            if letter.default:
                marks.append("по умолчанию")
            if letter.keywords:
                marks.append("слова: " + ", ".join(letter.keywords[:4]))
            if letter.filters:
                marks.append("фильтры: " + ", ".join(letter.filters))
            suffix = f" — {escape('; '.join(marks))}" if marks else ""
            lines.append(f"✉️ <b>{escape(letter.name)}</b>{suffix}")
        text = "\n".join(lines)
    rows = [
        [(f"{'⭐️ ' if letter.default else ''}{letter.name}", f"l:s:{index}")]
        for index, letter in enumerate(config.letters)
    ]
    rows.append([("➕ Добавить письмо", "l:new")])
    rows.append(BACK_ROW)
    return text, keyboard(rows)


def letter_card(config: AppConfig, index: int) -> View:
    letter = config.letters[index]
    lines = [f"<b>Письмо «{escape(letter.name)}»</b>"]
    if letter.title and letter.title != letter.name:
        lines.append(escape(letter.title))
    if letter.default:
        lines.append("⭐️ используется, когда ничего другое не подошло")
    if letter.keywords:
        lines.append("Ключевые слова: " + escape(", ".join(letter.keywords)))
    if letter.filters:
        lines.append("Привязано к фильтрам: " + escape(", ".join(letter.filters)))
    lines.append("")
    lines.append(f"<pre>{escape(letter.text)}</pre>")
    rows = [
        [("⭐️ Сделать основным", f"l:def:{index}")] if not letter.default else [],
        [("🗑 Удалить", f"l:d:{index}")],
        [("‹ К письмам", "l")],
    ]
    return "\n".join(lines), keyboard(rows)


def letter_delete_confirm(config: AppConfig, index: int) -> View:
    letter = config.letters[index]
    return (
        f"Удалить письмо «{escape(letter.name)}»?",
        keyboard([[("🗑 Да, удалить", f"l:dc:{index}"), ("Отмена", f"l:s:{index}")]]),
    )


# -- поиск и отклики --------------------------------------------------------


def pick_filter(config: AppConfig, action: str, title: str) -> View:
    """Экран выбора фильтра для поиска или откликов."""
    if not config.filters:
        return "Сначала создайте хотя бы один фильтр.", keyboard([[("➕ Создать фильтр", "f:new")], BACK_ROW])
    rows = [[("Все включённые", f"{action}:all")]]
    rows += [
        [(f"{'🟢' if flt.enabled else '⚪️'} {flt.name}", f"{action}:{index}")]
        for index, flt in enumerate(config.filters)
    ]
    rows.append(BACK_ROW)
    return title, keyboard(rows)


def run_modes(config: AppConfig, target: str) -> View:
    """Экран выбора режима запуска откликов."""
    name = "по всем включённым фильтрам" if target == "all" else f"по фильтру «{config.filters[int(target)].name}»"
    text = (
        f"<b>Отклики {escape(name)}</b>\n\n"
        "• <b>Проверка</b> — покажу, на что откликнулся бы, ничего не отправляя.\n"
        "• <b>С подтверждением</b> — по каждой вакансии спрошу здесь, в чате.\n"
        "• <b>Автоматически</b> — отправлю без вопросов, соблюдая лимиты."
    )
    rows = [
        [("🧪 Проверка", f"run:mode:dry:{target}")],
        [("✋ С подтверждением", f"run:mode:ask:{target}")],
        [("⚡️ Автоматически", f"run:mode:auto:{target}")],
        BACK_ROW,
    ]
    return text, keyboard(rows)


def vacancy_message(vacancy: Vacancy, letter_name: str, letter_text: str, reason: str = "") -> str:
    """Карточка вакансии с предпросмотром письма."""
    salary = str(vacancy.salary) if vacancy.salary else "з/п не указана"
    lines = [
        f"<b>{escape(vacancy.name)}</b>",
        escape(f"{vacancy.employer_name or '—'} · {vacancy.area_name or '—'}"),
        escape(salary),
    ]
    details = ", ".join(x for x in (vacancy.experience_name, vacancy.schedule_name) if x)
    if details:
        lines.append(escape(details))
    if vacancy.snippet_requirement:
        lines.append("")
        lines.append(escape(textwrap.shorten(vacancy.snippet_requirement, 300)))
    if vacancy.url:
        lines.append("")
        lines.append(f'<a href="{escape(vacancy.url)}">Открыть на hh.ru</a>')
    lines.append("")
    hint = f" ({escape(reason)})" if reason else ""
    lines.append(f"✉️ Письмо <b>{escape(letter_name)}</b>{hint}:")
    lines.append(f"<pre>{escape(textwrap.shorten(letter_text.replace(chr(10), ' '), 600))}</pre>")
    return "\n".join(lines)


def vacancy_keyboard(letters: Sequence[Letter]) -> Keyboard:
    rows = [
        [("✅ Отправить", "r:yes"), ("⏭ Пропустить", "r:no")],
        [("✉️ Другое письмо", "r:letter"), ("⏹ Остановить", "r:stop")],
    ]
    if len(letters) <= 1:
        rows[1] = [("⏹ Остановить", "r:stop")]
    return keyboard(rows)


def letter_choice_keyboard(letters: Sequence[Letter]) -> Keyboard:
    rows = [[(letter.name, f"r:l:{index}")] for index, letter in enumerate(letters)]
    rows.append([("‹ Назад", "r:back")])
    return keyboard(rows)


def preview_result(flt_name: str, passed: List[Dict[str, Any]], rejected: List[Dict[str, Any]]) -> str:
    """Результат проверки фильтра без откликов."""
    lines = [f"<b>Фильтр «{escape(flt_name)}»</b>", ""]
    if passed:
        lines.append(f"Подходящих вакансий: {len(passed)}")
        lines.append("")
        for item in passed[:10]:
            vacancy = item["vacancy"]
            salary = str(vacancy.salary) if vacancy.salary else "з/п не указана"
            link = f'<a href="{escape(vacancy.url)}">{escape(vacancy.name)}</a>' if vacancy.url \
                else f"<b>{escape(vacancy.name)}</b>"
            lines.append(f"• {link}")
            lines.append(escape(f"  {vacancy.employer_name or '—'} · {salary} · письмо: {item['letter']}"))
        if len(passed) > 10:
            lines.append(f"\n…и ещё {len(passed) - 10}")
    else:
        lines.append("Подходящих вакансий не нашлось.")
    if rejected:
        lines.append("")
        lines.append(f"<b>Отсеяно: {len(rejected)}</b>")
        for item in rejected[:5]:
            lines.append(escape(f"• {item['vacancy'].name} — {item['reason']}"))
        if len(rejected) > 5:
            lines.append(f"…и ещё {len(rejected) - 5}")
    return "\n".join(lines)


def run_summary(report, dry_run: bool = False) -> str:
    """Итог прогона откликов."""
    head = "🧪 <b>Проверка завершена</b>" if dry_run else "<b>Прогон завершён</b>"
    lines = [head, ""]
    if dry_run:
        lines.append(f"Откликнулись бы на: {len(report.dry_run)}")
    else:
        lines.append(f"Отправлено откликов: {len(report.applied)}")
    lines.append(f"Просмотрено вакансий: {report.scanned}")
    lines.append(f"Отсеяно правилами: {report.rejected}")
    if report.skipped:
        lines.append(f"Пропущено вручную: {len(report.skipped)}")
    if report.failed:
        lines.append(f"Ошибок: {len(report.failed)}")
        for result in report.failed[:3]:
            lines.append(escape(f"• {result.vacancy.name}: {result.reason}"))
    if report.stopped_reason:
        lines.append("")
        lines.append(escape(f"Остановка: {report.stopped_reason}"))
    return "\n".join(lines)


# -- статистика, история, настройки ----------------------------------------


def stats_view(stats: Dict[str, Any]) -> View:
    lines = [
        "<b>Статистика откликов</b>",
        "",
        f"Всего откликов: {stats['applied']}",
        f"Сегодня: {stats['today']}",
        f"За неделю: {stats['week']}",
    ]
    if stats["by_letter"]:
        lines.append("")
        lines.append("<b>По письмам</b>")
        for name, count in stats["by_letter"].items():
            lines.append(f"  {escape(name)}: {count}")
    if stats["by_filter"]:
        lines.append("")
        lines.append("<b>По фильтрам</b>")
        for name, count in stats["by_filter"].items():
            lines.append(f"  {escape(name)}: {count}")
    if stats["by_status"].get("failed"):
        lines.append("")
        lines.append(f"Ошибок при отправке: {stats['by_status']['failed']}")
    return "\n".join(lines), keyboard([BACK_ROW])


def history_view(rows: Iterable[Any]) -> View:
    rows = list(rows)
    if not rows:
        return "История пока пуста.", keyboard([BACK_ROW])
    lines = ["<b>Последние отклики</b>", ""]
    icons = {"applied": "✅", "failed": "⚠️", "skipped": "⏭", "dry-run": "🧪"}
    for row in rows:
        when = row["created_at"].replace("T", " ")[5:16]
        icon = icons.get(row["status"], "•")
        name = row["vacancy_name"] or row["vacancy_id"]
        lines.append(f"{icon} {escape(when)} — {escape(name)}")
        if row["employer_name"]:
            lines.append(escape(f"     {row['employer_name']}"))
    return "\n".join(lines), keyboard([BACK_ROW])


def settings_view(config: AppConfig) -> View:
    settings = config.settings
    lines = [
        "<b>Настройки</b>",
        "",
        f"Резюме: {escape(settings.resume_id) if settings.resume_id else '⚠️ не выбрано'}",
        f"Имя в письмах: {escape(settings.candidate_name) or '—'}",
        f"Максимум откликов за запуск: {settings.max_per_run}",
        f"Дневной лимит: {settings.max_per_day}",
        f"Пауза между откликами: {int(settings.delay_min)}–{int(settings.delay_max)} с",
    ]
    rows = [
        [("📄 Выбрать резюме", "cfg:resume")],
        [("➖", "cfg:run:-5"), (f"За запуск: {settings.max_per_run}", "nop"), ("➕", "cfg:run:5")],
        [("➖", "cfg:day:-10"), (f"В день: {settings.max_per_day}", "nop"), ("➕", "cfg:day:10")],
        BACK_ROW,
    ]
    return "\n".join(lines), keyboard(rows)


def resume_choice(resumes: Sequence[Any], current: Optional[str]) -> View:
    if not resumes:
        return "Резюме на hh.ru не найдены.", keyboard([[("‹ Настройки", "cfg")]])
    lines = ["<b>Выберите резюме для откликов</b>", ""]
    rows = []
    for resume in resumes:
        mark = "✅ " if resume.id == current else ""
        lines.append(f"{mark}{escape(resume.title)} — {escape(resume.status)}")
        rows.append([(f"{mark}{resume.title}"[:60], f"cfg:res:{resume.id}")])
    rows.append([("‹ Настройки", "cfg")])
    return "\n".join(lines), keyboard(rows)


_RULE_TITLES = {
    "title_include": "нужные слова в названии",
    "title_exclude": "стоп-слова в названии",
    "text_include": "нужные слова в описании",
    "text_exclude": "стоп-слова в описании",
    "employer_include": "белый список компаний",
    "employer_exclude": "чёрный список компаний",
    "employer_exclude_ids": "чёрный список id компаний",
    "salary_min": "зарплата от",
    "salary_max": "зарплата до",
    "require_salary": "только с указанной зарплатой",
    "skip_with_test": "пропускать вакансии с тестом",
    "skip_letter_required": "пропускать требующие письмо",
    "skip_already_applied": "пропускать отработанные",
    "max_applications": "максимум откликов за запуск",
}


def _join(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item) for item in value)
    if isinstance(value, bool):
        return "да" if value else "нет"
    return str(value)


def progress_text(
    *,
    applied: int,
    rejected: int,
    scanned: int,
    current: str = "",
    dry_run: bool = False,
    finished: bool = False,
) -> str:
    """Строка прогресса, которая обновляется по ходу прогона."""
    head = "🧪 <b>Проверка</b>" if dry_run else "🚀 <b>Отклики</b>"
    if finished:
        head += " — завершено"
    label = "нашлось" if dry_run else "отправлено"
    lines = [head, "", f"Просмотрено: {scanned} · отсеяно: {rejected} · {label}: {applied}"]
    if current and not finished:
        lines.append("")
        lines.append(escape(f"Сейчас: {current}"))
    return "\n".join(lines)


def result_line(result) -> str:
    """Короткий итог по одной вакансии — им заменяется карточка в чате."""
    vacancy = result.vacancy
    icons = {"applied": "✅", "dry-run": "🧪", "skipped": "⏭", "failed": "⚠️"}
    titles = {
        "applied": "Отклик отправлен",
        "dry-run": "Откликнулись бы",
        "skipped": "Пропущено",
        "failed": "Не удалось",
    }
    icon = icons.get(result.status, "•")
    title = titles.get(result.status, result.status)
    name = f'<a href="{escape(vacancy.url)}">{escape(vacancy.name)}</a>' if vacancy.url else escape(vacancy.name)
    lines = [f"{icon} <b>{title}</b>", name, escape(vacancy.employer_name or "—")]
    if result.letter_name and result.status in ("applied", "dry-run"):
        lines.append(escape(f"письмо: {result.letter_name}"))
    if result.status == "failed" and result.reason:
        lines.append(escape(result.reason))
    return "\n".join(lines)


def access_denied(user_id: int) -> str:
    return (
        "Доступ к панели закрыт.\n\n"
        f"Ваш Telegram ID: <code>{user_id}</code>\n"
        "Добавьте его в <code>settings.telegram.allowed_users</code> в конфиге "
        "и перезапустите бота."
    )


def help_text() -> str:
    return (
        "<b>Панель управления помощником hh.ru</b>\n\n"
        "/menu — главное меню\n"
        "/filters — фильтры вакансий\n"
        "/letters — сопроводительные письма\n"
        "/apply — запустить отклики\n"
        "/search — посмотреть, что находится\n"
        "/stats — статистика\n"
        "/stop — остановить текущий прогон\n"
        "/cancel — отменить создание фильтра или письма\n"
        "/id — показать ваш Telegram ID"
    )
