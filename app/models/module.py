"""AI automation Module model and role<->module mapping.

Modules are the tiles shown on the Operations page.  Each role is granted a
set of modules via the ``role_modules`` association; a user sees the modules
mapped to their role (Administrators see all active modules).
"""
from __future__ import annotations

from datetime import datetime

from app.extensions import db

# Icon sources and sane bounds for the admin-editable icon size.
ICON_BUILTIN = "builtin"
ICON_UPLOAD = "upload"
ICON_SIZE_MIN = 16
ICON_SIZE_MAX = 128
ICON_SIZE_DEFAULT = 32

MODULE_STATUSES = ("available", "planned")
MODULE_ACCENTS = ("blue", "green", "amber", "purple", "teal")

# Names of the built-in inline SVG icons (kept in sync with
# templates/main/_icons.html).  Offered to admins as an icon library.
BUILTIN_ICONS = [
    "file-check", "calendar", "bell", "package", "credit-card", "shield",
    "scan", "mail", "message-circle", "target", "star", "chart", "user-check",
    "car", "wrench", "phone", "cog", "users", "clock", "tag", "truck",
    "database",
]

# Association table: which modules each role can see.
role_modules = db.Table(
    "role_modules",
    db.Column("role_id", db.Integer,
              db.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    db.Column("module_id", db.Integer,
              db.ForeignKey("modules.id", ondelete="CASCADE"), primary_key=True),
)


class Category(db.Model):
    """Grouping for modules (the section headings on the Operations page).

    A disabled category hides all its modules on the Operations page.  Deleting
    a category leaves its modules uncategorised (category_id -> NULL).
    """

    __tablename__ = "categories"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False, index=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    sort_order = db.Column(db.Integer, default=0, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    modules = db.relationship("Module", back_populates="category")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Category {self.name}>"


class Module(db.Model):
    __tablename__ = "modules"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(64), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(400))
    category_id = db.Column(
        db.Integer,
        db.ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status = db.Column(db.String(20), default="planned", nullable=False)
    accent = db.Column(db.String(20), default="blue", nullable=False)

    # Icon: either a built-in name (icon_kind="builtin") or an uploaded file
    # name stored under MODULE_ICON_DIR (icon_kind="upload").
    icon_kind = db.Column(db.String(10), default=ICON_BUILTIN, nullable=False)
    icon_value = db.Column(db.String(255))
    icon_size = db.Column(db.Integer, default=ICON_SIZE_DEFAULT, nullable=False)

    is_active = db.Column(db.Boolean, default=True, nullable=False)
    sort_order = db.Column(db.Integer, default=0, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    category = db.relationship("Category", back_populates="modules")
    roles = db.relationship(
        "Role", secondary="role_modules", back_populates="modules"
    )

    @property
    def is_upload_icon(self) -> bool:
        return self.icon_kind == ICON_UPLOAD

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Module {self.key}>"
