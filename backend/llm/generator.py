"""Generate personalized Lichess club invitations with an LLM.

The module talks to any OpenAI-compatible chat completions endpoint
(OpenRouter by default) and keeps the message format stable so the review UI
can show drafts without post-processing. A deterministic offline fallback is
provided for the case when the LLM is unavailable.

No message generation assumes that a candidate has ratings: unrated players
must still receive a meaningful invitation.
"""

from __future__ import annotations

import time
from typing import Any

import requests

from backend.config import settings

#: Human-readable club name used in prompts and fallback messages.
CLUB_NAME = "Senpai Chess"

#: Hard upper bound for the generated invitation, in characters.
MAX_MESSAGE_CHARS = 500

#: Maximum number of attempts for a single generation request.
MAX_ATTEMPTS = 3

#: Base of the exponential back-off between attempts (seconds).
BACKOFF_BASE = 2.0

#: Maximum number of characters of an error body kept in :class:`LLMError`.
BODY_LIMIT = 200

#: Status codes worth retrying. Other 4xx responses are permanent.
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

#: Rating keys in "best available" precedence order.
RATING_KEYS = ("rapid", "blitz", "bullet")

#: Mapping of ISO 639-1 language codes to English language names for prompts.
LANGUAGE_NAMES = {
    "en": "English",
    "ru": "Russian",
    "uk": "Ukrainian",
    "be": "Belarusian",
    "kk": "Kazakh",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "pl": "Polish",
    "tr": "Turkish",
    "nl": "Dutch",
    "cs": "Czech",
    "sv": "Swedish",
}


class LLMError(Exception):
    """Raised when an LLM request cannot be completed or returns no message."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"LLM_{status_code}: {message}")


def _language_name(language: str) -> str:
    """Return the English name of a language code, falling back to the code."""
    return LANGUAGE_NAMES.get(language.lower(), language)


def _candidate_rating(candidate: dict) -> tuple[str, int] | None:
    """Return the best available ``(rating_key, rating)`` pair, if any."""
    for key in RATING_KEYS:
        value = candidate.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return key, value
    return None


def _candidate_profile(candidate: dict) -> str:
    """Render the candidate fields as plain-text lines for the user prompt."""
    lines = [f"Nick: {candidate.get('nick', '')}"]
    for key in RATING_KEYS:
        value = candidate.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            lines.append(f"{key.capitalize()} rating: unknown")
        else:
            lines.append(f"{key.capitalize()} rating: {value}")

    games_total = candidate.get("games_total")
    if isinstance(games_total, int) and not isinstance(games_total, bool):
        lines.append(f"Total rated games: {games_total}")
    else:
        lines.append("Total rated games: unknown")

    last_seen = candidate.get("last_seen")
    if last_seen is not None:
        lines.append(f"Last seen (Lichess timestamp): {last_seen}")
    else:
        lines.append("Last seen: unknown")

    if _candidate_rating(candidate) is None:
        lines.append("The player has no ratings yet.")
    return "\n".join(lines)


def build_messages(
    candidate: dict, *, club_name: str, club_url: str, language: str
) -> list[dict]:
    """Build the chat completion messages for one candidate.

    Returns ``[system, user]`` messages. The system prompt enforces the tone,
    length and formatting rules; the user prompt carries the candidate data.
    """
    language_name = _language_name(language)
    system_prompt = (
        "You write personal invitation messages for a Lichess chess club.\n"
        f"Club name: {club_name}\n"
        f"Club link: {club_url}\n"
        "Rules:\n"
        "- Write a SHORT invitation of at most 500 characters.\n"
        "- Be warm, personal and respectful.\n"
        "- Address the player directly by their Lichess nick.\n"
        "- Mention one concrete detail from their ratings or activity.\n"
        f"- Include the club link exactly as given: {club_url}\n"
        "- Do not sound like spam or bulk marketing; no hype, no pressure.\n"
        "- Output plain text only: no markdown, no headings, no bullet lists.\n"
        f"- Write the entire message in {language_name} (language code: {language})."
    )
    user_prompt = (
        "Write an invitation for this player based on their profile:\n"
        f"{_candidate_profile(candidate)}\n"
        f"Invite them to the Lichess club {club_name} ({club_url})."
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _extract_content(response: Any) -> str:
    """Return the stripped assistant message content or raise :class:`LLMError`."""
    try:
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise LLMError(
            getattr(response, "status_code", 0),
            "malformed LLM response: missing choices[0].message.content",
        ) from exc

    if not isinstance(content, str) or not content.strip():
        raise LLMError(
            getattr(response, "status_code", 0),
            "empty message content in LLM response",
        )
    # Keep the stored message within the advertised limit even if the model
    # ignores the prompt.
    return content.strip()[:MAX_MESSAGE_CHARS].rstrip()


def _resolve_club_url(club_url: str | None) -> str:
    if club_url:
        return club_url
    if settings.club_url:
        return settings.club_url
    return f"https://lichess.org/team/{settings.senpai_team_id}"


def generate_message(
    candidate: dict,
    *,
    club_url: str | None = None,
    language: str | None = None,
    session: Any = None,
    sleep=time.sleep,
) -> str:
    """Generate one personalized invitation through the LLM.

    ``session`` may be any object with a ``post`` method (a ``requests``
    session in production); when omitted the module-level :mod:`requests`
    function is used. ``sleep`` is injected so retries can be tested without
    waiting. Raises :class:`LLMError` on any failure.
    """
    if not settings.llm_api_key or not settings.llm_api_key.strip():
        raise LLMError(0, "LLM_API_KEY is not configured")

    resolved_club_url = _resolve_club_url(club_url)
    resolved_language = language or settings.message_language
    messages = build_messages(
        candidate,
        club_name=CLUB_NAME,
        club_url=resolved_club_url,
        language=resolved_language,
    )

    url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": settings.llm_temperature,
        "max_tokens": settings.llm_max_tokens,
    }
    client = session if session is not None else requests

    last_status = 0
    last_error = "no attempts made"

    for attempt in range(MAX_ATTEMPTS):
        try:
            response = client.post(
                url, headers=headers, json=payload, timeout=settings.llm_timeout
            )
        except requests.RequestException as exc:
            last_status = 0
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt + 1 >= MAX_ATTEMPTS:
                break
            sleep(BACKOFF_BASE**attempt)
            continue

        status_code = response.status_code
        if status_code < 400:
            return _extract_content(response)

        last_status = status_code
        last_error = (getattr(response, "text", "") or "")[:BODY_LIMIT]

        if status_code not in RETRYABLE_STATUS_CODES:
            raise LLMError(status_code, last_error)

        if attempt + 1 >= MAX_ATTEMPTS:
            break
        sleep(BACKOFF_BASE**attempt)

    raise LLMError(last_status, last_error)


def _fallback_templates(
    candidate: dict, *, club_url: str, language: str
) -> dict[str, str]:
    """Build the localized fallback text lines for a candidate."""
    nick = candidate.get("nick", "")
    best = _candidate_rating(candidate)

    templates = {
        "ru": {
            "greeting": f"Привет, {nick}!",
            "intro": (
                f"Я заметил тебя на Lichess и приглашаю в клуб «{CLUB_NAME}» — "
                "тёплое сообщество игроков, где регулярно играют и разбирают партии."
            ),
            "rating_known": (
                "Твой рейтинг в {key} — {rating}. Будем рады видеть тебя за доской."
            ),
            "rating_unknown": (
                "У тебя пока нет рейтинга в быстрых контролях, "
                "но новичкам у нас тоже рады."
            ),
            "link": f"Загляни к нам: {club_url}",
            "outro": "Если интересно — просто ответь на это сообщение. Хорошей игры!",
        },
        "en": {
            "greeting": f"Hi {nick}!",
            "intro": (
                f"I noticed you on Lichess and would like to invite you to "
                f"{CLUB_NAME}, a friendly club where players meet, play and "
                "review games together."
            ),
            "rating_known": (
                "Your {key} rating is {rating}; we would be glad to see you at "
                "the board."
            ),
            "rating_unknown": (
                "You have no rapid rating yet, but beginners are very welcome too."
            ),
            "link": f"Take a look: {club_url}",
            "outro": "If you are interested, just reply to this message. Good games!",
        },
    }
    chosen = templates.get(language.lower()[:2], templates["en"])
    if best is None:
        rating_line = chosen["rating_unknown"]
    else:
        rating_line = chosen["rating_known"].format(key=best[0], rating=best[1])
    return {
        "greeting": chosen["greeting"],
        "intro": chosen["intro"],
        "rating": rating_line,
        "link": chosen["link"],
        "outro": chosen["outro"],
    }


def fallback_message(
    candidate: dict, *, club_url: str, language: str = "ru"
) -> str:
    """Return a deterministic plain-text invitation without any network access."""
    parts = _fallback_templates(candidate, club_url=club_url, language=language)
    message = "\n\n".join(
        [
            parts["greeting"],
            parts["intro"],
            parts["rating"],
            parts["link"],
            parts["outro"],
        ]
    )
    return message[:MAX_MESSAGE_CHARS].rstrip()
