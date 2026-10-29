"""JSON API backing the mobile review UI.

Everything here is deliberately thin: the parser, LLM and sender modules own
the business logic, while the routes only compose them and shape JSON.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.analytics.conversion import (
    conversion_stats,
    dataset_csv_bytes,
    refresh_joined,
)
from backend.api.deps import require_admin
from backend.api.schemas import GenerateRequest, OutreachUpdate, ParseRequest
from backend.config import settings
from backend.db import repository
from backend.db.database import get_session
from backend.db.models import (
    CANDIDATE_STATUSES,
    REACTIONS,
    candidates,
    outreach,
)
from backend.lichess_api.client import build_session
from backend.lichess_api.teams import fetch_team_members
from backend.lichess_api.transport import LichessError
from backend.llm.generator import LLMError, fallback_message, generate_message
from backend.parser.candidates import parse_and_store
from backend.sender.queue import SendRefused, send_outreach

router = APIRouter(prefix="/api", dependencies=[Depends(require_admin)])

SessionDep = Annotated[Session, Depends(get_session)]


def _club_url() -> str:
    """Resolve the club link used by the offline fallback message."""
    return settings.club_url or f"https://lichess.org/team/{settings.senpai_team_id}"


def _count_by(session: Session, column: Any) -> dict[str, int]:
    rows = session.execute(select(column, func.count()).group_by(column)).all()
    return {str(value): int(count) for value, count in rows}


@router.get("/stats")
def get_stats(session: SessionDep) -> dict[str, Any]:
    """Aggregate counters for the dashboard.

    Outreach numbers come from :func:`conversion_stats`, so the dashboard and
    the CLI always agree (both use distinct candidates, not outreach rows).
    """
    candidates_by_status = {status: 0 for status in sorted(CANDIDATE_STATUSES)}
    candidates_by_status.update(_count_by(session, candidates.c.status))

    outreach_stats = conversion_stats(session)
    return {
        "candidates_total": int(
            session.execute(select(func.count()).select_from(candidates)).scalar_one()
        ),
        "candidates_by_status": candidates_by_status,
        "outreach_total": outreach_stats["outreach_total"],
        "outreach_by_send_status": outreach_stats["by_send_status"],
        "sent_total": outreach_stats["sent_total"],
        "joined_total": outreach_stats["joined_total"],
        "conversion_rate": outreach_stats["conversion_rate"],
    }


@router.get("/candidates")
def list_candidates(
    session: SessionDep,
    status: str | None = None,
    source_club_id: str | None = None,
) -> list[dict[str, Any]]:
    """List candidates, each with its most recent outreach row."""
    stmt = select(candidates).order_by(candidates.c.id)
    if status:
        stmt = stmt.where(candidates.c.status == status)
    if source_club_id:
        stmt = stmt.where(candidates.c.source_club_id == source_club_id)

    rows = [dict(row._mapping) for row in session.execute(stmt).all()]
    candidate_ids = {row["id"] for row in rows}

    # Iterating in ascending id order keeps the last (latest) outreach per candidate.
    latest: dict[int, dict[str, Any]] = {}
    for row in session.execute(select(outreach).order_by(outreach.c.id)).all():
        if row.candidate_id in candidate_ids:
            latest[row.candidate_id] = dict(row._mapping)

    return [{**row, "latest_outreach": latest.get(row["id"])} for row in rows]


@router.post("/parse")
def parse_club(
    payload: ParseRequest,
    session: SessionDep,
) -> dict[str, Any]:
    """Fetch a club's members from Lichess and store them as candidates."""
    api_session = build_session()
    try:
        exclude_nicks: set[str] | None = None
        if payload.exclude_own and settings.senpai_team_id:
            exclude_nicks = {
                nick.lower()
                for nick in fetch_team_members(api_session, settings.senpai_team_id)
            }

        parsed = parse_and_store(
            api_session,
            session,
            payload.club_id,
            min_rapid=payload.min_rapid,
            max_rapid=payload.max_rapid,
            min_games=payload.min_games,
            exclude_nicks=exclude_nicks,
            limit=payload.limit,
        )
    except LichessError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc
    finally:
        close = getattr(api_session, "close", None)
        if callable(close):
            close()

    return {"parsed": len(parsed), "candidates": parsed}


@router.post("/outreach/generate")
def generate_outreach(
    payload: GenerateRequest,
    session: SessionDep,
) -> dict[str, Any]:
    """Generate an invitation draft for a candidate, reusing an existing draft."""
    candidate = repository.get_candidate(session, payload.candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="candidate not found")

    llm_model = settings.llm_model
    try:
        message_text = generate_message(candidate)
    except LLMError:
        message_text = fallback_message(
            candidate,
            club_url=_club_url(),
            language=settings.message_language,
        )
        llm_model = "fallback"

    existing = session.execute(
        select(outreach)
        .where(
            outreach.c.candidate_id == payload.candidate_id,
            outreach.c.send_status == "draft",
        )
        .order_by(outreach.c.id.desc())
        .limit(1)
    ).first()

    if existing is not None:
        outreach_id = int(existing._mapping["id"])
        repository.update_outreach(
            session,
            outreach_id,
            message_text=message_text,
            llm_model=llm_model,
        )
    else:
        outreach_id = repository.create_outreach(
            session,
            candidate_id=payload.candidate_id,
            message_text=message_text,
            llm_model=llm_model,
        )

    return repository.get_outreach(session, outreach_id) or {}


@router.get("/outreach")
def list_outreach(
    session: SessionDep,
    send_status: str | None = None,
) -> list[dict[str, Any]]:
    """List outreach rows with the candidate's nick and ratings joined in."""
    stmt = (
        select(
            outreach,
            candidates.c.nick,
            candidates.c.rapid,
            candidates.c.blitz,
            candidates.c.bullet,
        )
        .join(candidates, outreach.c.candidate_id == candidates.c.id)
        .order_by(outreach.c.id)
    )
    if send_status:
        stmt = stmt.where(outreach.c.send_status == send_status)

    return [dict(row._mapping) for row in session.execute(stmt).all()]


@router.patch("/outreach/{outreach_id}")
def update_outreach(
    outreach_id: int,
    payload: OutreachUpdate,
    session: SessionDep,
) -> dict[str, Any]:
    """Update the human-editable fields of one outreach row."""
    existing = repository.get_outreach(session, outreach_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="outreach not found")

    fields = payload.model_dump(exclude_unset=True)
    if (
        "message_text" in fields
        and existing["send_status"] == "sent"
        and fields["message_text"] != existing["message_text"]
    ):
        raise HTTPException(
            status_code=409, detail="sent messages are immutable"
        )
    if "reaction" in fields:
        if fields["reaction"] is None:
            del fields["reaction"]
        elif fields["reaction"] not in REACTIONS:
            raise HTTPException(status_code=422, detail="invalid reaction")

    repository.update_outreach(session, outreach_id, **fields)
    return repository.get_outreach(session, outreach_id) or {}


#: Statuses that a human may move back to ``approved``.
_APPROVABLE_FROM = {
    "draft",
    "failed",
    "rejected",
    "skipped_400",
    "skipped_429",
    "skipped_joined",
}

#: Statuses a human may reject. A sent message is immutable history.
_REJECTABLE_FROM = {
    "draft",
    "approved",
    "failed",
    "rejected",
    "skipped_400",
    "skipped_429",
    "skipped_joined",
}


def _transition(
    session: Session,
    outreach_id: int,
    target: str,
    allowed_from: set[str],
) -> dict[str, Any]:
    row = repository.get_outreach(session, outreach_id)
    if row is None:
        raise HTTPException(status_code=404, detail="outreach not found")
    current = row["send_status"]
    if current == target:
        return row
    if current not in allowed_from:
        raise HTTPException(
            status_code=409,
            detail=f"cannot move outreach from {current} to {target}",
        )
    repository.update_outreach(
        session,
        outreach_id,
        send_status=target,
        send_error=None,
    )
    return repository.get_outreach(session, outreach_id) or {}


@router.post("/outreach/{outreach_id}/approve")
def approve_outreach(
    outreach_id: int,
    session: SessionDep,
) -> dict[str, Any]:
    """Mark an outreach message as approved for sending."""
    return _transition(session, outreach_id, "approved", _APPROVABLE_FROM)


@router.post("/outreach/{outreach_id}/reject")
def reject_outreach(
    outreach_id: int,
    session: SessionDep,
) -> dict[str, Any]:
    """Reject an outreach message so the sender skips it."""
    return _transition(session, outreach_id, "rejected", _REJECTABLE_FROM)


@router.post("/outreach/{outreach_id}/send")
def send_outreach_now(
    outreach_id: int,
    session: SessionDep,
) -> dict[str, Any]:
    """Send one outreach row, approving an untouched draft on the fly.

    The actual send goes through the same guarded sender used by the CLI:
    daily cap, 429 cooldown, pacing and duplicate protection all apply.
    """
    row = repository.get_outreach(session, outreach_id)
    if row is None:
        raise HTTPException(status_code=404, detail="outreach not found")

    current = row["send_status"]
    if current == "sent":
        raise HTTPException(status_code=409, detail="outreach already sent")
    if current == "sending":
        raise HTTPException(status_code=409, detail="outreach is being sent")
    if not (row["message_text"] or "").strip():
        raise HTTPException(status_code=422, detail="message text is empty")
    if current == "draft":
        repository.update_outreach(session, outreach_id, send_status="approved")
    elif current != "approved":
        # Rejected / failed / rate-limited rows need an explicit re-approval.
        raise HTTPException(status_code=400, detail="outreach is not approved")

    try:
        result = send_outreach(session, outreach_id)
    except SendRefused as exc:
        if exc.reason in {"daily_limit", "cooldown", "too_soon"}:
            headers = (
                {"Retry-After": str(exc.retry_after)}
                if exc.retry_after is not None
                else None
            )
            raise HTTPException(
                status_code=429,
                detail=f"send refused: {exc.reason}",
                headers=headers,
            ) from exc
        if exc.reason == "empty_text":
            raise HTTPException(
                status_code=422, detail="message text is empty"
            ) from exc
        raise HTTPException(
            status_code=409, detail=f"send refused: {exc.reason}"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "status": result.status.value,
        "error": result.error,
        "outreach": repository.get_outreach(session, outreach_id) or {},
    }


@router.post("/analytics/refresh")
def refresh_analytics(session: SessionDep) -> dict[str, Any]:
    """Check the configured team and mark candidates that have joined."""
    api_session = build_session()
    try:
        return refresh_joined(session, session=api_session)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LichessError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc
    finally:
        close = getattr(api_session, "close", None)
        if callable(close):
            close()


@router.get("/analytics/export.csv")
def export_analytics(session: SessionDep) -> Response:
    """Download the outreach dataset as a CSV attachment."""
    return Response(
        content=dataset_csv_bytes(session),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="dataset.csv"',
        },
    )
