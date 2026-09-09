"""Пошаговые диалоги создания фильтра и письма.

Это чистая машина состояний: на каждом шаге принимает ответ пользователя
(текст сообщения или значение кнопки) и возвращает следующий вопрос.
Сохранением занимается бот — здесь только сбор данных и проверки.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .views import Keyboard, keyboard

#: Значение, которым кнопки мастера отвечают «пропустить этот шаг».
SKIP = "-"
CANCEL_ROW = [("✖️ Отмена", "w:cancel")]


@dataclass
class WizardState:
    """Текущий шаг диалога и собранные ответы."""

    kind: str  # filter | letter
    step: int = 0
    data: Dict[str, Any] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WizardReply:
    """Что показать пользователю после его ответа."""

    text: str
    keyboard: Optional[Keyboard] = None
    done: bool = False
    cancelled: bool = False
    result: Optional[Dict[str, Any]] = None


@dataclass
class Step:
    """Один шаг диалога."""

    question: str
    apply: Callable[[Dict[str, Any], str], Optional[str]]
    options: Sequence[Sequence[Tuple[str, str]]] = ()
    skippable: bool = True

    def render(self) -> Tuple[str, Keyboard]:
        rows: List[List[Tuple[str, str]]] = [list(row) for row in self.options]
        if self.skippable:
            rows.append([("Пропустить", f"w:{SKIP}")])
        rows.append(CANCEL_ROW)
        return self.question, keyboard(rows)


def start(kind: str, context: Optional[Dict[str, Any]] = None) -> Tuple[WizardState, WizardReply]:
    """Начинает диалог и возвращает первый вопрос."""
    state = WizardState(kind=kind, context=context or {})
    step = _steps(state)[0]
    text, markup = step.render()
    return state, WizardReply(text=_intro(kind) + "\n\n" + text, keyboard=markup)


def advance(state: WizardState, answer: str) -> WizardReply:
    """Принимает ответ на текущий шаг и переходит к следующему."""
    answer = (answer or "").strip()
    if answer == "w:cancel" or answer.lower() in ("/cancel", "отмена"):
        return WizardReply(text="Создание отменено.", cancelled=True)
    if answer.startswith("w:"):
        answer = answer[2:]

    steps = _steps(state)
    step = steps[state.step]
    if answer == SKIP:
        if not step.skippable:
            text, markup = step.render()
            return WizardReply(text="Этот шаг пропустить нельзя.\n\n" + text, keyboard=markup)
    else:
        error = step.apply(state.data, answer)
        if error:
            text, markup = step.render()
            return WizardReply(text=f"⚠️ {error}\n\n{text}", keyboard=markup)

    state.step += 1
    steps = _steps(state)  # набор шагов может зависеть от собранных данных
    if state.step >= len(steps):
        return WizardReply(text=_summary(state), done=True, result=_result(state))
    text, markup = steps[state.step].render()
    return WizardReply(text=text, keyboard=markup)


# -- описание шагов ---------------------------------------------------------


def _steps(state: WizardState) -> List[Step]:
    return _FILTER_STEPS(state) if state.kind == "filter" else _LETTER_STEPS(state)


def _FILTER_STEPS(state: WizardState) -> List[Step]:
    letters: Sequence[str] = state.context.get("letters") or []
    letter_rows = [[(name, f"w:{name}")] for name in letters]
    return [
        Step(
            "<b>1/7. Название фильтра</b>\nКоротко и латиницей, например <code>python-remote</code>.",
            _set_name,
            skippable=False,
        ),
        Step(
            "<b>2/7. Поисковый запрос</b>\nТо же, что вы вводите в строку поиска hh.ru.",
            _set_text,
        ),
        Step(
            "<b>3/7. Регион</b>\nВыберите кнопкой или пришлите ID регионов через запятую.",
            _set_area,
            options=[[("Москва", "w:1"), ("Санкт-Петербург", "w:2")], [("Вся Россия", "w:113")]],
        ),
        Step(
            "<b>4/7. Опыт работы</b>",
            _set_experience,
            options=[
                [("Без опыта", "w:noExperience"), ("1–3 года", "w:between1And3")],
                [("3–6 лет", "w:between3And6"), ("Более 6", "w:moreThan6")],
            ],
        ),
        Step(
            "<b>5/7. График работы</b>",
            _set_schedule,
            options=[
                [("Удалённо", "w:remote"), ("Полный день", "w:fullDay")],
                [("Гибкий", "w:flexible")],
            ],
        ),
        Step(
            "<b>6/7. Зарплата от, ₽</b>\nПришлите число — вакансии ниже порога отсеются.",
            _set_salary,
        ),
        Step(
            "<b>7/7. Стоп-слова в названии</b>\nЧерез запятую, например: <code>1С, стажер, senior</code>.",
            _set_stopwords,
        ),
    ] + (
        [
            Step(
                "<b>Письмо для этого фильтра</b>\nЕсли пропустить — письмо подберётся автоматически.",
                _set_letter,
                options=letter_rows,
            )
        ]
        if letters
        else []
    )


def _LETTER_STEPS(state: WizardState) -> List[Step]:
    return [
        Step(
            "<b>1/4. Название письма</b>\nКоротко, например <code>backend</code>.",
            _set_name,
            skippable=False,
        ),
        Step(
            "<b>2/4. Текст письма</b>\nМожно использовать подстановки: "
            "<code>{vacancy_name}</code>, <code>{employer}</code>, <code>{candidate_name}</code>.",
            _set_letter_text,
            skippable=False,
        ),
        Step(
            "<b>3/4. Ключевые слова</b>\nЧерез запятую. Если слово встретится в вакансии — "
            "помощник выберет это письмо.",
            _set_keywords,
        ),
        Step(
            "<b>4/4. Сделать письмом по умолчанию?</b>",
            _set_default,
            options=[[("Да", "w:да"), ("Нет", "w:нет")]],
        ),
    ]


# -- обработчики ответов ----------------------------------------------------


def _set_name(data: Dict[str, Any], answer: str) -> Optional[str]:
    name = answer.strip()
    if not name:
        return "Название не может быть пустым."
    if len(name) > 40:
        return "Слишком длинное название, уложитесь в 40 символов."
    data["name"] = name
    return None


def _set_text(data: Dict[str, Any], answer: str) -> Optional[str]:
    data.setdefault("search", {})["text"] = answer
    return None


def _set_area(data: Dict[str, Any], answer: str) -> Optional[str]:
    areas = [part.strip() for part in answer.replace(" ", ",").split(",") if part.strip()]
    if not all(area.isdigit() for area in areas):
        return "ID региона — это число (1 — Москва, 2 — СПб, 113 — Россия)."
    data.setdefault("search", {})["area"] = areas
    return None


def _set_experience(data: Dict[str, Any], answer: str) -> Optional[str]:
    allowed = {"noExperience", "between1And3", "between3And6", "moreThan6"}
    if answer not in allowed:
        return "Выберите вариант кнопкой."
    data.setdefault("search", {})["experience"] = answer
    return None


def _set_schedule(data: Dict[str, Any], answer: str) -> Optional[str]:
    allowed = {"remote", "fullDay", "flexible", "shift", "flyInFlyOut"}
    if answer not in allowed:
        return "Выберите вариант кнопкой."
    data.setdefault("search", {})["schedule"] = answer
    return None


def _set_salary(data: Dict[str, Any], answer: str) -> Optional[str]:
    digits = answer.replace(" ", "").replace("_", "")
    if not digits.isdigit():
        return "Нужно число, например 200000."
    data.setdefault("rules", {})["salary_min"] = int(digits)
    return None


def _set_stopwords(data: Dict[str, Any], answer: str) -> Optional[str]:
    words = [part.strip() for part in answer.split(",") if part.strip()]
    if words:
        data.setdefault("rules", {})["title_exclude"] = words
    return None


def _set_letter(data: Dict[str, Any], answer: str) -> Optional[str]:
    data["letter"] = answer
    return None


def _set_letter_text(data: Dict[str, Any], answer: str) -> Optional[str]:
    if len(answer) < 20:
        return "Письмо слишком короткое — напишите пару предложений."
    data["text"] = answer
    return None


def _set_keywords(data: Dict[str, Any], answer: str) -> Optional[str]:
    words = [part.strip() for part in answer.split(",") if part.strip()]
    if words:
        data.setdefault("match", {})["keywords"] = words
    return None


def _set_default(data: Dict[str, Any], answer: str) -> Optional[str]:
    data["default"] = answer.strip().lower() in ("да", "yes", "y", "1", "true")
    return None


# -- итоги ------------------------------------------------------------------


def _intro(kind: str) -> str:
    if kind == "filter":
        return "<b>Создание фильтра.</b> Необязательные шаги можно пропускать."
    return "<b>Создание письма.</b>"


def _result(state: WizardState) -> Dict[str, Any]:
    data = dict(state.data)
    if state.kind == "filter":
        data.setdefault("search", {})
        data.setdefault("rules", {})
        data["rules"].setdefault("max_applications", 15)
        data["search"].setdefault("period", 7)
        data["pages"] = 2
    return data


def _summary(state: WizardState) -> str:
    data = state.data
    if state.kind == "filter":
        search = data.get("search", {})
        rules = data.get("rules", {})
        lines = [f"✅ Фильтр «{data.get('name')}» создан.", ""]
        if search.get("text"):
            lines.append(f"Запрос: {search['text']}")
        if search.get("area"):
            lines.append("Регионы: " + ", ".join(search["area"]))
        if search.get("experience"):
            lines.append(f"Опыт: {search['experience']}")
        if search.get("schedule"):
            lines.append(f"График: {search['schedule']}")
        if rules.get("salary_min"):
            lines.append(f"Зарплата от: {rules['salary_min']}")
        if rules.get("title_exclude"):
            lines.append("Стоп-слова: " + ", ".join(rules["title_exclude"]))
        if data.get("letter"):
            lines.append(f"Письмо: {data['letter']}")
        return "\n".join(lines)
    lines = [f"✅ Письмо «{data.get('name')}» сохранено."]
    if data.get("match", {}).get("keywords"):
        lines.append("Ключевые слова: " + ", ".join(data["match"]["keywords"]))
    if data.get("default"):
        lines.append("Используется по умолчанию.")
    return "\n".join(lines)
