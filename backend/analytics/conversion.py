"""Detect who joined the club and export the outreach dataset.

The Lichess team membership is the source of truth for conversion: a candidate
counts as joined when their nick appears in ``settings.senpai_team_id``. The
detection is idempotent, so it is safe to run it after every review session.
"""

from __future__ import annotations

import argparse
import csv
import io
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from backend.config import settings
from backend.db import database
from backend.db.models import REACTIONS, SEND_STATUSES, candidates, outreach
from backend.lichess_api.client import build_session
from backend.lichess_api.teams import fetch_team_members

DATASET_HEADER = (
    "nick",
    "source_club_id",
    "rapid",
    "blitz",
    "bullet",
    "games_total",
    "message_text",
    "llm_model",
    "reviewer_rating",
    "reviewer_notes",
    "send_status",
    "send_error",
    "sent_at",
    "reaction",
    "joined",
    "joined_at",
)


def _utcnow() -> datetime:
    """Naive UTC timestamp, matching the DateTime columns."""
    return datetime.now(UTC).replace(tzinfo=None)


def _to_naive_utc(value: datetime) -> datetime:
    """Normalize an optional aware datetime to the naive UTC stored in the DB."""
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _close_quietly(session: Any) -> None:
    close = getattr(session, "close", None)
    if callable(close):
        close()


def refresh_joined(
    db_session: Session,
    *,
    session: Any | None = None,
    team_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Mark candidates found in the team as joined.

    Every outreach row of a matching candidate gets ``joined=True`` and the
    candidate status becomes ``joined``. The function only counts candidates
    that were not marked before, which makes repeated runs idempotent.
    """
    resolved_team_id = team_id or settings.senpai_team_id
    if not resolved_team_id:
        raise ValueError("senpai_team_id is not configured")

    api_session = session
    owns_session = api_session is None
    if api_session is None:
        api_session = build_session()

    try:
        member_nicks = fetch_team_members(api_session, resolved_team_id)
    finally:
        if owns_session:
            _close_quietly(api_session)

    members = {nick.lower() for nick in member_nicks}
    timestamp = (
        _to_naive_utc(now) if now is not None else _utcnow()
    )

    candidate_rows = db_session.execute(select(candidates)).all()

    newly_joined = 0
    for candidate in candidate_rows:
        if candidate.nick.lower() not in members:
            continue

        if candidate.status != "joined":
            newly_joined += 1
            db_session.execute(
                update(candidates)
                .where(candidates.c.id == candidate.id)
                .values(status="joined", updated_at=timestamp)
            )

        # Only messages that were actually sent count towards conversion: a
        # draft or a rejected message must not look like a successful invite.
        db_session.execute(
            update(outreach)
            .where(
                outreach.c.candidate_id == candidate.id,
                outreach.c.send_status == "sent",
                outreach.c.joined.is_(False),
            )
            .values(joined=True, joined_at=timestamp, updated_at=timestamp)
        )

    db_session.commit()

    total_joined = int(
        db_session.execute(
            select(func.count(func.distinct(outreach.c.candidate_id))).where(
                outreach.c.joined.is_(True),
                outreach.c.send_status == "sent",
            )
        ).scalar_one()
    )

    return {
        "team_id": resolved_team_id,
        "checked": len(members),
        "newly_joined": newly_joined,
        "total_joined": total_joined,
    }


def _count_rows(db_session: Session, stmt: Any) -> int:
    return int(db_session.execute(stmt).scalar_one())


def conversion_stats(db_session: Session) -> dict[str, Any]:
    """Aggregate counters used to measure which messages convert."""
    outreach_total = _count_rows(
        db_session, select(func.count()).select_from(outreach)
    )
    sent_total = _count_rows(
        db_session,
        select(func.count(func.distinct(outreach.c.candidate_id))).where(
            outreach.c.send_status == "sent"
        ),
    )
    joined_total = _count_rows(
        db_session,
        select(func.count(func.distinct(outreach.c.candidate_id))).where(
            outreach.c.joined.is_(True),
            outreach.c.send_status == "sent",
        ),
    )

    by_send_status = {status: 0 for status in sorted(SEND_STATUSES)}
    for value, count in db_session.execute(
        select(outreach.c.send_status, func.count()).group_by(
            outreach.c.send_status
        )
    ).all():
        by_send_status[str(value)] = int(count)

    by_reaction = {reaction: 0 for reaction in sorted(REACTIONS)}
    for value, count in db_session.execute(
        select(outreach.c.reaction, func.count()).group_by(outreach.c.reaction)
    ).all():
        by_reaction[str(value)] = int(count)

    conversion_rate = (
        round(joined_total / sent_total, 4) if sent_total else 0.0
    )

    return {
        "outreach_total": outreach_total,
        "sent_total": sent_total,
        "joined_total": joined_total,
        "conversion_rate": conversion_rate,
        "by_send_status": by_send_status,
        "by_reaction": by_reaction,
    }


def _dataset_rows(db_session: Session) -> list[list[Any]]:
    """Return CSV-ready rows, one per outreach record, ordered by id."""
    stmt = (
        select(
            candidates.c.nick,
            candidates.c.source_club_id,
            candidates.c.rapid,
            candidates.c.blitz,
            candidates.c.bullet,
            candidates.c.games_total,
            outreach.c.message_text,
            outreach.c.llm_model,
            outreach.c.reviewer_rating,
            outreach.c.reviewer_notes,
            outreach.c.send_status,
            outreach.c.send_error,
            outreach.c.sent_at,
            outreach.c.reaction,
            outreach.c.joined,
            outreach.c.joined_at,
        )
        .select_from(
            # Keep every outreach row even if its candidate is missing.
            outreach.outerjoin(
                candidates, outreach.c.candidate_id == candidates.c.id
            )
        )
        .order_by(outreach.c.id)
    )

    rows: list[list[Any]] = []
    for row in db_session.execute(stmt).all():
        rows.append([_csv_value(value) for value in row])
    return rows


#: Leading characters that spreadsheet apps treat as formulas.
_CSV_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, str) and value.startswith(_CSV_INJECTION_PREFIXES):
        # Neutralize CSV formula injection when the dataset is opened in Excel.
        return "'" + value
    return value


def _write_csv(rows: Iterable[Iterable[Any]], buffer: Any) -> None:
    writer = csv.writer(buffer)
    writer.writerow(DATASET_HEADER)
    writer.writerows(rows)


def export_dataset(db_session: Session, path: str | Path) -> int:
    """Write the outreach dataset to a UTF-8 CSV file and return its row count."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    rows = _dataset_rows(db_session)
    with target.open("w", encoding="utf-8", newline="") as handle:
        _write_csv(rows, handle)
    return len(rows)


def dataset_csv_bytes(db_session: Session) -> bytes:
    """Return the outreach dataset as UTF-8 CSV bytes for an HTTP download."""
    buffer = io.StringIO(newline="")
    _write_csv(_dataset_rows(db_session), buffer)
    return buffer.getvalue().encode("utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.analytics.conversion",
        description="Track conversions and export the outreach dataset.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--refresh-joined",
        action="store_true",
        help="check the senpai team and mark joined candidates",
    )
    mode.add_argument(
        "--export",
        metavar="PATH",
        help="write the dataset CSV to PATH",
    )
    mode.add_argument(
        "--stats",
        action="store_true",
        help="print conversion statistics",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    database.init_db()
    db_session = database.SessionLocal()
    try:
        if args.refresh_joined:
            print(refresh_joined(db_session))
        elif args.export:
            print(export_dataset(db_session, args.export))
        else:
            print(conversion_stats(db_session))
    finally:
        db_session.close()

    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
