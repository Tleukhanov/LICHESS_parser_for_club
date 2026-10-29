"""Tests for conversion tracking and CSV export (temp sqlite, no network)."""

import csv
import io
from collections.abc import Iterator
from datetime import datetime

import pytest
import requests
from sqlalchemy.orm import Session, sessionmaker

from backend.analytics import conversion
from backend.config import settings
from backend.db.database import build_engine, init_db
from backend.db.models import candidates, outreach
from backend.db.repository import (
    create_candidate,
    create_outreach,
    get_candidate,
    get_outreach,
    update_outreach,
)

MOMENT = datetime(2026, 5, 1, 12, 0, 0)


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Fail loudly if a test reaches the real ``requests`` transport."""

    def _forbidden(*args, **kwargs):
        raise AssertionError("real network call attempted")

    monkeypatch.setattr(requests.Session, "request", _forbidden)
    monkeypatch.setattr(requests.Session, "send", _forbidden)


@pytest.fixture()
def db_session(tmp_path) -> Iterator[Session]:
    engine = build_engine(f"sqlite:///{tmp_path / 'analytics.db'}")
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
    nick: str,
    *,
    message: str = "Hello",
    send_status: str = "draft",
    reaction: str = "none",
) -> tuple[int, int]:
    candidate_id = create_candidate(db, nick=nick)
    outreach_id = create_outreach(
        db, candidate_id, message, llm_model="test-model"
    )
    update_outreach(
        db, outreach_id, send_status=send_status, reaction=reaction
    )
    return candidate_id, outreach_id


def test_refresh_joined_marks_matching_candidates_case_insensitively(
    db_session: Session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "senpai_team_id", "senpai")

    alice_id = create_candidate(db_session, nick="Alice")
    first = create_outreach(db_session, alice_id, "First")
    second = create_outreach(db_session, alice_id, "Second")
    bob_id = create_candidate(db_session, nick="Bob")
    bob_outreach = create_outreach(db_session, bob_id, "Hi Bob")

    calls: list[tuple] = []

    def fake_fetch(session, team_id):
        calls.append((session, team_id))
        return ["ALICE", "someone-else"]

    monkeypatch.setattr(conversion, "fetch_team_members", fake_fetch)

    api_session = object()
    result = conversion.refresh_joined(
        db_session, session=api_session, now=MOMENT
    )

    assert calls == [(api_session, "senpai")]
    assert result == {
        "team_id": "senpai",
        "checked": 2,
        "newly_joined": 1,
        "total_joined": 1,
    }

    for outreach_id in (first, second):
        row = get_outreach(db_session, outreach_id)
        assert row["joined"] is True
        assert row["joined_at"] == MOMENT

    assert get_outreach(db_session, bob_outreach)["joined"] is False
    assert get_candidate(db_session, alice_id)["status"] == "joined"
    assert get_candidate(db_session, bob_id)["status"] == "new"

    # A second run with the same members is a no-op.
    second_run = conversion.refresh_joined(
        db_session, session=api_session, now=MOMENT
    )
    assert second_run["newly_joined"] == 0
    assert second_run["total_joined"] == 1
    assert get_outreach(db_session, first)["joined_at"] == MOMENT


def test_refresh_joined_respects_explicit_team_id_and_raises_when_empty(
    db_session: Session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "senpai_team_id", "")

    with pytest.raises(ValueError, match="senpai_team_id"):
        conversion.refresh_joined(db_session, session=object())

    seen: dict = {}

    def fake_fetch(session, team_id):
        seen["team_id"] = team_id
        return []

    monkeypatch.setattr(conversion, "fetch_team_members", fake_fetch)

    result = conversion.refresh_joined(
        db_session, session=object(), team_id="other-team"
    )
    assert seen["team_id"] == "other-team"
    assert result["team_id"] == "other-team"


def test_conversion_stats_counts_and_rate(db_session: Session) -> None:
    alice_id = create_candidate(db_session, nick="Alice")
    first = create_outreach(db_session, alice_id, "First")
    second = create_outreach(db_session, alice_id, "Second")
    update_outreach(
        db_session, first, send_status="sent", joined=True, reaction="replied"
    )
    update_outreach(
        db_session, second, send_status="sent", joined=True, reaction="replied"
    )

    _make_outreach(
        db_session, "Bob", send_status="sent", reaction="declined"
    )
    _make_outreach(db_session, "Carol", send_status="draft")

    stats = conversion.conversion_stats(db_session)

    assert stats["outreach_total"] == 4
    assert stats["sent_total"] == 3
    assert stats["joined_total"] == 1
    assert stats["conversion_rate"] == round(1 / 3, 4)
    assert stats["by_send_status"]["sent"] == 3
    assert stats["by_send_status"]["draft"] == 1
    assert stats["by_reaction"]["replied"] == 2
    assert stats["by_reaction"]["declined"] == 1
    assert stats["by_reaction"]["none"] == 1


def test_conversion_stats_rate_is_zero_without_sends(db_session: Session) -> None:
    _make_outreach(db_session, "Alice", send_status="draft")

    stats = conversion.conversion_stats(db_session)

    assert stats["outreach_total"] == 1
    assert stats["sent_total"] == 0
    assert stats["joined_total"] == 0
    assert stats["conversion_rate"] == 0.0


def test_export_dataset_writes_header_and_one_row_per_outreach(
    db_session: Session, tmp_path
) -> None:
    alice_id = create_candidate(
        db_session,
        nick="Alice",
        source_club_id="club-1",
        rapid=1800,
        blitz=1700,
        bullet=1600,
        games_total=42,
    )
    draft = create_outreach(db_session, alice_id, "Draft text", llm_model="m1")
    update_outreach(
        db_session,
        draft,
        reviewer_rating=4,
        reviewer_notes="good",
        reaction="replied",
    )
    sent_at = datetime(2026, 5, 1, 12, 0, 0)
    joined_at = datetime(2026, 5, 2, 9, 30, 0)
    sent = create_outreach(db_session, alice_id, "Sent text", llm_model="m2")
    update_outreach(
        db_session,
        sent,
        send_status="sent",
        sent_at=sent_at,
        joined=True,
        joined_at=joined_at,
    )

    # A candidate without outreach produces no dataset row.
    create_candidate(db_session, nick="Bob")

    path = tmp_path / "nested" / "dataset.csv"
    row_count = conversion.export_dataset(db_session, path)

    assert row_count == 2
    content = path.read_text(encoding="utf-8")
    rows = list(csv.reader(io.StringIO(content)))

    assert tuple(rows[0]) == conversion.DATASET_HEADER
    assert rows[0][0] == "nick"
    assert len(rows) == 3

    assert rows[1][0] == "Alice"
    assert rows[1][1] == "club-1"
    assert rows[1][2:6] == ["1800", "1700", "1600", "42"]
    assert rows[1][6] == "Draft text"
    assert rows[1][7] == "m1"
    assert rows[1][8] == "4"
    assert rows[1][9] == "good"
    assert rows[1][10] == "draft"
    assert rows[1][13] == "replied"
    assert rows[1][14] == "false"
    assert rows[1][15] == ""

    assert rows[2][10] == "sent"
    assert rows[2][12] == "2026-05-01 12:00:00"
    assert rows[2][14] == "true"
    assert rows[2][15] == "2026-05-02 09:30:00"

    assert conversion.dataset_csv_bytes(db_session) == path.read_bytes()


def test_dataset_includes_drafts_and_exported_model_matches_db(
    db_session: Session, tmp_path
) -> None:
    _make_outreach(db_session, "Alice", send_status="draft")

    # Smoke-check the tables are populated as expected before exporting.
    assert len(db_session.execute(candidates.select()).all()) == 1
    assert len(db_session.execute(outreach.select()).all()) == 1

    path = tmp_path / "dataset.csv"
    assert conversion.export_dataset(db_session, path) == 1
    assert ",draft," in path.read_text(encoding="utf-8")
