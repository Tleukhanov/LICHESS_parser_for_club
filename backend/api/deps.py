"""Shared FastAPI dependencies for the admin API."""

from fastapi import Header, HTTPException, Query

from backend.config import settings


def require_admin(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    token: str | None = Query(default=None),
) -> None:
    """Gate the API behind ``settings.admin_token``.

    An empty configured token means development mode and lets every request
    through. Otherwise the caller must present the token either as the
    ``X-Admin-Token`` header or as a ``?token=`` query parameter.
    """
    expected = settings.admin_token
    if not expected:
        return

    supplied = x_admin_token or token
    if supplied != expected:
        raise HTTPException(status_code=401, detail="invalid admin token")
