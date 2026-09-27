"""Flask web layer: pages, forms and local-only security (BLUEPRINT §3, §11.4)."""
from __future__ import annotations

import hmac
import logging
import secrets
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from flask import Flask, abort, current_app, flash, g, render_template, request, session

from .. import __version__
from .. import db as dbmod
from ..config import DataDir
from ..domain.money import format_money
from ..services import backup, startup
from ..services.common import ServiceError

log = logging.getLogger(__name__)
LOCAL_HOSTS = {"127.0.0.1", "localhost"}


@dataclass
class AppState:
    data: DataDir
    launch_token: str | None
    today: Callable[[], date]
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_catch_up: date | None = None


def state() -> AppState:
    return current_app.extensions["rental_tracker"]


def today() -> date:
    return state().today()


def db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = dbmod.connect(state().data.db)
    return g.db


@contextmanager
def attempt(success: str | None = None) -> Iterator[dict]:
    """Run a user action in one transaction; show problems as messages instead of crashing.

    Usage:  with attempt("Saved") as ok: ...  then check ok["done"].
    """
    outcome: dict[str, Any] = {"done": False}
    conn = db()
    try:
        with dbmod.transaction(conn):
            yield outcome
        outcome["done"] = True
        if success:
            flash(success, "ok")
    except (ServiceError, ValueError) as e:
        flash(str(e), "error")
    except sqlite3.IntegrityError as e:
        log.warning("integrity error: %s", e)
        flash(f"That conflicts with existing data ({e}).", "error")


def run_catch_up_if_due() -> None:
    st = state()
    t = st.today()
    if st.last_catch_up == t:
        return
    with st.lock:
        if st.last_catch_up == t:
            return
        conn = db()
        result = startup.run_catch_up(conn, t)
        try:
            backup.run_scheduled_backups(conn, st.data)
        except OSError as e:  # never block the app because a backup drive is missing or full
            log.error("scheduled backup failed: %s", e)
            flash(f"Automatic backup failed: {e}", "error")
        st.last_catch_up = t
        if result.summary():
            flash(f"Daily update: {result.summary()}.", "info")


def create_app(data: DataDir, *, launch_token: str | None = None,
               today_fn: Callable[[], date] | None = None, testing: bool = False) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secrets.token_hex(32),
        MAX_CONTENT_LENGTH=50 * 1024 * 1024,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_HTTPONLY=True,
        TESTING=testing,
        TEMPLATES_AUTO_RELOAD=testing,
    )
    app.extensions["rental_tracker"] = AppState(data=data, launch_token=launch_token,
                                                today=today_fn or date.today)

    from .routes import admin, expenses, leases, main, properties, rentday, reports, tenants
    for bp in (main.bp, properties.bp, tenants.bp, leases.bp, rentday.bp, expenses.bp, reports.bp, admin.bp):
        app.register_blueprint(bp)

    from . import filters
    filters.register(app)

    @app.before_request
    def guard():
        host = request.host.rsplit(":", 1)[0]
        if host not in LOCAL_HOSTS:  # blocks DNS-rebinding and LAN access
            abort(400)
        if request.endpoint in ("static", "main.auth"):
            return None
        st = state()
        if st.launch_token and not session.get("auth"):
            return render_template("locked.html"), 403
        if request.method == "POST":
            sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
            expected = session.get("csrf")
            if not expected or not hmac.compare_digest(sent, expected):
                abort(400, "The form expired. Go back, reload the page and try again.")
        run_catch_up_if_due()
        return None

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("Content-Security-Policy",
                                "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                                "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.context_processor
    def inject():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return {"csrf_token": session["csrf"], "today": today(), "version": __version__,
                "now": datetime.now()}

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def http_error(e):
        return render_template("error.html", error=e), e.code

    @app.errorhandler(ServiceError)
    def service_error(e):
        # e.g. an old link to a record that no longer exists
        from werkzeug.exceptions import BadRequest, NotFound
        err = NotFound(str(e)) if "not found" in str(e).lower() else BadRequest(str(e))
        return render_template("error.html", error=err), err.code

    @app.errorhandler(500)
    def server_error(e):
        log.exception("unhandled error")
        return render_template("error.html", error=e), 500

    app.jinja_env.globals["format_money"] = format_money
    return app
