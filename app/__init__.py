"""Application factory for the AMW-AI web application."""
from __future__ import annotations

import os
import threading
from datetime import datetime

from flask import Flask

from app.config import Config
from app.extensions import csrf, db, login_manager, migrate

# Stalled-upload sweeper: how often to scan, plus a one-time start guard so a
# repeated app creation (e.g. in tests) never spawns a second polling thread.
_STALL_SWEEP_POLL_SEC = 60
_STALL_SWEEPER_LOCK = threading.Lock()
_STALL_SWEEPER_STARTED = False


def _configure_logging(app: Flask) -> None:
    """Attach a rotating file handler to the root logger at the configured level
    so runtime errors are traceable.  Set LOG_LEVEL=DEBUG in the .env to capture
    debug detail and full tracebacks.  A single handler on the root logger covers
    app.logger and every module logger via propagation; output goes to
    <ProgramData>/AMW-AI/logs/app.log (rotating)."""
    import logging
    from logging.handlers import RotatingFileHandler

    level_name = str(app.config.get("LOG_LEVEL", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    try:
        os.makedirs(app.config["LOG_DIR"], exist_ok=True)
    except OSError:
        pass

    log_file = os.path.abspath(app.config["LOG_FILE"])
    root = logging.getLogger()
    # Idempotent: a repeated create_app (tests / scripts) must not stack a
    # second handler on the same file.
    if not any(isinstance(h, RotatingFileHandler)
               and getattr(h, "baseFilename", "") == log_file
               for h in root.handlers):
        handler = RotatingFileHandler(
            log_file,
            maxBytes=int(app.config.get("LOG_MAX_BYTES", 5 * 1024 * 1024)),
            backupCount=int(app.config.get("LOG_BACKUP_COUNT", 5)),
            encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s:%(threadName)s] %(message)s"))
        root.addHandler(handler)
    root.setLevel(level)
    app.logger.setLevel(level)
    app.logger.propagate = True
    # Drop Flask's default stderr handler (service stderr is discarded); the
    # root file handler receives app.logger records via propagation.
    try:
        from flask.logging import default_handler
        app.logger.removeHandler(default_handler)
    except Exception:
        pass
    app.logger.info("Logging configured: level=%s file=%s", level_name, log_file)


def create_app(config_class: type = Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_class)
    # Configure logging first so startup steps (recovery/sweeper) are captured.
    _configure_logging(app)

    # Ensure the writable uploads directory exists (module icons).
    try:
        os.makedirs(app.config["MODULE_ICON_DIR"], exist_ok=True)
    except OSError:
        pass

    # Initialise extensions
    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    login_manager.init_app(app)

    # Ensure models are imported so migrations/metadata are aware of them.
    from app import models  # noqa: F401

    # Recover orphaned uploads: a background OCR worker that was killed mid-run
    # (e.g. the service was restarted) leaves its upload stuck at status
    # "processing" forever, which then blocks Process/Re-process (both refuse to
    # act on an already-processing upload).  No worker can survive a restart, so
    # at startup demote any still-"processing" upload to a terminal state.
    _recover_orphaned_uploads(app)
    _start_background_watchers(app)
    _start_stuck_upload_sweeper(app)

    # Register blueprints
    from app.blueprints.auth import auth_bp
    from app.blueprints.main import main_bp
    from app.blueprints.admin import admin_bp
    from app.blueprints.errors import errors_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(errors_bp)

    # Template globals available in every page.
    @app.context_processor
    def inject_globals():
        return {
            "app_name": app.config.get("APP_NAME", "AMW-AI"),
            "app_version": app.config.get("APP_VERSION", "0.0"),
            "current_year": datetime.utcnow().year,
        }

    return app


def _recover_orphaned_uploads(app: Flask) -> None:
    """Reset uploads left in the "processing" state by an interrupted worker so
    they can be (re-)processed again.  Ones that had completed a prior run
    (``processed``) are marked done; the rest are marked errored.  Guarded so a
    fresh/unmigrated database never breaks app creation."""
    from sqlalchemy import inspect as sqla_inspect

    from app.models.registration import (
        CRBookUpload, ReceiptUpload, STATUS_DONE, STATUS_ERROR,
        STATUS_PENDING, STATUS_PROCESSING, STATUS_QUEUED,
    )

    with app.app_context():
        try:
            tables = set(sqla_inspect(db.engine).get_table_names())
            if not {"cr_book_uploads", "receipt_uploads"} <= tables:
                return
            fixed = 0
            for model in (CRBookUpload, ReceiptUpload):
                # Interrupted mid-run (was actively processing): recover to its
                # prior terminal state, or error if it had not completed before.
                for up in model.query.filter_by(status=STATUS_PROCESSING).all():
                    if up.processed:
                        up.status = STATUS_DONE
                    else:
                        up.status = STATUS_ERROR
                        up.error = ("Processing was interrupted (service "
                                    "restart). Re-process to retry.")
                        up.progress_message = "Interrupted \u2014 re-process to retry."
                    fixed += 1
                # Queued for a bulk re-process that never started before the
                # restart: the previous run's records are untouched, so restore
                # the prior result instead of flagging a false error.
                for up in model.query.filter_by(status=STATUS_QUEUED).all():
                    if up.processed or (up.captured_records or 0) > 0:
                        up.status = STATUS_DONE
                        up.processed = True
                    else:
                        up.status = STATUS_PENDING
                    up.error = None
                    up.progress_message = None
                    fixed += 1
            if fixed:
                db.session.commit()
                app.logger.warning(
                    "Recovered %d orphaned upload(s) stuck in 'processing'.",
                    fixed)
        except Exception as exc:  # pragma: no cover - best-effort startup cleanup
            db.session.rollback()
            app.logger.warning("Orphaned-upload recovery skipped: %s", exc)


def _start_background_watchers(app: Flask) -> None:
    """Start long-lived background workers (e.g. the automated CR-book folder
    watcher).  Guarded so a fresh/unmigrated database never breaks startup."""
    from sqlalchemy import inspect as sqla_inspect

    with app.app_context():
        try:
            tables = set(sqla_inspect(db.engine).get_table_names())
        except Exception as exc:  # pragma: no cover - best-effort startup guard
            app.logger.warning("Background watcher start skipped: %s", exc)
            return
    from app.blueprints.main.routes import (
        start_cr_auto_watcher,
        start_rc_auto_watcher,
    )
    if "cr_auto_settings" in tables:
        start_cr_auto_watcher(app)
    if "rc_auto_settings" in tables:
        start_rc_auto_watcher(app)


def _start_stuck_upload_sweeper(app: Flask) -> None:
    """Start a background thread that demotes uploads whose OCR worker died
    mid-run.  The startup recovery (``_recover_orphaned_uploads``) only runs when
    the service restarts, so a worker that ends abnormally while the service
    keeps running leaves its upload stuck at "processing" indefinitely.  This
    sweeper watches live progress and, once an upload has made no progress for
    ``UPLOAD_STALL_TIMEOUT_SEC`` seconds, demotes it to a terminal state so it
    can be re-processed.  Guarded so a fresh/unmigrated database never breaks
    startup and so only one thread is ever started."""
    from sqlalchemy import inspect as sqla_inspect

    with app.app_context():
        try:
            tables = set(sqla_inspect(db.engine).get_table_names())
        except Exception as exc:  # pragma: no cover - best-effort startup guard
            app.logger.warning("Stuck-upload sweeper start skipped: %s", exc)
            return
    if not {"cr_book_uploads", "receipt_uploads"} <= tables:
        return
    global _STALL_SWEEPER_STARTED
    with _STALL_SWEEPER_LOCK:
        if _STALL_SWEEPER_STARTED:
            return
        _STALL_SWEEPER_STARTED = True
    threading.Thread(
        target=_stuck_upload_sweep_loop, args=(app,), daemon=True).start()


def _stuck_upload_sweep_loop(app: Flask) -> None:  # pragma: no cover - thread
    """Poll the upload tables and demote any "processing" upload whose live
    progress (page counter + message) has not changed for the configured stall
    timeout, mirroring the startup orphan recovery."""
    import time

    from app.models.registration import (
        CRBookUpload, ReceiptUpload, STATUS_DONE, STATUS_ERROR,
        STATUS_PROCESSING,
    )

    # (table, id) -> (progress signature, monotonic time first seen at it).
    seen: dict[tuple[str, int], tuple[tuple, float]] = {}
    while True:
        time.sleep(_STALL_SWEEP_POLL_SEC)
        timeout = max(60, int(app.config.get("UPLOAD_STALL_TIMEOUT_SEC", 900)))
        with app.app_context():
            try:
                now = time.monotonic()
                active: set[tuple[str, int]] = set()
                demoted = 0
                for model in (CRBookUpload, ReceiptUpload):
                    for up in model.query.filter_by(
                            status=STATUS_PROCESSING).all():
                        key = (model.__tablename__, up.id)
                        active.add(key)
                        sig = (up.progress_done, up.progress_message)
                        prev = seen.get(key)
                        # First sighting, or progress advanced -> (re)start timer.
                        if prev is None or prev[0] != sig:
                            seen[key] = (sig, now)
                            continue
                        if now - prev[1] < timeout:
                            continue
                        if up.processed:
                            up.status = STATUS_DONE
                        else:
                            mins = timeout // 60
                            up.status = STATUS_ERROR
                            up.error = (
                                f"Processing stalled (no progress for {mins} "
                                f"min). Re-process to retry.")
                            up.progress_message = (
                                "Interrupted \u2014 re-process to retry.")
                        seen.pop(key, None)
                        demoted += 1
                if demoted:
                    db.session.commit()
                    app.logger.warning(
                        "Stuck-upload sweeper demoted %d stalled upload(s).",
                        demoted)
                # Drop keys that are no longer processing so the map stays bounded.
                for key in list(seen):
                    if key not in active:
                        seen.pop(key, None)
            except Exception as exc:
                db.session.rollback()
                app.logger.warning("Stuck-upload sweep skipped: %s", exc)
