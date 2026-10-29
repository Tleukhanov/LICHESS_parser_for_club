"""HTTP API tests: auth, CRUD flows, stats and the static mount.

Every external side effect (Lichess, LLM, sender) is monkeypatched, so these
tests never touch the network.
"""

from collections.abc import Iterator

import pytest
import requests
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from backend.api import routes
from backend.config import settings
from backend.db import database as database_module
from backend.db.database import build_engine, get_session, init_db
from backend.db.repository import (
    create_candidate,
    create_outreach,
    get_outreach,
    list_outreach,
    update_outreach,
)
from backend.lichess_api.messaging import SendResult, SendStatus


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Fail loudly if a test reaches the real ``requests`` transport."""

    def _forbidden(*args, **kwargs):
        raise AssertionError("real network call attempted")

    monkeypatch.setattr(requests.Session, "request", _forbidden)
    monkeypatch.setattr(requests.Session, "send", _forbidden)


@pytest.fixture()
def temp_engine(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path / 'api.db'}")
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def session_factory(temp_engine):
    return sessionmaker(bind=temp_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def db(session_factory) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(temp_engine, session_factory, monkeypatch) -> Iterator[TestClient]:
    """Test client wired to a temp sqlite DB and a tokenless admin API."""
    from app import app

    monkeypatch.setattr(database_module, "engine", temp_engine)
    monkeypatch.setattr(database_module, "SessionLocal", session_factory)
    monkeypatch.setattr(settings, "admin_token", "")
    monkeypatch.setattr(settings, "senpai_team_id", "")

    def override_get_session() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = override_get_session
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


def test_health_and_static_mount_do_not_shadow_api(client: TestClient) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    assert client.get("/api/stats").status_code == 200

    index = client.get("/")
    assert index.status_code == 200
    assert "Приглашения в клуб" in index.text

    assert client.get("/app.js").status_code == 200
    assert client.get("/style.css").status_code == 200


def test_admin_token_is_required_when_configured(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "admin_token", "secret")

    assert client.get("/api/stats").status_code == 401
    assert client.get("/api/stats", headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert client.get("/api/stats", headers={"X-Admin-Token": "secret"}).status_code == 200
    assert client.get("/api/stats?token=secret").status_code == 200
    # The health check stays public even when the API is protected.
    assert client.get("/health").status_code == 200


class FakeApiSession:
    """Stand-in for ``requests.Session`` returned by ``build_session``."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_parse_calls_parser_and_returns_count(
    client: TestClient, monkeypatch
) -> None:
    api_session = FakeApiSession()
    calls: dict = {}

    monkeypatch.setattr(routes, "build_session", lambda: api_session)

    def fake_parse(api, db_session, club_id, **filters):
        calls["api"] = api
        calls["club_id"] = club_id
        calls["filters"] = filters
        return [{"nick": "alice", "rapid": 1500}, {"nick": "bob", "rapid": 1400}]

    monkeypatch.setattr(routes, "parse_and_store", fake_parse)

    response = client.post(
        "/api/parse",
        json={
            "club_id": "club-1",
            "min_rapid": 1200,
            "max_rapid": 2000,
            "min_games": 5,
            "limit": 10,
            "exclude_own": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["parsed"] == 2
    assert [row["nick"] for row in response.json()["candidates"]] == ["alice", "bob"]
    assert calls["api"] is api_session
    assert calls["club_id"] == "club-1"
    assert calls["filters"]["min_rapid"] == 1200
    assert calls["filters"]["max_rapid"] == 2000
    assert calls["filters"]["min_games"] == 5
    assert calls["filters"]["limit"] == 10
    assert calls["filters"]["exclude_nicks"] is None
    assert api_session.closed is True


def test_parse_excludes_own_team_members(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "senpai_team_id", "senpai")
    monkeypatch.setattr(routes, "build_session", lambda: FakeApiSession())
    monkeypatch.setattr(
        routes, "fetch_team_members", lambda session, club: ["Alice", "BOB"]
    )

    calls: dict = {}

    def fake_parse(api, db_session, club_id, **filters):
        calls.update(filters)
        return []

    monkeypatch.setattr(routes, "parse_and_store", fake_parse)

    response = client.post("/api/parse", json={"club_id": "club-1"})

    assert response.status_code == 200
    assert response.json() == {"parsed": 0, "candidates": []}
    assert calls["exclude_nicks"] == {"alice", "bob"}


def test_generate_creates_draft_then_updates_it(
    client: TestClient, db: Session, monkeypatch
) -> None:
    candidate_id = create_candidate(db, nick="alice", rapid=1500, games_total=20)
    monkeypatch.setattr(
        routes, "generate_message", lambda candidate: f"Hello {candidate['nick']}"
    )

    first = client.post("/api/outreach/generate", json={"candidate_id": candidate_id})
    assert first.status_code == 200
    draft = first.json()
    assert draft["candidate_id"] == candidate_id
    assert draft["send_status"] == "draft"
    assert draft["message_text"] == "Hello alice"
    assert draft["llm_model"] == settings.llm_model

    second = client.post("/api/outreach/generate", json={"candidate_id": candidate_id})
    assert second.status_code == 200
    assert second.json()["id"] == draft["id"]
    assert len(list_outreach(db)) == 1

    missing = client.post("/api/outreach/generate", json={"candidate_id": 9999})
    assert missing.status_code == 404


def test_generate_uses_fallback_on_llm_error(
    client: TestClient, db: Session, monkeypatch
) -> None:
    candidate_id = create_candidate(db, nick="bob", rapid=1400)

    def boom(candidate):
        raise routes.LLMError(500, "boom")

    monkeypatch.setattr(routes, "generate_message", boom)
    monkeypatch.setattr(
        routes, "fallback_message", lambda candidate, **kwargs: "fallback text"
    )

    response = client.post("/api/outreach/generate", json={"candidate_id": candidate_id})

    assert response.status_code == 200
    assert response.json()["message_text"] == "fallback text"
    assert response.json()["llm_model"] == "fallback"


def test_patch_outreach_validates_and_updates(
    client: TestClient, db: Session
) -> None:
    candidate_id = create_candidate(db, nick="carol")
    outreach_id = create_outreach(db, candidate_id, "Draft text")

    response = client.patch(
        f"/api/outreach/{outreach_id}",
        json={
            "message_text": "Edited text",
            "reviewer_rating": 5,
            "reviewer_notes": "Looks good",
            "reaction": "replied",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["message_text"] == "Edited text"
    assert body["reviewer_rating"] == 5
    assert body["reviewer_notes"] == "Looks good"
    assert body["reaction"] == "replied"

    assert client.patch(f"/api/outreach/{outreach_id}", json={"reviewer_rating": 6}).status_code == 422
    assert client.patch(f"/api/outreach/{outreach_id}", json={"reviewer_rating": 0}).status_code == 422
    assert client.patch(f"/api/outreach/{outreach_id}", json={"reaction": "bogus"}).status_code == 422
    assert client.patch("/api/outreach/9999", json={"reviewer_rating": 4}).status_code == 404

    # Failed patches must not change the stored row.
    stored = get_outreach(db, outreach_id)
    assert stored["reviewer_rating"] == 5
    assert stored["reaction"] == "replied"


def test_approve_and_reject(client: TestClient, db: Session) -> None:
    candidate_id = create_candidate(db, nick="dave")
    outreach_id = create_outreach(db, candidate_id, "Hi")

    approved = client.post(f"/api/outreach/{outreach_id}/approve")
    assert approved.status_code == 200
    assert approved.json()["send_status"] == "approved"

    rejected = client.post(f"/api/outreach/{outreach_id}/reject")
    assert rejected.status_code == 200
    assert rejected.json()["send_status"] == "rejected"

    assert client.post("/api/outreach/9999/approve").status_code == 404
    assert client.post("/api/outreach/9999/reject").status_code == 404


def test_send_approves_draft_and_returns_mocked_status(
    client: TestClient, db: Session, monkeypatch
) -> None:
    candidate_id = create_candidate(db, nick="erin")
    outreach_id = create_outreach(db, candidate_id, "Hi")

    seen: dict = {}

    def fake_send(db_session, target_id):
        seen["status_before"] = get_outreach(db_session, target_id)["send_status"]
        seen["id"] = target_id
        update_outreach(db_session, target_id, send_status="sent")
        return SendResult(SendStatus.SENT)

    monkeypatch.setattr(routes, "send_outreach", fake_send)

    response = client.post(f"/api/outreach/{outreach_id}/send")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "sent"
    assert body["error"] is None
    assert body["outreach"]["send_status"] == "sent"
    assert seen == {"status_before": "approved", "id": outreach_id}


def test_send_refuses_rejected_row(
    client: TestClient, db: Session, monkeypatch
) -> None:
    candidate_id = create_candidate(db, nick="frank")
    outreach_id = create_outreach(db, candidate_id, "Hi")
    update_outreach(db, outreach_id, send_status="rejected")

    def fake_send(db_session, target_id):
        raise AssertionError("sender must not run for a rejected row")

    monkeypatch.setattr(routes, "send_outreach", fake_send)

    assert client.post(f"/api/outreach/{outreach_id}/send").status_code == 400
    assert client.post("/api/outreach/9999/send").status_code == 404


def test_stats_shape(client: TestClient, db: Session) -> None:
    first = create_candidate(db, nick="gina")
    create_candidate(db, nick="hans")

    sent_id = create_outreach(db, first, "Hi")
    update_outreach(db, sent_id, send_status="sent", joined=True)
    create_outreach(db, first, "Second try")

    response = client.get("/api/stats")
    assert response.status_code == 200
    stats = response.json()

    assert stats["candidates_total"] == 2
    assert stats["candidates_by_status"]["new"] == 2
    assert stats["outreach_total"] == 2
    assert stats["outreach_by_send_status"]["sent"] == 1
    assert stats["outreach_by_send_status"]["draft"] == 1
    assert stats["sent_total"] == 1
    assert stats["joined_total"] == 1
    assert stats["conversion_rate"] == 1.0


def test_candidates_list_includes_latest_outreach(
    client: TestClient, db: Session
) -> None:
    candidate_id = create_candidate(
        db, nick="eve", source_club_id="club-1", rapid=1800, blitz=1700
    )
    other_id = create_candidate(db, nick="frank", source_club_id="club-2")

    first = create_outreach(db, candidate_id, "first")
    update_outreach(db, first, send_status="rejected")
    second = create_outreach(db, candidate_id, "second")

    rows = client.get("/api/candidates").json()
    assert len(rows) == 2

    row = next(item for item in rows if item["id"] == candidate_id)
    assert row["nick"] == "eve"
    assert row["rapid"] == 1800
    assert row["status"] == "new"
    assert row["latest_outreach"]["id"] == second
    assert row["latest_outreach"]["message_text"] == "second"

    other = next(item for item in rows if item["id"] == other_id)
    assert other["latest_outreach"] is None

    filtered = client.get(
        "/api/candidates", params={"status": "new", "source_club_id": "club-1"}
    ).json()
    assert [item["id"] for item in filtered] == [candidate_id]


def test_list_outreach_joins_candidate_data(
    client: TestClient, db: Session
) -> None:
    candidate_id = create_candidate(
        db, nick="gina", rapid=1700, blitz=1600, bullet=1500
    )
    draft_id = create_outreach(db, candidate_id, "Hi")

    other_id = create_candidate(db, nick="hans")
    sent_id = create_outreach(db, other_id, "Bye")
    update_outreach(db, sent_id, send_status="sent")

    rows = client.get("/api/outreach", params={"send_status": "draft"}).json()
    assert [row["id"] for row in rows] == [draft_id]
    assert rows[0]["nick"] == "gina"
    assert rows[0]["rapid"] == 1700
    assert rows[0]["blitz"] == 1600
    assert rows[0]["bullet"] == 1500

    assert len(client.get("/api/outreach").json()) == 2
