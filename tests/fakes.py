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
