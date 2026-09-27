"""One Flask blueprint per area of the app."""
from __future__ import annotations

from ...domain.periods import period_of
from ..filters import label


def options(values, labels: dict | None = None) -> list[tuple[str, str]]:
    return [(v, (labels or {}).get(v, label(v))) for v in values]


def safe_next(target: str | None, fallback: str) -> str:
    """Only allow redirects back into this app."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return fallback


def current_period(today) -> str:
    return period_of(today)
