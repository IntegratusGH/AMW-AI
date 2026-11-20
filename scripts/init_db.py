"""Bootstrap the AMW-AI database.

Steps:
  1. Connect to the SQL Server 'master' database and CREATE the target
     database if it does not already exist.
  2. Create all application tables.
  3. Seed the default roles and a default administrator account.

Run from the project root:
    venv\\Scripts\\python.exe scripts\\init_db.py
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pyodbc

# Module logger.  When run under the installer bootstrap the root logger is
# already configured to write to %PROGRAMDATA%\AMW-AI\logs\install.log, so
# these messages land there too.  Standalone runs still print via the prints
# below; add a NullHandler so logging never errors if unconfigured.
log = logging.getLogger("init_db")
log.addHandler(logging.NullHandler())

# Make the project root importable when run as a script.
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

# The .env consulted at install time lives under ProgramData; surface its path
# in provisioning error messages so operators know exactly what to edit.
_PD_ENV_HINT = str(
    Path(os.getenv("PROGRAMDATA", r"C:\ProgramData")) / "AMW-AI" / ".env"
)

from app import create_app  # noqa: E402
from app.config import Config  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import (  # noqa: E402
    DEFAULT_ROLES,
    ROLE_ADMIN,
    ROLE_OPERATOR,
    Category,
    Module,
    Role,
    User,
)

# Default AI modules seeded on first run.  "sort" drives display order.
DEFAULT_MODULES = [
    {"key": "registration_updater", "name": "Registration Updater AI",
     "category": "Registration & Compliance", "icon": "file-check",
     "accent": "blue", "status": "available", "sort": 0,
     "description": "Automates vehicle registration (DMT) updates and keeps "
                    "ownership records in sync."},
    {"key": "service_scheduler", "name": "Service Scheduler AI",
     "category": "After-Sales & Service", "icon": "calendar",
     "accent": "green", "status": "available", "sort": 1,
     "description": "Books and optimises workshop and Pitstop service "
                    "appointments automatically."},
    {"key": "service_warranty_reminder", "name": "Service & Warranty Reminder AI",
     "category": "After-Sales & Service", "icon": "bell",
     "accent": "amber", "status": "planned", "sort": 2,
     "description": "Proactively reminds customers of service due dates and "
                    "warranty expiry."},
    {"key": "spare_parts_inventory", "name": "Spare Parts Inventory AI",
     "category": "After-Sales & Service", "icon": "package",
     "accent": "teal", "status": "planned", "sort": 3,
     "description": "Forecasts demand and optimises spare-part, tyre and "
                    "lubricant stock levels."},
    {"key": "payment_reconciliation", "name": "Payment Reconciliation AI",
     "category": "Finance & Leasing", "icon": "credit-card",
     "accent": "green", "status": "available", "sort": 4,
     "description": "Matches customer receipts, bank statements and leasing "
                    "payments automatically."},
    {"key": "leasing_insurance_document", "name": "Leasing & Insurance Document AI",
     "category": "Finance & Leasing", "icon": "shield",
     "accent": "purple", "status": "planned", "sort": 5,
     "description": "Extracts and verifies KYC and contract documents for "
                    "AMW Capital Leasing."},
    {"key": "invoice_expense_ocr", "name": "Invoice & Expense OCR AI",
     "category": "Finance & Leasing", "icon": "scan",
     "accent": "amber", "status": "planned", "sort": 6,
     "description": "Reads supplier invoices and posts expense data without "
                    "manual entry."},
    {"key": "email_message_handler", "name": "Email / Message Handler AI",
     "category": "Sales & Customer Engagement", "icon": "mail",
     "accent": "blue", "status": "available", "sort": 7,
     "description": "Triages incoming email, WhatsApp and SMS and drafts "
                    "suggested replies."},
    {"key": "showroom_assistant", "name": "Showroom Assistant AI",
     "category": "Sales & Customer Engagement", "icon": "message-circle",
     "accent": "purple", "status": "planned", "sort": 8,
     "description": "Answers product, pricing and availability questions "
                    "across all brands."},
    {"key": "vehicle_sales_lead", "name": "Vehicle Sales Lead AI",
     "category": "Sales & Customer Engagement", "icon": "target",
     "accent": "blue", "status": "planned", "sort": 9,
     "description": "Qualifies and follows up showroom enquiries to boost "
                    "conversions."},
    {"key": "customer_feedback_sentiment", "name": "Customer Feedback & Sentiment AI",
     "category": "Back-office & Insight", "icon": "star",
     "accent": "teal", "status": "planned", "sort": 10,
     "description": "Analyses after-sales feedback and reviews to surface "
                    "issues early."},
    {"key": "sales_service_analytics", "name": "Sales & Service Analytics AI",
     "category": "Back-office & Insight", "icon": "chart",
     "accent": "blue", "status": "planned", "sort": 11,
     "description": "Turns sales and service data into forecasts and live "
                    "dashboards."},
    {"key": "hr_recruitment_screening", "name": "HR Recruitment Screening AI",
     "category": "Back-office & Insight", "icon": "user-check",
     "accent": "purple", "status": "planned", "sort": 12,
     "description": "Screens career applications and shortlists candidates "
                    "faster."},
]

# Modules the Operator role sees by default (the delivered/available ones).
OPERATOR_MODULE_KEYS = [
    "registration_updater", "service_scheduler",
    "payment_reconciliation", "email_message_handler",
]

# Category display order (first appearance in DEFAULT_MODULES).
DEFAULT_CATEGORY_ORDER = [
    "Registration & Compliance",
    "After-Sales & Service",
    "Finance & Leasing",
    "Sales & Customer Engagement",
    "Back-office & Insight",
]


def _server() -> str:
    host = os.getenv("DB_HOST", "localhost").strip()
    port = os.getenv("DB_PORT", "").strip()
    return f"{host},{port}" if port else host


def _bool(name: str, default: str = "no") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _encryption_parts(driver: str) -> list[str]:
    """Encrypt / TrustServerCertificate are only valid for the modern
    "ODBC Driver 17/18 for SQL Server"; the legacy driver rejects them."""
    parts: list[str] = []
    if "odbc driver" in driver.lower():
        parts.append("Encrypt=yes" if _bool("DB_ENCRYPT", "no") else "Encrypt=no")
        if _bool("DB_TRUST_SERVER_CERTIFICATE", "yes"):
            parts.append("TrustServerCertificate=yes")
    return parts


def _master_connection_string() -> str:
    driver = os.getenv("DB_DRIVER", "SQL Server").strip()
    parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={_server()}",
        "DATABASE=master",
        f"UID={os.getenv('DB_USER', 'sa')}",
        f"PWD={os.getenv('DB_PASSWORD', '')}",
        *_encryption_parts(driver),
    ]
    return ";".join(parts) + ";"


def _admin_master_connection_string() -> str:
    """Connection string to 'master' using an administrative identity.

    Used to provision the application login before the app credential can be
    used.  By default a Windows trusted connection is attempted (the installer
    runs elevated and BUILTIN\\Administrators is a sysadmin), which needs no
    stored password.  Set DB_ADMIN_TRUSTED=no to use DB_ADMIN_USER /
    DB_ADMIN_PASSWORD (defaulting to the SQL 'sa' account) instead.
    """
    driver = os.getenv("DB_DRIVER", "SQL Server").strip()
    parts = [f"DRIVER={{{driver}}}", f"SERVER={_server()}", "DATABASE=master"]
    if _bool("DB_ADMIN_TRUSTED", "yes"):
        parts.append("Trusted_Connection=yes")
    else:
        parts.append(f"UID={os.getenv('DB_ADMIN_USER', 'sa')}")
        parts.append(f"PWD={os.getenv('DB_ADMIN_PASSWORD', os.getenv('DB_PASSWORD', ''))}")
    parts.extend(_encryption_parts(driver))
    # Bounded login timeout so a misconfigured/unreachable admin identity fails
    # fast instead of hanging the installer custom action indefinitely.
    parts.append("Connection Timeout=5")
    return ";".join(parts) + ";"


def _redact_conn(conn_str: str) -> str:
    """Return a connection string safe to log: PWD value replaced with ***."""
    out = []
    for part in conn_str.split(";"):
        if part.upper().startswith("PWD="):
            out.append("PWD=***" if part[4:] else "PWD=<empty>")
        else:
            out.append(part)
    return ";".join(out)


def log_db_config() -> None:
    """Log the effective, non-secret DB configuration for diagnostics.

    Never logs passwords; only names, flags, and presence booleans."""
    def present(name: str) -> str:
        return "set" if os.getenv(name, "").strip() else "MISSING"

    log.info("DB config: server=%s db=%s driver=%r",
             _server(), os.getenv("DB_NAME", "AMW-AI"),
             os.getenv("DB_DRIVER", "SQL Server").strip())
    log.info("DB app login: DB_USER=%r DB_PASSWORD=%s",
             os.getenv("DB_USER", "sa"), present("DB_PASSWORD"))
    log.info("DB provisioning: DB_PROVISION_LOGIN=%s DB_ADMIN_TRUSTED=%s "
             "DB_ADMIN_USER=%r DB_ADMIN_PASSWORD=%s",
             _bool("DB_PROVISION_LOGIN", "yes"),
             _bool("DB_ADMIN_TRUSTED", "yes"),
             os.getenv("DB_ADMIN_USER", "sa"),
             present("DB_ADMIN_PASSWORD"))
    log.info("DB encryption: DB_ENCRYPT=%s DB_TRUST_SERVER_CERTIFICATE=%s",
             _bool("DB_ENCRYPT", "no"), _bool("DB_TRUST_SERVER_CERTIFICATE", "yes"))
    log.info("Installed ODBC drivers: %s", pyodbc.drivers())


def _admin_perm_message(who: str, trusted: bool) -> str:
    """Actionable guidance when the admin identity cannot create logins."""
    hint = (
        "the account running the installer is not a SQL sysadmin"
        if trusted
        else "the DB_ADMIN_USER account is not a SQL sysadmin/securityadmin"
    )
    return (
        f"Admin identity '{who}' lacks permission to create SQL logins "
        f"({hint}). Provide sysadmin credentials by setting, in "
        f"{_PD_ENV_HINT}:\n"
        "    DB_PROVISION_LOGIN=yes\n"
        "    DB_ADMIN_TRUSTED=no\n"
        "    DB_ADMIN_USER=sa\n"
        "    DB_ADMIN_PASSWORD=<sa password>\n"
        "then re-run.  (The installer's Bootstrap action runs as "
        "NT AUTHORITY\\SYSTEM, which is usually NOT a SQL sysadmin, so the "
        "default Windows trusted admin path will fail.)  Alternatively set "
        "DB_PROVISION_LOGIN=no and create the login manually."
    )


def provision_login() -> None:
    """Ensure the application's SQL login (DB_USER) exists and is usable.

    Idempotent and safe to skip: set DB_PROVISION_LOGIN=no to disable.  Only
    SQL-authentication logins are provisioned; a trusted/Windows DB_USER (one
    containing a backslash) or the built-in 'sa' account is left untouched.

    Requires an administrative connection (see _admin_master_connection_string);
    the app credential itself cannot create logins.  Never logs the password.
    """
    if not _bool("DB_PROVISION_LOGIN", "yes"):
        print("[login] DB_PROVISION_LOGIN disabled; skipping login provisioning.")
        log.info("Login provisioning disabled (DB_PROVISION_LOGIN=no); skipping.")
        return

    login = os.getenv("DB_USER", "sa").strip()
    password = os.getenv("DB_PASSWORD", "")
    if not login or login.lower() == "sa" or "\\" in login:
        print(f"[login] Login '{login}' needs no provisioning; skipping.")
        log.info("Login '%s' needs no provisioning (sa/Windows/empty); skipping.", login)
        return
    if not password:
        print(f"[login] DB_PASSWORD empty; cannot provision login '{login}'.")
        log.error("DB_PASSWORD empty; cannot provision login '%s'.", login)
        return

    trusted = _bool("DB_ADMIN_TRUSTED", "yes")
    admin_desc = "Windows trusted connection" if trusted else f"'{os.getenv('DB_ADMIN_USER', 'sa')}'"
    print(f"[login] Ensuring SQL login '{login}' exists on {_server()} ...")
    print(f"[login] Provisioning uses an admin connection ({admin_desc}).")
    admin_cs = _admin_master_connection_string()
    log.info("Provisioning login '%s' on %s via admin connection (%s).",
             login, _server(), admin_desc)
    log.info("Admin connection string: %s", _redact_conn(admin_cs))
    try:
        conn = pyodbc.connect(admin_cs, autocommit=True)
    except pyodbc.Error as exc:
        state = exc.args[0] if exc.args else ""
        log.error("Admin connection FAILED (SQLSTATE %s): %s",
                  state, exc.args[-1] if len(exc.args) > 1 else exc)
        if "28000" in str(state):
            log.error("SQLSTATE 28000 = authentication failed for the admin "
                      "identity (%s). Check DB_ADMIN_* in %s and that SQL "
                      "Server allows this login (mixed mode for sa).",
                      admin_desc, _PD_ENV_HINT)
        raise
    try:
        cur = conn.cursor()
        # Identifiers cannot be parameterised; quote to guard special chars.
        ident = "[" + login.replace("]", "]]") + "]"
        literal = "'" + password.replace("'", "''") + "'"

        # Preflight: the admin identity must be able to create logins.  Without
        # this the CREATE LOGIN below fails with a raw error 15247; check first
        # and raise an actionable message instead.
        who = cur.execute("SELECT SUSER_SNAME()").fetchval()
        can_create = cur.execute(
            "SELECT CASE WHEN IS_SRVROLEMEMBER('sysadmin') = 1 "
            "OR IS_SRVROLEMEMBER('securityadmin') = 1 "
            "OR HAS_PERMS_BY_NAME(NULL, NULL, 'ALTER ANY LOGIN') = 1 "
            "OR HAS_PERMS_BY_NAME(NULL, NULL, 'CONTROL SERVER') = 1 "
            "THEN 1 ELSE 0 END"
        ).fetchval()
        log.info("Connected to master as '%s'; can_create_logins=%s.",
                 who, bool(can_create))
        if not can_create:
            log.error(_admin_perm_message(who, trusted))
            raise PermissionError(_admin_perm_message(who, trusted))

        try:
            exists = cur.execute(
                "SELECT 1 FROM sys.sql_logins WHERE name = ?", login
            ).fetchone()
            log.info("Login '%s' exists=%s.", login, bool(exists))
            if exists:
                cur.execute(
                    f"ALTER LOGIN {ident} WITH PASSWORD = {literal}, "
                    f"CHECK_POLICY = OFF; ALTER LOGIN {ident} ENABLE;"
                )
                print(f"      Updated existing login '{login}' (password reset, enabled).")
                log.info("Updated existing login '%s' (password reset, enabled).", login)
            else:
                cur.execute(
                    f"CREATE LOGIN {ident} WITH PASSWORD = {literal}, "
                    f"CHECK_POLICY = OFF, DEFAULT_DATABASE = master;"
                )
                print(f"      Created login '{login}'.")
                log.info("Created login '%s'.", login)
            # dbcreator lets the app login CREATE its own database; ownership of
            # the created DB then grants full rights inside it.
            cur.execute(f"ALTER SERVER ROLE [dbcreator] ADD MEMBER {ident};")
            print(f"      Granted 'dbcreator' to '{login}'.")
            log.info("Granted 'dbcreator' server role to '%s'.", login)
        except pyodbc.Error as exc:
            # 15247 = "User does not have permission to perform this action."
            log.error("Login DDL FAILED (SQLSTATE %s): %s",
                      exc.args[0] if exc.args else "", exc.args[-1] if len(exc.args) > 1 else exc)
            if "15247" in "".join(str(a) for a in exc.args):
                log.error(_admin_perm_message(who, trusted))
                raise PermissionError(_admin_perm_message(who, trusted)) from exc
            raise
    finally:
        conn.close()


def create_database_if_missing() -> None:
    db_name = os.getenv("DB_NAME", "AMW-AI")
    print(f"[1/3] Ensuring database [{db_name}] exists on {_server()} ...")
    app_cs = _master_connection_string()
    log.info("Ensuring database [%s] on %s as DB_USER=%r.",
             db_name, _server(), os.getenv("DB_USER", "sa"))
    log.info("App connection string: %s", _redact_conn(app_cs))
    try:
        conn = pyodbc.connect(app_cs, autocommit=True)
    except pyodbc.Error as exc:
        state = exc.args[0] if exc.args else ""
        log.error("App connection FAILED (SQLSTATE %s): %s",
                  state, exc.args[-1] if len(exc.args) > 1 else exc)
        if "28000" in str(state):
            log.error("SQLSTATE 28000 = login failed for DB_USER=%r. If the "
                      "login was just provisioned, verify DB_PASSWORD matches "
                      "what provisioning set. See %s.",
                      os.getenv("DB_USER", "sa"), _PD_ENV_HINT)
        raise
    try:
        cur = conn.cursor()
        cur.execute("SELECT database_id FROM sys.databases WHERE name = ?", db_name)
        if cur.fetchone():
            print(f"      Database [{db_name}] already exists.")
            log.info("Database [%s] already exists.", db_name)
        else:
            # Database identifiers cannot be parameterised; brackets guard the hyphen.
            cur.execute(f"CREATE DATABASE [{db_name}]")
            print(f"      Created database [{db_name}].")
            log.info("Created database [%s].", db_name)
    finally:
        conn.close()


def seed_roles_admin(app) -> None:
    """Seed default roles and the administrator account (data only).

    Assumes the schema already exists (created by Alembic migrations or a
    prior db.create_all()).  Idempotent: safe to run repeatedly.
    """
    with app.app_context():
        print("Seeding roles and default administrator ...")
        for name, description in DEFAULT_ROLES.items():
            if not Role.query.filter_by(name=name).first():
                db.session.add(Role(name=name, description=description))
        db.session.commit()

        admin_username = Config.DEFAULT_ADMIN_USERNAME
        if not User.query.filter_by(username=admin_username).first():
            admin_role = Role.query.filter_by(name="Administrator").first()
            admin = User(
                username=admin_username,
                email=Config.DEFAULT_ADMIN_EMAIL,
                full_name="System Administrator",
                role_id=admin_role.id,
                is_active=True,
                is_ai_trainer=True,
                must_change_password=True,
            )
            admin.set_password(Config.DEFAULT_ADMIN_PASSWORD)
            db.session.add(admin)
            db.session.commit()
            print(f"      Created administrator '{admin_username}'.")
            print("      NOTE: change this password at first sign in.")
        else:
            print(f"      Administrator '{admin_username}' already exists.")


def seed_modules(app) -> None:
    """Seed default AI modules and role mappings (data only).

    Assumes the schema and roles already exist.  Idempotent: does nothing if
    any module already exists, so admin edits are never overwritten.
    """
    with app.app_context():
        if Module.query.count() > 0:
            print("      Modules already present; skipping module seed.")
            return
        print("Seeding AI modules ...")
        cat_by_name: dict[str, Category] = {}
        for i, cname in enumerate(DEFAULT_CATEGORY_ORDER):
            category = Category(name=cname, is_active=True, sort_order=i)
            db.session.add(category)
            cat_by_name[cname] = category
        db.session.flush()  # assign category ids before linking modules

        created: dict[str, Module] = {}
        for data in DEFAULT_MODULES:
            module = Module(
                key=data["key"], name=data["name"],
                category=cat_by_name.get(data["category"]),
                description=data["description"], icon_kind="builtin",
                icon_value=data["icon"], icon_size=32, accent=data["accent"],
                status=data["status"], is_active=True, sort_order=data["sort"],
            )
            db.session.add(module)
            created[data["key"]] = module
        db.session.commit()

        admin_role = Role.query.filter_by(name=ROLE_ADMIN).first()
        operator_role = Role.query.filter_by(name=ROLE_OPERATOR).first()
        if admin_role:
            admin_role.modules = list(created.values())
        if operator_role:
            operator_role.modules = [
                created[k] for k in OPERATOR_MODULE_KEYS if k in created
            ]
        db.session.commit()
        print(f"      Seeded {len(created)} modules.")


def seed(app) -> None:
    """Standalone bootstrap: create all tables then seed roles/admin/modules."""
    with app.app_context():
        print("[2/3] Creating tables ...")
        db.create_all()

    print("[3/3] Seeding roles, administrator and modules ...")
    seed_roles_admin(app)
    seed_modules(app)


def main() -> None:
    provision_login()
    create_database_if_missing()
    app = create_app()
    seed(app)
    print("\nDone. The AMW-AI database is ready.")


if __name__ == "__main__":
    main()
