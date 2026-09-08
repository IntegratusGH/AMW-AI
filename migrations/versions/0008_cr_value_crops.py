"""registration updater: store value-region scan crops for CR records

Revision ID: 0008_cr_value_crops
Revises: 0007_cr_field_confidence
Create Date: 2026-09-08

Adds chassis_crop / reg_crop to cr_book_records, holding the filename of the
cropped scan of each value region so the records page can show the original
image beside the captured value for verification / correction.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0008_cr_value_crops"
down_revision = "0007_cr_field_confidence"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("cr_book_records",
                  sa.Column("chassis_crop", sa.String(length=255), nullable=True))
    op.add_column("cr_book_records",
                  sa.Column("reg_crop", sa.String(length=255), nullable=True))


def downgrade():
    op.drop_column("cr_book_records", "reg_crop")
    op.drop_column("cr_book_records", "chassis_crop")
