"""Turn a source Lichess club's members into ranked, stored candidate rows.

The pipeline is deliberately split into pure steps so it can be tested without
network access:

1. :func:`fetch_team_members` -> member nicks of the source club.
2. :func:`fetch_perfs` -> ratings, game counts and last-seen timestamps.
3. :func:`fetch_candidates` -> filtered and ranked candidate dicts.
4. :func:`persist_candidates` -> upserted rows in the ``candidates`` table.
"""

from __future__ import annotations

import argparse
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config import settings
from backend.db import database
from backend.db.models import candidates as candidates_table
from backend.db.repository import create_candidate
from backend.lichess_api.client import build_session
from backend.lichess_api.teams import fetch_team_members
from backend.lichess_api.users import fetch_perfs

#: Rating keys in filter precedence order.
RATING_KEYS = ("rapid", "blitz", "bullet")

#: Keyword filters accepted by :func:`select_candidates`.
FILTER_KEYS = frozenset(
    {"min_rapid", "max_rapid", "min_games", "exclude_nicks", "limit"}
)


def effective_rating(candidate: dict) -> int | None:
    """Return the rating used for filtering and ranking.

    Rapid is the primary rating; blitz is used when rapid is missing, and
    bullet as the last fallback. ``None`` means the player has no usable
    rating at all, and such candidates are only kept when no rating bounds
    are configured.
    """
    for key in RATING_KEYS:
        value = candidate.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    return None


def _stats_for(nick: str, stats: dict[str, dict]) -> dict | None:
    """Look up per-nick stats, tolerating case differences in user IDs."""
    found = stats.get(nick)
    if found is not None:
        return found

    lowered = nick.lower()
    for key, value in stats.items():
        if key.lower() == lowered:
            return value
    return None


def _excluded_lower(exclude_nicks: set[str] | None) -> set[str]:
    return {nick.lower() for nick in exclude_nicks} if exclude_nicks else set()


def _passes_filters(
    candidate: dict,
    *,
    min_rapid: int | None,
    max_rapid: int | None,
    min_games: int,
    excluded: set[str],
) -> bool:
    if candidate["nick"].lower() in excluded:
        return False

    games_total = candidate.get("games_total") or 0
    if games_total < min_games:
        return False

    has_bounds = min_rapid is not None or max_rapid is not None
    rating = effective_rating(candidate)
    if not has_bounds:
        # Unrated players are only acceptable when nothing is being filtered.
        return True
    if rating is None:
        return False
    if min_rapid is not None and rating < min_rapid:
        return False
    return max_rapid is None or rating <= max_rapid


def _sort_key(candidate: dict) -> tuple[bool, int, str]:
    """Descending rating with ``None`` last, then nick ascending."""
    rating = effective_rating(candidate)
    return (rating is None, -(rating or 0), candidate["nick"])


def _filter_and_rank(
    items: list[dict],
    *,
    min_rapid: int | None,
    max_rapid: int | None,
    min_games: int,
    exclude_nicks: set[str] | None,
    limit: int | None,
) -> list[dict]:
    excluded = _excluded_lower(exclude_nicks)
    kept = [
        candidate
        for candidate in items
        if _passes_filters(
            candidate,
            min_rapid=min_rapid,
            max_rapid=max_rapid,
            min_games=min_games,
            excluded=excluded,
        )
    ]
    kept.sort(key=_sort_key)
    if limit is not None:
        kept = kept[: max(limit, 0)]
    return kept


def fetch_candidates(
    session: Any,
    source_club_id: str,
    *,
    min_rapid: int | None = None,
    max_rapid: int | None = None,
    min_games: int = 0,
    exclude_nicks: set[str] | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Fetch club members, enrich them with ratings and return ranked rows.

    Members missing from the ratings response are skipped. The result is
    sorted by :func:`effective_rating` descending (unrated last) and then by
    nick for stable ordering; ``limit`` is applied after sorting.
    """
    nicks = fetch_team_members(session, source_club_id)

    excluded = _excluded_lower(exclude_nicks)
    nicks = [nick for nick in nicks if nick.lower() not in excluded]
    if not nicks:
        return []

    stats = fetch_perfs(session, nicks)

    items: list[dict] = []
    for nick in nicks:
        user_stats = _stats_for(nick, stats)
        if user_stats is None:
            continue
        items.append(
            {
                "nick": nick,
                "rapid": user_stats.get("rapid"),
                "blitz": user_stats.get("blitz"),
                "bullet": user_stats.get("bullet"),
                "games_total": user_stats.get("games_total") or 0,
                "last_seen": user_stats.get("last_seen"),
            }
        )

    return _filter_and_rank(
        items,
        min_rapid=min_rapid,
        max_rapid=max_rapid,
        min_games=min_games,
        exclude_nicks=None,
        limit=limit,
    )


def persist_candidates(
    db_session: Session, candidates: list[dict], source_club_id: str
) -> int:
    """Upsert candidates into the database and return how many were processed.

    :func:`backend.db.repository.create_candidate` refreshes ratings on an
    existing nick and leaves its ``status`` untouched, so re-parsing a club
    never rewinds outreach progress.
    """
    for candidate in candidates:
        create_candidate(
            db_session,
            nick=candidate["nick"],
            source_club_id=source_club_id,
            rapid=candidate.get("rapid"),
            blitz=candidate.get("blitz"),
            bullet=candidate.get("bullet"),
            games_total=candidate.get("games_total") or 0,
            last_seen=candidate.get("last_seen"),
        )
    return len(candidates)


def parse_and_store(
    session: Any, db_session: Session, source_club_id: str, **filters: Any
) -> list[dict]:
    """Fetch candidates from Lichess, persist them and return the ranked list."""
    unknown = set(filters) - FILTER_KEYS
    if unknown:
        raise TypeError(f"unexpected filter(s): {', '.join(sorted(unknown))}")

    candidates = fetch_candidates(session, source_club_id, **filters)
    persist_candidates(db_session, candidates, source_club_id)
    return candidates


def select_candidates(
    db_session: Session, source_club_id: str, **filters: Any
) -> list[dict]:
    """Select already-stored candidates of a club, filtered and ranked in memory.

    This performs no network access and does not persist anything; use
    :func:`parse_and_store` to refresh rows from Lichess.
    """
    unknown = set(filters) - FILTER_KEYS
    if unknown:
        raise TypeError(f"unexpected filter(s): {', '.join(sorted(unknown))}")

    rows = db_session.execute(
        select(candidates_table).where(
            candidates_table.c.source_club_id == source_club_id
        )
    ).all()

    items = [dict(row._mapping) for row in rows]
    return _filter_and_rank(
        items,
        min_rapid=filters.get("min_rapid"),
        max_rapid=filters.get("max_rapid"),
        min_games=filters.get("min_games") or 0,
        exclude_nicks=filters.get("exclude_nicks"),
        limit=filters.get("limit"),
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.parser.candidates",
        description="Parse a Lichess club and store its members as candidates.",
    )
    parser.add_argument("--club", required=True, help="source club/team ID")
    parser.add_argument("--min-rapid", type=int, default=None)
    parser.add_argument("--max-rapid", type=int, default=None)
    parser.add_argument("--min-games", type=int, default=0)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--exclude-own",
        action="store_true",
        help="exclude members of settings.senpai_team_id",
    )
    return parser


def _print_summary(club_id: str, candidates: list[dict]) -> None:
    print(f"club: {club_id}")
    print(f"candidates: {len(candidates)}")
    if not candidates:
        return
    print(f"{'nick':<28} {'effective':>9} {'rapid':>6} {'blitz':>6} {'bullet':>6}")
    for candidate in candidates[:10]:
        rating = effective_rating(candidate)
        print(
            f"{candidate['nick']:<28} "
            f"{'-' if rating is None else rating:>9} "
            f"{'-' if candidate.get('rapid') is None else candidate['rapid']:>6} "
            f"{'-' if candidate.get('blitz') is None else candidate['blitz']:>6} "
            f"{'-' if candidate.get('bullet') is None else candidate['bullet']:>6}"
        )


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    session = build_session()
    exclude_nicks: set[str] = set()
    if args.exclude_own and settings.senpai_team_id:
        exclude_nicks = {
            nick.lower()
            for nick in fetch_team_members(session, settings.senpai_team_id)
        }

    database.init_db()
    db_session = database.SessionLocal()
    try:
        found = parse_and_store(
            session,
            db_session,
            args.club,
            min_rapid=args.min_rapid,
            max_rapid=args.max_rapid,
            min_games=args.min_games,
            exclude_nicks=exclude_nicks or None,
            limit=args.limit,
        )
    finally:
        db_session.close()

    _print_summary(args.club, found)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
