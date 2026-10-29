"""Shared Lichess API constants and session factory."""

import requests

from backend.config import settings

BASE_URL = "https://lichess.org"
USER_AGENT = "LICHESS_parser_for_club/0.1"


def build_session(token: str | None = None) -> requests.Session:
    """Create a session with a User-Agent and optional bearer token.

    ``token`` falls back to ``settings.lichess_token``; when no token is
    available the session stays anonymous (enough for public endpoints).
    """
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    effective_token = token or settings.lichess_token
    if effective_token:
        session.headers["Authorization"] = f"Bearer {effective_token}"

    return session
