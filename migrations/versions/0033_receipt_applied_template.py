"""Applied Template-picker template name per receipt slip.

Adds a nullable ``applied_template`` column to ``receipt_records`` recording the
name of the template that actually validated/was applied to each slip (the
picker chain may resolve a different template per slip).
"""
import sqlalchemy as sa
from alembic import op

revision = "0033_receipt_applied_template"
down_revision = "0032_receipt_error"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("receipt_records",
                  sa.Column("applied_template", sa.String(length=120),
                            nullable=True))


def downgrade():
    op.drop_column("receipt_records", "applied_template")
