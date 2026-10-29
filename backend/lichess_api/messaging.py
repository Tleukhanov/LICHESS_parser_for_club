"""Private message sending via the Lichess inbox endpoint."""

from dataclasses import dataclass
from enum import Enum

import requests

from backend.lichess_api.client import BASE_URL

REQUEST_TIMEOUT = 30
BODY_LIMIT = 200


class SendStatus(str, Enum):
    """Outcome of a single message send attempt."""

    SENT = "sent"
    REJECTED = "rejected"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"


@dataclass
class SendResult:
    """Result of :func:`send_message`."""

    status: SendStatus
    error: str | None = None


def _body(response: requests.Response) -> str:
    return (response.text or "")[:BODY_LIMIT]


def send_message(session: requests.Session, username: str, text: str) -> SendResult:
    """Send one private message without retrying.

    Pacing and retries belong to the sender queue; this function maps the
    API response to a :class:`SendResult` and never raises for HTTP errors.
    """
    try:
        response = session.post(
            f"{BASE_URL}/inbox/{username}",
            data={"text": text},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        return SendResult(SendStatus.ERROR, str(exc))

    status_code = response.status_code

    if status_code == 200:
        return SendResult(SendStatus.SENT)
    if status_code == 400:
        # Blocked, friends-only or otherwise rejected: do not retry.
        return SendResult(SendStatus.REJECTED, _body(response))
    if status_code == 429:
        return SendResult(SendStatus.RATE_LIMITED, _body(response))
    if status_code >= 400:
        return SendResult(SendStatus.ERROR, _body(response))

    return SendResult(SendStatus.SENT)
