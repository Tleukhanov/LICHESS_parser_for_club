"""Tests for the rate-limited sender (temp sqlite DB, no real network)."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import requests
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from backend.config import settings
from backend.db.database import build_engine, init_db
from backend.db.models import candidates, outreach
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
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(2.5, abs=0.1)
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


# ---------------------------------------------------------------------------
# Safety guards (P0): cap, cooldown, pacing, duplicates, joined, empty text.
# ---------------------------------------------------------------------------


def _approved_row(db: Session, nick: str, text: str = MESSAGE) -> int:
    candidate_id = create_candidate(db, nick=nick, games_total=10)
    outreach_id = create_outreach(db, candidate_id, text)
    update_outreach(db, outreach_id, send_status="approved")
    return outreach_id


def test_single_send_respects_daily_cap(
    db_session: Session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "daily_send_limit", 1)
    _make_outreach(
        db_session,
        "Done",
        send_status="sent",
        sent_at=datetime.now(UTC).replace(tzinfo=None),
    )
    outreach_id = _approved_row(db_session, "Alice")
    calls = _fake_send(monkeypatch, SendResult(SendStatus.SENT))

    with pytest.raises(queue.SendRefused) as excinfo:
        queue.send_outreach(db_session, outreach_id, session=object())

    assert excinfo.value.reason == "daily_limit"
    assert calls == []
    assert get_outreach(db_session, outreach_id)["send_status"] == "approved"


def test_rate_limited_sets_a_cooldown_that_blocks_the_next_send(
    db_session: Session, monkeypatch
) -> None:
    first = _approved_row(db_session, "Alice")
    second = _approved_row(db_session, "Bob")
    calls = _fake_send(
        monkeypatch, SendResult(SendStatus.RATE_LIMITED, "slow down")
    )

    queue.send_outreach(db_session, first, session=object())

    with pytest.raises(queue.SendRefused) as excinfo:
        queue.send_outreach(db_session, second, session=object())

    assert excinfo.value.reason == "cooldown"
    assert excinfo.value.retry_after is not None
    assert len(calls) == 1
    assert get_outreach(db_session, second)["send_status"] == "approved"


def test_empty_message_is_refused_and_not_sent(
    db_session: Session, monkeypatch
) -> None:
    outreach_id = _approved_row(db_session, "Alice", "   ")
    calls = _fake_send(monkeypatch, SendResult(SendStatus.SENT))

    with pytest.raises(queue.SendRefused) as excinfo:
        queue.send_outreach(db_session, outreach_id, session=object())

    assert excinfo.value.reason == "empty_text"
    assert calls == []


def test_joined_candidate_is_skipped(
    db_session: Session, monkeypatch
) -> None:
    outreach_id = _approved_row(db_session, "Alice")
    candidate_id = get_outreach(db_session, outreach_id)["candidate_id"]
    db_session.execute(
        update(candidates)
        .where(candidates.c.id == candidate_id)
        .values(status="joined")
    )
    db_session.commit()
    calls = _fake_send(monkeypatch, SendResult(SendStatus.SENT))

    with pytest.raises(queue.SendRefused) as excinfo:
        queue.send_outreach(db_session, outreach_id, session=object())

    assert excinfo.value.reason == "joined"
    assert calls == []
    assert get_outreach(db_session, outreach_id)["send_status"] == "skipped_joined"


def test_second_message_to_the_same_player_is_refused(
    db_session: Session, monkeypatch
) -> None:
    candidate_id = create_candidate(db_session, nick="Alice")
    sent = create_outreach(db_session, candidate_id, "old")
    update_outreach(
        db_session,
        sent,
        send_status="sent",
        sent_at=datetime.now(UTC).replace(tzinfo=None),
    )
    approved = create_outreach(db_session, candidate_id, MESSAGE)
    update_outreach(db_session, approved, send_status="approved")
    calls = _fake_send(monkeypatch, SendResult(SendStatus.SENT))

    with pytest.raises(queue.SendRefused) as excinfo:
        queue.send_outreach(db_session, approved, session=object())

    assert excinfo.value.reason == "already_sent"
    assert calls == []
    assert get_outreach(db_session, approved)["send_status"] == "rejected"


def test_pacing_refuses_a_second_send_in_the_same_moment(
    db_session: Session, monkeypatch
) -> None:
    first = _approved_row(db_session, "Alice")
    second = _approved_row(db_session, "Bob")
    calls = _fake_send(monkeypatch, SendResult(SendStatus.SENT))
    moment = datetime(2026, 3, 1, 10, 0, 0)

    queue.send_outreach(db_session, first, session=object(), now=moment)

    with pytest.raises(queue.SendRefused) as excinfo:
        queue.send_outreach(db_session, second, session=object(), now=moment)

    assert excinfo.value.reason == "too_soon"
    assert len(calls) == 1


def test_row_in_sending_state_is_not_claimed_again(
    db_session: Session, monkeypatch
) -> None:
    outreach_id = _approved_row(db_session, "Alice")
    db_session.execute(
        update(outreach)
        .where(outreach.c.id == outreach_id)
        .values(send_status="sending")
    )
    db_session.commit()
    calls = _fake_send(monkeypatch, SendResult(SendStatus.SENT))

    with pytest.raises(queue.SendRefused) as excinfo:
        queue.send_outreach(db_session, outreach_id, session=object())

    assert excinfo.value.reason == "in_progress"
    assert calls == []


def test_batch_skips_joined_candidate_and_duplicate_rows(
    db_session: Session, monkeypatch
) -> None:
    joined = _approved_row(db_session, "Joined")
    joined_candidate = get_outreach(db_session, joined)["candidate_id"]
    db_session.execute(
        update(candidates)
        .where(candidates.c.id == joined_candidate)
        .values(status="joined")
    )
    db_session.commit()

    duplicate_candidate = create_candidate(db_session, nick="Dup")
    duplicate_a = create_outreach(db_session, duplicate_candidate, "a")
    duplicate_b = create_outreach(db_session, duplicate_candidate, "b")
    update_outreach(db_session, duplicate_a, send_status="approved")
    update_outreach(db_session, duplicate_b, send_status="approved")

    normal = _approved_row(db_session, "Normal")
    calls = _fake_send(
        monkeypatch,
        [SendResult(SendStatus.SENT), SendResult(SendStatus.SENT)],
    )

    result = queue.send_batch(
        db_session, session=object(), sleep=lambda seconds: None, interval=0
    )

    # One duplicate row plus the normal candidate are sent; the joined row and
    # the second duplicate are skipped without any HTTP call.
    assert len(calls) == 2
    assert result["sent"] == 2
    assert get_outreach(db_session, joined)["send_status"] == "skipped_joined"
    statuses = {
        get_outreach(db_session, duplicate_a)["send_status"],
        get_outreach(db_session, duplicate_b)["send_status"],
    }
    assert statuses == {"sent", "rejected"}
    assert get_outreach(db_session, normal)["send_status"] == "sent"


def test_batch_pacing_counts_a_previous_send(
    db_session: Session, monkeypatch
) -> None:
    _make_outreach(
        db_session,
        "Recent",
        send_status="sent",
        sent_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=5),
    )
    _approved_row(db_session, "Alice")
    _fake_send(monkeypatch, SendResult(SendStatus.SENT))
    sleeps: list[float] = []

    queue.send_batch(
        db_session,
        session=object(),
        sleep=sleeps.append,
        interval=45,
    )

    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(40, abs=0.5)


def test_candidate_in_sending_state_is_not_claimed_again(
    db_session: Session, monkeypatch
) -> None:
    outreach_id = _approved_row(db_session, "Alice")
    candidate_id = get_outreach(db_session, outreach_id)["candidate_id"]
    db_session.execute(
        update(candidates)
        .where(candidates.c.id == candidate_id)
        .values(
            status="sending",
            updated_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    db_session.commit()
    calls = _fake_send(monkeypatch, SendResult(SendStatus.SENT))

    with pytest.raises(queue.SendRefused) as excinfo:
        queue.send_outreach(db_session, outreach_id, session=object())

    assert excinfo.value.reason == "in_progress"
    assert calls == []


def test_stale_sending_candidate_can_be_reclaimed(
    db_session: Session, monkeypatch
) -> None:
    outreach_id = _approved_row(db_session, "Alice")
    candidate_id = get_outreach(db_session, outreach_id)["candidate_id"]
    db_session.execute(
        update(candidates)
        .where(candidates.c.id == candidate_id)
        .values(
            status="sending",
            updated_at=datetime.now(UTC).replace(tzinfo=None)
            - timedelta(hours=1),
        )
    )
    db_session.commit()
    calls = _fake_send(monkeypatch, SendResult(SendStatus.SENT))

    result = queue.send_outreach(db_session, outreach_id, session=object())

    assert result.status is SendStatus.SENT
    assert len(calls) == 1
    assert get_candidate(db_session, candidate_id)["status"] == "messaged"


def test_batch_resets_abandoned_sending_rows(
    db_session: Session, monkeypatch
) -> None:
    outreach_id = _approved_row(db_session, "Alice")
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    db_session.execute(
        update(outreach)
        .where(outreach.c.id == outreach_id)
        .values(send_status="sending", updated_at=old)
    )
    db_session.commit()
    calls = _fake_send(monkeypatch, SendResult(SendStatus.SENT))

    result = queue.send_batch(
        db_session, session=object(), sleep=lambda seconds: None, interval=0
    )

    assert len(calls) == 1
    assert result["sent"] == 1
    assert get_outreach(db_session, outreach_id)["send_status"] == "sent"
