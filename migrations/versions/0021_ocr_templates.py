"""OCR templates (named image settings + value boxes) referenced by CR uploads.

Revision ID: 0021_ocr_templates
Revises: 0020_cr_ocr_rects
Create Date: 2026-09-13

Creates ocr_templates (name, ocr_pp JSON, ocr_rects JSON, audit columns) and
adds cr_book_uploads.template_id -> ocr_templates.id.  The upload's image
settings and value-region boxes now live on the referenced template.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0021_ocr_templates"
down_revision = "0020_cr_ocr_rects"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ocr_templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("ocr_pp", sa.Text(), nullable=True),
        sa.Column("ocr_rects", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    with op.batch_alter_table("cr_book_uploads") as batch:
        batch.add_column(sa.Column("template_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_cr_book_uploads_template_id",
            "ocr_templates", ["template_id"], ["id"])


def downgrade():
    with op.batch_alter_table("cr_book_uploads") as batch:
        batch.drop_constraint("fk_cr_book_uploads_template_id",
                              type_="foreignkey")
        batch.drop_column("template_id")
    op.drop_table("ocr_templates")
