"""Full field snapshots on matched records.

Adds two JSON columns to ``matched_records`` so Process Mapping can capture the
complete field set of both source rows (every data column of ``cr_book_records``
/ ``receipt_records`` plus template custom fields), letting the Mapped-records
tab render every field and hide columns that are empty for all rows:

* ``cr_fields`` -- ordered JSON list of the CR-book record's fields.
* ``rc_fields`` -- ordered JSON list of the receipt record's fields.

Additive; the existing typed snapshot columns stay authoritative for
export/dedup/search/revoke, so old rows keep working (the view falls back to
them when these are null).
"""
import sqlalchemy as sa
from alembic import op

revision = "0029_matched_field_snapshots"
down_revision = "0028_field_masks_lists"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("matched_records") as batch:
        batch.add_column(sa.Column("cr_fields", sa.Text(), nullable=True))
        batch.add_column(sa.Column("rc_fields", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("matched_records") as batch:
        batch.drop_column("rc_fields")
        batch.drop_column("cr_fields")
