"""Template filters and helpers."""
from __future__ import annotations

from datetime import date, datetime

from flask import Flask, request, url_for
from markupsafe import Markup, escape

from ..domain.money import cents_to_input, format_money
from ..services.ledger import method_name

REPORT_LINKS = {
    "property": ("properties.detail", "pid"),
    "unit": ("properties.unit_detail", "unit_id"),
    "lease": ("leases.detail", "lease_id"),
    "tenant": ("tenants.detail", "tid"),
}

STATUS_CLASS = {"active": "ok", "month_to_month": "info", "future": "info", "draft": "muted",
                "ended": "muted", "terminated": "bad", "occupied": "ok", "vacant": "warn",
                "sold": "muted", "archived": "muted", "offline": "warn"}


def money(cents, blank_zero: bool = False) -> str:
    if cents is None or (blank_zero and not cents):
        return ""
    return format_money(cents)


def fdate(value, fmt: str = "%b %-d, %Y") -> str:
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value[:10])
        except ValueError:
            return value
    if isinstance(value, datetime):
        fmt = "%b %-d, %Y %-I:%M %p"
    try:
        return value.strftime(fmt)
    except ValueError:  # Windows strftime spells "no leading zero" as %#d, not %-d
        return value.strftime(fmt.replace("%-", "%#"))


def pct(value, digits: int = 1) -> str:
    return "" if value is None else f"{value:.{digits}f}%"


def label(value) -> str:
    return "" if value is None else str(value).replace("_", " ").capitalize()


def pay_method(payment) -> str:
    """'GCash', 'Cash', or whatever was typed for Other."""
    keys = payment.keys()
    return method_name(payment["method"], payment["method_other"] if "method_other" in keys else None)


def badge(status) -> Markup:
    if not status:
        return Markup("")
    return Markup(f'<span class="badge {STATUS_CLASS.get(status, "muted")}">{escape(label(status))}</span>')


def cell(row: dict, col) -> Markup:
    v = row.get(col.key)
    if col.kind == "money":
        text = money(v)
        cls = "num neg" if isinstance(v, int) and v < 0 else "num"
    elif col.kind == "date":
        text, cls = fdate(v), "nowrap"
    elif col.kind == "pct":
        text, cls = pct(v), "num"
    elif col.key == "unit_label":
        text, cls = ("" if v in (None, "Main") else str(v)), ""
    elif col.kind == "int":
        text, cls = "" if v is None else f"{v:,}", "num"
    else:
        text, cls = "" if v is None else str(v), ""
    html = escape(text)
    if col.link and text:
        endpoint, arg = REPORT_LINKS[col.link[0]]
        target = row.get(col.link[1])
        if target:
            html = Markup(f'<a href="{escape(url_for(endpoint, **{arg: target}))}">{html}</a>')
    return Markup(f'<td class="{cls}">{html}</td>')


def place(code, unit_label) -> str:
    """'12 Maple St' for single-unit properties, '12 Maple St · 2B' otherwise."""
    if not code:
        return ""
    if not unit_label or unit_label == "Main":
        return str(code)
    return f"{code} · {unit_label}"


def url_with(**changes) -> str:
    args = request.args.to_dict()
    args.update({k: v for k, v in changes.items() if v is not None})
    for k, v in changes.items():
        if v is None:
            args.pop(k, None)
    return url_for(request.endpoint, **(request.view_args or {}), **args)


def register(app: Flask) -> None:
    app.add_template_filter(money)
    app.add_template_filter(fdate)
    app.add_template_filter(pct)
    app.add_template_filter(label)
    app.add_template_filter(badge)
    app.add_template_filter(pay_method)
    app.add_template_filter(cents_to_input, "input_money")
    app.jinja_env.globals.update(cell=cell, url_with=url_with, place=place)
