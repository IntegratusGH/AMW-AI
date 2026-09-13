"""Drop legacy per-upload OCR columns now that settings live on ocr_templates.

Revision ID: 0022_drop_upload_ocr_cols
Revises: 0021_ocr_templates
Create Date: 2026-09-13

Image settings and value boxes are stored on ocr_templates (referenced by
cr_book_uploads.template_id), so the per-upload ocr_pp / ocr_rects columns are
removed.  Downgrade re-adds them as nullable Text (data is not restored).
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0022_drop_upload_ocr_cols"
down_revision = "0021_ocr_templates"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("cr_book_uploads") as batch:
        batch.drop_column("ocr_rects")
        batch.drop_column("ocr_pp")


def downgrade():
    with op.batch_alter_table("cr_book_uploads") as batch:
        batch.add_column(sa.Column("ocr_pp", sa.Text(), nullable=True))
        batch.add_column(sa.Column("ocr_rects", sa.Text(), nullable=True))
