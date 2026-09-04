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
from app.models import DEFAULT_ROLES, Role, User  # noqa: E402


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
                must_change_password=True,
            )
            admin.set_password(Config.DEFAULT_ADMIN_PASSWORD)
            db.session.add(admin)
            db.session.commit()
            print(f"      Created administrator '{admin_username}'.")
            print("      NOTE: change this password at first sign in.")
        else:
            print(f"      Administrator '{admin_username}' already exists.")


def seed(app) -> None:
    """Standalone bootstrap: create all tables then seed roles/admin."""
    with app.app_context():
        print("[2/3] Creating tables ...")
        db.create_all()

    print("[3/3] Seeding roles and default administrator ...")
    seed_roles_admin(app)


def main() -> None:
    provision_login()
    create_database_if_missing()
    app = create_app()
    seed(app)
    print("\nDone. The AMW-AI database is ready.")


if __name__ == "__main__":
    main()
