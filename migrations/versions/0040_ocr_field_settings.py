"""Shared per-field settings (Apply mask / Value / Min %) across templates.

Adds ``ocr_field_settings`` -- one row per (``kind``, ``key``) holding the
field's ``mask`` / ``value`` / ``min_conf``.  These three settings were
previously stored per-template inside ``ocr_templates.fields`` JSON; they now
live here so editing them in any template applies to every template of that
kind, whether or not a given template captures the field.  Per-template data
(the capture tick, box position, per-field image settings and label) stays in
``ocr_templates.fields``.

Backfill: for each field the most-used non-default value found across existing
templates is adopted as the shared starting value (mask/value chosen together;
min_conf chosen independently).  Additive; the JSON copies are left in place as
a fallback and are kept in sync on the next template save.
"""
import json
from collections import Counter
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision = "0040_ocr_field_settings"
down_revision = "0039_template_validation_box2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ocr_field_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=4), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("mask", sa.String(length=32), nullable=False,
                  server_default="none"),
        sa.Column("value", sa.String(length=255), nullable=True),
        sa.Column("min_conf", sa.Float(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "key", name="uq_ocr_field_kind_key"),
    )

    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT kind, fields FROM ocr_templates")).fetchall()

    mv: dict = {}   # (kind, key) -> Counter of (mask, value)
    mc: dict = {}   # (kind, key) -> Counter of min_conf
    for kind, fields in rows:
        if not fields:
            continue
        try:
            defs = json.loads(fields)
        except (ValueError, TypeError):
            continue
        if not isinstance(defs, list):
            continue
        k_kind = (kind or "cr")
        for f in defs:
            if not isinstance(f, dict):
                continue
            key = str(f.get("key") or "").strip()
            if not key:
                continue
            mask = str(f.get("mask") or "none").strip()
            value = str(f.get("value") or "").strip()
            try:
                minc = round(max(0.0, min(1.0, float(f.get("min_conf")))), 3)
            except (TypeError, ValueError):
                minc = 0.0
            if mask != "none":
                mv.setdefault((k_kind, key), Counter())[(mask, value)] += 1
            if minc > 0:
                mc.setdefault((k_kind, key), Counter())[minc] += 1

    now = datetime.utcnow()
    inserts = []
    for (kind, key) in sorted(set(mv) | set(mc)):
        mask, value = "none", ""
        if (kind, key) in mv:
            (mask, value), _ = mv[(kind, key)].most_common(1)[0]
        minc = 0.0
        if (kind, key) in mc:
            minc, _ = mc[(kind, key)].most_common(1)[0]
        inserts.append({"kind": kind, "key": key, "mask": mask,
                        "value": value or None, "min_conf": minc,
                        "updated_at": now})
    if inserts:
        conn.execute(sa.text(
            "INSERT INTO ocr_field_settings "
            "(kind, [key], mask, [value], min_conf, updated_at) "
            "VALUES (:kind, :key, :mask, :value, :min_conf, :updated_at)"),
            inserts)


def downgrade():
    op.drop_table("ocr_field_settings")
