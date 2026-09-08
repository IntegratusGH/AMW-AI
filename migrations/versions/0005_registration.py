"""registration updater: uploads and extracted records

Revision ID: 0005_registration
Revises: 0004_categories
Create Date: 2026-09-08

Adds the CR-book / receipt upload tracking tables and their OCR-extracted
record tables used by the Registration Updater module (module id 1).
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0005_registration"
down_revision = "0004_categories"
branch_labels = None
depends_on = None


def _upload_columns():
    return [
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("stored_path", sa.String(length=500), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False),
        sa.Column("uploaded_by", sa.Integer(), nullable=True),
        sa.Column("processed", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("captured_records", sa.Integer(), nullable=False),
        sa.Column("duplicate_records", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"]),
    ]


def upgrade():
    op.create_table("cr_book_uploads", *_upload_columns())
    op.create_table("receipt_uploads", *_upload_columns())

    op.create_table(
        "cr_book_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("upload_id", sa.Integer(), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=True),
        sa.Column("cr_number", sa.String(length=60), nullable=True),
        sa.Column("registration_no", sa.String(length=60), nullable=True),
        sa.Column("chassis_no", sa.String(length=60), nullable=True),
        sa.Column("engine_no", sa.String(length=60), nullable=True),
        sa.Column("current_owner", sa.String(length=200), nullable=True),
        sa.Column("current_owner_address", sa.String(length=400), nullable=True),
        sa.Column("absolute_owner", sa.String(length=300), nullable=True),
        sa.Column("make", sa.String(length=80), nullable=True),
        sa.Column("model", sa.String(length=80), nullable=True),
        sa.Column("class_of_vehicle", sa.String(length=80), nullable=True),
        sa.Column("fuel_type", sa.String(length=40), nullable=True),
        sa.Column("cylinder_capacity", sa.String(length=40), nullable=True),
        sa.Column("year_of_manufacture", sa.String(length=10), nullable=True),
        sa.Column("colour", sa.String(length=60), nullable=True),
        sa.Column("country_of_origin", sa.String(length=80), nullable=True),
        sa.Column("provincial_council", sa.String(length=80), nullable=True),
        sa.Column("date_first_registration", sa.String(length=40), nullable=True),
        sa.Column("is_duplicate", sa.Boolean(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["upload_id"], ["cr_book_uploads.id"], ondelete="CASCADE"),
    )
    op.create_index(op.f("ix_cr_book_records_upload_id"), "cr_book_records", ["upload_id"])
    op.create_index(op.f("ix_cr_book_records_chassis_no"), "cr_book_records", ["chassis_no"])
    op.create_index(op.f("ix_cr_book_records_registration_no"), "cr_book_records", ["registration_no"])

    op.create_table(
        "receipt_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("upload_id", sa.Integer(), nullable=False),
        sa.Column("segment_no", sa.Integer(), nullable=True),
        sa.Column("slip_no", sa.String(length=40), nullable=True),
        sa.Column("receipt_no", sa.String(length=40), nullable=True),
        sa.Column("receipt_date", sa.String(length=20), nullable=True),
        sa.Column("receipt_time", sa.String(length=20), nullable=True),
        sa.Column("vehicle_no", sa.String(length=40), nullable=True),
        sa.Column("application_no", sa.String(length=40), nullable=True),
        sa.Column("applicant_name", sa.String(length=200), nullable=True),
        sa.Column("total_amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("bank_charges", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("bank_total", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("is_duplicate", sa.Boolean(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["upload_id"], ["receipt_uploads.id"], ondelete="CASCADE"),
    )
    op.create_index(op.f("ix_receipt_records_upload_id"), "receipt_records", ["upload_id"])
    op.create_index(op.f("ix_receipt_records_slip_no"), "receipt_records", ["slip_no"])
    op.create_index(op.f("ix_receipt_records_vehicle_no"), "receipt_records", ["vehicle_no"])
    op.create_index(op.f("ix_receipt_records_application_no"), "receipt_records", ["application_no"])


def downgrade():
    op.drop_table("receipt_records")
    op.drop_table("cr_book_records")
    op.drop_table("receipt_uploads")
    op.drop_table("cr_book_uploads")
