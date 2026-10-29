"""SQLAlchemy Core table definitions (no dialect-specific SQL)."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    func,
)

metadata = MetaData()

# Allowed values for the status-like columns. Enforced by the application layer.
CANDIDATE_STATUSES = {
    "new",
    "selected",
    "sending",
    "messaged",
    "joined",
    "skipped",
}
SEND_STATUSES = {
    "draft",
    "approved",
    "rejected",
    "sending",
    "sent",
    "skipped_400",
    "skipped_429",
    "skipped_joined",
    "failed",
}
REACTIONS = {"none", "replied", "declined", "spam_report"}

candidates = Table(
    "candidates",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("nick", String(50), nullable=False, unique=True),
    Column("source_club_id", String(50), nullable=True),
    Column("rapid", Integer, nullable=True),
    Column("blitz", Integer, nullable=True),
    Column("bullet", Integer, nullable=True),
    Column("games_total", Integer, nullable=False, default=0),
    # Lichess ``seenAt`` value in Unix milliseconds (see users.fetch_perfs).
    Column("last_seen", BigInteger, nullable=True),
    Column("status", String(20), nullable=False, default="new"),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    Column("updated_at", DateTime, nullable=False, server_default=func.now()),
)

outreach = Table(
    "outreach",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("candidate_id", Integer, ForeignKey("candidates.id"), nullable=False),
    Column("message_text", Text, nullable=True),
    Column("llm_model", String(100), nullable=True),
    Column("reviewer_rating", Integer, nullable=True),
    Column("reviewer_notes", Text, nullable=True),
    Column("send_status", String(20), nullable=False, default="draft"),
    Column("send_error", Text, nullable=True),
    Column("sent_at", DateTime, nullable=True),
    Column("reaction", String(20), nullable=False, default="none"),
    Column("joined", Boolean, nullable=False, default=False),
    Column("joined_at", DateTime, nullable=True),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    Column("updated_at", DateTime, nullable=False, server_default=func.now()),
)

Index("ix_outreach_candidate_id", outreach.c.candidate_id)
Index("ix_outreach_send_status", outreach.c.send_status)

#: Tiny key/value store for cross-request state such as the 429 cooldown.
app_state = Table(
    "app_state",
    metadata,
    Column("key", String(50), primary_key=True),
    Column("value", Text, nullable=True),
    Column("updated_at", DateTime, nullable=False, server_default=func.now()),
)
