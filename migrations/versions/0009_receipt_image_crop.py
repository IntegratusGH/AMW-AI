"""registration updater: store per-receipt image crop for receipt records

Revision ID: 0009_receipt_image_crop
Revises: 0008_cr_value_crops
Create Date: 2026-09-09

Adds image_crop to receipt_records, holding the filename of the cropped scan of
each receipt's region on the source page so the records page can open the
receipt image in a viewer (zoom / rotate).
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0009_receipt_image_crop"
down_revision = "0008_cr_value_crops"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("receipt_records",
                  sa.Column("image_crop", sa.String(length=255), nullable=True))


def downgrade():
    op.drop_column("receipt_records", "image_crop")
