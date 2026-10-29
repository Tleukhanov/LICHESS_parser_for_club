"""Engine, session factory and FastAPI dependency."""

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.config import settings
from backend.db.models import metadata


def _sqlite_path(database_url: str) -> Path | None:
    """Return the filesystem path of a sqlite URL, or None for other backends."""
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return None
    raw_path = database_url[len(prefix) :]
    if not raw_path or raw_path == ":memory:":
        return None
    return Path(raw_path)


def build_engine(database_url: str) -> Engine:
    """Create an engine, preparing the sqlite parent directory when needed."""
    kwargs: dict = {}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        db_path = _sqlite_path(database_url)
        if db_path is not None:
            db_path.parent.mkdir(parents=True, exist_ok=True)
    elif database_url.startswith("postgresql://"):
        # Heroku-style URLs still need the psycopg driver name.
        kwargs["pool_pre_ping"] = True
    return create_engine(database_url, **kwargs)


engine = build_engine(settings.database_url)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db(target_engine: Engine | None = None) -> None:
    """Create all tables if they do not exist yet."""
    metadata.create_all(target_engine or engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a database session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
