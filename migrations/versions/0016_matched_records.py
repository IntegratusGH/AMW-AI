"""registration updater: matched_records (CR book <-> receipt vehicle mapping)

Revision ID: 0016_matched_records
Revises: 0015_ocr_corrections
Create Date: 2026-09-10

The "Process Mapping" action pairs receipt_records.vehicle_no with
cr_book_records.registration_no, snapshots both matched rows into this table and
removes the originals from their source tables.  The snapshot is self-contained
(no FKs to the source records) because those rows are deleted once matched.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0016_matched_records"
down_revision = "0015_ocr_corrections"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "matched_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("matched_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("matched_by", sa.Integer(), nullable=True),
        sa.Column("match_key", sa.String(length=60), nullable=True),
        # CR-book side (snapshot)
        sa.Column("cr_source_upload_id", sa.Integer(), nullable=True),
        sa.Column("cr_source_record_id", sa.Integer(), nullable=True),
        sa.Column("registration_no", sa.String(length=60), nullable=True),
        sa.Column("cr_number", sa.String(length=60), nullable=True),
        sa.Column("chassis_no", sa.String(length=60), nullable=True),
        sa.Column("engine_no", sa.String(length=60), nullable=True),
        sa.Column("current_owner", sa.String(length=200), nullable=True),
        sa.Column("make", sa.String(length=80), nullable=True),
        sa.Column("model", sa.String(length=80), nullable=True),
        sa.Column("class_of_vehicle", sa.String(length=80), nullable=True),
        sa.Column("colour", sa.String(length=60), nullable=True),
        sa.Column("year_of_manufacture", sa.String(length=10), nullable=True),
        # Receipt side (snapshot)
        sa.Column("rc_source_upload_id", sa.Integer(), nullable=True),
        sa.Column("rc_source_record_id", sa.Integer(), nullable=True),
        sa.Column("vehicle_no", sa.String(length=40), nullable=True),
        sa.Column("slip_no", sa.String(length=40), nullable=True),
        sa.Column("receipt_no", sa.String(length=40), nullable=True),
        sa.Column("receipt_date", sa.String(length=20), nullable=True),
        sa.Column("applicant_name", sa.String(length=200), nullable=True),
        sa.Column("total_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("bank_charges", sa.Numeric(12, 2), nullable=True),
        sa.Column("bank_total", sa.Numeric(12, 2), nullable=True),
        sa.ForeignKeyConstraint(["matched_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_matched_records_matched_at", "matched_records",
                    ["matched_at"])
    op.create_index("ix_matched_records_match_key", "matched_records",
                    ["match_key"])
    op.create_index("ix_matched_records_registration_no", "matched_records",
                    ["registration_no"])
    op.create_index("ix_matched_records_vehicle_no", "matched_records",
                    ["vehicle_no"])


def downgrade():
    op.drop_index("ix_matched_records_vehicle_no", table_name="matched_records")
    op.drop_index("ix_matched_records_registration_no",
                  table_name="matched_records")
    op.drop_index("ix_matched_records_match_key", table_name="matched_records")
    op.drop_index("ix_matched_records_matched_at", table_name="matched_records")
    op.drop_table("matched_records")
