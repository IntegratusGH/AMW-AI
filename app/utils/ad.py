"""Active Directory / LDAP helpers.

Two operations are supported:

* ``ad_lookup_user`` - uses a read-only service account to find a user in the
  directory (validating existence and pulling email / display name) for the
  admin "add AD user" flow.
* ``ad_authenticate`` - verifies an end user's credentials by performing an
  LDAP bind as that user.  No end-user password is ever stored.

Configuration comes from ``app.config`` (AD_* keys, loaded from .env).  Only
non-secret facts are ever surfaced in messages; passwords are never logged.
"""
from __future__ import annotations

from dataclasses import dataclass

from flask import current_app
from ldap3 import NONE, SIMPLE, Connection, Server
from ldap3.core.exceptions import LDAPBindError, LDAPException
from ldap3.utils.conv import escape_filter_chars


class ADError(Exception):
    """Directory unavailable or misconfigured (distinct from bad credentials)."""


@dataclass
class ADUser:
    username: str          # sAMAccountName
    email: str | None
    full_name: str | None
    dn: str | None


def ad_configured() -> bool:
    cfg = current_app.config
    return bool(cfg.get("AD_ENABLED") and cfg.get("AD_SERVER"))


def _server() -> Server:
    cfg = current_app.config
    host = cfg.get("AD_SERVER")
    if not host:
        raise ADError("Active Directory is not configured (AD_SERVER is empty).")
    return Server(
        host,
        port=cfg.get("AD_PORT"),
        use_ssl=bool(cfg.get("AD_USE_SSL", True)),
        get_info=NONE,
    )


def _user_principal(username: str) -> str:
    """Build the bind identity for an end user from configuration."""
    cfg = current_app.config
    suffix = cfg.get("AD_UPN_SUFFIX")
    domain = cfg.get("AD_DOMAIN")
    if suffix:
        return f"{username}@{suffix}"
    if domain:
        return f"{domain}\\{username}"
    return username


def ad_authenticate(username: str, password: str) -> bool:
    """Return True if the credentials bind successfully against AD.

    A failed bind (wrong password / unknown user) returns False; a directory
    problem (server unreachable, TLS failure) raises ADError.
    """
    if not password:
        return False
    server = _server()
    principal = _user_principal(username)
    try:
        conn = Connection(
            server, user=principal, password=password,
            authentication=SIMPLE, auto_bind=True,
        )
    except LDAPBindError:
        return False
    except LDAPException as exc:  # unreachable / TLS / protocol error
        raise ADError(f"Directory bind failed: {exc.__class__.__name__}") from exc
    try:
        return bool(conn.bound)
    finally:
        conn.unbind()


def ad_lookup_user(username: str) -> ADUser | None:
    """Find a user via the read-only service account; None if not present."""
    cfg = current_app.config
    bind_user = cfg.get("AD_BIND_USER")
    bind_password = cfg.get("AD_BIND_PASSWORD")
    base_dn = cfg.get("AD_BASE_DN")
    if not (bind_user and bind_password and base_dn):
        raise ADError(
            "Directory lookup needs AD_BIND_USER, AD_BIND_PASSWORD and AD_BASE_DN."
        )

    server = _server()
    try:
        conn = Connection(
            server, user=bind_user, password=bind_password,
            authentication=SIMPLE, auto_bind=True,
        )
    except LDAPBindError as exc:
        raise ADError("Service account bind failed; check AD_BIND_USER/PASSWORD.") from exc
    except LDAPException as exc:
        raise ADError(f"Directory connection failed: {exc.__class__.__name__}") from exc

    try:
        flt = cfg.get("AD_USER_SEARCH_FILTER", "(sAMAccountName={username})")
        search_filter = flt.format(username=escape_filter_chars(username))
        conn.search(
            search_base=base_dn,
            search_filter=search_filter,
            attributes=["sAMAccountName", "mail", "displayName", "userPrincipalName"],
        )
        if not conn.entries:
            return None
        entry = conn.entries[0]

        def _val(name: str) -> str | None:
            v = getattr(entry, name, None)
            text = str(v) if v else ""
            return text or None

        sam = _val("sAMAccountName") or username
        return ADUser(
            username=sam,
            email=_val("mail"),
            full_name=_val("displayName"),
            dn=entry.entry_dn,
        )
    except LDAPException as exc:
        raise ADError(f"Directory search failed: {exc.__class__.__name__}") from exc
    finally:
        conn.unbind()
