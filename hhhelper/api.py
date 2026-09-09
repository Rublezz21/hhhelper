"""Клиент официального API hh.ru (https://api.hh.ru).

Используются только документированные методы: поиск вакансий, чтение резюме
и отклик через ``POST /negotiations``. Никакой эмуляции браузера и обхода
защиты — работа идёт от имени кандидата по его OAuth-токену.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

import requests

from .models import Resume, Vacancy

log = logging.getLogger(__name__)

BASE_URL = "https://api.hh.ru"
#: hh.ru просит не «долбить» API: держим паузу между запросами.
MIN_REQUEST_INTERVAL = 0.34
MAX_RETRIES = 4
MAX_PER_PAGE = 100
#: Глубина выдачи поиска ограничена самим hh.ru.
MAX_SEARCH_ITEMS = 2000

#: Понятные объяснения кодов ошибок hh.ru при отклике.
NEGOTIATION_ERRORS = {
    "already_applied": "отклик уже был отправлен раньше",
    "archived": "вакансия в архиве",
    "test_required": "по вакансии обязательно тестовое задание",
    "limit_exceeded": "исчерпан дневной лимит откликов на hh.ru",
    "resume_not_found": "резюме не найдено — проверьте resume_id",
    "vacancy_not_found": "вакансия не найдена",
    "resume_visibility": "резюме скрыто настройками видимости",
    "incomplete_resume": "резюме заполнено не полностью",
    "letter_required": "вакансия требует сопроводительное письмо",
    "blacklisted": "работодатель в вашем чёрном списке",
    "not_enough_purchased_services": "у работодателя закончились услуги",
    "bad_argument": "hh.ru не принял параметры отклика",
}


class HHApiError(Exception):
    """Ошибка обращения к API hh.ru."""

    def __init__(self, message: str, status: Optional[int] = None, payload: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}

    @property
    def codes(self) -> List[str]:
        return [str(err.get("value") or err.get("type") or "") for err in (self.payload.get("errors") or [])]


class RateLimiter:
    """Простейший ограничитель: не чаще одного запроса в `interval` секунд."""

    def __init__(self, interval: float = MIN_REQUEST_INTERVAL, sleep: Callable[[float], None] = time.sleep):
        self.interval = interval
        self._sleep = sleep
        self._last = 0.0

    def wait(self) -> None:
        if self.interval <= 0:
            return
        elapsed = time.monotonic() - self._last
        if 0 <= elapsed < self.interval:
            self._sleep(self.interval - elapsed)
        self._last = time.monotonic()


class HHClient:
    """Тонкая обёртка над HTTP API hh.ru."""

    def __init__(
        self,
        token_provider: Callable[[], str],
        *,
        user_agent: str = "hhhelper/1.0",
        base_url: str = BASE_URL,
        session: Optional[requests.Session] = None,
        rate_limiter: Optional[RateLimiter] = None,
        timeout: int = 30,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._token_provider = token_provider
        self.user_agent = user_agent
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.rate_limiter = rate_limiter or RateLimiter(sleep=sleep)
        self.timeout = timeout
        self._sleep = sleep

    # -- низкий уровень -----------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Выполняет запрос с повторами при 429/5xx и понятными ошибками."""
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {self._token_provider()}",
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        last_error: Optional[HHApiError] = None
        for attempt in range(1, MAX_RETRIES + 1):
            self.rate_limiter.wait()
            try:
                response = self.session.request(
                    method, url, params=params, data=data, headers=headers, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_error = HHApiError(f"Сетевая ошибка при запросе {method} {url}: {exc}")
                self._backoff(attempt)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                last_error = HHApiError(
                    f"hh.ru ответил {response.status_code} на {method} {url}", response.status_code
                )
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                log.debug("Повтор %s/%s после %s", attempt, MAX_RETRIES, response.status_code)
                self._backoff(attempt, retry_after)
                continue

            return self._handle_response(response, method, url)

        assert last_error is not None
        raise last_error

    def _handle_response(self, response: requests.Response, method: str, url: str) -> Any:
        if response.status_code == 204 or not (response.content or b"").strip():
            return {}
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code >= 400:
            raise HHApiError(
                _describe_error(response.status_code, payload, response.text),
                response.status_code,
                payload if isinstance(payload, dict) else {},
            )
        return payload

    def _backoff(self, attempt: int, retry_after: Optional[float] = None) -> None:
        delay = retry_after if retry_after is not None else min(2 ** attempt, 30) + random.uniform(0, 0.5)
        self._sleep(delay)

    # -- методы API ---------------------------------------------------------

    def me(self) -> Dict[str, Any]:
        """Информация о текущем пользователе (GET /me)."""
        return self.request("GET", "/me")

    def resumes(self) -> List[Resume]:
        """Список резюме кандидата (GET /resumes/mine)."""
        data = self.request("GET", "/resumes/mine")
        return [Resume.parse(item) for item in (data.get("items") or [])]

    def get_vacancy(self, vacancy_id: str) -> Vacancy:
        """Полная карточка вакансии (GET /vacancies/{id})."""
        return Vacancy.parse(self.request("GET", f"/vacancies/{vacancy_id}"))

    def search_vacancies(
        self,
        params: Dict[str, Any],
        *,
        pages: int = 1,
        limit: Optional[int] = None,
    ) -> Iterator[Vacancy]:
        """Постранично выдаёт вакансии по поисковым параметрам (GET /vacancies)."""
        per_page = min(int(params.get("per_page") or 50), MAX_PER_PAGE)
        found = 0
        for page in range(pages):
            query = dict(params)
            query["per_page"] = per_page
            query["page"] = page
            data = self.request("GET", "/vacancies", params=query)
            items = data.get("items") or []
            for item in items:
                yield Vacancy.parse(item)
                found += 1
                if limit is not None and found >= limit:
                    return
            total_pages = int(data.get("pages") or 0)
            if not items or page + 1 >= total_pages:
                return

    def negotiations_vacancy_ids(self, max_pages: int = 20) -> List[str]:
        """ID вакансий, на которые кандидат уже откликался (GET /negotiations)."""
        ids: List[str] = []
        for page in range(max_pages):
            data = self.request("GET", "/negotiations", params={"page": page, "per_page": 100})
            items = data.get("items") or []
            for item in items:
                vacancy = item.get("vacancy") or {}
                if vacancy.get("id"):
                    ids.append(str(vacancy["id"]))
            if not items or page + 1 >= int(data.get("pages") or 0):
                break
        return ids

    def suitable_resumes(self, vacancy_id: str) -> List[Resume]:
        """Резюме, которыми можно откликнуться на вакансию."""
        data = self.request("GET", f"/vacancies/{vacancy_id}/suitable_resumes")
        return [Resume.parse(item) for item in (data.get("items") or [])]

    def apply(self, vacancy_id: str, resume_id: str, message: str = "") -> None:
        """Отправляет отклик (POST /negotiations)."""
        payload: Dict[str, Any] = {"vacancy_id": str(vacancy_id), "resume_id": str(resume_id)}
        if message:
            payload["message"] = message
        self.request("POST", "/negotiations", data=payload)


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _describe_error(status: int, payload: Any, text: str) -> str:
    """Превращает ответ hh.ru в человекочитаемое сообщение."""
    if isinstance(payload, dict):
        errors = payload.get("errors") or []
        parts = []
        for err in errors:
            code = str(err.get("value") or err.get("type") or "")
            human = NEGOTIATION_ERRORS.get(code)
            parts.append(f"{human} [{code}]" if human else code or str(err))
        described = payload.get("description") or payload.get("error_description")
        if described:
            parts.append(str(described))
        if parts:
            return f"hh.ru {status}: " + "; ".join(p for p in parts if p)
    snippet = (text or "").strip()[:300]
    if status == 401:
        return "hh.ru 401: токен недействителен — выполните `hhhelper auth login`"
    if status == 403:
        return f"hh.ru 403: доступ запрещён. {snippet}"
    return f"hh.ru {status}: {snippet or 'неизвестная ошибка'}"
