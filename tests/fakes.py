"""Заглушки для тестов: фейковый клиент API hh.ru."""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional

from hhhelper.api import HHApiError
from hhhelper.models import Resume, Vacancy


def make_vacancy(
    vacancy_id: str,
    name: str = "Python-разработчик",
    employer: str = "ООО Ромашка",
    salary_from: Optional[int] = 200000,
    **extra: Any,
) -> Dict[str, Any]:
    """Сырой ответ hh.ru для одной вакансии."""
    raw: Dict[str, Any] = {
        "id": vacancy_id,
        "name": name,
        "employer": {"id": "100", "name": employer},
        "area": {"id": "1", "name": "Москва"},
        "alternate_url": f"https://hh.ru/vacancy/{vacancy_id}",
        "salary": {"from": salary_from, "to": None, "currency": "RUR", "gross": False}
        if salary_from is not None
        else None,
        "snippet": {"requirement": "Python, PostgreSQL", "responsibility": "Разработка сервисов"},
        "schedule": {"id": "remote", "name": "Удалённая работа"},
        "experience": {"id": "between1And3", "name": "От 1 года до 3 лет"},
    }
    raw.update(extra)
    return raw


class FakeClient:
    """Мини-версия HHClient: отдаёт заранее заданные вакансии и копит отклики."""

    def __init__(
        self,
        vacancies: Optional[List[Dict[str, Any]]] = None,
        *,
        fail_with: Optional[Dict[str, HHApiError]] = None,
        descriptions: Optional[Dict[str, str]] = None,
    ) -> None:
        self.vacancies = vacancies or []
        self.fail_with = fail_with or {}
        self.descriptions = descriptions or {}
        self.applications: List[Dict[str, str]] = []
        self.search_calls: List[Dict[str, Any]] = []
        self.fetched: List[str] = []

    def search_vacancies(self, params: Dict[str, Any], *, pages: int = 1, limit: Optional[int] = None) -> Iterator[Vacancy]:
        self.search_calls.append({"params": params, "pages": pages})
        for index, raw in enumerate(self.vacancies):
            if limit is not None and index >= limit:
                return
            yield Vacancy.parse(raw)

    def get_vacancy(self, vacancy_id: str) -> Vacancy:
        self.fetched.append(vacancy_id)
        for raw in self.vacancies:
            if str(raw["id"]) == str(vacancy_id):
                full = dict(raw)
                full["description"] = self.descriptions.get(str(vacancy_id), "Полное описание вакансии")
                return Vacancy.parse(full)
        raise HHApiError("vacancy not found", 404)

    def apply(self, vacancy_id: str, resume_id: str, message: str = "") -> None:
        error = self.fail_with.get(str(vacancy_id))
        if error is not None:
            raise error
        self.applications.append({"vacancy_id": str(vacancy_id), "resume_id": resume_id, "message": message})

    def resumes(self) -> List[Resume]:
        return [Resume(id="resume-1", title="Python-разработчик", status="Опубликовано")]

    def negotiations_vacancy_ids(self, max_pages: int = 20) -> List[str]:
        return [str(raw["id"]) for raw in self.vacancies[:1]]

    def me(self) -> Dict[str, Any]:
        return {"first_name": "Иван", "last_name": "Иванов"}


class FakeTelegramApi:
    """Заглушка Telegram Bot API: копит отправленные сообщения и правки."""

    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []
        self.edited: List[Dict[str, Any]] = []
        self.keyboards: List[Dict[str, Any]] = []
        self.answers: List[Dict[str, Any]] = []
        self._next_message_id = 100

    # -- методы, которые использует бот -------------------------------------

    def send_message(self, chat_id, text, keyboard=None, *, preview=False):
        self._next_message_id += 1
        message = {"message_id": self._next_message_id, "chat_id": chat_id, "text": text, "keyboard": keyboard}
        self.sent.append(message)
        return message

    def edit_message(self, chat_id, message_id, text, keyboard=None):
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text, "keyboard": keyboard})
        return {"message_id": message_id}

    def edit_keyboard(self, chat_id, message_id, keyboard=None):
        self.keyboards.append({"chat_id": chat_id, "message_id": message_id, "keyboard": keyboard})
        return {"message_id": message_id}

    def answer_callback(self, callback_id, text="", alert=False):
        self.answers.append({"id": callback_id, "text": text, "alert": alert})
        return True

    def get_me(self):
        return {"username": "hh_helper_bot", "id": 1}

    # -- помощники для тестов ------------------------------------------------

    @property
    def texts(self) -> List[str]:
        return [message["text"] for message in self.sent]

    @property
    def last_text(self) -> str:
        return self.sent[-1]["text"] if self.sent else ""

    @property
    def last_keyboard(self) -> List[str]:
        """Callback-данные кнопок последнего сообщения."""
        keyboard = self.sent[-1]["keyboard"] if self.sent else None
        if not keyboard:
            return []
        return [button["callback_data"] for row in keyboard["inline_keyboard"] for button in row]

    def buttons(self, index: int = -1) -> List[str]:
        keyboard = self.sent[index]["keyboard"]
        if not keyboard:
            return []
        return [button["text"] for row in keyboard["inline_keyboard"] for button in row]


def message_update(text: str, chat_id: int = 555, user_id: int = 555, update_id: int = 1) -> Dict[str, Any]:
    """Событие «пришло текстовое сообщение»."""
    return {
        "update_id": update_id,
        "message": {"message_id": 1, "chat": {"id": chat_id}, "from": {"id": user_id}, "text": text},
    }


def callback_update(data: str, chat_id: int = 555, user_id: int = 555, update_id: int = 1) -> Dict[str, Any]:
    """Событие «нажата инлайн-кнопка»."""
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb-{update_id}",
            "from": {"id": user_id},
            "data": data,
            "message": {"message_id": 10, "chat": {"id": chat_id}},
        },
    }
