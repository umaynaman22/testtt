"""Start Rental Tracker (BLUEPRINT §11.1).

    python -m rental_tracker                    # desktop window (pywebview) or your browser
    python -m rental_tracker --data-dir D:\\Rentals
    python -m rental_tracker --demo             # try it with a made-up 60-property portfolio

The Windows build (RentalTracker.exe) runs this same code.
"""
from __future__ import annotations

import argparse
import logging
import os
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
FROZEN = bool(getattr(sys, "frozen", False))  # running from the PyInstaller .exe


def alert(message: str, error: bool = True) -> None:
    """Tell the user something. The windowed .exe has no console, so it shows a dialog."""
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    if os.name == "nt" and (FROZEN or sys.stderr is None):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "Rental Tracker", 0x10 if error else 0x40)
        except Exception:  # pragma: no cover - best effort only
            pass


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
                             f"{data.backups}: close the app and copy a backup over rental.db.")
        with dbmod.transaction(conn):
            search.rebuild(conn)
        result = startup.run_catch_up(conn, datetime.now().date())
        if result.summary():
            log.info("catch-up: %s", result.summary())
        backup.run_scheduled_backups(conn, data)
    finally:
        conn.close()


def self_test(report: Path | None) -> int:
    """Check that everything the app needs is present (used by the Windows build pipeline)."""
    lines, ok = [f"Rental Tracker {__version__}", f"python {sys.version.split()[0]} frozen={FROZEN}"], True
    try:
        import sqlite3

        from . import web
        pkg = Path(__file__).parent
        for rel in ("db/migrations/0001_initial.sql", "web/templates/base.html", "web/static/app.css",
                    "web/static/vendor/htmx.min.js"):
            if not (pkg / rel).is_file():
                raise FileNotFoundError(rel)
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        lines.append(f"sqlite {sqlite3.sqlite_version} fts5 ok; files ok; flask app {web.__name__} ok")
        import importlib
        importlib.import_module("webview")
        lines.append("pywebview ok")
        if os.name == "nt":
            from webview.platforms import winforms
            lines.append(f"window renderer {winforms.renderer}")
    except Exception as e:
        ok = False
        lines.append(f"FAIL {type(e).__name__}: {e}")
    lines.append("ok" if ok else "failed")
    text = "\n".join(lines) + "\n"
    if report:
        report.write_text(text, encoding="utf-8")
    if sys.stdout is not None:
        print(text, end="")
    return 0 if ok else 1


def _serve_in_browser(server, url: str, thread: threading.Thread | None = None) -> None:
    threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        if thread is None:
            server.serve_forever()
        else:
            thread.join()
    except KeyboardInterrupt:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rental-tracker", description="Offline rental property tracker")
    parser.add_argument("--data-dir", type=Path, help=f"data folder (default: {default_data_root()})")
    parser.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: a free one)")
    parser.add_argument("--browser", action="store_true", help="open in your web browser instead of a window")
    parser.add_argument("--no-open", action="store_true", help="don't open anything; just print the address")
    parser.add_argument("--demo", action="store_true", help="use a separate demo data folder filled with sample data")
    parser.add_argument("--self-test", nargs="?", const="", metavar="REPORT", help=argparse.SUPPRESS)
    parser.add_argument("--url-file", type=Path, help=argparse.SUPPRESS)  # for automated tests
    parser.add_argument("--version", action="version", version=f"Rental Tracker {__version__}")
    args = parser.parse_args(argv)
    if args.self_test is not None:
        return self_test(Path(args.self_test) if args.self_test else None)

    root = args.data_dir or (default_data_root().with_name("RentalTracker-Demo") if args.demo else default_data_root())
    try:
        data = DataDir(root.expanduser().resolve()).ensure()
    except OSError as e:
        alert(f"Can't use the data folder {root}:\n{e}")
        return 1
    _setup_logging(data)
    try:
        lock = InstanceLock(data.lock).acquire()
    except AlreadyRunningError:
        alert(f"Rental Tracker is already running (data folder {data.root}).\n\n"
              "Switch to its window, or close it before starting another copy.", error=False)
        return 1
    started = False
    try:
        if args.demo and not data.db.exists():
            from .demo import build_demo
            print("Creating demo portfolio…")
            build_demo(data)
        try:
            prepare(data)
        except (SystemExit, dbmod.NewerDatabaseError) as e:
            alert(str(e))
            return 1

        from werkzeug.serving import make_server

        from .web import create_app

        token = secrets.token_urlsafe(24)
        app = create_app(data, launch_token=token)
        state = app.extensions["rental_tracker"]
        server = make_server("127.0.0.1", args.port or _free_port(), app, threaded=True)
        url = f"http://127.0.0.1:{server.server_port}/auth?token={token}"
        print(f"Rental Tracker {__version__}\n  data:  {data.root}\n  open:  {url}\n  Press Ctrl+C to quit.", flush=True)
        log.info("started on port %s with data %s", server.server_port, data.root)
        if args.url_file:
            args.url_file.write_text(url, encoding="utf-8")
        started = True

        if args.no_open or args.browser:
            state.on_quit = server.shutdown
            if args.browser:
                _serve_in_browser(server, url)
            else:
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
            return 0

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        state.on_quit = server.shutdown
        try:
            import webview  # pywebview: the native app window
            if os.name == "nt":
                from webview.platforms import winforms
                if winforms.renderer != "edgechromium":
                    raise RuntimeError("Microsoft Edge WebView2 Runtime is not installed")
        except Exception as e:
            log.warning("no app window (%s); using the browser", e)
            alert("Rental Tracker will open in your web browser, because the app window isn't available "
                  f"on this computer ({e}).\n\nTo get the app window, install the free Microsoft Edge "
                  "WebView2 Runtime. Use 'Quit' in the sidebar to close Rental Tracker.", error=False)
            _serve_in_browser(server, url, thread)
            return 0
        webview.settings["ALLOW_DOWNLOADS"] = True                   # CSV exports and templates
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False   # keep receipts/documents in the app
        width, height, maximized = 1360, 880, False
        try:  # on smaller screens (e.g. 1366x768 laptops) fill the screen instead of spilling off it
            screen = webview.screens[0]
            maximized = screen.width < width + 40 or screen.height < height + 70
            log.info("screen %sx%s, window %s", screen.width, screen.height,
                     "maximized" if maximized else f"{width}x{height}")
        except Exception as e:
            log.warning("couldn't read the screen size: %s", e)
        window = webview.create_window("Rental Tracker", url + "&window=1", width=width, height=height,
                                       maximized=maximized, min_size=(900, 600), text_select=True, zoomable=True)
        state.on_quit = window.destroy
        try:
            webview.start()
        except Exception as e:
            log.exception("app window failed")
            alert(f"The app window couldn't start ({e}). Opening your web browser instead.", error=False)
            state.on_quit = server.shutdown
            _serve_in_browser(server, url, thread)
            return 0
        server.shutdown()
        return 0
    except Exception as e:
        log.exception("Rental Tracker stopped because of an error")
        alert(f"Rental Tracker stopped because of an error:\n{e}\n\nDetails are in {data.logs / 'app.log'}")
        return 1
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
