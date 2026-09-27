"""Start Rental Tracker (BLUEPRINT §11.1).

    python -m rental_tracker                    # desktop window (pywebview) or your browser
    python -m rental_tracker --data-dir D:\\Rentals
    python -m rental_tracker --demo             # try it with a made-up 60-property portfolio
"""
from __future__ import annotations

import argparse
import logging
import secrets
import socket
import sys
import threading
import webbrowser
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from . import db as dbmod
from .config import DataDir, default_data_root
from .services import backup, search, startup
from .services.instance_lock import AlreadyRunningError, InstanceLock

log = logging.getLogger("rental_tracker")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _setup_logging(data: DataDir) -> None:
    handler = RotatingFileHandler(data.logs / "app.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def prepare(data: DataDir) -> None:
    """Open the database, back up and migrate if needed, check integrity, run catch-up jobs."""
    conn = dbmod.connect(data.db)
    try:
        applied = dbmod.migrate(conn, before=lambda: backup.create_backup(conn, data, "snapshots", "pre-upgrade"))
        if applied:
            log.info("applied %d migration(s)", applied)
        check = conn.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise SystemExit(f"The database failed its integrity check ({check}). Restore a backup from "
                             f"{data.backups} (Backups page, or copy a backup over rental.db).")
        with dbmod.transaction(conn):
            search.rebuild(conn)
        result = startup.run_catch_up(conn, datetime.now().date())
        if result.summary():
            log.info("catch-up: %s", result.summary())
        backup.run_scheduled_backups(conn, data)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rental-tracker", description="Offline rental property tracker")
    parser.add_argument("--data-dir", type=Path, help=f"data folder (default: {default_data_root()})")
    parser.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: a free one)")
    parser.add_argument("--browser", action="store_true", help="open in your web browser instead of a window")
    parser.add_argument("--no-open", action="store_true", help="don't open anything; just print the address")
    parser.add_argument("--demo", action="store_true", help="use a demo data folder filled with sample data")
    parser.add_argument("--version", action="version", version=f"Rental Tracker {__version__}")
    args = parser.parse_args(argv)

    root = args.data_dir or (Path.cwd() / "demo-data" if args.demo else default_data_root())
    data = DataDir(root.expanduser().resolve()).ensure()
    _setup_logging(data)
    try:
        lock = InstanceLock(data.lock).acquire()
    except AlreadyRunningError as e:
        print(e, file=sys.stderr)
        return 1
    started = False
    try:
        if args.demo and not data.db.exists():
            from .demo import build_demo
            print("Creating demo portfolio…")
            build_demo(data)
        prepare(data)

        from werkzeug.serving import make_server

        from .web import create_app

        token = secrets.token_urlsafe(24)
        app = create_app(data, launch_token=token)
        server = make_server("127.0.0.1", args.port or _free_port(), app, threaded=True)
        url = f"http://127.0.0.1:{server.server_port}/auth?token={token}"
        print(f"Rental Tracker {__version__}\n  data:  {data.root}\n  open:  {url}\n  Press Ctrl+C to quit.")
        log.info("started on port %s with data %s", server.server_port, data.root)
        started = True

        if args.no_open or args.browser:
            if args.browser:
                threading.Timer(0.5, webbrowser.open, args=(url,)).start()
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            return 0
        try:
            import webview  # pywebview: optional native window
        except ImportError:
            print("  (pywebview not installed — opening your browser instead)")
            threading.Timer(0.5, webbrowser.open, args=(url,)).start()
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            return 0
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        webview.create_window("Rental Tracker", url, width=1360, height=880, min_size=(900, 600))
        webview.start()
        server.shutdown()
        return 0
    finally:
        if started:
            try:
                conn = dbmod.connect(data.db)
                try:
                    path = backup.create_backup(conn, data, "snapshots", "exit")
                    backup.copy_to_external(conn, data, path)
                finally:
                    conn.close()
            except Exception as e:  # never block quitting because of a backup problem
                log.error("exit backup failed: %s", e)
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
