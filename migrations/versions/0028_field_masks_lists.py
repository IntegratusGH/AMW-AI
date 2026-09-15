"""Field masks, global lookup lists, and the custom-field registry.

Adds two tables that back the Field-setting tab:

* ``ocr_lists``         -- named, global lookup lists referenced from a field's
  Value as ``[name]`` (newline-separated ``items``).
* ``ocr_custom_fields`` -- registry of user-added (non-canonical) record fields.
  Each row corresponds to a real ``NVARCHAR(255)`` column added on demand to the
  matching records table (``cr_book_records`` / ``receipt_records``); those
  per-field columns are created at runtime (idempotent ALTER) when a field is
  first defined, so they are intentionally NOT created here.

All additive; existing data/behaviour is unchanged.  The per-field ``mask`` /
``value`` / ``capture`` attributes live inside ``ocr_templates.fields`` JSON and
need no schema change.
"""
import sqlalchemy as sa
from alembic import op

revision = "0028_field_masks_lists"
down_revision = "0027_template_validation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ocr_lists",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("items", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "ocr_custom_fields",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=4), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "key", name="uq_ocr_custom_kind_key"),
    )


def downgrade():
    op.drop_table("ocr_custom_fields")
    op.drop_table("ocr_lists")
