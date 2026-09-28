"""Reading and validating HTML form input."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from ..domain.money import cents_to_input, parse_money
from ..domain.periods import parse_date
from ..services.common import ServiceError


def record_id(value: str | None) -> int | None:
    """A database id typed or passed in a link; None unless it's a plain, sensible number."""
    v = (value or "").strip()
    return int(v) if v.isascii() and v.isdigit() and len(v) <= 18 else None


class Form:
    """Collects every problem in a form so the user sees them all at once."""

    def __init__(self, data: Mapping[str, Any]):
        self.data = data
        self.errors: list[str] = []

    def raw(self, name: str) -> str:
        return (self.data.get(name) or "").strip()

    def _fail(self, message: str) -> None:
        self.errors.append(message)

    def str(self, name: str, label: str | None = None, required: bool = False, max_len: int = 5000) -> str | None:
        v = self.raw(name)
        if not v:
            if required:
                self._fail(f"{label or name} is required")
            return None
        if len(v) > max_len:
            self._fail(f"{label or name} is too long (max {max_len} characters)")
        return v

    def int(self, name: str, label: str, required: bool = False, lo: int | None = None,
            hi: int | None = None) -> int | None:
        v = self.raw(name)
        if not v:
            if required:
                self._fail(f"{label} is required")
            return None
        try:
            n = int(v)
        except ValueError:
            self._fail(f"{label} must be a whole number")
            return None
        if (lo is not None and n < lo) or (hi is not None and n > hi):
            self._fail(f"{label} must be between {lo} and {hi}")
        return n

    def money(self, name: str, label: str, required: bool = False) -> int | None:
        v = self.raw(name)
        if not v:
            if required:
                self._fail(f"{label} is required")
            return None
        try:
            return parse_money(v)
        except ValueError:
            self._fail(f"{label}: enter an amount like 1250 or 1,250.50")
            return None

    def percent_bp(self, name: str, label: str) -> int | None:
        v = self.raw(name).rstrip("%").strip()
        if not v:
            return None
        try:
            return int((Decimal(v) * 100).to_integral_value())
        except InvalidOperation:
            self._fail(f"{label} must be a percentage like 5 or 2.5")
            return None

    def date(self, name: str, label: str, required: bool = False) -> str | None:
        v = self.raw(name)
        if not v:
            if required:
                self._fail(f"{label} is required")
            return None
        try:
            return parse_date(v).isoformat()
        except ValueError:
            self._fail(f"{label} must be a date (YYYY-MM-DD)")
            return None

    def choice(self, name: str, label: str, choices: Iterable[str], default: str | None = None) -> str | None:
        v = self.raw(name) or default
        if v not in set(choices):
            self._fail(f"Choose a {label.lower()}")
            return None
        return v

    def bool(self, name: str) -> int:
        return int(self.raw(name) in ("1", "on", "yes", "true"))

    def id(self, name: str) -> int | None:
        return record_id(self.raw(name))

    def check(self) -> None:
        if self.errors:
            raise ServiceError(" · ".join(self.errors))


def values_from(row: Mapping[str, Any] | None, money: Mapping[str, str] | None = None,
                percent: Mapping[str, str] | None = None) -> dict[str, str]:
    """Row -> form values. money maps input name -> cents column."""
    if row is None:
        return {}
    out = {k: ("" if row[k] is None else str(row[k])) for k in row.keys()}
    for name, col in (money or {}).items():
        out[name] = cents_to_input(row[col]) if row[col] is not None else ""
    for name, col in (percent or {}).items():
        out[name] = f"{Decimal(row[col]) / 100:g}" if row[col] is not None else ""
    return out
