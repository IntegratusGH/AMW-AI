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

    # --- Uploads (module icons) ------------------------------------------
    # Icons are stored under the writable ProgramData tree (Program Files is
    # not writable by the service account).
    UPLOAD_DIR = str(
        Path(os.getenv("PROGRAMDATA", r"C:\ProgramData")) / "AMW-AI" / "uploads"
    )
    MODULE_ICON_DIR = str(Path(UPLOAD_DIR) / "modules")
    ALLOWED_ICON_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "svg"}

    # --- Uploads (Registration Updater documents) ------------------------
    # Scanned CR-book / receipt PDFs are stored under the writable ProgramData
    # tree, one sub-directory per document type.
    CR_BOOK_UPLOAD_DIR = str(Path(UPLOAD_DIR) / "cr_book")
    RECEIPT_UPLOAD_DIR = str(Path(UPLOAD_DIR) / "receipt")
    # Cropped value-region scans (Chassis No / Reg No) per CR-book record,
    # grouped in a sub-directory per upload id.
    CR_BOOK_CROP_DIR = str(Path(UPLOAD_DIR) / "cr_book_crops")
    # Cropped scan of each receipt's region on the source page, grouped in a
    # sub-directory per upload id.
    RECEIPT_CROP_DIR = str(Path(UPLOAD_DIR) / "receipt_crops")
    # Persistent labeled-data store for the OCR correction feedback loop: a copy
    # of each verified value-region crop (kept even after the source upload is
    # deleted) plus the exported training corpus.
    OCR_LABEL_DIR = str(Path(UPLOAD_DIR) / "ocr_labels")
    # Generated SAP batch-input export files (RMV UPLOAD), one per export action.
    EXPORT_DIR = str(Path(UPLOAD_DIR) / "exports")
    ALLOWED_DOC_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "tif", "tiff"}
    MAX_DOC_UPLOAD_MB = int(os.getenv("MAX_DOC_UPLOAD_MB", "100"))

    # Global request-body cap (also bounds uploads); icons are further limited
    # per-field in the form.  Sized to the larger document uploads so scanned
    # CR-book batches are accepted.  A small margin over the per-file cap covers
    # multipart boundaries + other form fields, so an at-limit file trips the
    # friendly FileSize message instead of a hard 413.
    MAX_CONTENT_LENGTH = max(
        int(os.getenv("MAX_UPLOAD_KB", "1024")) * 1024,
        MAX_DOC_UPLOAD_MB * 1024 * 1024 + 5 * 1024 * 1024,
    )

    # Default admin bootstrap (used by scripts/init_db.py)
    DEFAULT_ADMIN_USERNAME = os.getenv("DEFAULT_ADMIN_USERNAME", "admin")
    DEFAULT_ADMIN_EMAIL = os.getenv("DEFAULT_ADMIN_EMAIL", "admin@example.com")
    DEFAULT_ADMIN_PASSWORD = os.getenv("DEFAULT_ADMIN_PASSWORD", "Admin@12345")

    # --- Active Directory / LDAP -----------------------------------------
    # When enabled, AD-backed accounts are verified by an LDAP bind and the
    # admin add-user flow looks users up via a read-only service account.
    AD_ENABLED = _bool("AD_ENABLED", "no")
    AD_SERVER = os.getenv("AD_SERVER", "").strip()          # host or FQDN of a DC
    AD_PORT = int(os.getenv("AD_PORT", "0")) or None        # 636 (LDAPS) / 389
    AD_USE_SSL = _bool("AD_USE_SSL", "yes")                  # LDAPS recommended
    AD_DOMAIN = os.getenv("AD_DOMAIN", "").strip()          # NetBIOS, e.g. AMWLTD
    AD_UPN_SUFFIX = os.getenv("AD_UPN_SUFFIX", "").strip()  # e.g. corp.example.com
    AD_BASE_DN = os.getenv("AD_BASE_DN", "").strip()        # search base for lookups
    # Read-only service account used only for directory lookups (never for
    # authenticating end users, who bind with their own credentials).
    AD_BIND_USER = os.getenv("AD_BIND_USER", "").strip()
    AD_BIND_PASSWORD = os.getenv("AD_BIND_PASSWORD", "")
    AD_USER_SEARCH_FILTER = os.getenv(
        "AD_USER_SEARCH_FILTER", "(sAMAccountName={username})"
    ).strip()
