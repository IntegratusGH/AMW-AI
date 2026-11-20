"""Text/label calibration anchors on OCR templates.

Adds ``ocr_templates.ocr_text_anchors`` -- a JSON list (up to 3) of text
anchors ``[{"box":[x0,y0,x1,y1], "text":"EXPECTED"}]``.  Each anchor marks a
fixed, always-printed label; at extraction the label is located by OCR within
its box and the matched positions solve the same similarity transform as the
image-patch ``ocr_anchors``.  Additive and nullable; existing templates keep
working with no text anchors.
"""
import sqlalchemy as sa
from alembic import op

revision = "0038_ocr_text_anchors"
down_revision = "0037_cr_applied_template"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ocr_templates",
                  sa.Column("ocr_text_anchors", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("ocr_templates", "ocr_text_anchors")
