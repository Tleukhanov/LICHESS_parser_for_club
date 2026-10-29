"""Send human-approved outreach messages with pacing and daily limits.

Every send goes through :func:`send_message`, which maps the Lichess API
response to a :class:`SendResult` and never raises for HTTP errors. This
module turns that result into the persisted ``send_status`` of the outreach
row:

===============  ==================  ====================================
SendStatus       outreach status      candidate status
===============  ==================  ====================================
SENT             ``sent``             ``messaged``
REJECTED         ``skipped_400``      unchanged
RATE_LIMITED     ``skipped_429``      unchanged
ERROR            ``failed``           unchanged
===============  ==================  ====================================

Only rows with ``send_status == "approved"`` are ever sent, so approval stays
a human decision.
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from backend.config import settings
from backend.db import database
from backend.db.models import candidates, outreach
from backend.db.repository import get_candidate, get_outreach, update_outreach
from backend.lichess_api.client import build_session
from backend.lichess_api.messaging import SendResult, SendStatus, send_message

#: Mapping of send outcomes to the persisted ``outreach.send_status``.
STATUS_FOR_RESULT = {
    SendStatus.SENT: "sent",
    SendStatus.REJECTED: "skipped_400",
    SendStatus.RATE_LIMITED: "skipped_429",
    SendStatus.ERROR: "failed",
}

#: Counter key in :func:`send_batch` for each outcome.
COUNTER_FOR_RESULT = {
    SendStatus.SENT: "sent",
    SendStatus.REJECTED: "rejected",
    SendStatus.RATE_LIMITED: "rate_limited",
    SendStatus.ERROR: "failed",
}


def _utcnow() -> datetime:
    """Naive UTC timestamp, matching the DateTime columns."""
    return datetime.now(UTC).replace(tzinfo=None)


def _to_naive_utc(value: datetime) -> datetime:
    """Normalize an optional aware datetime to the naive UTC stored in the DB."""
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def start_of_day_utc(now: datetime) -> datetime:
    """Return midnight UTC of the day containing ``now``.

    The result is a naive UTC datetime because the ``outreach.sent_at`` column
    is timezone-naive.
    """
    return _to_naive_utc(now).replace(hour=0, minute=0, second=0, microsecond=0)


def sent_today(db_session: Session, now: datetime | None = None) -> int:
    """Count outreach rows already sent since midnight UTC."""
    moment = _to_naive_utc(now) if now is not None else _utcnow()
    total = db_session.execute(
        select(func.count())
        .select_from(outreach)
        .where(
            outreach.c.send_status == "sent",
            outreach.c.sent_at.is_not(None),
            outreach.c.sent_at >= start_of_day_utc(moment),
        )
    ).scalar_one()
    return int(total)


def _mark_candidate_messaged(db_session: Session, candidate_id: int) -> None:
    db_session.execute(
        update(candidates)
        .where(candidates.c.id == candidate_id)
        .values(status="messaged", updated_at=_utcnow())
    )


def send_outreach(
    db_session: Session,
    outreach_id: int,
    *,
    session: Any | None = None,
    now: datetime | None = None,
) -> SendResult:
    """Send one approved outreach message and persist the outcome.

    Raises :class:`ValueError` when the row does not exist or is not approved.
    An already sent row is a no-op so the function is safe to retry.
    """
    row = get_outreach(db_session, outreach_id)
    if row is None:
        raise ValueError(f"outreach {outreach_id} not found")

    if row["send_status"] == "sent":
        return SendResult(SendStatus.SENT)
    if row["send_status"] != "approved":
        raise ValueError("outreach is not approved")

    candidate = get_candidate(db_session, row["candidate_id"])
    if candidate is None:
        raise ValueError(f"candidate {row['candidate_id']} not found")

    api_session = session if session is not None else build_session()
    result = send_message(
        api_session,
        candidate["nick"],
        row["message_text"] or "",
    )

    if result.status is SendStatus.SENT:
        update_outreach(
            db_session,
            outreach_id,
            send_status="sent",
            send_error=None,
            sent_at=_to_naive_utc(now) if now is not None else _utcnow(),
        )
        _mark_candidate_messaged(db_session, row["candidate_id"])
        db_session.commit()
    else:
        # Rejected, rate limited and hard errors keep the row out of the
        # approved queue and are never retried automatically.
        update_outreach(
            db_session,
            outreach_id,
            send_status=STATUS_FOR_RESULT[result.status],
            send_error=result.error,
        )

    return result


def send_batch(
    db_session: Session,
    *,
    session: Any | None = None,
    limit: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: datetime | None = None,
    interval: float | None = None,
) -> dict[str, Any]:
    """Send approved messages until the batch, the cap or a 429 stops it.

    ``limit`` only shrinks the batch; the daily cap always wins. Pacing uses
    ``settings.send_interval_seconds`` unless ``interval`` is given. On a
    ``429`` the remaining approved rows stay approved for a later run.
    """
    moment = _to_naive_utc(now) if now is not None else _utcnow()
    remaining = settings.daily_send_limit - sent_today(db_session, moment)
    counters: dict[str, Any] = {
        "sent": 0,
        "rejected": 0,
        "rate_limited": 0,
        "failed": 0,
        "stopped": None,
    }
    if remaining <= 0:
        counters["stopped"] = "daily_limit"
        return counters

    effective_limit = remaining if limit is None else min(limit, remaining)
    if effective_limit <= 0:
        return counters

    ids = db_session.execute(
        select(outreach.c.id)
        .where(outreach.c.send_status == "approved")
        .order_by(outreach.c.id)
        .limit(effective_limit)
    ).scalars().all()

    pause = interval if interval is not None else settings.send_interval_seconds
    for index, outreach_id in enumerate(ids):
        if index > 0:
            sleep(pause)

        result = send_outreach(db_session, int(outreach_id), session=session, now=moment)
        counters[COUNTER_FOR_RESULT[result.status]] += 1

        if result.status is SendStatus.RATE_LIMITED:
            counters["stopped"] = "rate_limited"
            break

    return counters


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.sender.queue",
        description="Send approved outreach messages with pacing.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--batch", action="store_true", help="send the approved queue")
    mode.add_argument("--outreach-id", type=int, help="send a single outreach row")
    parser.add_argument("--limit", type=int, default=None, help="max rows per batch")
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="seconds between sends (default: settings.send_interval_seconds)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    database.init_db()
    db_session = database.SessionLocal()
    api_session = build_session()
    try:
        if args.batch:
            print(
                send_batch(
                    db_session,
                    session=api_session,
                    limit=args.limit,
                    interval=args.interval,
                )
            )
        else:
            result = send_outreach(
                db_session,
                args.outreach_id,
                session=api_session,
            )
            print(asdict(result))
    finally:
        db_session.close()

    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
