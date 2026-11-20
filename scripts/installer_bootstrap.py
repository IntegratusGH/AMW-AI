"""Install-time bootstrap: dep checks, DB create, Alembic migrate/stamp, seed.

Invoked by the MSI custom action:
    runtime\\python.exe scripts\\installer_bootstrap.py

Exits non-zero on hard failure so the MSI rolls back.  Never logs secrets
(e.g. DB_PASSWORD); status only.  Log file: %PROGRAMDATA%\\AMW-AI\\logs\\install.log
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

PD = Path(os.getenv("PROGRAMDATA", r"C:\ProgramData")) / "AMW-AI"
(PD / "logs").mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=PD / "logs" / "install.log",
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("bootstrap")

MIGRATIONS_DIR = str(INSTALL_DIR / "migrations")


def log_banner() -> None:
    """Log environment facts up front so failures have full context."""
    log.info("=" * 60)
    log.info("Bootstrap starting.")
    log.info("Install dir: %s", INSTALL_DIR)
    log.info("ProgramData: %s", PD)
    log.info("Python: %s", sys.version.replace("\n", " "))
    log.info("Executable: %s", sys.executable)
    try:
        whoami = subprocess.run(["whoami"], capture_output=True, text=True)
        log.info("Running as: %s", whoami.stdout.strip())
    except Exception as exc:  # pragma: no cover
        log.warning("Could not determine current user: %s", exc)


def ensure_pd_permissions() -> None:
    """Grant Everyone modify on the ProgramData dir so the service/app (under
    any account) can read .env and write logs.  Replaces the WiX
    util:PermissionEx, which cannot be authored in the build environment.
    Uses the Everyone SID (S-1-1-0) to stay locale-independent."""
    try:
        subprocess.run(
            ["icacls", str(PD), "/grant", "*S-1-1-0:(OI)(CI)M", "/T", "/C", "/Q"],
            check=False,
            capture_output=True,
        )
        log.info("ProgramData ACL ensured (Everyone: modify).")
    except Exception as exc:  # pragma: no cover
        log.warning("Could not set ProgramData ACL: %s", exc)


def ensure_env() -> None:
    """Create ProgramData\\.env from the template on first install only."""
    env = PD / ".env"
    if not env.exists():
        shutil.copyfile(INSTALL_DIR / ".env.example", env)
        log.warning(
            ".env created from template at %s - EDIT DB_PASSWORD before use", env
        )
    else:
        log.info(".env already present at %s (left unchanged)", env)


def load_env() -> None:
    """Load the installed (ProgramData) .env into the process environment before
    any DB step so provisioning/connection flags (e.g. DB_PROVISION_LOGIN) and
    credentials are honored.  ``create_app`` loads it too, but that import
    happens after the login/DB-provisioning steps, so without this the DB steps
    would run with unset vars (defaults).  Dev fallback does not override."""
    load_dotenv(PD / ".env")
    load_dotenv(INSTALL_DIR / ".env")
    log.info("Environment loaded from %s (dev fallback: %s).",
             PD / ".env", INSTALL_DIR / ".env")


def check_odbc() -> None:
    import pyodbc

    all_drivers = pyodbc.drivers()
    log.info("Installed ODBC drivers: %s", all_drivers)
    drivers = [d for d in all_drivers if "ODBC Driver 17" in d]
    if not drivers:
        log.error("ODBC Driver 17 for SQL Server not found. Install msodbcsql17.")
        sys.exit(2)
    log.info("ODBC Driver 17 for SQL Server detected.")


def log_config() -> None:
    """Log the effective, non-secret DB configuration for diagnostics."""
    from scripts.init_db import log_db_config

    log_db_config()


def ensure_login() -> None:
    from scripts.init_db import provision_login

    provision_login()  # admin connection creates/enables the app SQL login
    log.info("Application login ensured.")


def check_and_create_db() -> None:
    from scripts.init_db import create_database_if_missing

    create_database_if_missing()  # connects to master, CREATE DATABASE if missing
    log.info("Database presence ensured.")


def migrate_db(app) -> None:
    from flask_migrate import stamp, upgrade
    from sqlalchemy import inspect

    from app.extensions import db

    with app.app_context():
        tables = set(inspect(db.engine).get_table_names())
        if "alembic_version" in tables:
            log.info("alembic_version present - upgrading to head.")
            upgrade(directory=MIGRATIONS_DIR)
        elif "users" in tables:  # existing schema, no alembic history
            log.info("Existing schema without alembic history - stamping head.")
            stamp(directory=MIGRATIONS_DIR)
            upgrade(directory=MIGRATIONS_DIR)
        else:  # brand-new empty DB
            log.info("Empty database - creating schema via upgrade head.")
            upgrade(directory=MIGRATIONS_DIR)


def seed(app) -> None:
    from scripts.init_db import seed_roles_admin

    seed_roles_admin(app)  # roles + admin only; schema handled by migrations
    log.info("Roles/admin seeded.")


def seed_modules(app) -> None:
    from scripts.init_db import seed_modules as _seed_modules

    _seed_modules(app)  # default AI modules + role mappings (idempotent)
    log.info("Modules seeded.")


def _step(name: str, func, *args):
    """Run a named step with start/finish logging so a failing step is obvious."""
    log.info("---- step: %s ----", name)
    try:
        result = func(*args)
        log.info("---- step OK: %s ----", name)
        return result
    except Exception:
        log.error("---- step FAILED: %s ----", name)
        raise


def main() -> None:
    log_banner()
    _step("ensure_pd_permissions", ensure_pd_permissions)
    _step("ensure_env", ensure_env)
    _step("load_env", load_env)
    _step("check_odbc", check_odbc)
    _step("log_config", log_config)
    _step("ensure_login", ensure_login)
    _step("check_and_create_db", check_and_create_db)
    from app import create_app

    app = _step("create_app", create_app)
    _step("migrate_db", migrate_db, app)
    _step("seed", seed, app)
    _step("seed_modules", seed_modules, app)
    log.info("Bootstrap complete.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover
        log.exception("Bootstrap failed: %s", exc)
        sys.exit(1)
