"""Settings."""
from __future__ import annotations

from flask import Blueprint, redirect, render_template, request, url_for

from ... import db as dbmod
from ...services.common import audit, get_setting, set_setting
from .. import attempt, db, state
from ..forms import Form

bp = Blueprint("admin", __name__)

SETTINGS = [
    # key, label, kind, help / choices
    ("business_name", "Your name or business", "str", "Shown on receipts and statements"),
    ("business_contact", "Your contact details", "text", "Address, phone or email for receipts and statements"),
    ("default_late_fee_cents", "Late fee for new tenants", "money", "Blank = no late fee. You can change it per tenant."),
    ("default_grace_days", "Days before rent counts as late", "int", "For new tenants"),
    ("late_fee_mode", "Late fees", "choice",
     [("review", "Show them on the Late page for me to charge"), ("auto", "Charge them automatically")]),
    ("rent_post_days_before_due", "Bill rent this many days before it's due", "int", "0 = on the due day"),
    ("proration_method", "Rent for part of a month", "choice",
     [("actual_days", "By the actual days in the month"), ("thirty_day_month", "As if every month had 30 days")]),
]
DEFAULTS = {"default_grace_days": "5", "late_fee_mode": "review", "rent_post_days_before_due": "0",
            "proration_method": "actual_days"}


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    conn = db()
    if request.method == "POST":
        f = Form(request.form)
        with attempt("Settings saved"):
            new = {}
            for key, label, kind, extra in SETTINGS:
                if kind == "int":
                    v = f.int(key, label, lo=0, hi=365)
                    new[key] = str(v) if v is not None else DEFAULTS.get(key, "0")
                elif kind == "money":
                    v = f.money(key, label) or 0
                    if v < 0:
                        raise ValueError(f"{label} can't be negative")
                    new[key] = str(v)
                elif kind == "choice":
                    v = f.raw(key)
                    new[key] = v if v in {o for o, _ in extra} else DEFAULTS[key]
                else:
                    new[key] = f.raw(key)
            f.check()
            changes = {k: [get_setting(conn, k), v] for k, v in new.items() if get_setting(conn, k) != v}
            for k, v in new.items():
                set_setting(conn, k, v)
            if changes:
                audit(conn, "settings", changes=changes)
        return redirect(url_for("admin.settings"))
    values = {k: get_setting(conn, k, DEFAULTS.get(k, "")) for k, *_ in SETTINGS}
    fee = int(values.get("default_late_fee_cents") or 0)
    values["default_late_fee_cents"] = f"{fee / 100:.2f}" if fee else ""
    return render_template("admin/settings.html", settings=SETTINGS, values=values, data=state().data,
                           schema=dbmod.schema_version(conn))
