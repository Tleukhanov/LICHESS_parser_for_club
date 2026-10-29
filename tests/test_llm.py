"""Tests for the LLM invitation layer (no real network access)."""

import json

import pytest
import requests

from backend.config import settings
from backend.llm.generator import (
    LLMError,
    build_messages,
    fallback_message,
    generate_message,
)

CLUB_URL = "https://lichess.org/team/senpai-chess"


class FakeResponse:
    """Minimal stand-in for :class:`requests.Response`."""

    def __init__(self, status_code: int = 200, payload=None, text: str | None = None):
        self.status_code = status_code
        self._payload = payload
        if text is not None:
            self.text = text
        elif payload is not None:
            self.text = json.dumps(payload)
        else:
            self.text = ""

    def json(self):
        if self._payload is None:
            raise ValueError("response body is not JSON")
        return self._payload


def completion(content: str) -> FakeResponse:
    return FakeResponse(
        200, {"choices": [{"message": {"role": "assistant", "content": content}}]}
    )


class FakeSession:
    """Session double that records calls and replays queued responses."""

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if not self._responses:
            raise AssertionError("unexpected request: no queued response")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Fail loudly if any test reaches the real ``requests`` transport."""

    def _forbidden(*args, **kwargs):
        raise AssertionError("real network call attempted")

    monkeypatch.setattr(requests, "post", _forbidden)
    monkeypatch.setattr(requests.Session, "request", _forbidden)
    monkeypatch.setattr(requests.Session, "send", _forbidden)


@pytest.fixture()
def llm_settings(monkeypatch):
    """Configure the settings singleton for a request without touching ``.env``."""
    values = {
        "llm_api_key": "test-key",
        "llm_base_url": "https://llm.example/api/v1",
        "llm_model": "test/model",
        "llm_temperature": 0.3,
        "llm_max_tokens": 123,
        "llm_timeout": 7,
        "message_language": "ru",
        "club_url": "",
        "senpai_team_id": "senpai-team",
    }
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)
    return settings


@pytest.fixture()
def candidate() -> dict:
    return {
        "nick": "Alice",
        "rapid": 1800,
        "blitz": 1750,
        "bullet": 1600,
        "games_total": 42,
        "last_seen": 1_700_000_000_000,
    }


def test_build_messages_contains_candidate_details(candidate: dict) -> None:
    messages = build_messages(
        candidate, club_name="Senpai Chess", club_url=CLUB_URL, language="ru"
    )

    assert [message["role"] for message in messages] == ["system", "user"]
    text = "\n".join(message["content"] for message in messages)
    assert "Alice" in text
    assert "1800" in text
    assert CLUB_URL in text
    assert "Russian" in text
    assert "500" in text
    assert "no markdown" in text.lower()


def test_build_messages_handles_missing_ratings() -> None:
    messages = build_messages(
        {"nick": "Bob"}, club_name="Senpai Chess", club_url=CLUB_URL, language="en"
    )

    text = "\n".join(message["content"] for message in messages)
    assert "Bob" in text
    assert "unknown" in text
    assert "no ratings" in text
    assert "English" in text


def test_generate_message_sends_expected_request(
    llm_settings, candidate: dict
) -> None:
    session = FakeSession([completion("  Hello Alice!  \n")])

    message = generate_message(candidate, session=session)

    assert message == "Hello Alice!"
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == "https://llm.example/api/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer test-key"
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["timeout"] == 7
    assert call["json"]["model"] == "test/model"
    assert call["json"]["temperature"] == 0.3
    assert call["json"]["max_tokens"] == 123
    assert call["json"]["messages"][0]["role"] == "system"
    assert call["json"]["messages"][1]["role"] == "user"
    assert "Alice" in call["json"]["messages"][1]["content"]


def test_generate_message_uses_defaults_from_settings(
    llm_settings, candidate: dict
) -> None:
    session = FakeSession([completion("Hi")])

    generate_message(candidate, session=session)

    messages = session.calls[0]["json"]["messages"]
    text = "\n".join(message["content"] for message in messages)
    assert "https://lichess.org/team/senpai-team" in text
    assert "Russian" in text


def test_generate_message_accepts_explicit_language_and_url(
    llm_settings, candidate: dict
) -> None:
    session = FakeSession([completion("Hi")])

    generate_message(
        candidate,
        club_url="https://lichess.org/team/other",
        language="en",
        session=session,
    )

    text = "\n".join(
        message["content"] for message in session.calls[0]["json"]["messages"]
    )
    assert "https://lichess.org/team/other" in text
    assert "English" in text


def test_missing_api_key_raises_without_network(
    llm_settings, candidate: dict, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "llm_api_key", "")

    with pytest.raises(LLMError) as exc_info:
        generate_message(candidate)

    error = exc_info.value
    assert error.status_code == 0
    assert error.message == "LLM_API_KEY is not configured"
    assert str(error) == "LLM_0: LLM_API_KEY is not configured"


def test_429_is_retried_and_succeeds(llm_settings, candidate: dict) -> None:
    session = FakeSession([FakeResponse(429, text="slow down"), completion("Hi")])
    sleep = FakeSleep()

    message = generate_message(candidate, session=session, sleep=sleep)

    assert message == "Hi"
    assert len(session.calls) == 2
    assert sleep.calls == [1.0]


def test_500_is_retried_up_to_three_attempts(llm_settings, candidate: dict) -> None:
    session = FakeSession([FakeResponse(503, text="unavailable")] * 3)
    sleep = FakeSleep()

    with pytest.raises(LLMError) as exc_info:
        generate_message(candidate, session=session, sleep=sleep)

    assert exc_info.value.status_code == 503
    assert exc_info.value.message == "unavailable"
    assert len(session.calls) == 3
    assert sleep.calls == [1.0, 2.0]


def test_network_error_is_retried(llm_settings, candidate: dict) -> None:
    session = FakeSession([requests.ConnectionError("offline"), completion("Hi")])
    sleep = FakeSleep()

    message = generate_message(candidate, session=session, sleep=sleep)

    assert message == "Hi"
    assert len(session.calls) == 2
    assert sleep.calls == [1.0]


def test_400_raises_without_retry(llm_settings, candidate: dict) -> None:
    session = FakeSession([FakeResponse(400, text="bad request")])
    sleep = FakeSleep()

    with pytest.raises(LLMError) as exc_info:
        generate_message(candidate, session=session, sleep=sleep)

    error = exc_info.value
    assert error.status_code == 400
    assert error.message == "bad request"
    assert len(session.calls) == 1
    assert sleep.calls == []


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(200, {"choices": []}),
        FakeResponse(200, {"choices": [{"message": {"content": "   "}}]}),
        FakeResponse(200, {"choices": [{"message": {}}]}),
        FakeResponse(200, text="not json"),
    ],
)
def test_malformed_or_empty_content_raises(
    llm_settings, candidate: dict, response: FakeResponse
) -> None:
    session = FakeSession([response])

    with pytest.raises(LLMError):
        generate_message(candidate, session=session)


def test_fallback_message_contains_nick_rating_and_link(candidate: dict) -> None:
    message = fallback_message(candidate, club_url=CLUB_URL, language="ru")

    assert len(message) <= 500
    assert "Alice" in message
    assert "1800" in message
    assert CLUB_URL in message
    assert "Senpai Chess" in message


def test_fallback_message_handles_unrated_candidate() -> None:
    message = fallback_message(
        {"nick": "Bob", "games_total": 0}, club_url=CLUB_URL, language="ru"
    )

    assert len(message) <= 500
    assert "Bob" in message
    assert CLUB_URL in message
    assert "рейтинг" in message


def test_fallback_message_supports_english() -> None:
    message = fallback_message(
        {"nick": "Bob", "rapid": 1200}, club_url=CLUB_URL, language="en"
    )

    assert "Bob" in message
    assert "1200" in message
    assert CLUB_URL in message
    assert len(message) <= 500
