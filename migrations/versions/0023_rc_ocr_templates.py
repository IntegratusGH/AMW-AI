"""Scope OCR templates by kind and let receipt uploads use a template.

Revision ID: 0023_rc_ocr_templates
Revises: 0022_drop_upload_ocr_cols
Create Date: 2026-09-13

Adds ocr_templates.kind ('cr'/'rc'; existing rows default to 'cr') and
receipt_uploads.template_id (FK -> ocr_templates) so the Registration Receipts
tab can drive OCR from a saved template just like CR books.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0023_rc_ocr_templates"
down_revision = "0022_drop_upload_ocr_cols"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "ocr_templates",
        sa.Column("kind", sa.String(length=4), nullable=False,
                  server_default="cr"),
    )
    with op.batch_alter_table("receipt_uploads") as batch:
        batch.add_column(sa.Column("template_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_receipt_uploads_template_id_ocr_templates",
            "ocr_templates", ["template_id"], ["id"],
        )


def downgrade():
    with op.batch_alter_table("receipt_uploads") as batch:
        batch.drop_constraint(
            "fk_receipt_uploads_template_id_ocr_templates", type_="foreignkey")
        batch.drop_column("template_id")
    op.drop_column("ocr_templates", "kind")
