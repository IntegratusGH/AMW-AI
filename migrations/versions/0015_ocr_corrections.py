"""ocr feedback loop: durable labeled corrections for accuracy + rule mining

Revision ID: 0015_ocr_corrections
Revises: 0014_cr_engine_confidence
Create Date: 2026-09-09

Persists, whenever an operator verifies/corrects a record's critical field, the
model's original reading, the operator's final value and a copy of the value
region crop.  This is the foundation of the Tier-3 correction feedback loop:
report real accuracy, mine confusion rules / per-field priors, and export a
training corpus.  One row per (doc_kind, record_id, field); ocr_value is captured
once (the model output before the first correction).
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0015_ocr_corrections"
down_revision = "0014_cr_engine_confidence"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ocr_corrections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("doc_kind", sa.String(length=4), nullable=False),
        sa.Column("upload_id", sa.Integer(), nullable=True),
        sa.Column("record_id", sa.Integer(), nullable=True),
        sa.Column("page_no", sa.Integer(), nullable=True),
        sa.Column("field", sa.String(length=30), nullable=False),
        sa.Column("ocr_value", sa.String(length=120), nullable=True),
        sa.Column("corrected_value", sa.String(length=120), nullable=True),
        sa.Column("was_correct", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("rule_set", sa.String(length=60), nullable=True),
        sa.Column("crop_file", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("doc_kind", "record_id", "field",
                            name="uq_ocr_corr_rec_field"),
    )
    op.create_index("ix_ocr_corrections_created_at", "ocr_corrections",
                    ["created_at"])
    op.create_index("ix_ocr_corrections_record_id", "ocr_corrections",
                    ["record_id"])
    op.create_index("ix_ocr_corrections_field", "ocr_corrections", ["field"])


def downgrade():
    op.drop_index("ix_ocr_corrections_field", table_name="ocr_corrections")
    op.drop_index("ix_ocr_corrections_record_id", table_name="ocr_corrections")
    op.drop_index("ix_ocr_corrections_created_at", table_name="ocr_corrections")
    op.drop_table("ocr_corrections")
