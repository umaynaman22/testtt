"""Settings, categories, backups, import and the audit log."""
from __future__ import annotations

import secrets
import shutil
from datetime import datetime
from pathlib import Path

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from ... import db as dbmod
from ...services import backup, expenses, importer
from ...services.common import ServiceError, audit, get_setting, set_setting
from .. import attempt, db, state, today
from ..forms import Form

bp = Blueprint("admin", __name__)

SETTINGS = [
    # key, label, kind, options/help
    ("rent_post_days_before_due", "Bill rent this many days before it is due", "int", "0 = on the due date"),
    ("proration_method", "Partial-month proration", "choice",
     [("actual_days", "Actual days in the month"), ("thirty_day_month", "30-day month")]),
    ("late_fee_mode", "Late fee mode", "choice",
     [("review", "Suggest fees for me to approve"), ("auto", "Charge automatically")]),
    ("late_fee_min_balance_cents", "Only charge a late fee if more than this is unpaid", "money", "0 = any amount"),
    ("payment_application_order", "Apply payments to", "choice",
     [("oldest_first_rent_before_fees", "Oldest charges first, rent before fees"),
      ("oldest_first", "Oldest charges first")]),
    ("expired_lease_action", "When a lease passes its end date", "choice",
     [("month_to_month", "Switch it to month-to-month"), ("leave_active", "Leave it (stop billing)")]),
    ("deposit_return_days", "Days allowed to return a deposit after move-out", "int", "Set by local law"),
    ("books_locked_through", "Books locked through", "date",
     "Entries on or before this date can't be changed (e.g. after filing taxes). Blank = unlocked."),
    ("receipt_number_prefix", "Receipt number prefix", "str", None),
    ("backup_keep_daily", "Daily backups to keep", "int", None),
    ("backup_keep_weekly", "Weekly backups to keep", "int", None),
    ("backup_keep_monthly", "Monthly backups to keep", "int", None),
]


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    conn = db()
    if request.method == "POST":
        f = Form(request.form)
        with attempt("Settings saved"):
            new = {}
            for key, label, kind, extra in SETTINGS:
                if kind == "int":
                    v = f.int(key, label, required=True, lo=0, hi=3650)
                    new[key] = str(v) if v is not None else None
                elif kind == "money":
                    v = f.money(key, label) or 0
                    new[key] = str(v)
                elif kind == "choice":
                    new[key] = f.choice(key, label, [o for o, _ in extra])
                elif kind == "date":
                    new[key] = f.date(key, label) or ""
                else:
                    new[key] = f.raw(key)
            f.check()
            changes = {k: [get_setting(conn, k), v] for k, v in new.items() if get_setting(conn, k) != v}
            for k, v in new.items():
                set_setting(conn, k, v)
            if changes:
                audit(conn, "settings", changes=changes)
        return redirect(url_for("admin.settings"))
    values = {k: get_setting(conn, k) for k, *_ in SETTINGS}
    if values.get("late_fee_min_balance_cents"):
        values["late_fee_min_balance_cents"] = f"{int(values['late_fee_min_balance_cents']) / 100:.2f}"
    return render_template("admin/settings.html", settings=SETTINGS, values=values,
                           data_dir=state().data.root, schema=dbmod.schema_version(conn))


@bp.route("/settings/categories", methods=["GET", "POST"])
def categories():
    conn = db()
    if request.method == "POST":
        f = Form(request.form)
        with attempt("Category saved"):
            expenses.save_category(conn, f.id("id"), name=f.raw("name"), tax_line=f.raw("tax_line"),
                                   is_capital=bool(f.bool("is_capital")), is_active=bool(f.bool("is_active")))
        return redirect(url_for("admin.categories"))
    usage = {r[0]: r[1] for r in conn.execute("SELECT category_id, COUNT(*) FROM expenses GROUP BY category_id")}
    return render_template("admin/categories.html", cats=expenses.list_categories(conn), usage=usage, values={})


# ---- backups ------------------------------------------------------------------

@bp.route("/backups")
def backups():
    conn = db()
    return render_template("admin/backups.html", backups=backup.list_backups(state().data),
                           external=get_setting(conn, "backup_external_path"),
                           last_external=get_setting(conn, "last_external_backup"), data=state().data, values={})


@bp.route("/backups/create", methods=["POST"])
def backup_create():
    conn = db()
    try:
        path = backup.create_backup(conn, state().data, "snapshots", "manual")
        copied = backup.copy_to_external(conn, state().data, path)
        flash(f"Backup saved: {path.name}" + (" (also copied to the external drive)" if copied else ""), "ok")
    except OSError as e:
        flash(f"Backup failed: {e}", "error")
    return redirect(url_for("admin.backups"))


@bp.route("/backups/external", methods=["POST"])
def backup_external():
    path = request.form.get("path", "").strip()
    with attempt():
        set_setting(db(), "backup_external_path", path)
    if path and not Path(path).expanduser().is_dir():
        flash("Saved, but that folder isn't available right now. Plug the drive in and click 'Back up now'.", "info")
    else:
        flash("External backup location saved." if path else "External backup turned off.", "ok")
    return redirect(url_for("admin.backups"))


@bp.route("/backups/restore", methods=["POST"])
def backup_restore():
    conn = db()
    try:
        target = backup.resolve_backup(state().data, request.form.get("name", ""))
        safety = backup.restore_backup(conn, state().data, target)
        state().last_catch_up = None
        flash(f"Restored {target.name}. The data from before the restore was saved as {safety.name}.", "ok")
    except ServiceError as e:
        flash(str(e), "error")
    return redirect(url_for("admin.backups"))


@bp.route("/backups/delete", methods=["POST"])
def backup_delete():
    try:
        name = backup.delete_backup(state().data, request.form.get("name", ""))
        audit_conn = db()
        audit(audit_conn, "delete", "backup", changes={"file": name})
        flash(f"Deleted backup {name}.", "ok")
    except ServiceError as e:
        flash(str(e), "error")
    return redirect(url_for("admin.backups"))


# ---- import -------------------------------------------------------------------

def _pending_dir(token: str) -> Path:
    if not token or not token.replace("-", "").replace("_", "").isalnum():
        abort(400)
    return state().data.imports / f"pending-{token}"


@bp.route("/import", methods=["GET", "POST"])
def import_page():
    result, token = None, None
    if request.method == "POST":
        files = {}
        try:
            for kind in importer.KINDS:
                up = request.files.get(kind)
                if up and up.filename:
                    files[kind] = importer.decode(up.read())
        except ServiceError as e:
            flash(str(e), "error")
            return redirect(url_for("admin.import_page"))
        result = importer.run_import(db(), files, commit=False, today=today())
        if files and not result.errors:
            token = secrets.token_urlsafe(12)
            folder = _pending_dir(token)
            folder.mkdir(parents=True, exist_ok=True)
            for kind, text in files.items():
                (folder / f"{kind}.csv").write_text(text, encoding="utf-8")
    return render_template("admin/import.html", result=result, token=token, kinds=importer.KINDS,
                           templates=importer.TEMPLATES, help=importer.HELP)


@bp.route("/import/commit", methods=["POST"])
def import_commit():
    folder = _pending_dir(request.form.get("token", ""))
    if not folder.is_dir():
        flash("That import expired. Choose the files again.", "error")
        return redirect(url_for("admin.import_page"))
    files = {p.stem: p.read_text(encoding="utf-8") for p in folder.glob("*.csv") if p.stem in importer.KINDS}
    conn = db()
    backup.create_backup(conn, state().data, "snapshots", "pre-import")
    result = importer.run_import(conn, files, commit=True, today=today())
    if result.committed:
        done = state().data.imports / f"imported-{datetime.now():%Y%m%d-%H%M%S}"
        shutil.move(str(folder), str(done))
        state().last_catch_up = None  # bill rent for the new leases right away
        flash("Import complete. A backup was taken first, and the files were archived in " + done.name + ".", "ok")
    return render_template("admin/import.html", result=result, token=None, kinds=importer.KINDS,
                           templates=importer.TEMPLATES, help=importer.HELP)


@bp.route("/import/template/<kind>.csv")
def import_template(kind: str):
    if kind not in importer.TEMPLATES:
        abort(404)
    return Response(importer.template_csv(kind), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={kind}.csv"})


# ---- audit log -----------------------------------------------------------------

@bp.route("/audit", endpoint="audit")
def audit_log():
    conn = db()
    et = request.args.get("entity") or None
    rows = conn.execute("SELECT * FROM audit_log WHERE (? IS NULL OR entity_type = ?) ORDER BY id DESC LIMIT 500",
                        (et, et)).fetchall()
    types = [r[0] for r in conn.execute("SELECT DISTINCT entity_type FROM audit_log WHERE entity_type IS NOT NULL ORDER BY 1")]
    return render_template("admin/audit.html", rows=rows, types=types, entity=et)

