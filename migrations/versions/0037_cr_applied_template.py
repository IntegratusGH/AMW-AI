"""Applied Template-picker template name per CR-book record.

Adds a nullable ``applied_template`` column to ``cr_book_records`` recording the
name of the template that validated/was applied to the record (the picker chain
resolves one template per CR upload); shown in View Records like receipts.
"""
import sqlalchemy as sa
from alembic import op

revision = "0037_cr_applied_template"
down_revision = "0036_upload_source_mtime"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("cr_book_records",
                  sa.Column("applied_template", sa.String(length=120),
                            nullable=True))


def downgrade():
    op.drop_column("cr_book_records", "applied_template")
