"""Low-level HTTP transport for the Lichess API with retry and back-off."""

import time

import requests

#: Status codes worth retrying. 429 is critical: Lichess rate-limits writes.
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

#: Maximum number of characters of a response body kept in error messages.
BODY_LIMIT = 200

REQUEST_TIMEOUT = 30


class LichessError(Exception):
    """Raised when a Lichess API request cannot be completed."""

    def __init__(self, status_code: int, message: str, retryable: bool) -> None:
        self.status_code = status_code
        self.message = message
        self.retryable = retryable
        super().__init__(f"Lichess_{status_code}: {message}")


def _retry_after_seconds(response: requests.Response, default: float) -> float:
    """Return a valid ``Retry-After`` header value in seconds, else ``default``."""
    raw_value = response.headers.get("Retry-After")
    if raw_value is None:
        return float(default)
    try:
        seconds = int(str(raw_value).strip())
    except (TypeError, ValueError):
        return float(default)
    if seconds < 0:
        return float(default)
    return float(seconds)


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    data: str | dict | None = None,
    json: dict | None = None,
    headers: dict | None = None,
    tries: int = 3,
    backoff_base: float = 2.0,
    rate_limit_wait: float = 60.0,
    sleep=time.sleep,
) -> requests.Response:
    """Perform an HTTP request, retrying transient failures.

    ``429`` responses wait for ``rate_limit_wait`` seconds (or the
    ``Retry-After`` header) before the next attempt. Other retryable
    failures use exponential back-off (``backoff_base ** attempt``).
    Permanent client errors are raised immediately.
    """
    attempts = max(1, int(tries))
    last_status = 0
    last_error = "no attempts made"

    for attempt in range(attempts):
        try:
            response = session.request(
                method,
                url,
                params=params,
                data=data,
                json=json,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_status = 0
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt + 1 >= attempts:
                break
            sleep(backoff_base**attempt)
            continue

        if response.status_code < 400:
            return response

        status_code = response.status_code
        last_status = status_code
        last_error = (response.text or "")[:BODY_LIMIT]

        if status_code not in RETRYABLE_STATUS_CODES:
            raise LichessError(status_code, last_error, retryable=False)

        if attempt + 1 >= attempts:
            break

        if status_code == 429:
            sleep(_retry_after_seconds(response, rate_limit_wait))
        else:
            sleep(backoff_base**attempt)

    raise LichessError(last_status, last_error, retryable=True)
