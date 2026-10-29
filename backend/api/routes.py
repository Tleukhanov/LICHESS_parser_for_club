"""JSON API backing the mobile review UI.

Everything here is deliberately thin: the parser, LLM and sender modules own
the business logic, while the routes only compose them and shape JSON.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.api.deps import require_admin
from backend.api.schemas import GenerateRequest, OutreachUpdate, ParseRequest
from backend.config import settings
from backend.db import repository
from backend.db.database import get_session
from backend.db.models import (
    CANDIDATE_STATUSES,
    REACTIONS,
    SEND_STATUSES,
    candidates,
    outreach,
)
from backend.lichess_api.client import build_session
from backend.lichess_api.teams import fetch_team_members
from backend.llm.generator import LLMError, fallback_message, generate_message
from backend.parser.candidates import parse_and_store
from backend.sender.queue import send_outreach

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
    """Aggregate counters for the dashboard."""
    candidates_by_status = {status: 0 for status in sorted(CANDIDATE_STATUSES)}
    candidates_by_status.update(_count_by(session, candidates.c.status))

    outreach_by_send_status = {status: 0 for status in sorted(SEND_STATUSES)}
    outreach_by_send_status.update(_count_by(session, outreach.c.send_status))

    sent_total = int(
        session.execute(
            select(func.count())
            .select_from(outreach)
            .where(outreach.c.send_status == "sent")
        ).scalar_one()
    )
    joined_total = int(
        session.execute(
            select(func.count())
            .select_from(outreach)
            .where(outreach.c.joined.is_(True))
        ).scalar_one()
    )
    conversion_rate = round(joined_total / sent_total, 4) if sent_total else 0.0

    return {
        "candidates_total": int(
            session.execute(select(func.count()).select_from(candidates)).scalar_one()
        ),
        "candidates_by_status": candidates_by_status,
        "outreach_total": int(
            session.execute(select(func.count()).select_from(outreach)).scalar_one()
        ),
        "outreach_by_send_status": outreach_by_send_status,
        "sent_total": sent_total,
        "joined_total": joined_total,
        "conversion_rate": conversion_rate,
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
    if repository.get_outreach(session, outreach_id) is None:
        raise HTTPException(status_code=404, detail="outreach not found")

    fields = payload.model_dump(exclude_unset=True)
    if "reaction" in fields:
        if fields["reaction"] is None:
            del fields["reaction"]
        elif fields["reaction"] not in REACTIONS:
            raise HTTPException(status_code=422, detail="invalid reaction")

    repository.update_outreach(session, outreach_id, **fields)
    return repository.get_outreach(session, outreach_id) or {}


def _set_send_status(
    session: Session, outreach_id: int, send_status: str
) -> dict[str, Any]:
    if repository.get_outreach(session, outreach_id) is None:
        raise HTTPException(status_code=404, detail="outreach not found")
    repository.update_outreach(
        session,
        outreach_id,
        send_status=send_status,
        send_error=None,
    )
    return repository.get_outreach(session, outreach_id) or {}


@router.post("/outreach/{outreach_id}/approve")
def approve_outreach(
    outreach_id: int,
    session: SessionDep,
) -> dict[str, Any]:
    """Mark an outreach message as approved for sending."""
    return _set_send_status(session, outreach_id, "approved")


@router.post("/outreach/{outreach_id}/reject")
def reject_outreach(
    outreach_id: int,
    session: SessionDep,
) -> dict[str, Any]:
    """Reject an outreach message so the sender skips it."""
    return _set_send_status(session, outreach_id, "rejected")


@router.post("/outreach/{outreach_id}/send")
def send_outreach_now(
    outreach_id: int,
    session: SessionDep,
) -> dict[str, Any]:
    """Send one outreach row, approving an untouched draft on the fly."""
    row = repository.get_outreach(session, outreach_id)
    if row is None:
        raise HTTPException(status_code=404, detail="outreach not found")

    if row["send_status"] == "draft":
        repository.update_outreach(session, outreach_id, send_status="approved")
    elif row["send_status"] != "approved":
        raise HTTPException(status_code=400, detail="outreach is not approved")

    try:
        result = send_outreach(session, outreach_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "status": result.status.value,
        "error": result.error,
        "outreach": repository.get_outreach(session, outreach_id) or {},
    }
