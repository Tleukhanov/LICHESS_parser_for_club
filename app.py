"""FastAPI entrypoint for the Lichess club outreach tool."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.db.database import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="LICHESS club outreach", lifespan=lifespan)

# TODO(T5): mount the static review UI here, e.g.
# app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
