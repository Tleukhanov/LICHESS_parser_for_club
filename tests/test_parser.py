"""Tests for the club parser and candidate selector (no network access)."""

from collections.abc import Iterator

import pytest
import requests
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from backend.db.database import build_engine, init_db
from backend.db.models import candidates as candidates_table
from backend.parser import candidates as parser
from backend.parser.candidates import (
    build_arg_parser,
    effective_rating,
    fetch_candidates,
    parse_and_store,
    persist_candidates,
    select_candidates,
)

CLUB = "source-club"


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Fail loudly if a test reaches the real ``requests`` transport."""

    def _forbidden(*args, **kwargs):
        raise AssertionError("real network call attempted")

    monkeypatch.setattr(requests.Session, "request", _forbidden)
    monkeypatch.setattr(requests.Session, "send", _forbidden)


@pytest.fixture()
def db_session(tmp_path) -> Iterator[Session]:
    engine = build_engine(f"sqlite:///{tmp_path / 'parser.db'}")
    init_db(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _stats(
    rapid: int | None = None,
    blitz: int | None = None,
    bullet: int | None = None,
    games_total: int = 0,
    last_seen: int | None = None,
) -> dict:
    return {
        "rapid": rapid,
        "blitz": blitz,
        "bullet": bullet,
        "games_total": games_total,
        "last_seen": last_seen,
    }


def _patch_api(monkeypatch, members: list[str], stats: dict[str, dict]) -> dict:
    """Replace the two API calls and record their arguments."""
    calls: dict = {}

    def fake_members(session, team_id):
        calls["team_id"] = team_id
        return list(members)

    def fake_perfs(session, nicks):
        calls["nicks"] = list(nicks)
        return dict(stats)

    monkeypatch.setattr(parser, "fetch_team_members", fake_members)
    monkeypatch.setattr(parser, "fetch_perfs", fake_perfs)
    return calls


def test_effective_rating_prefers_rapid_then_blitz_then_bullet() -> None:
    assert effective_rating({"rapid": 1800, "blitz": 1900, "bullet": 2000}) == 1800
    assert effective_rating({"rapid": None, "blitz": 1700, "bullet": 2000}) == 1700
    assert effective_rating({"rapid": None, "blitz": None, "bullet": 1600}) == 1600
    assert effective_rating({"rapid": None, "blitz": None, "bullet": None}) is None
    assert effective_rating({}) is None


def test_fetch_candidates_filters_by_rating_bounds_and_games(monkeypatch) -> None:
    members = ["Alice", "Bob", "Carol", "Dave"]
    stats = {
        "Alice": _stats(rapid=1800, games_total=100),
        "Bob": _stats(rapid=1200, games_total=100),
        "Carol": _stats(rapid=1600, games_total=5),
        "Dave": _stats(rapid=2200, games_total=100),
    }
    calls = _patch_api(monkeypatch, members, stats)

    result = fetch_candidates(
        object(), CLUB, min_rapid=1500, max_rapid=2000, min_games=10
    )

    assert [item["nick"] for item in result] == ["Alice"]
    assert calls["team_id"] == CLUB
    assert calls["nicks"] == members


def test_min_games_filters_without_rating_bounds(monkeypatch) -> None:
    stats = {
        "Newbie": _stats(games_total=3),
        "Veteran": _stats(games_total=50),
    }
    _patch_api(monkeypatch, ["Newbie", "Veteran"], stats)

    result = fetch_candidates(object(), CLUB, min_games=10)

    assert [item["nick"] for item in result] == ["Veteran"]


def test_unrated_kept_only_without_rating_bounds(monkeypatch) -> None:
    stats = {
        "Unrated": _stats(games_total=20),
        "Rated": _stats(rapid=1500, games_total=20),
    }
    _patch_api(monkeypatch, ["Unrated", "Rated"], stats)

    unbounded = fetch_candidates(object(), CLUB)
    assert {item["nick"] for item in unbounded} == {"Unrated", "Rated"}

    bounded = fetch_candidates(object(), CLUB, min_rapid=1000)
    assert [item["nick"] for item in bounded] == ["Rated"]


def test_exclude_nicks_is_case_insensitive(monkeypatch) -> None:
    members = ["Alice", "Bob", "Carol"]
    stats = {nick: _stats(rapid=1500) for nick in members}
    calls = _patch_api(monkeypatch, members, stats)

    result = fetch_candidates(object(), CLUB, exclude_nicks={"ALICE", "bob"})

    assert [item["nick"] for item in result] == ["Carol"]
    # Excluded nicks are dropped before the ratings lookup.
    assert calls["nicks"] == ["Carol"]


def test_sorted_by_rating_desc_then_nick_and_limit_applied_after_sorting(
    monkeypatch,
) -> None:
    stats = {
        "zed": _stats(rapid=1700),
        "amy": _stats(rapid=1700),
        "Bob": _stats(blitz=1500),
        "Unrated": _stats(games_total=10),
    }
    _patch_api(monkeypatch, ["Unrated", "Bob", "zed", "amy"], stats)

    full = fetch_candidates(object(), CLUB)
    assert [item["nick"] for item in full] == ["amy", "zed", "Bob", "Unrated"]

    limited = fetch_candidates(object(), CLUB, limit=2)
    assert [item["nick"] for item in limited] == ["amy", "zed"]
    assert [effective_rating(item) for item in limited] == [1700, 1700]
    assert fetch_candidates(object(), CLUB, limit=0) == []


def test_members_missing_from_stats_are_skipped(monkeypatch) -> None:
    stats = {"Alice": _stats(rapid=1600)}
    _patch_api(monkeypatch, ["Alice", "Ghost"], stats)

    result = fetch_candidates(object(), CLUB)

    assert [item["nick"] for item in result] == ["Alice"]


def test_empty_club_skips_ratings_lookup(monkeypatch) -> None:
    calls = _patch_api(monkeypatch, [], {})

    assert fetch_candidates(object(), CLUB, exclude_nicks={"anyone"}) == []
    assert "nicks" not in calls


def test_persist_candidates_inserts_then_upserts_preserving_status(
    db_session: Session,
) -> None:
    first = [
        {
            "nick": "Alice",
            "rapid": 1800,
            "blitz": 1750,
            "bullet": 1700,
            "games_total": 100,
            "last_seen": 1_700_000_000,
        },
        {
            "nick": "Bob",
            "rapid": 1500,
            "blitz": None,
            "bullet": None,
            "games_total": 30,
            "last_seen": None,
        },
    ]

    assert persist_candidates(db_session, first, CLUB) == 2
    rows = db_session.execute(
        select(candidates_table).order_by(candidates_table.c.id)
    ).all()
    assert [row.nick for row in rows] == ["Alice", "Bob"]
    assert rows[0].status == "new"
    assert rows[0].source_club_id == CLUB

    # A human reviewer moves Alice forward; re-parsing must not reset that.
    db_session.execute(
        update(candidates_table)
        .where(candidates_table.c.nick == "Alice")
        .values(status="messaged")
    )
    db_session.commit()

    refreshed = [dict(first[0], rapid=1850), dict(first[1])]
    assert persist_candidates(db_session, refreshed, CLUB) == 2

    rows = db_session.execute(
        select(candidates_table).order_by(candidates_table.c.id)
    ).all()
    assert len(rows) == 2
    assert rows[0].id != rows[1].id
    assert rows[0].nick == "Alice"
    assert rows[0].rapid == 1850
    assert rows[0].status == "messaged"
    assert rows[1].status == "new"


def test_parse_and_store_persists_and_returns_ranked_candidates(
    db_session: Session, monkeypatch
) -> None:
    stats = {
        "Alice": _stats(rapid=1900, games_total=50),
        "Bob": _stats(rapid=1400, games_total=50),
    }
    _patch_api(monkeypatch, ["Bob", "Alice"], stats)

    result = parse_and_store(object(), db_session, CLUB, min_rapid=1000)

    assert [item["nick"] for item in result] == ["Alice", "Bob"]
    stored = db_session.execute(
        select(func.count()).select_from(candidates_table)
    ).scalar_one()
    assert stored == 2

    # select_candidates reads stored rows without touching the network.
    selected = select_candidates(db_session, CLUB, min_rapid=1800)
    assert [row["nick"] for row in selected] == ["Alice"]


def test_select_candidates_unknown_filter_raises(db_session: Session) -> None:
    with pytest.raises(TypeError):
        select_candidates(db_session, CLUB, min_rating=1500)

    with pytest.raises(TypeError):
        parse_and_store(object(), db_session, CLUB, min_rating=1500)


def test_cli_parser_defaults() -> None:
    args = build_arg_parser().parse_args(["--club", "my-club"])

    assert args.club == "my-club"
    assert args.min_rapid is None
    assert args.max_rapid is None
    assert args.min_games == 0
    assert args.limit is None
    assert args.exclude_own is False
