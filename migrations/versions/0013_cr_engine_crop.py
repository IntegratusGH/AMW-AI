"""cr records: add engine_crop value-region image

Revision ID: 0013_cr_engine_crop
Revises: 0012_user_ai_trainer
Create Date: 2026-09-09

Stores the filename of the cropped Engine No scan so the records page can show
it beside the value, mirroring the Chassis No / Reg No crops.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0013_cr_engine_crop"
down_revision = "0012_user_ai_trainer"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "cr_book_records",
        sa.Column("engine_crop", sa.String(length=255), nullable=True),
    )


def downgrade():
    op.drop_column("cr_book_records", "engine_crop")
