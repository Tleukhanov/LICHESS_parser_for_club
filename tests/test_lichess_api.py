"""Tests for the Lichess API client layer (no real network access)."""

import json

import pytest
import requests

from backend.lichess_api.messaging import SendStatus, send_message
from backend.lichess_api.teams import fetch_team_info, fetch_team_members
from backend.lichess_api.transport import LichessError, request_with_retry
from backend.lichess_api.users import fetch_perfs

API_USERS = "https://lichess.org/api/users"
API_TEAM = "https://lichess.org/api/team"


class FakeResponse:
    """Minimal stand-in for :class:`requests.Response`."""

    def __init__(
        self,
        status_code: int = 200,
        text: str = "",
        headers: dict | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)

    def iter_lines(self):
        for line in self.text.splitlines():
            yield line.encode("utf-8")


class FakeSession:
    """Session double that records calls and replays queued responses."""

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def _next(self):
        if not self._responses:
            raise AssertionError("unexpected request: no queued response")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def request(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._next()

    def post(self, url: str, **kwargs):
        self.calls.append({"method": "POST", "url": url, **kwargs})
        return self._next()


class FakeSleep:
    """Injectable sleep that records the requested delays."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Fail loudly if any test reaches the real ``requests`` transport."""

    def _forbidden(*args, **kwargs):
        raise AssertionError("real network call attempted")

    monkeypatch.setattr(requests.Session, "request", _forbidden)
    monkeypatch.setattr(requests.Session, "send", _forbidden)


def test_fetch_team_info_returns_parsed_json() -> None:
    session = FakeSession(
        [FakeResponse(200, json.dumps({"id": "club", "nbMembers": 3}))]
    )

    info = fetch_team_info(session, "club")

    assert info == {"id": "club", "nbMembers": 3}
    assert session.calls[0]["method"] == "GET"
    assert session.calls[0]["url"] == f"{API_TEAM}/club"


def test_fetch_team_members_parses_ndjson_and_dedupes() -> None:
    body = "\n".join(
        [
            json.dumps({"id": "Alice", "name": "Alice"}),
            "",
            "not-json",
            json.dumps({"id": "Bob", "name": "Bob"}),
            json.dumps({"id": "Alice", "name": "Alice"}),
            json.dumps({"username": "Carol"}),
        ]
    )
    session = FakeSession([FakeResponse(200, body)])

    members = fetch_team_members(session, "club")

    assert members == ["Alice", "Bob", "Carol"]
    assert len(session.calls) == 1
    assert session.calls[0]["url"] == f"{API_TEAM}/club/users"


def test_fetch_perfs_parses_ratings_and_seen_at() -> None:
    payload = [
        {
            "id": "Alice",
            "perfs": {
                "rapid": {"rating": 1800, "games": 10},
                "blitz": {"rating": 1700},
                "bullet": {"rating": 1600},
            },
            "count": {"all": 42},
            "seenAt": 1_700_000_000_000,
        },
        {
            "id": "Bob",
            "perfs": {"rapid": {"rating": 1200}},
            "count": {"all": 7},
        },
    ]
    session = FakeSession([FakeResponse(200, json.dumps(payload))])

    perfs = fetch_perfs(session, ["Alice", "Bob"])

    assert perfs["Alice"] == {
        "rapid": 1800,
        "blitz": 1700,
        "bullet": 1600,
        "games_total": 42,
        "last_seen": 1_700_000_000_000,
    }
    assert perfs["Bob"] == {
        "rapid": 1200,
        "blitz": None,
        "bullet": None,
        "games_total": 7,
        "last_seen": None,
    }
    assert session.calls[0]["method"] == "POST"
    assert session.calls[0]["url"] == API_USERS
    assert session.calls[0]["data"] == "Alice,Bob"


def test_fetch_perfs_parses_ndjson_fallback() -> None:
    body = "\n".join(
        [
            json.dumps({"id": "Alice", "perfs": {"rapid": {"rating": 1500}}}),
            "garbage",
            json.dumps({"id": "Bob", "perfs": {}}),
        ]
    )
    session = FakeSession([FakeResponse(200, body)])

    perfs = fetch_perfs(session, ["Alice", "Bob"])

    assert perfs["Alice"]["rapid"] == 1500
    assert perfs["Bob"]["rapid"] is None


def test_fetch_perfs_chunks_more_than_300_nicks() -> None:
    nicks = [f"user{index}" for index in range(305)]
    session = FakeSession([FakeResponse(200, "[]"), FakeResponse(200, "[]")])

    result = fetch_perfs(session, nicks)

    assert result == {}
    assert len(session.calls) == 2
    first_chunk = session.calls[0]["data"].split(",")
    second_chunk = session.calls[1]["data"].split(",")
    assert len(first_chunk) == 300
    assert len(second_chunk) == 5
    assert first_chunk[0] == "user0"
    assert second_chunk[-1] == "user304"


def test_send_message_200_is_sent() -> None:
    session = FakeSession([FakeResponse(200, "")])

    result = send_message(session, "Alice", "hello")

    assert result.status is SendStatus.SENT
    assert result.error is None
    assert len(session.calls) == 1
    assert session.calls[0]["url"] == "https://lichess.org/inbox/Alice"
    assert session.calls[0]["data"] == {"text": "hello"}
    assert session.calls[0]["timeout"] == 30


def test_send_message_400_is_rejected_without_retry() -> None:
    session = FakeSession([FakeResponse(400, "This user does not accept messages")])

    result = send_message(session, "Alice", "hello")

    assert result.status is SendStatus.REJECTED
    assert result.error == "This user does not accept messages"
    assert len(session.calls) == 1


def test_send_message_429_is_rate_limited() -> None:
    session = FakeSession([FakeResponse(429, "Too many requests")])

    result = send_message(session, "Alice", "hello")

    assert result.status is SendStatus.RATE_LIMITED
    assert result.error == "Too many requests"
    assert len(session.calls) == 1


def test_send_message_500_is_error() -> None:
    session = FakeSession([FakeResponse(500, "boom")])

    result = send_message(session, "Alice", "hello")

    assert result.status is SendStatus.ERROR
    assert result.error == "boom"


def test_send_message_network_error_is_error() -> None:
    session = FakeSession([requests.ConnectionError("no route to host")])

    result = send_message(session, "Alice", "hello")

    assert result.status is SendStatus.ERROR
    assert "no route to host" in (result.error or "")


def test_request_with_retry_recovers_after_500() -> None:
    session = FakeSession([FakeResponse(500, "boom"), FakeResponse(200, "{}")])
    sleep = FakeSleep()

    response = request_with_retry(
        session, "GET", "https://lichess.org/api/team/club", sleep=sleep
    )

    assert response.status_code == 200
    assert len(session.calls) == 2
    assert sleep.calls == [1.0]


def test_request_with_retry_persistent_500_raises_retryable() -> None:
    session = FakeSession([FakeResponse(500, "boom")] * 3)
    sleep = FakeSleep()

    with pytest.raises(LichessError) as exc_info:
        request_with_retry(
            session, "GET", "https://lichess.org/api/team/club", sleep=sleep
        )

    error = exc_info.value
    assert error.status_code == 500
    assert error.message == "boom"
    assert error.retryable is True
    assert str(error) == "Lichess_500: boom"
    assert len(session.calls) == 3
    assert sleep.calls == [1.0, 2.0]


def test_request_with_retry_400_raises_immediately() -> None:
    session = FakeSession([FakeResponse(400, "bad request")])
    sleep = FakeSleep()

    with pytest.raises(LichessError) as exc_info:
        request_with_retry(
            session, "GET", "https://lichess.org/api/team/club", sleep=sleep
        )

    error = exc_info.value
    assert error.status_code == 400
    assert error.message == "bad request"
    assert error.retryable is False
    assert len(session.calls) == 1
    assert sleep.calls == []


def test_request_with_retry_429_waits_and_recovers() -> None:
    session = FakeSession([FakeResponse(429, "slow down"), FakeResponse(200, "{}")])
    sleep = FakeSleep()

    response = request_with_retry(
        session, "GET", "https://lichess.org/api/team/club", sleep=sleep
    )

    assert response.status_code == 200
    assert len(session.calls) == 2
    assert sleep.calls == [60.0]


def test_request_with_retry_429_honours_retry_after_header() -> None:
    session = FakeSession(
        [FakeResponse(429, "slow down", {"Retry-After": "7"}), FakeResponse(200, "{}")]
    )
    sleep = FakeSleep()

    request_with_retry(session, "GET", "https://lichess.org/api/team/club", sleep=sleep)

    assert sleep.calls == [7.0]


def test_request_with_retry_network_error_is_retryable() -> None:
    session = FakeSession(
        [requests.ConnectionError("offline"), FakeResponse(200, "{}")]
    )
    sleep = FakeSleep()

    response = request_with_retry(
        session, "GET", "https://lichess.org/api/team/club", sleep=sleep
    )

    assert response.status_code == 200
    assert sleep.calls == [1.0]
