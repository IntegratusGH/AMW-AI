"""modules and role<->module mapping

Revision ID: 0003_modules
Revises: 0002_ad_auth
Create Date: 2026-09-08

Adds the ``modules`` table (AI automation tiles shown on the Operations page)
and the ``role_modules`` association controlling which modules each role sees.
Seed data is inserted by scripts.init_db.seed_modules (after roles exist).
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0003_modules"
down_revision = "0002_ad_auth"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "modules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=400), nullable=True),
        sa.Column("category", sa.String(length=80), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("accent", sa.String(length=20), nullable=False),
        sa.Column("icon_kind", sa.String(length=10), nullable=False),
        sa.Column("icon_value", sa.String(length=255), nullable=True),
        sa.Column("icon_size", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_modules_key"), "modules", ["key"], unique=True)

    op.create_table(
        "role_modules",
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("module_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["module_id"], ["modules.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("role_id", "module_id"),
    )


def downgrade():
    op.drop_table("role_modules")
    op.drop_index(op.f("ix_modules_key"), table_name="modules")
    op.drop_table("modules")
