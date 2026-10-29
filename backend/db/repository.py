"""Small session-based helpers around the SQLAlchemy Core tables."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.db.models import app_state, candidates, outreach


def _utcnow() -> datetime:
    """Naive UTC timestamp, matching the DateTime columns."""
    return datetime.now(UTC).replace(tzinfo=None)


def _row_to_dict(row: Any) -> dict[str, Any] | None:
    return dict(row._mapping) if row is not None else None


def create_candidate(
    session: Session,
    nick: str,
    source_club_id: str | None = None,
    rapid: int | None = None,
    blitz: int | None = None,
    bullet: int | None = None,
    games_total: int = 0,
    last_seen: int | None = None,
) -> int:
    """Insert a candidate, or refresh ratings if the nick already exists."""
    existing = session.execute(
        select(candidates.c.id).where(candidates.c.nick == nick)
    ).scalar_one_or_none()

    if existing is not None:
        session.execute(
            update(candidates)
            .where(candidates.c.id == existing)
            .values(
                source_club_id=source_club_id,
                rapid=rapid,
                blitz=blitz,
                bullet=bullet,
                games_total=games_total,
                last_seen=last_seen,
                updated_at=_utcnow(),
            )
        )
        session.commit()
        return int(existing)

    result = session.execute(
        insert(candidates)
        .values(
            nick=nick,
            source_club_id=source_club_id,
            rapid=rapid,
            blitz=blitz,
            bullet=bullet,
            games_total=games_total,
            last_seen=last_seen,
        )
        .returning(candidates.c.id)
    )
    candidate_id = int(result.scalar_one())
    session.commit()
    return candidate_id


def get_candidate(session: Session, candidate_id: int) -> dict[str, Any] | None:
    row = session.execute(
        select(candidates).where(candidates.c.id == candidate_id)
    ).first()
    return _row_to_dict(row)


def list_candidates(
    session: Session, status: str | None = None
) -> list[dict[str, Any]]:
    stmt = select(candidates).order_by(candidates.c.id)
    if status is not None:
        stmt = stmt.where(candidates.c.status == status)
    return [dict(row._mapping) for row in session.execute(stmt).all()]


def create_outreach(
    session: Session,
    candidate_id: int,
    message_text: str,
    llm_model: str | None = None,
) -> int:
    result = session.execute(
        insert(outreach)
        .values(
            candidate_id=candidate_id,
            message_text=message_text,
            llm_model=llm_model,
        )
        .returning(outreach.c.id)
    )
    outreach_id = int(result.scalar_one())
    session.commit()
    return outreach_id


def get_outreach(session: Session, outreach_id: int) -> dict[str, Any] | None:
    row = session.execute(select(outreach).where(outreach.c.id == outreach_id)).first()
    return _row_to_dict(row)


def list_outreach(
    session: Session, send_status: str | None = None
) -> list[dict[str, Any]]:
    stmt = select(outreach).order_by(outreach.c.id)
    if send_status is not None:
        stmt = stmt.where(outreach.c.send_status == send_status)
    return [dict(row._mapping) for row in session.execute(stmt).all()]


def has_sent_outreach(
    session: Session, candidate_id: int, exclude_id: int | None = None
) -> bool:
    """Return True when the candidate already has a successfully sent message."""
    stmt = select(outreach.c.id).where(
        outreach.c.candidate_id == candidate_id,
        outreach.c.send_status == "sent",
    )
    if exclude_id is not None:
        stmt = stmt.where(outreach.c.id != exclude_id)
    return session.execute(stmt.limit(1)).first() is not None


def get_state(session: Session, key: str) -> str | None:
    """Read a value from the small ``app_state`` key/value store."""
    return session.execute(
        select(app_state.c.value).where(app_state.c.key == key)
    ).scalar_one_or_none()


def set_state(session: Session, key: str, value: str | None) -> None:
    """Insert or update a key in the ``app_state`` store.

    The insert path tolerates a concurrent insert of the same key: another
    worker may have created the row between our ``UPDATE`` and ``INSERT``.
    """
    updated = session.execute(
        update(app_state)
        .where(app_state.c.key == key)
        .values(value=value, updated_at=_utcnow())
    )
    if updated.rowcount == 0:
        try:
            session.execute(
                insert(app_state).values(key=key, value=value, updated_at=_utcnow())
            )
            session.commit()
            return
        except IntegrityError:
            session.rollback()
            session.execute(
                update(app_state)
                .where(app_state.c.key == key)
                .values(value=value, updated_at=_utcnow())
            )
    session.commit()


def update_outreach(
    session: Session, outreach_id: int, **fields: Any
) -> bool:
    """Update the given columns of one outreach row. Returns False if not found."""
    allowed = set(outreach.c.keys()) - {"id", "created_at"}
    payload = {key: value for key, value in fields.items() if key in allowed}
    if not payload:
        return get_outreach(session, outreach_id) is not None

    payload["updated_at"] = _utcnow()
    result = session.execute(
        update(outreach).where(outreach.c.id == outreach_id).values(**payload)
    )
    session.commit()
    return result.rowcount > 0
