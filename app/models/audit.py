"""Audit log model - records security-relevant actions."""
from __future__ import annotations

from datetime import datetime

from flask import request
from flask_login import current_user

from app.extensions import db


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    username = db.Column(db.String(64))  # snapshot, survives user deletion
    action = db.Column(db.String(80), nullable=False, index=True)
    detail = db.Column(db.String(500))
    ip_address = db.Column(db.String(45))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AuditLog {self.action} by {self.username}>"


def record_audit(action: str, detail: str = "", user=None) -> None:
    """Create and persist an audit entry for the given action.

    Falls back to the current logged-in user and request context when
    explicit values are not supplied. Safe to call inside request handlers.
    """
    actor = user if user is not None else (
        current_user if getattr(current_user, "is_authenticated", False) else None
    )
    ip = request.remote_addr if request else None
    entry = AuditLog(
        user_id=getattr(actor, "id", None),
        username=getattr(actor, "username", None),
        action=action,
        detail=detail,
        ip_address=ip,
    )
    db.session.add(entry)
