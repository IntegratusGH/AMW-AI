"""cr records: add engine_no_confidence

Revision ID: 0014_cr_engine_confidence
Revises: 0013_cr_engine_crop
Create Date: 2026-09-09

Engine No now goes through the same multi-scale recognition-only refinement and
voting as Chassis No / Reg No, which yields a per-field confidence used for the
review flag.  Store it alongside the other identifier confidences.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0014_cr_engine_confidence"
down_revision = "0013_cr_engine_crop"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "cr_book_records",
        sa.Column("engine_no_confidence", sa.Float(), nullable=True),
    )


def downgrade():
    op.drop_column("cr_book_records", "engine_no_confidence")
