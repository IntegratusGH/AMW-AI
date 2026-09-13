"""CR book upload-level OCR value-region rectangles.

Revision ID: 0020_cr_ocr_rects
Revises: 0019_cr_ocr_pp
Create Date: 2026-09-13

Adds cr_book_uploads.ocr_rects (JSON string mapping each captured field to a
normalized [x0,y0,x1,y1] rectangle).  Configured from the CR Book upload tab's
OCR-settings popup (zoomable image with movable/resizable boxes) and used to
crop-and-OCR each value region during (re-)processing.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0020_cr_ocr_rects"
down_revision = "0019_cr_ocr_pp"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("cr_book_uploads",
                  sa.Column("ocr_rects", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("cr_book_uploads", "ocr_rects")
