"""Dashboard, search, launch-token login, quit, and the delete confirmation page."""
from __future__ import annotations

import hmac
import logging
import threading

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

from ... import db as dbmod
from ...services import backup, deletion
from ...services import dashboard as dashboard_svc
from ...services import search as search_svc
from .. import db, state, today

bp = Blueprint("main", __name__)
log = logging.getLogger(__name__)

SEARCH_LINKS = {
    "property": ("properties.detail", "pid"),
    "tenant": ("tenants.detail", "tid"),
}


@bp.route("/auth")
def auth():
    token = state().launch_token
    if token and hmac.compare_digest(request.args.get("token", ""), token):
        session.clear()
        session["auth"] = True
        log.info("opened in the %s", "app window" if request.args.get("window") == "1" else "browser")
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
    return render_template("dashboard.html", d=dashboard_svc.build(db(), today()))


@bp.route("/search")
def search():
    q = request.args.get("q", "").strip()
    results = [r for r in (search_svc.search(db(), q) if q else []) if r["entity_type"] in SEARCH_LINKS]
    if len(results) == 1:
        endpoint, arg = SEARCH_LINKS[results[0]["entity_type"]]
        return redirect(url_for(endpoint, **{arg: results[0]["entity_id"]}))
    links = [(r, url_for(SEARCH_LINKS[r["entity_type"]][0], **{SEARCH_LINKS[r["entity_type"]][1]: r["entity_id"]}))
             for r in results]
    return render_template("search.html", q=q, results=links)


# Where to go after deleting, and back to if it can't be deleted.
AFTER_DELETE = {
    "property": lambda p: url_for("properties.index"),
    "lease": lambda p: url_for("properties.detail", pid=p.parent["property_id"]),
    "tenant": lambda p: url_for("tenants.index"),
}
RECORD_PAGE = {
    "property": ("properties.detail", "pid"), "lease": ("leases.detail", "lease_id"),
    "tenant": ("tenants.detail", "tid"),
}


@bp.route("/delete/<kind>/<int:record_id>", methods=["POST"])
def delete_record(kind: str, record_id: int):
    """Delete straight away (no confirmation), after a safety backup."""
    if kind not in deletion.KINDS:
        abort(404)
    conn = db()
    p = deletion.preview(conn, kind, record_id)
    try:
        deletion.validate(p)
        backup.create_backup(conn, state().data, "snapshots", f"before-delete-{kind}")
        with dbmod.transaction(conn):
            deletion.delete(conn, kind, record_id)
        flash(f"Deleted {p.label}.", "ok")
        return redirect(AFTER_DELETE[kind](p))
    except (ValueError, OSError) as e:
        flash(str(e), "error")
    endpoint, arg = RECORD_PAGE[kind]
    return redirect(url_for(endpoint, **{arg: record_id}))
