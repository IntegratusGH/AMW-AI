"""registration updater: flag receipts whose amounts do not balance

Revision ID: 0010_receipt_needs_review
Revises: 0009_receipt_image_crop
Create Date: 2026-09-09

Adds needs_review to receipt_records, set when the Total/Charges/Bank Total
figures fail the Bank Total = Total + Charges cross-check (or a value had to be
derived/corrected), so the records page can surface it for a human check.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0010_receipt_needs_review"
down_revision = "0009_receipt_image_crop"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "receipt_records",
        sa.Column("needs_review", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )


def downgrade():
    op.drop_column("receipt_records", "needs_review")
