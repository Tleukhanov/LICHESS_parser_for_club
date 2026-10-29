"""Club members parser and candidate selector (T3).

Names are re-exported lazily so that ``python -m backend.parser.candidates``
does not import the submodule twice (which triggers a ``runpy`` warning).
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from backend.parser.candidates import (
        effective_rating,
        fetch_candidates,
        parse_and_store,
        persist_candidates,
        select_candidates,
    )

__all__ = [
    "effective_rating",
    "fetch_candidates",
    "parse_and_store",
    "persist_candidates",
    "select_candidates",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from backend.parser import candidates as _candidates

        return getattr(_candidates, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
