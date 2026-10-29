"""Send human-approved outreach messages with pacing and safety guards.

Every send path (the web UI button, ``--outreach-id`` and ``--batch``) goes
through :func:`send_outreach`, so the daily cap, the ``429`` cooldown, the
inter-message pacing and the "one message per player" rule are enforced in one
place.

The persisted ``send_status`` transitions are:

===============  =========================  ==================
SendStatus       outreach status            candidate status
===============  =========================  ==================
SENT             ``sent``                   ``messaged``
REJECTED         ``skipped_400``            unchanged
RATE_LIMITED     ``skipped_429`` (+cooldown) unchanged
ERROR            ``failed``                 unchanged
===============  =========================  ==================

Rows are atomically claimed (``approved`` -> ``sending``) before the HTTP
request, which prevents a double submit from sending the same DM twice.
"""

from __future__ import annotations

import argparse
import math
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from backend.config import settings
from backend.db import database
from backend.db.models import candidates, outreach
from backend.db.repository import (
    get_candidate,
    get_outreach,
    get_state,
    has_sent_outreach,
    set_state,
    update_outreach,
)
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

#: ``app_state`` key holding the moment until which sending is paused after 429.
COOLDOWN_KEY = "send_cooldown_until"


class SendRefused(Exception):
    """Raised when a send is blocked before any HTTP request is made."""

    def __init__(self, reason: str, retry_after: int | None = None) -> None:
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(reason)


def _utcnow() -> datetime:
    """Naive UTC timestamp, matching the DateTime columns."""
    return datetime.now(UTC).replace(tzinfo=None)


def _to_naive_utc(value: datetime) -> datetime:
    """Normalize an optional aware datetime to the naive UTC stored in the DB."""
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def start_of_day_utc(now: datetime) -> datetime:
    """Return midnight UTC of the day containing ``now`` (naive UTC)."""
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


def latest_sent_at(db_session: Session) -> datetime | None:
    """Return the most recent successful send time, if any."""
    return db_session.execute(
        select(func.max(outreach.c.sent_at)).where(outreach.c.send_status == "sent")
    ).scalar_one_or_none()


def _cooldown_until(db_session: Session) -> datetime | None:
    raw = get_state(db_session, COOLDOWN_KEY)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _set_cooldown(db_session: Session, until: datetime) -> None:
    set_state(db_session, COOLDOWN_KEY, until.isoformat(sep=" "))


def _reset_stale_sending(db_session: Session, now: datetime) -> None:
    """Return abandoned ``sending`` rows to ``approved``.

    A row is abandoned when a worker claimed it but never recorded an outcome
    (crash, killed dyno). Without this, such rows would stay stuck forever.
    """
    cutoff = now - timedelta(seconds=settings.stale_sending_seconds)
    db_session.execute(
        update(outreach)
        .where(
            outreach.c.send_status == "sending",
            outreach.c.updated_at < cutoff,
        )
        .values(send_status="approved", updated_at=now)
    )
    db_session.commit()


def _claim(db_session: Session, outreach_id: int, now: datetime) -> bool:
    """Atomically move one row from ``approved`` to ``sending``."""
    result = db_session.execute(
        update(outreach)
        .where(
            outreach.c.id == outreach_id,
            outreach.c.send_status == "approved",
        )
        .values(send_status="sending", updated_at=now)
    )
    db_session.commit()
    return result.rowcount == 1


def _retry_after(until: datetime, now: datetime) -> int:
    return max(1, math.ceil((until - now).total_seconds()))


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
    enforce_pacing: bool = True,
) -> SendResult:
    """Send one approved outreach message and persist the outcome.

    Raises :class:`SendRefused` when a safety guard blocks the send and
    :class:`ValueError` when the row is missing or not approved. An already
    sent row is a no-op so the function is safe to retry.
    """
    row = get_outreach(db_session, outreach_id)
    if row is None:
        raise ValueError(f"outreach {outreach_id} not found")

    if row["send_status"] == "sent":
        return SendResult(SendStatus.SENT)
    if row["send_status"] == "sending":
        raise SendRefused("in_progress")
    if row["send_status"] != "approved":
        raise ValueError("outreach is not approved")

    candidate = get_candidate(db_session, row["candidate_id"])
    if candidate is None:
        raise ValueError(f"candidate {row['candidate_id']} not found")

    moment = _to_naive_utc(now) if now is not None else _utcnow()

    if candidate["status"] == "joined":
        update_outreach(
            db_session,
            outreach_id,
            send_status="skipped_joined",
            send_error="candidate already joined the club",
        )
        raise SendRefused("joined")

    text = (row["message_text"] or "").strip()
    if not text:
        raise SendRefused("empty_text")

    if has_sent_outreach(
        db_session, row["candidate_id"], exclude_id=outreach_id
    ):
        update_outreach(
            db_session,
            outreach_id,
            send_status="rejected",
            send_error="candidate already messaged",
        )
        raise SendRefused("already_sent")

    cooldown = _cooldown_until(db_session)
    if cooldown is not None and moment < cooldown:
        raise SendRefused("cooldown", _retry_after(cooldown, moment))

    if sent_today(db_session, moment) >= settings.daily_send_limit:
        raise SendRefused("daily_limit")

    if enforce_pacing:
        last = latest_sent_at(db_session)
        if last is not None:
            elapsed = (moment - last).total_seconds()
            if elapsed < settings.send_interval_seconds:
                raise SendRefused(
                    "too_soon",
                    max(1, math.ceil(settings.send_interval_seconds - elapsed)),
                )

    api_session = session
    owns_session = api_session is None
    if api_session is None:
        api_session = build_session()

    try:
        if not _claim(db_session, outreach_id, moment):
            raise SendRefused("in_progress")
        try:
            result = send_message(api_session, candidate["nick"], text)
        except Exception as exc:  # defensive: send_message maps HTTP errors itself
            update_outreach(
                db_session, outreach_id, send_status="failed", send_error=str(exc)
            )
            raise
    finally:
        if owns_session:
            close = getattr(api_session, "close", None)
            if callable(close):
                close()

    if result.status is SendStatus.SENT:
        update_outreach(
            db_session,
            outreach_id,
            send_status="sent",
            send_error=None,
            sent_at=moment,
        )
        if candidate["status"] != "joined":
            _mark_candidate_messaged(db_session, row["candidate_id"])
            db_session.commit()
    else:
        # Rejected, rate limited and hard errors leave the approved queue.
        update_outreach(
            db_session,
            outreach_id,
            send_status=STATUS_FOR_RESULT[result.status],
            send_error=result.error,
        )
        if result.status is SendStatus.RATE_LIMITED:
            _set_cooldown(
                db_session,
                moment
                + timedelta(seconds=settings.rate_limit_cooldown_seconds),
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

    The daily cap and the 429 cooldown are checked before the batch starts and
    again by :func:`send_outreach` for every message. Only one outreach row per
    candidate is ever attempted.
    """
    moment = _to_naive_utc(now) if now is not None else _utcnow()
    _reset_stale_sending(db_session, moment)

    counters: dict[str, Any] = {
        "sent": 0,
        "rejected": 0,
        "rate_limited": 0,
        "failed": 0,
        "stopped": None,
    }

    remaining = settings.daily_send_limit - sent_today(db_session, moment)
    if remaining <= 0:
        counters["stopped"] = "daily_limit"
        return counters

    cooldown = _cooldown_until(db_session)
    if cooldown is not None and moment < cooldown:
        counters["stopped"] = "cooldown"
        return counters

    candidate_rows = db_session.execute(
        select(outreach.c.id, outreach.c.candidate_id, candidates.c.status)
        .join(candidates, outreach.c.candidate_id == candidates.c.id)
        .where(outreach.c.send_status == "approved")
        .order_by(outreach.c.id)
    ).all()

    already_sent = set(
        db_session.execute(
            select(outreach.c.candidate_id).where(outreach.c.send_status == "sent")
        ).scalars()
    )

    selected: list[int] = []
    seen: set[int] = set()
    for outreach_id, candidate_id, candidate_status in candidate_rows:
        if candidate_status == "joined":
            update_outreach(
                db_session,
                int(outreach_id),
                send_status="skipped_joined",
                send_error="candidate already joined the club",
            )
            continue
        if candidate_id in already_sent:
            update_outreach(
                db_session,
                int(outreach_id),
                send_status="rejected",
                send_error="candidate already messaged",
            )
            continue
        if candidate_id in seen:
            update_outreach(
                db_session,
                int(outreach_id),
                send_status="rejected",
                send_error="duplicate approved row for candidate",
            )
            continue
        seen.add(candidate_id)
        selected.append(int(outreach_id))
        if len(selected) >= remaining:
            break

    effective_limit = remaining if limit is None else min(limit, remaining)
    if effective_limit <= 0:
        return counters
    selected = selected[:effective_limit]

    pause = interval if interval is not None else settings.send_interval_seconds
    for index, outreach_id in enumerate(selected):
        if index > 0:
            sleep(pause)

        try:
            result = send_outreach(
                db_session,
                outreach_id,
                session=session,
                now=moment,
                enforce_pacing=False,
            )
        except SendRefused as exc:
            if exc.reason in {"daily_limit", "cooldown", "too_soon"}:
                counters["stopped"] = exc.reason
                break
            counters["failed"] += 1
            continue

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
            try:
                result = send_outreach(
                    db_session,
                    args.outreach_id,
                    session=api_session,
                )
            except SendRefused as exc:
                print({"refused": exc.reason, "retry_after": exc.retry_after})
                return 1
            print(asdict(result))
    finally:
        db_session.close()
        close = getattr(api_session, "close", None)
        if callable(close):
            close()

    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
