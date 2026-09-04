"""Application factory for the AMW-AI web application."""
from __future__ import annotations

from datetime import datetime

from flask import Flask

from app.config import Config
from app.extensions import csrf, db, login_manager, migrate


def create_app(config_class: type = Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_class)

    # Initialise extensions
    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    login_manager.init_app(app)

    # Ensure models are imported so migrations/metadata are aware of them.
    from app import models  # noqa: F401

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
