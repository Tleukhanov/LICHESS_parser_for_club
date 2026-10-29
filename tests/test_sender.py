"""Tests for the rate-limited sender (temp sqlite DB, no real network)."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import requests
from sqlalchemy.orm import Session, sessionmaker

from backend.config import settings
from backend.db.database import build_engine, init_db
from backend.db.repository import (
    create_candidate,
    create_outreach,
    get_candidate,
    get_outreach,
    list_outreach,
    update_outreach,
)
from backend.lichess_api.messaging import SendResult, SendStatus
from backend.sender import queue

MESSAGE = "Hello from the club!"


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Fail loudly if a test reaches the real ``requests`` transport."""

    def _forbidden(*args, **kwargs):
        raise AssertionError("real network call attempted")

    monkeypatch.setattr(requests.Session, "request", _forbidden)
    monkeypatch.setattr(requests.Session, "send", _forbidden)


@pytest.fixture()
def db_session(tmp_path) -> Iterator[Session]:
    engine = build_engine(f"sqlite:///{tmp_path / 'sender.db'}")
    init_db(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _make_outreach(
    db: Session,
    nick: str = "Alice",
    *,
    send_status: str = "approved",
    sent_at: datetime | None = None,
) -> int:
    candidate_id = create_candidate(db, nick=nick, games_total=10)
    outreach_id = create_outreach(db, candidate_id, MESSAGE, llm_model="test-model")
    fields: dict = {"send_status": send_status}
    if sent_at is not None:
        fields["sent_at"] = sent_at
    update_outreach(db, outreach_id, **fields)
    return outreach_id


def _fake_send(monkeypatch, results: list[SendResult] | SendResult) -> list[tuple]:
    """Patch ``queue.send_message`` and record every call."""
    calls: list[tuple] = []
    pending = list(results) if isinstance(results, list) else [results]

    def fake(session, username, text):
        calls.append((session, username, text))
        if not pending:
            raise AssertionError("unexpected extra send")
        return pending.pop(0)

    monkeypatch.setattr(queue, "send_message", fake)
    return calls


def test_approved_is_sent_and_marks_candidate_messaged(
    db_session: Session, monkeypatch
) -> None:
    outreach_id = _make_outreach(db_session, "Alice")
    calls = _fake_send(monkeypatch, SendResult(SendStatus.SENT))
    api_session = object()

    result = queue.send_outreach(
        db_session, outreach_id, session=api_session
    )

    assert result.status is SendStatus.SENT
    assert calls == [(api_session, "Alice", MESSAGE)]

    row = get_outreach(db_session, outreach_id)
    assert row["send_status"] == "sent"
    assert row["send_error"] is None
    assert row["sent_at"] is not None
    assert get_candidate(db_session, row["candidate_id"])["status"] == "messaged"


def test_rejected_400_is_skipped_without_retry(
    db_session: Session, monkeypatch
) -> None:
    outreach_id = _make_outreach(db_session, "Blocked")
    calls = _fake_send(
        monkeypatch,
        SendResult(SendStatus.REJECTED, "The message was rejected"),
    )

    result = queue.send_outreach(db_session, outreach_id, session=object())

    assert result.status is SendStatus.REJECTED
    assert len(calls) == 1
    row = get_outreach(db_session, outreach_id)
    assert row["send_status"] == "skipped_400"
    assert row["send_error"] == "The message was rejected"
    assert row["sent_at"] is None


def test_rate_limited_is_skipped_429(db_session: Session, monkeypatch) -> None:
    outreach_id = _make_outreach(db_session, "TooFast")
    _fake_send(monkeypatch, SendResult(SendStatus.RATE_LIMITED, "slow down"))

    queue.send_outreach(db_session, outreach_id, session=object())

    row = get_outreach(db_session, outreach_id)
    assert row["send_status"] == "skipped_429"
    assert row["send_error"] == "slow down"


def test_error_is_marked_failed(db_session: Session, monkeypatch) -> None:
    outreach_id = _make_outreach(db_session, "Broken")
    _fake_send(monkeypatch, SendResult(SendStatus.ERROR, "connection reset"))

    queue.send_outreach(db_session, outreach_id, session=object())

    row = get_outreach(db_session, outreach_id)
    assert row["send_status"] == "failed"
    assert row["send_error"] == "connection reset"


@pytest.mark.parametrize("status", ["draft", "rejected", "skipped_400", "failed"])
def test_unapproved_rows_are_never_sent(
    db_session: Session, monkeypatch, status: str
) -> None:
    outreach_id = _make_outreach(db_session, "Pending", send_status=status)
    calls = _fake_send(monkeypatch, SendResult(SendStatus.SENT))

    with pytest.raises(ValueError, match="not approved"):
        queue.send_outreach(db_session, outreach_id, session=object())

    assert calls == []
    assert get_outreach(db_session, outreach_id)["send_status"] == status


def test_missing_outreach_raises(db_session: Session, monkeypatch) -> None:
    calls = _fake_send(monkeypatch, SendResult(SendStatus.SENT))

    with pytest.raises(ValueError, match="not found"):
        queue.send_outreach(db_session, 999, session=object())

    assert calls == []


def test_already_sent_is_idempotent(db_session: Session, monkeypatch) -> None:
    sent_at = datetime(2026, 1, 1, 12, 0, 0)
    outreach_id = _make_outreach(
        db_session, "Done", send_status="sent", sent_at=sent_at
    )
    calls = _fake_send(monkeypatch, SendResult(SendStatus.SENT))

    result = queue.send_outreach(db_session, outreach_id, session=object())

    assert result.status is SendStatus.SENT
    assert calls == []
    assert get_outreach(db_session, outreach_id)["sent_at"] == sent_at


def test_batch_sleeps_between_sends_but_not_before_the_first(
    db_session: Session, monkeypatch
) -> None:
    _make_outreach(db_session, "Alice")
    _make_outreach(db_session, "Bob")
    calls = _fake_send(
        monkeypatch,
        [SendResult(SendStatus.SENT), SendResult(SendStatus.SENT)],
    )
    sleeps: list[float] = []

    result = queue.send_batch(
        db_session,
        session=object(),
        sleep=sleeps.append,
        interval=2.5,
    )

    assert len(calls) == 2
    assert sleeps == [2.5]
    assert result == {
        "sent": 2,
        "rejected": 0,
        "rate_limited": 0,
        "failed": 0,
        "stopped": None,
    }


def test_batch_stops_on_rate_limited_and_keeps_rest_approved(
    db_session: Session, monkeypatch
) -> None:
    first = _make_outreach(db_session, "Alice")
    second = _make_outreach(db_session, "Bob")
    third = _make_outreach(db_session, "Carol")
    calls = _fake_send(
        monkeypatch,
        [
            SendResult(SendStatus.SENT),
            SendResult(SendStatus.RATE_LIMITED, "rate limited"),
        ],
    )

    result = queue.send_batch(
        db_session,
        session=object(),
        sleep=lambda seconds: None,
        interval=0,
    )

    assert len(calls) == 2
    assert result["sent"] == 1
    assert result["rate_limited"] == 1
    assert result["stopped"] == "rate_limited"
    assert get_outreach(db_session, first)["send_status"] == "sent"
    assert get_outreach(db_session, second)["send_status"] == "skipped_429"
    assert get_outreach(db_session, third)["send_status"] == "approved"


def test_batch_counts_rejected_and_failed(
    db_session: Session, monkeypatch
) -> None:
    _make_outreach(db_session, "Alice")
    _make_outreach(db_session, "Bob")
    _fake_send(
        monkeypatch,
        [
            SendResult(SendStatus.REJECTED, "rejected"),
            SendResult(SendStatus.ERROR, "boom"),
        ],
    )

    result = queue.send_batch(
        db_session, session=object(), sleep=lambda seconds: None, interval=0
    )

    assert result["sent"] == 0
    assert result["rejected"] == 1
    assert result["failed"] == 1
    assert result["stopped"] is None


def test_batch_respects_daily_cap(db_session: Session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "daily_send_limit", 1)
    _make_outreach(db_session, "Alice")
    _make_outreach(db_session, "Bob")
    calls = _fake_send(monkeypatch, SendResult(SendStatus.SENT))

    result = queue.send_batch(
        db_session, session=object(), sleep=lambda seconds: None, interval=0
    )

    assert len(calls) == 1
    assert result["sent"] == 1
    assert result["stopped"] is None
    approved = list_outreach(db_session, send_status="approved")
    assert len(approved) == 1
    assert get_candidate(db_session, approved[0]["candidate_id"])["nick"] == "Bob"


def test_batch_stops_when_daily_cap_is_exhausted(
    db_session: Session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "daily_send_limit", 1)
    _make_outreach(
        db_session,
        "Done",
        send_status="sent",
        sent_at=datetime.now(UTC).replace(tzinfo=None),
    )
    _make_outreach(db_session, "Alice")
    calls = _fake_send(monkeypatch, SendResult(SendStatus.SENT))

    result = queue.send_batch(
        db_session, session=object(), sleep=lambda seconds: None, interval=0
    )

    assert calls == []
    assert result == {
        "sent": 0,
        "rejected": 0,
        "rate_limited": 0,
        "failed": 0,
        "stopped": "daily_limit",
    }


def test_batch_limit_is_capped_by_remaining(db_session: Session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "daily_send_limit", 2)
    for nick in ("Alice", "Bob", "Carol"):
        _make_outreach(db_session, nick)
    calls = _fake_send(
        monkeypatch,
        [SendResult(SendStatus.SENT), SendResult(SendStatus.SENT)],
    )

    result = queue.send_batch(
        db_session,
        session=object(),
        limit=5,
        sleep=lambda seconds: None,
        interval=0,
    )

    assert len(calls) == 2
    assert result["sent"] == 2


def test_sent_today_counts_only_todays_sent_rows(db_session: Session) -> None:
    today = datetime.now(UTC).replace(tzinfo=None)
    yesterday = today - timedelta(days=1)
    _make_outreach(db_session, "Today", send_status="sent", sent_at=today)
    _make_outreach(db_session, "Old", send_status="sent", sent_at=yesterday)
    _make_outreach(db_session, "Approved")
    _make_outreach(db_session, "Rejected", send_status="skipped_400")

    assert queue.sent_today(db_session) == 1
    assert queue.sent_today(db_session, today) == 1


def test_start_of_day_utc_is_midnight() -> None:
    moment = datetime(2026, 10, 29, 17, 42, 13, 123456, tzinfo=UTC)

    assert queue.start_of_day_utc(moment) == datetime(2026, 10, 29, 0, 0, 0)
