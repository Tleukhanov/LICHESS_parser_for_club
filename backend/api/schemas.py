"""Request models of the admin API (Pydantic v2)."""

from pydantic import BaseModel, Field, field_validator


class ParseRequest(BaseModel):
    """Filters for a Lichess club parse run."""

    club_id: str = Field(min_length=1)

    @field_validator("club_id")
    @classmethod
    def _strip_club_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("club_id must not be blank")
        return stripped

    min_rapid: int | None = None
    max_rapid: int | None = None
    min_games: int = 0
    limit: int | None = None
    exclude_own: bool = True


class GenerateRequest(BaseModel):
    """Identify the candidate to write an invitation for."""

    candidate_id: int


class OutreachUpdate(BaseModel):
    """Editable fields of one outreach row."""

    message_text: str | None = None
    reviewer_rating: int | None = Field(default=None, ge=1, le=5)
    reviewer_notes: str | None = None
    reaction: str | None = None
