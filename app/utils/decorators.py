"""Authorization decorators for role-based access control."""
from __future__ import annotations

from functools import wraps

from flask import abort
from flask_login import current_user

from app.models import ROLE_ADMIN


def role_required(*roles: str):
    """Allow access only to authenticated users holding one of the roles."""

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)
            if not current_user.has_role(*roles):
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def admin_required(view):
    """Shortcut decorator restricting a view to Administrators."""
    return role_required(ROLE_ADMIN)(view)
