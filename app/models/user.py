"""User and Role models."""
from __future__ import annotations

from datetime import datetime

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db, login_manager

# Canonical role names used throughout the application.
ROLE_ADMIN = "Administrator"
ROLE_OPERATOR = "Operator"

# Authentication sources.  Local accounts store a password hash; Active
# Directory accounts store no password and are verified by an LDAP bind.
AUTH_LOCAL = "local"
AUTH_AD = "ad"
DEFAULT_ROLES = {
    ROLE_ADMIN: "Full access: manage users, roles and system settings.",
    ROLE_OPERATOR: "Standard operational access to the application.",
}


class Role(db.Model):
    __tablename__ = "roles"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False, index=True)
    description = db.Column(db.String(255))

    users = db.relationship("User", back_populates="role", lazy="dynamic")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Role {self.name}>"


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(120))
    # Nullable: Active Directory accounts have no local password.
    password_hash = db.Column(db.String(255), nullable=True)
    auth_source = db.Column(
        db.String(10), default=AUTH_LOCAL, nullable=False, index=True
    )

    role_id = db.Column(db.Integer, db.ForeignKey("roles.id"), nullable=False)
    role = db.relationship("Role", back_populates="users")

    is_active = db.Column(db.Boolean, default=True, nullable=False)
    theme = db.Column(db.String(10), default="light", nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    last_login_at = db.Column(db.DateTime)
    must_change_password = db.Column(db.Boolean, default=False, nullable=False)

    # --- password helpers -------------------------------------------------
    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    @property
    def is_ad_user(self) -> bool:
        return self.auth_source == AUTH_AD

    # --- role helpers -----------------------------------------------------
    @property
    def role_name(self) -> str:
        return self.role.name if self.role else ""

    @property
    def is_administrator(self) -> bool:
        return self.role_name == ROLE_ADMIN

    def has_role(self, *names: str) -> bool:
        return self.role_name in names

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.username}>"


@login_manager.user_loader
def load_user(user_id: str):
    return db.session.get(User, int(user_id))
