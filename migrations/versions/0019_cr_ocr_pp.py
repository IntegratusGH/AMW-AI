"""CR book upload-level OCR image settings.

Revision ID: 0019_cr_ocr_pp
Revises: 0018_export_documents
Create Date: 2026-09-13

Adds cr_book_uploads.ocr_pp (JSON string of a cleaned image-adjustment dict).
Configured from the CR Book upload tab's OCR-settings popup and applied to every
page of the book during (re-)processing.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0019_cr_ocr_pp"
down_revision = "0018_export_documents"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("cr_book_uploads",
                  sa.Column("ocr_pp", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("cr_book_uploads", "ocr_pp")
