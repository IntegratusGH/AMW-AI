"""Receipt calibration anchors on OCR templates.

Adds ``ocr_templates.ocr_anchors`` -- a JSON list (up to 3) of calibration
anchors ``[{"box":[x0,y0,x1,y1], "patch":"<base64 png>"}]``.  Each anchor is a
small image patch captured from a reference slip at a fixed, always-printed
spot; at extraction every slip is registered to the reference via these anchors
(similarity transform) so the value boxes track its actual offset/scale.
Additive and nullable; existing templates keep working with no anchors.
"""
import sqlalchemy as sa
from alembic import op

revision = "0026_ocr_anchors"
down_revision = "0025_unified_template_fields"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ocr_templates",
                  sa.Column("ocr_anchors", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("ocr_templates", "ocr_anchors")
