"""registration updater: CR-book format registry + per-page detection review

Revision ID: 0011_cr_formats
Revises: 0010_receipt_needs_review
Create Date: 2026-09-09

Adds:
  * cr_formats        - recognised CR-book layouts (label/position signature +
                        the code rule-set used to OCR pages of that format).
  * cr_upload_pages   - per-page result of the detection pass for an upload
                        (known / unknown_format / not_cr + matched format).
  * cr_book_uploads.review_status - state of the format/validity pre-check that
                        now runs on upload, before the heavy Process step.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0011_cr_formats"
down_revision = "0010_receipt_needs_review"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "cr_formats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=60), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("signature", sa.Text(), nullable=True),
        sa.Column("rule_set", sa.String(length=60), nullable=False,
                  server_default="_generic"),
        sa.Column("is_builtin", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    op.create_table(
        "cr_upload_pages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("upload_id", sa.Integer(), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default="unknown_format"),
        sa.Column("format_id", sa.Integer(), nullable=True),
        sa.Column("core_hits", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("signature", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["upload_id"], ["cr_book_uploads.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["format_id"], ["cr_formats.id"],
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cr_upload_pages_upload_id", "cr_upload_pages",
                    ["upload_id"])
    op.add_column(
        "cr_book_uploads",
        sa.Column("review_status", sa.String(length=20), nullable=False,
                  server_default="pending"),
    )
    # Existing uploads predate the format pre-check; treat them as ready so they
    # remain processable without a (now-missing) detection pass.
    op.execute("UPDATE cr_book_uploads SET review_status = 'ready'")


def downgrade():
    op.drop_column("cr_book_uploads", "review_status")
    op.drop_index("ix_cr_upload_pages_upload_id", table_name="cr_upload_pages")
    op.drop_table("cr_upload_pages")
    op.drop_table("cr_formats")
