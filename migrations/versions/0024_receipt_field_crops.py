"""registration updater: per-field value-region crops for receipt records

Revision ID: 0024_receipt_field_crops
Revises: 0023_rc_ocr_templates
Create Date: 2026-09-13

Adds one crop-filename column per template-box receipt field so the records page
can show the cropped scan above each captured value (mirrors the CR-book value
crops).
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0024_receipt_field_crops"
down_revision = "0023_rc_ocr_templates"
branch_labels = None
depends_on = None

_COLS = ("slip_crop", "receipt_no_crop", "date_crop", "vehicle_crop",
         "total_crop", "charges_crop", "bank_total_crop")


def upgrade():
    for col in _COLS:
        op.add_column("receipt_records",
                      sa.Column(col, sa.String(length=255), nullable=True))


def downgrade():
    for col in reversed(_COLS):
        op.drop_column("receipt_records", col)
