"""Per-record extraction error for receipt slips.

Adds a nullable ``error`` column to ``receipt_records`` so a slip that matches no
template in the picker's validation/fallback chain is skipped, flagged and left
in the table (with the message) instead of aborting the whole document.
"""
import sqlalchemy as sa
from alembic import op

revision = "0032_receipt_error"
down_revision = "0031_export_templates"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("receipt_records",
                  sa.Column("error", sa.String(length=200), nullable=True))


def downgrade():
    op.drop_column("receipt_records", "error")
