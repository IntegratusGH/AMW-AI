"""Preserve source field_values / field_crops on matched records.

Adds verbatim copies of each source row's ``field_values`` and ``field_crops``
JSON to ``matched_records`` so revoking a mapping restores the record whole
(template custom fields such as R55/M17 and their value-region crops included),
and re-running Process Mapping snapshots them again:

* ``cr_field_values`` / ``cr_field_crops`` -- CR-book source JSON blobs.
* ``rc_field_values`` / ``rc_field_crops`` -- receipt source JSON blobs.

Additive; existing rows keep NULLs and simply revoke without custom-field data.
"""
import sqlalchemy as sa
from alembic import op

revision = "0030_matched_source_field_values"
down_revision = "0029_matched_field_snapshots"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("matched_records") as batch:
        batch.add_column(sa.Column("cr_field_values", sa.Text(), nullable=True))
        batch.add_column(sa.Column("cr_field_crops", sa.Text(), nullable=True))
        batch.add_column(sa.Column("rc_field_values", sa.Text(), nullable=True))
        batch.add_column(sa.Column("rc_field_crops", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("matched_records") as batch:
        batch.drop_column("rc_field_crops")
        batch.drop_column("rc_field_values")
        batch.drop_column("cr_field_crops")
        batch.drop_column("cr_field_values")
