"""Scaffold tests: schema creation, repository round-trip and the health endpoint."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.orm import Session, sessionmaker

from backend.db import database as database_module
from backend.db.database import build_engine, init_db
from backend.db.models import app_state, candidates, metadata, outreach
from backend.db.repository import (
    create_candidate,
    create_outreach,
    get_candidate,
    get_outreach,
    list_candidates,
    list_outreach,
    update_outreach,
)


@pytest.fixture()
def temp_engine(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path / 'test.db'}")
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def session(temp_engine) -> Iterator[Session]:
    factory = sessionmaker(bind=temp_engine, autoflush=False, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.close()


def test_tables_created(temp_engine) -> None:
    names = set(inspect(temp_engine).get_table_names())
    assert {"candidates", "outreach"} <= names

    indexes = {idx["name"] for idx in inspect(temp_engine).get_indexes("outreach")}
    assert {"ix_outreach_candidate_id", "ix_outreach_send_status"} <= indexes


def test_repository_round_trip(session: Session) -> None:
    candidate_id = create_candidate(
        session,
        nick="test_player",
        source_club_id="club-1",
        rapid=1800,
        blitz=1750,
        bullet=1600,
        games_total=42,
        last_seen=1_700_000_000,
    )
    assert candidate_id > 0

    candidate = get_candidate(session, candidate_id)
    assert candidate is not None
    assert candidate["nick"] == "test_player"
    assert candidate["rapid"] == 1800
    assert candidate["status"] == "new"

    # Upsert on nick keeps the same row but refreshes ratings.
    same_id = create_candidate(session, nick="test_player", rapid=1900)
    assert same_id == candidate_id
    assert get_candidate(session, candidate_id)["rapid"] == 1900
    assert len(list_candidates(session)) == 1

    outreach_id = create_outreach(
        session,
        candidate_id=candidate_id,
        message_text="Hi! Join our team.",
        llm_model="openai/gpt-4o-mini",
    )
    row = get_outreach(session, outreach_id)
    assert row is not None
    assert row["send_status"] == "draft"
    assert row["reaction"] == "none"
    assert row["joined"] is False

    assert update_outreach(session, outreach_id, send_status="approved") is True
    assert get_outreach(session, outreach_id)["send_status"] == "approved"

    assert len(list_outreach(session, send_status="approved")) == 1
    assert list_outreach(session, send_status="sent") == []
    assert len(list_outreach(session)) == 1


def test_health_endpoint(tmp_path, monkeypatch) -> None:
    from app import app

    engine = build_engine(f"sqlite:///{tmp_path / 'health.db'}")
    monkeypatch.setattr(database_module, "engine", engine)

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    # The lifespan hook must have created the schema on the patched engine.
    assert "candidates" in inspect(engine).get_table_names()
    engine.dispose()


def test_metadata_has_no_extra_tables() -> None:
    assert set(metadata.tables) == {
        candidates.name,
        outreach.name,
        app_state.name,
    }
