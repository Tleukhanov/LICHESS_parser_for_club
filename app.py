"""FastAPI entrypoint for the Lichess club outreach tool."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend.api.routes import router as api_router
from backend.db.database import init_db

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="LICHESS club outreach", lifespan=lifespan)

app.include_router(api_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# The static mount is a catch-all, so it must stay registered last.
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
