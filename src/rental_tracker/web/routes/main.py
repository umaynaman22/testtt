"""Dashboard, search, launch-token login and document files."""
from __future__ import annotations

import hmac
import logging
import threading

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, session, url_for

from ...services import dashboard as dashboard_svc
from ... import db as dbmod
from ...services import backup, deletion, documents
from ...services import search as search_svc
from .. import attempt, db, state, today
from ..forms import Form
from . import safe_next

bp = Blueprint("main", __name__)
log = logging.getLogger(__name__)

SEARCH_LINKS = {
    "property": ("properties.detail", "pid"),
    "unit": ("properties.unit_detail", "unit_id"),
    "tenant": ("tenants.detail", "tid"),
    "vendor": ("expenses.vendor_detail", "vendor_id"),
    "owner": ("properties.owner_edit", "owner_id"),
}


@bp.route("/auth")
def auth():
    token = state().launch_token
    if token and hmac.compare_digest(request.args.get("token", ""), token):
        session.clear()
        session["auth"] = True
        session["in_window"] = request.args.get("window") == "1"
        log.info("opened in the %s", "app window" if session["in_window"] else "browser")
        return redirect(url_for("main.dashboard"))
    return render_template("locked.html"), 403


@bp.route("/quit", methods=["POST"])
def quit_app():
    """Close the app (a backup is taken on the way out)."""
    on_quit = state().on_quit
    if on_quit is None:
        abort(404)
    threading.Timer(0.3, on_quit).start()
    return render_template("closed.html")


@bp.route("/")
def dashboard():
    return render_template("dashboard.html", d=dashboard_svc.build(db(), today(), state().data))


@bp.route("/search")
def search():
    q = request.args.get("q", "").strip()
    results = search_svc.search(db(), q) if q else []
    if len(results) == 1:
        endpoint, arg = SEARCH_LINKS[results[0]["entity_type"]]
        return redirect(url_for(endpoint, **{arg: results[0]["entity_id"]}))
    links = [(r, url_for(SEARCH_LINKS[r["entity_type"]][0], **{SEARCH_LINKS[r["entity_type"]][1]: r["entity_id"]}))
             for r in results]
    return render_template("search.html", q=q, results=links)


@bp.route("/documents/<int:doc_id>")
def document(doc_id: int):
    try:
        doc = documents.get(db(), doc_id)
        path = documents.file_path(state().data, doc)
    except ValueError:
        abort(404)
    if not path.exists():
        abort(404, "The file is missing from the documents folder. Restore it from a backup.")
    mime = doc["mime_type"] or ""
    inline = mime.startswith(("image/", "application/pdf", "text/plain")) and "svg" not in mime
    return send_file(path, mimetype=doc["mime_type"], as_attachment=not inline,
                     download_name=doc["original_filename"] or path.name)


@bp.route("/documents/upload", methods=["POST"])
def document_upload():
    f = Form(request.form)
    upload = request.files.get("file")
    with attempt("File attached"):
        related_id = f.int("related_id", "Record", required=True)
        expires = f.date("expires_on", "Expiry date")
        if not upload or not upload.filename:
            f.errors.append("Choose a file to attach")
        f.check()
        documents.store(db(), state().data, related_type=f.raw("related_type"), related_id=related_id,
                        filename=upload.filename, content=upload.read(), title=f.str("title"),
                        doc_type=f.raw("doc_type") or "other", expires_on=expires)
    return redirect(safe_next(request.form.get("next"), url_for("main.dashboard")))


@bp.route("/documents/<int:doc_id>/delete", methods=["POST"])
def document_delete(doc_id: int):
    with attempt() as r:
        r["msg"] = deletion.delete_document(db(), doc_id)
    if r["done"]:
        flash(r["msg"], "ok")
    return redirect(safe_next(request.form.get("next"), url_for("main.dashboard")))


# Where to go after deleting, and where "Cancel" goes.
AFTER_DELETE = {
    "owner": lambda p: url_for("properties.owners"),
    "property": lambda p: url_for("properties.index"),
    "unit": lambda p: url_for("properties.detail", pid=p.parent["property_id"]),
    "lease": lambda p: url_for("properties.unit_detail", unit_id=p.parent["unit_id"]),
    "tenant": lambda p: url_for("tenants.index"),
    "vendor": lambda p: url_for("expenses.vendors"),
    "category": lambda p: url_for("admin.categories"),
}
RECORD_PAGE = {
    "owner": ("properties.owner_edit", "owner_id"), "property": ("properties.detail", "pid"),
    "unit": ("properties.unit_detail", "unit_id"), "lease": ("leases.detail", "lease_id"),
    "tenant": ("tenants.detail", "tid"), "vendor": ("expenses.vendor_detail", "vendor_id"),
}


@bp.route("/delete/<kind>/<int:record_id>", methods=["GET", "POST"])
def delete_record(kind: str, record_id: int):
    """Show exactly what a delete removes, then do it (after a safety backup)."""
    if kind not in deletion.KINDS:
        abort(404)
    conn = db()
    p = deletion.preview(conn, kind, record_id)
    if request.method == "POST":
        typed, move_to = request.form.get("confirm"), request.form.get("move_to", type=int)
        try:
            deletion.validate(p, typed, move_to)
            backup.create_backup(conn, state().data, "snapshots", f"before-delete-{kind}")
            with dbmod.transaction(conn):
                deletion.delete(conn, kind, record_id, typed=typed, move_to=move_to)
            flash(f"Deleted {p.label}. A backup was saved just before, in case you need it back "
                  "(Backups page).", "ok")
            return redirect(AFTER_DELETE[kind](p))
        except (ValueError, OSError) as e:
            flash(str(e), "error")
    endpoint, arg = RECORD_PAGE.get(kind, ("admin.categories", None))
    cancel = url_for(endpoint, **({arg: record_id} if arg else {}))
    return render_template("delete.html", p=p, cancel_url=cancel, confirm_word=deletion.CONFIRM_WORD)
