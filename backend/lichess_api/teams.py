"""Team endpoints of the Lichess API."""

import json

import requests

from backend.lichess_api.client import BASE_URL
from backend.lichess_api.transport import request_with_retry


def fetch_team_info(session: requests.Session, team_id: str) -> dict:
    """Return the ``GET /api/team/{team_id}`` payload."""
    response = request_with_retry(
        session,
        "GET",
        f"{BASE_URL}/api/team/{team_id}",
    )
    return response.json()


def _member_nick(obj: dict) -> str | None:
    """Pick the username from a team member object."""
    for key in ("id", "username", "name"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def fetch_team_members(session: requests.Session, team_id: str) -> list[str]:
    """Return the member nicks of a team, in API order without duplicates.

    ``GET /api/team/{team_id}/users`` streams newline-delimited JSON.
    Invalid or empty lines are skipped.
    """
    response = request_with_retry(
        session,
        "GET",
        f"{BASE_URL}/api/team/{team_id}/users",
    )

    members: list[str] = []
    seen: set[str] = set()

    for raw_line in response.iter_lines():
        if not raw_line:
            continue
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8", errors="replace")
        else:
            line = raw_line
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        nick = _member_nick(obj)
        if nick is None or nick in seen:
            continue
        seen.add(nick)
        members.append(nick)

    return members
