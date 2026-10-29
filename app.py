"""FastAPI entrypoint for the Lichess club outreach tool."""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend.api.routes import router as api_router
from backend.config import settings
from backend.db.database import init_db

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR / "frontend"

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.environ.get("DYNO") and settings.database_url.startswith("sqlite"):
        logger.warning(
            "Running on Heroku with SQLite: the dyno filesystem is ephemeral, "
            "so data will be lost on restart. Set DATABASE_URL to the Postgres "
            "add-on instead."
        )
    init_db()
    yield


# Hide the interactive API docs when an admin token is configured: there is no
# reason to publish the whole API surface in production.
_public_docs = not settings.admin_token
app = FastAPI(
    title="LICHESS club outreach",
    lifespan=lifespan,
    docs_url="/docs" if _public_docs else None,
    redoc_url="/redoc" if _public_docs else None,
    openapi_url="/openapi.json" if _public_docs else None,
)

app.include_router(api_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# The static mount is a catch-all, so it must stay registered last.
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
