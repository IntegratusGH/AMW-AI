"""Set the SQL Server login used by AMW-AI in both .env files.

Updates DB_USER and DB_PASSWORD in:
  * C:\\ProgramData\\AMW-AI\\.env   (used by the installed app - takes precedence)
  * <repo>\\.env                    (used for local/dev runs)

The password is read with a hidden prompt (getpass); it is never echoed,
logged, or passed on the command line. Run:

    python scripts\\set_db_credentials.py

Optionally pass the username as the first argument (default: ybotsqladmin).
"""
from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

DEFAULT_USER = "ybotsqladmin"


def targets() -> list[Path]:
    pd = Path(os.getenv("PROGRAMDATA", r"C:\ProgramData")) / "AMW-AI" / ".env"
    proj = Path(__file__).resolve().parent.parent / ".env"
    return [pd, proj]


def upsert(path: Path, updates: dict[str, str]) -> None:
    """Set/replace the given KEY=VALUE lines, preserving everything else."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        replaced = False
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                replaced = True
        if not replaced:
            out.append(raw)
    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}={val}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> None:
    user = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_USER

    pw1 = getpass.getpass(f"Password for SQL login '{user}': ")
    pw2 = getpass.getpass("Re-enter password: ")
    if pw1 != pw2:
        print("Passwords do not match; nothing changed.")
        sys.exit(1)
    if not pw1:
        print("Empty password; nothing changed.")
        sys.exit(1)

    updates = {"DB_USER": user, "DB_PASSWORD": pw1}
    for path in targets():
        upsert(path, updates)
        print(f"Updated DB_USER='{user}' and DB_PASSWORD (hidden) in: {path}")

    print("\nDone. Restart the app/service (or re-run the installer bootstrap) "
          "so it picks up the new credentials.")


if __name__ == "__main__":
    main()
