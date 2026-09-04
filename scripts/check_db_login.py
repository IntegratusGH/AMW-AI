"""Diagnostic: verify the app SQL login's readiness for install.

Read-only. Loads %PROGRAMDATA%\\AMW-AI\\.env, connects to SQL Server as
DB_USER, and reports non-secret facts only (the password is never printed).

Run on the target server with the bundled runtime:
    "C:\\Program Files\\AMW-AI\\runtime\\python.exe" scripts\\check_db_login.py

Interpreting results (with DB_PROVISION_LOGIN=no the installer connects as
DB_USER for everything, so this login must already satisfy):
  * CONNECT succeeds                -> password + reachability OK
  * database[...]_exists=True        -> no create needed
      else need is_dbcreator=1 (or CREATE ANY DATABASE) to create it
  * in_db: is_db_owner=1 (or CREATE_TABLE) -> migrations/seeding can run
"""
from __future__ import annotations

import os
from pathlib import Path

import pyodbc


def _truthy(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def load_env(path: Path) -> None:
    if not path.exists():
        print(f"WARNING: .env not found at {path}; relying on process env.")
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def server() -> str:
    host = os.getenv("DB_HOST", "localhost").strip()
    port = os.getenv("DB_PORT", "").strip()
    return f"{host},{port}" if port else host


def enc_parts(driver: str) -> list[str]:
    parts: list[str] = []
    if "odbc driver" in driver.lower():
        parts.append("Encrypt=yes" if _truthy("DB_ENCRYPT", "no") else "Encrypt=no")
        if _truthy("DB_TRUST_SERVER_CERTIFICATE", "yes"):
            parts.append("TrustServerCertificate=yes")
    return parts


def conn_str(database: str) -> str:
    driver = os.getenv("DB_DRIVER", "SQL Server").strip()
    return ";".join([
        f"DRIVER={{{driver}}}",
        f"SERVER={server()}",
        f"DATABASE={database}",
        f"UID={os.getenv('DB_USER', 'sa').strip()}",
        f"PWD={os.getenv('DB_PASSWORD', '')}",
        *enc_parts(driver),
    ]) + ";"


def main() -> None:
    env_path = Path(os.getenv("PROGRAMDATA", r"C:\ProgramData")) / "AMW-AI" / ".env"
    load_env(env_path)

    driver = os.getenv("DB_DRIVER", "SQL Server").strip()
    user = os.getenv("DB_USER", "sa").strip()
    db_name = os.getenv("DB_NAME", "AMW-AI").strip()
    pwd_set = bool(os.getenv("DB_PASSWORD", "").strip())
    provision = _truthy("DB_PROVISION_LOGIN", "yes")

    print(f".env={env_path}")
    print(f"server={server()} driver={driver!r} DB_USER={user!r} DB_NAME={db_name!r} "
          f"DB_PASSWORD={'set' if pwd_set else 'MISSING'} DB_PROVISION_LOGIN={provision}")
    print(f"installed_odbc_drivers={pyodbc.drivers()}")
    if provision:
        print("NOTE: DB_PROVISION_LOGIN is still 'yes'. Set it to 'no' in the .env "
              "so the installer skips the sa/trusted admin step.")

    try:
        conn = pyodbc.connect(conn_str("master"), autocommit=True)
    except pyodbc.Error as exc:
        state = exc.args[0] if exc.args else ""
        print(f"CONNECT FAILED as {user!r} (SQLSTATE {state}). "
              f"Check DB_PASSWORD matches the real login and the server is reachable.")
        return

    cur = conn.cursor()
    who = cur.execute("SELECT SUSER_SNAME()").fetchval()
    is_dbcreator = cur.execute("SELECT IS_SRVROLEMEMBER('dbcreator')").fetchval()
    is_sysadmin = cur.execute("SELECT IS_SRVROLEMEMBER('sysadmin')").fetchval()
    can_create_db = cur.execute(
        "SELECT HAS_PERMS_BY_NAME(NULL, NULL, 'CREATE ANY DATABASE')"
    ).fetchval()
    db_exists = cur.execute(
        "SELECT database_id FROM sys.databases WHERE name = ?", db_name
    ).fetchone()

    print(f"connected_as={who!r}")
    print(f"database[{db_name}]_exists={bool(db_exists)}")
    print(f"server_roles: is_dbcreator={is_dbcreator} is_sysadmin={is_sysadmin} "
          f"has_CREATE_ANY_DATABASE={can_create_db}")

    if not db_exists and not (is_dbcreator or is_sysadmin or can_create_db):
        print("ACTION: DB missing and login cannot create it. In SSMS: "
              f"ALTER SERVER ROLE [dbcreator] ADD MEMBER [{user}];")

    if db_exists:
        owner = cur.execute(
            "SELECT SUSER_SNAME(owner_sid) FROM sys.databases WHERE name = ?", db_name
        ).fetchval()
        print(f"database_owner={owner!r}")
        try:
            c2 = pyodbc.connect(conn_str(db_name), autocommit=True)
            cu = c2.cursor()
            dbuser = cu.execute("SELECT USER_NAME()").fetchval()
            is_db_owner = cu.execute("SELECT IS_MEMBER('db_owner')").fetchval()
            is_ddladmin = cu.execute("SELECT IS_MEMBER('db_ddladmin')").fetchval()
            can_create_table = cu.execute(
                "SELECT HAS_PERMS_BY_NAME(NULL, NULL, 'CREATE TABLE')"
            ).fetchval()
            print(f"in_db_user_name={dbuser!r} (login can open [{db_name}])")
            print(f"in_db: is_db_owner={is_db_owner} is_db_ddladmin={is_ddladmin} "
                  f"has_CREATE_TABLE={can_create_table}")
            if not (is_db_owner or is_ddladmin or can_create_table):
                print("ACTION: login cannot create tables. In SSMS: "
                      f"USE [{db_name}]; CREATE USER [{user}] FOR LOGIN [{user}]; "
                      f"ALTER ROLE [db_owner] ADD MEMBER [{user}];")
            c2.close()
        except pyodbc.Error as exc:
            print(f"IN-DB CONNECT FAILED for [{db_name}] (SQLSTATE "
                  f"{exc.args[0] if exc.args else ''}); login has no usable user in the DB. "
                  f"In SSMS: USE [{db_name}]; CREATE USER [{user}] FOR LOGIN [{user}]; "
                  f"ALTER ROLE [db_owner] ADD MEMBER [{user}];")
    conn.close()
    print("Diagnostic complete.")


if __name__ == "__main__":
    main()
