"""ad auth: users.auth_source + nullable password_hash

Revision ID: 0002_ad_auth
Revises: 0001_baseline
Create Date: 2026-09-04

Adds Active Directory support to the users table: a per-account auth source
(local/ad) and a nullable password hash so AD accounts need no local password.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0002_ad_auth"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade():
    # New column defaults existing rows to local accounts; the server_default
    # is required because the column is NOT NULL and the table has rows.
    op.add_column(
        "users",
        sa.Column(
            "auth_source",
            sa.String(length=10),
            nullable=False,
            server_default="local",
        ),
    )
    op.create_index(op.f("ix_users_auth_source"), "users", ["auth_source"], unique=False)

    # AD accounts store no local password.
    op.alter_column(
        "users",
        "password_hash",
        existing_type=sa.String(length=255),
        nullable=True,
    )


def downgrade():
    op.alter_column(
        "users",
        "password_hash",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.drop_index(op.f("ix_users_auth_source"), table_name="users")
    op.drop_column("users", "auth_source")
