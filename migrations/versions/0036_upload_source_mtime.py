"""registration updater: track source file name + last-modified on uploads

Revision ID: 0036_upload_source_mtime
Revises: 0035_rc_auto_upload
Create Date: 2026-11-18

Adds ``source_name`` (original filename) and ``source_mtime`` (file last-modified
wall-clock time) to both upload tables.  The UPLOADED column shows source_mtime,
and (source_name, source_mtime) keys the same-name/different-date re-upload
handling (a new last-modified becomes a ``copy N`` upload; an identical one is
skipped as a duplicate).

Existing rows are backfilled: ``source_name`` from the current filename, and
``source_mtime`` from the folder-pickup ledger (``cr_auto_files`` /
``rc_auto_files``) which already recorded each source file's real last-modified.
Manually-uploaded rows have no recorded file time, so their source_mtime stays
NULL and the column keeps falling back to ``uploaded_at`` for them.
"""
from datetime import datetime

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0036_upload_source_mtime"
down_revision = "0035_rc_auto_upload"
branch_labels = None
depends_on = None

# (upload table, pickup-ledger table) pairs sharing the same layout.
_TABLES = (
    ("cr_book_uploads", "cr_auto_files"),
    ("receipt_uploads", "rc_auto_files"),
)


def upgrade():
    for table, _ledger in _TABLES:
        op.add_column(table,
                      sa.Column("source_name", sa.String(length=255),
                                nullable=True))
        op.add_column(table,
                      sa.Column("source_mtime", sa.DateTime(), nullable=True))

    bind = op.get_bind()
    for table, ledger in _TABLES:
        # Original name == current filename for existing rows (no copy-rename
        # was ever applied before this feature).
        bind.execute(sa.text(
            f"UPDATE {table} SET source_name = filename "
            f"WHERE source_name IS NULL"))
        # Real file last-modified from the pickup ledger (epoch seconds ->
        # local wall-clock, matching how new uploads store it).
        rows = bind.execute(sa.text(
            f"SELECT upload_id, MAX(mtime) AS m FROM {ledger} "
            f"WHERE upload_id IS NOT NULL GROUP BY upload_id")).fetchall()
        for upload_id, m in rows:
            try:
                dt = datetime.fromtimestamp(int(m)).replace(microsecond=0)
            except (TypeError, ValueError, OSError):
                continue
            bind.execute(
                sa.text(f"UPDATE {table} SET source_mtime = :dt "
                        f"WHERE id = :id AND source_mtime IS NULL"),
                {"dt": dt, "id": upload_id})


def downgrade():
    for table, _ledger in _TABLES:
        op.drop_column(table, "source_mtime")
        op.drop_column(table, "source_name")
