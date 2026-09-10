"""registration updater: snapshot crops/page on matched_records for revoke

Revision ID: 0017_matched_crops
Revises: 0016_matched_records
Create Date: 2026-09-10

Adds the value-region crop filenames and page/segment numbers to the mapping
snapshot so the "Revoke Mapping" action can return a matched pair to
cr_book_records / receipt_records with its images and page references intact
(the crop files themselves survive mapping on disk, keyed by source upload id).
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0017_matched_crops"
down_revision = "0016_matched_records"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("matched_records") as batch:
        batch.add_column(sa.Column("cr_page_no", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("chassis_crop", sa.String(length=255),
                                   nullable=True))
        batch.add_column(sa.Column("reg_crop", sa.String(length=255),
                                   nullable=True))
        batch.add_column(sa.Column("engine_crop", sa.String(length=255),
                                   nullable=True))
        batch.add_column(sa.Column("rc_segment_no", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("image_crop", sa.String(length=255),
                                   nullable=True))


def downgrade():
    with op.batch_alter_table("matched_records") as batch:
        batch.drop_column("image_crop")
        batch.drop_column("rc_segment_no")
        batch.drop_column("engine_crop")
        batch.drop_column("reg_crop")
        batch.drop_column("chassis_crop")
        batch.drop_column("cr_page_no")
