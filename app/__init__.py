"""Application factory for the AMW-AI web application."""
from __future__ import annotations

import os
from datetime import datetime

from flask import Flask

from app.config import Config
from app.extensions import csrf, db, login_manager, migrate


def create_app(config_class: type = Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_class)

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
        STATUS_PROCESSING,
    )

    with app.app_context():
        try:
            tables = set(sqla_inspect(db.engine).get_table_names())
            if not {"cr_book_uploads", "receipt_uploads"} <= tables:
                return
            fixed = 0
            for model in (CRBookUpload, ReceiptUpload):
                for up in model.query.filter_by(status=STATUS_PROCESSING).all():
                    if up.processed:
                        up.status = STATUS_DONE
                    else:
                        up.status = STATUS_ERROR
                        up.error = ("Processing was interrupted (service "
                                    "restart). Re-process to retry.")
                        up.progress_message = "Interrupted \u2014 re-process to retry."
                    fixed += 1
            if fixed:
                db.session.commit()
                app.logger.warning(
                    "Recovered %d orphaned upload(s) stuck in 'processing'.",
                    fixed)
        except Exception as exc:  # pragma: no cover - best-effort startup cleanup
            db.session.rollback()
            app.logger.warning("Orphaned-upload recovery skipped: %s", exc)
