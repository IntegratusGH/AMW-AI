"""registration updater: live per-page progress columns

Revision ID: 0006_upload_progress
Revises: 0005_registration
Create Date: 2026-09-08

Adds progress_done / progress_total / progress_message to both upload tables so
the OCR worker can report live per-page progress that the UI polls.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0006_upload_progress"
down_revision = "0005_registration"
branch_labels = None
depends_on = None

_TABLES = ("cr_book_uploads", "receipt_uploads")


def upgrade():
    for table in _TABLES:
        op.add_column(table, sa.Column(
            "progress_done", sa.Integer(), nullable=False, server_default="0"))
        op.add_column(table, sa.Column(
            "progress_total", sa.Integer(), nullable=False, server_default="0"))
        op.add_column(table, sa.Column(
            "progress_message", sa.String(length=200), nullable=True))


def downgrade():
    for table in _TABLES:
        op.drop_column(table, "progress_message")
        op.drop_column(table, "progress_total")
        op.drop_column(table, "progress_done")
