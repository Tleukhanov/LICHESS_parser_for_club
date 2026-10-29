"""Shared FastAPI dependencies for the admin API."""

import hmac

from fastapi import Header, HTTPException

from backend.config import settings


def require_admin(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> None:
    """Gate the API behind ``settings.admin_token``.

    An empty configured token means development mode and lets every request
    through. Otherwise the caller must present the token in the
    ``X-Admin-Token`` header. Query-string tokens are intentionally not
    accepted: they leak into proxy logs and browser history.
    """
    expected = settings.admin_token
    if not expected:
        return

    supplied = x_admin_token or ""
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid admin token")
