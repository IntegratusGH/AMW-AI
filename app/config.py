"""Application configuration loaded from environment variables (.env)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import URL

# Load the .env file from the installed (ProgramData) location first, then
# fall back to the project root for development.  load_dotenv does not
# override variables that are already set, so the ProgramData copy wins.
BASE_DIR = Path(__file__).resolve().parent.parent
_PROGRAMDATA_ENV = Path(os.getenv("PROGRAMDATA", r"C:\ProgramData")) / "AMW-AI" / ".env"
load_dotenv(_PROGRAMDATA_ENV)          # preferred (installed location)
load_dotenv(BASE_DIR / ".env")         # dev fallback (does not override existing)

# Import the version single source of truth.
try:
    from app.version import __version__ as _APP_VERSION
except Exception:  # pragma: no cover
    _APP_VERSION = "0.0"


def _bool(name: str, default: str = "no") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def build_database_url() -> URL:
    """Construct a SQLAlchemy URL for MS SQL Server via pyodbc.

    Using URL.create keeps special characters in the password safe.
    """
    host = os.getenv("DB_HOST", "localhost").strip()
    port = os.getenv("DB_PORT", "").strip()
    server = f"{host},{port}" if port else host

    driver = os.getenv("DB_DRIVER", "SQL Server").strip()
    query = {"driver": driver}
    # Encrypt / TrustServerCertificate are only understood by the modern
    # "ODBC Driver 17/18 for SQL Server"; the legacy "SQL Server" driver
    # rejects them as invalid connection-string attributes.
    if "odbc driver" in driver.lower():
        query["Encrypt"] = "yes" if _bool("DB_ENCRYPT", "no") else "no"
        if _bool("DB_TRUST_SERVER_CERTIFICATE", "yes"):
            query["TrustServerCertificate"] = "yes"

    return URL.create(
        "mssql+pyodbc",
        username=os.getenv("DB_USER", "sa"),
        password=os.getenv("DB_PASSWORD", ""),
        host=server,
        database=os.getenv("DB_NAME", "AMW-AI"),
        query=query,
    )


class Config:
    """Base configuration shared by all environments."""

    SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-change-me")

    SQLALCHEMY_DATABASE_URI = build_database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True, "pool_recycle": 3600}

    # Session / cookie hardening
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_HTTPONLY = True
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 8  # 8 hours

    # Network server (Waitress)
    HOST = os.getenv("HOST", "0.0.0.0")
    PORT = int(os.getenv("PORT", "8080"))

    # Application metadata
    APP_NAME = "AMW-AI"
    APP_VERSION = _APP_VERSION

    # Default admin bootstrap (used by scripts/init_db.py)
    DEFAULT_ADMIN_USERNAME = os.getenv("DEFAULT_ADMIN_USERNAME", "admin")
    DEFAULT_ADMIN_EMAIL = os.getenv("DEFAULT_ADMIN_EMAIL", "admin@example.com")
    DEFAULT_ADMIN_PASSWORD = os.getenv("DEFAULT_ADMIN_PASSWORD", "Admin@12345")
