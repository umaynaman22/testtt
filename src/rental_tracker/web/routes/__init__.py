"""One Flask blueprint per area of the app."""
from __future__ import annotations

import re

from flask import Response

from ...domain.periods import period_of
from ...services import excel
from ..filters import label


def options(values, labels: dict | None = None) -> list[tuple[str, str]]:
    return [(v, (labels or {}).get(v, label(v))) for v in values]


def safe_next(target: str | None, fallback: str) -> str:
    """Only allow redirects back into this app."""
    if target and target.startswith("/") and not target.startswith(("//", "/\\")):
        return target
    return fallback


def current_period(today) -> str:
    return period_of(today)


def excel_download(data: bytes, name: str) -> Response:
    """Send an Excel file. The file name is reduced to plain characters so any browser accepts it."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "export"
    return Response(data, mimetype=excel.MIMETYPE,
                    headers={"Content-Disposition": f'attachment; filename="{safe}.xlsx"'})
