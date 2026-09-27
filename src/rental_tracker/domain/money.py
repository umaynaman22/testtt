"""Money is always integer cents (BLUEPRINT §7.1), shown in Philippine pesos (₱)."""
from __future__ import annotations

import re

_AMOUNT_RE = re.compile(r"(\d+)(?:\.(\d{0,2}))?|\.(\d{1,2})")
_CURRENCY_RE = re.compile(r"₱|php|\$", re.IGNORECASE)
SYMBOL = "₱"


def parse_money(text: str | None) -> int:
    """Parse user input such as '1,250.50', '₱1250.5', 'PHP 1,250', '-12' or '(12.00)' into cents."""
    if text is None:
        raise ValueError("amount is required")
    s = _CURRENCY_RE.sub("", str(text)).replace(",", "").replace(" ", "").strip()
    if not s:
        raise ValueError("amount is required")
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    if s.startswith("-"):
        negative, s = not negative, s[1:]
    m = _AMOUNT_RE.fullmatch(s)
    if not m:
        raise ValueError(f"not a valid amount: {text!r} (use at most 2 decimal places)")
    if m.group(3) is not None:
        dollars, frac = "0", m.group(3)
    else:
        dollars, frac = m.group(1), m.group(2) or ""
    cents = int(dollars) * 100 + int((frac + "00")[:2])
    return -cents if negative else cents


def format_money(cents: int | None, symbol: str = SYMBOL) -> str:
    if cents is None:
        return ""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(int(cents)), 100)
    return f"{sign}{symbol}{whole:,}.{frac:02d}"


def cents_to_input(cents: int | None) -> str:
    """Plain decimal for form fields: 125050 -> '1250.50'."""
    if cents is None:
        return ""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(int(cents)), 100)
    return f"{sign}{whole}.{frac:02d}"


def round_div(numerator: int, denominator: int) -> int:
    """Integer division rounding half away from zero (half up for positive amounts)."""
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    q, r = divmod(abs(numerator), denominator)
    if 2 * r >= denominator:
        q += 1
    return q if numerator >= 0 else -q


def percent_of(cents: int, basis_points: int) -> int:
    """basis_points: 500 = 5.00%."""
    return round_div(cents * basis_points, 10_000)
