"""Configurable SAP export template.

Adds the ``export_templates`` table holding a single JSON ``config`` that drives
Generate Export: fixed constants, per-document field mappings and an ordered set
of conditional line items.  A missing row / blank config falls back to the
built-in default (the previous fixed four-line RMV UPLOAD layout), so existing
exports are unaffected until the template is edited via the Export-setting popup.
"""
import sqlalchemy as sa
from alembic import op

revision = "0031_export_templates"
down_revision = "0030_matched_source_field_values"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "export_templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), server_default="Default",
                  nullable=False),
        sa.Column("config", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade():
    op.drop_table("export_templates")
