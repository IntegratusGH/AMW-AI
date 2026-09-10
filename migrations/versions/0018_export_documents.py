"""registration updater: export_documents + matched_records.export_document_id

Revision ID: 0018_export_documents
Revises: 0017_matched_crops
Create Date: 2026-09-10

The "Generate Export" action writes a SAP batch-input file (RMV UPLOAD) from a
selection of matched records and records it here.  Included matched rows are
linked via matched_records.export_document_id (not deleted) so they leave the
Mapped-records tab; revoking the export clears the link and removes this row.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0018_export_documents"
down_revision = "0017_matched_crops"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "export_documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("stored_path", sa.String(length=500), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("records_count", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_export_documents_created_at", "export_documents",
                    ["created_at"])
    with op.batch_alter_table("matched_records") as batch:
        batch.add_column(sa.Column("export_document_id", sa.Integer(),
                                   nullable=True))
        batch.create_index("ix_matched_records_export_document_id",
                           ["export_document_id"])
        batch.create_foreign_key(
            "fk_matched_records_export_document_id", "export_documents",
            ["export_document_id"], ["id"], ondelete="SET NULL")


def downgrade():
    with op.batch_alter_table("matched_records") as batch:
        batch.drop_constraint("fk_matched_records_export_document_id",
                              type_="foreignkey")
        batch.drop_index("ix_matched_records_export_document_id")
        batch.drop_column("export_document_id")
    op.drop_index("ix_export_documents_created_at",
                  table_name="export_documents")
    op.drop_table("export_documents")
