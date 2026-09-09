"""Тесты клиента API hh.ru (без реальной сети)."""

import json
import unittest
from typing import Any, Dict, List, Optional

import requests

from hhhelper.api import HHApiError, HHClient, RateLimiter


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None, text: str = "", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload, ensure_ascii=False) if payload is not None else "")
        self.content = self.text.encode("utf-8")
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("нет тела")
        return self._payload


class FakeSession:
    def __init__(self, responses: List[Any]):
        self.responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    def request(self, method, url, params=None, data=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "data": data, "headers": headers})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def build_client(responses, **kwargs):
    session = FakeSession(responses)
    client = HHClient(
        lambda: "token-123",
        session=session,
        rate_limiter=RateLimiter(interval=0),
        sleep=lambda seconds: None,
        **kwargs,
    )
    return client, session


class TestRequests(unittest.TestCase):
    def test_authorization_and_user_agent(self):
        client, session = build_client([FakeResponse(200, {"ok": True})], user_agent="hhhelper/test")
        client.me()
        headers = session.calls[0]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer token-123")
        self.assertEqual(headers["User-Agent"], "hhhelper/test")

    def test_retries_on_429_then_succeeds(self):
        client, session = build_client(
            [FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(200, {"items": []})]
        )
        self.assertEqual(client.request("GET", "/vacancies"), {"items": []})
        self.assertEqual(len(session.calls), 2)

    def test_retries_on_network_error(self):
        client, session = build_client(
            [requests.RequestException("сеть упала"), FakeResponse(200, {"ok": 1})]
        )
        self.assertEqual(client.request("GET", "/me"), {"ok": 1})

    def test_gives_up_after_retries(self):
        client, _ = build_client([FakeResponse(500)] * 10)
        with self.assertRaises(HHApiError):
            client.request("GET", "/me")

    def test_error_body_is_explained(self):
        client, _ = build_client(
            [FakeResponse(403, {"errors": [{"value": "test_required", "type": "negotiations"}]})]
        )
        with self.assertRaises(HHApiError) as ctx:
            client.apply("1", "r1", "письмо")
        self.assertIn("тестовое задание", str(ctx.exception))
        self.assertEqual(ctx.exception.codes, ["test_required"])

    def test_empty_body_returns_dict(self):
        client, _ = build_client([FakeResponse(201, None, text="")])
        self.assertEqual(client.request("POST", "/negotiations"), {})


class TestSearch(unittest.TestCase):
    def test_pagination_stops_at_last_page(self):
        page0 = {"items": [{"id": "1", "name": "A"}], "pages": 2, "found": 2}
        page1 = {"items": [{"id": "2", "name": "B"}], "pages": 2, "found": 2}
        client, session = build_client([FakeResponse(200, page0), FakeResponse(200, page1)])
        vacancies = list(client.search_vacancies({"text": "python"}, pages=5))
        self.assertEqual([v.id for v in vacancies], ["1", "2"])
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[1]["params"]["page"], 1)

    def test_limit_stops_early(self):
        page = {"items": [{"id": str(i), "name": "V"} for i in range(5)], "pages": 3}
        client, _ = build_client([FakeResponse(200, page)])
        self.assertEqual(len(list(client.search_vacancies({}, pages=3, limit=2))), 2)

    def test_per_page_capped(self):
        client, session = build_client([FakeResponse(200, {"items": [], "pages": 1})])
        list(client.search_vacancies({"per_page": 500}, pages=1))
        self.assertEqual(session.calls[0]["params"]["per_page"], 100)


class TestOtherMethods(unittest.TestCase):
    def test_apply_sends_form_fields(self):
        client, session = build_client([FakeResponse(201, None, text="")])
        client.apply("777", "resume-1", "Здравствуйте")
        self.assertEqual(
            session.calls[0]["data"],
            {"vacancy_id": "777", "resume_id": "resume-1", "message": "Здравствуйте"},
        )

    def test_apply_without_message(self):
        client, session = build_client([FakeResponse(201, None, text="")])
        client.apply("777", "resume-1")
        self.assertNotIn("message", session.calls[0]["data"])

    def test_resumes_parsed(self):
        client, _ = build_client(
            [FakeResponse(200, {"items": [{"id": "r1", "title": "Python", "status": {"name": "Опубликовано"}}]})]
        )
        resumes = client.resumes()
        self.assertEqual(resumes[0].id, "r1")
        self.assertEqual(resumes[0].status, "Опубликовано")

    def test_negotiations_collects_vacancy_ids(self):
        client, _ = build_client(
            [FakeResponse(200, {"items": [{"vacancy": {"id": "5"}}, {"vacancy": {}}], "pages": 1})]
        )
        self.assertEqual(client.negotiations_vacancy_ids(), ["5"])

    def test_unauthorized_message(self):
        client, _ = build_client([FakeResponse(401, {}, text="unauthorized")])
        with self.assertRaises(HHApiError) as ctx:
            client.me()
        self.assertIn("auth login", str(ctx.exception))


class TestRateLimiter(unittest.TestCase):
    def test_waits_between_calls(self):
        slept: List[float] = []
        limiter = RateLimiter(interval=1.0, sleep=slept.append)
        limiter.wait()
        limiter.wait()
        self.assertTrue(slept and slept[0] > 0)

    def test_zero_interval_never_sleeps(self):
        slept: List[float] = []
        limiter = RateLimiter(interval=0, sleep=slept.append)
        limiter.wait()
        limiter.wait()
        self.assertEqual(slept, [])


if __name__ == "__main__":
    unittest.main()
