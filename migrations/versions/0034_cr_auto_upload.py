"""Automated CR-book folder watcher: settings + ingested-file ledger.

Adds ``cr_auto_settings`` (singleton: watched folder + enabled switch) and
``cr_auto_files`` (ledger of already-ingested source PDFs so a file is never
re-processed; identity is source path + size + modified-time).
"""
import sqlalchemy as sa
from alembic import op

revision = "0034_cr_auto_upload"
down_revision = "0033_receipt_applied_template"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "cr_auto_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("folder", sa.String(length=500), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("interval_seconds", sa.Integer(), nullable=False,
                  server_default=sa.text("30")),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "cr_auto_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_path", sa.String(length=700), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mtime", sa.BigInteger(), nullable=False),
        sa.Column("upload_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["upload_id"], ["cr_book_uploads.id"],
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_path", "size", "mtime",
                            name="uq_cr_auto_file_identity"),
    )
    op.create_index("ix_cr_auto_files_source_path", "cr_auto_files",
                    ["source_path"])


def downgrade():
    op.drop_index("ix_cr_auto_files_source_path", table_name="cr_auto_files")
    op.drop_table("cr_auto_files")
    op.drop_table("cr_auto_settings")
