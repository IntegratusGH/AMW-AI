"""registration updater: per-field confidence + review flag for CR records

Revision ID: 0007_cr_field_confidence
Revises: 0006_upload_progress
Create Date: 2026-09-08

Adds chassis_no_confidence / registration_no_confidence / needs_review to
cr_book_records so the high-accuracy (multi-scale voting) OCR of the two
critical fields can record per-field confidence and flag uncertain rows.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0007_cr_field_confidence"
down_revision = "0006_upload_progress"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("cr_book_records",
                  sa.Column("chassis_no_confidence", sa.Float(), nullable=True))
    op.add_column("cr_book_records",
                  sa.Column("registration_no_confidence", sa.Float(), nullable=True))
    op.add_column("cr_book_records", sa.Column(
        "needs_review", sa.Boolean(), nullable=False, server_default=sa.text("0")))


def downgrade():
    op.drop_column("cr_book_records", "needs_review")
    op.drop_column("cr_book_records", "registration_no_confidence")
    op.drop_column("cr_book_records", "chassis_no_confidence")
