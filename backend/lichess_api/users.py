"""User rating lookups for the Lichess API."""

import json

import requests

from backend.lichess_api.client import BASE_URL
from backend.lichess_api.transport import request_with_retry

PERFS = ("rapid", "blitz", "bullet")

#: Lichess accepts at most 300 usernames per ``POST /api/users`` call.
CHUNK_SIZE = 300


def _chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _parse_user_objects(text: str) -> list[dict]:
    """Parse a JSON array, falling back to newline-delimited JSON."""
    stripped = text.strip()
    if not stripped:
        return []

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        objects: list[dict] = []
        for line in stripped.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                objects.append(item)
        return objects

    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict):
        return [parsed]
    return []


def _rating(perfs: dict, key: str) -> int | None:
    """Return a perfs rating, tolerating a missing or malformed entry."""
    value = perfs.get(key)
    if isinstance(value, dict):
        value = value.get("rating")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _games_total(user: dict) -> int:
    count = user.get("count")
    if not isinstance(count, dict):
        return 0
    total = count.get("all")
    if isinstance(total, bool) or not isinstance(total, (int, float)):
        return 0
    return int(total)


def _last_seen(user: dict) -> int | None:
    seen_at = user.get("seenAt")
    if isinstance(seen_at, bool) or not isinstance(seen_at, (int, float)):
        return None
    return int(seen_at)


def fetch_perfs(session: requests.Session, nicks: list[str]) -> dict[str, dict]:
    """Return per-nick ratings for ``rapid``, ``blitz`` and ``bullet``.

    Nicks are requested in chunks of 300 via ``POST /api/users``. The result
    maps Lichess user IDs to a dict with the three ratings, ``games_total``
    and ``last_seen`` (milliseconds).
    """
    if not nicks:
        return {}

    results: dict[str, dict] = {}

    for chunk in _chunked(list(nicks), CHUNK_SIZE):
        response = request_with_retry(
            session,
            "POST",
            f"{BASE_URL}/api/users",
            data=",".join(chunk),
        )
        for user in _parse_user_objects(response.text or ""):
            user_id = user.get("id")
            if not isinstance(user_id, str) or not user_id:
                continue
            perfs = user.get("perfs")
            if not isinstance(perfs, dict):
                perfs = {}
            results[user_id] = {
                **{perf: _rating(perfs, perf) for perf in PERFS},
                "games_total": _games_total(user),
                "last_seen": _last_seen(user),
            }

    return results
