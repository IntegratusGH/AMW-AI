"""Unified template fields + per-record JSON field storage (Phase 1).

Additive, doc-type-agnostic groundwork for a single template-driven OCR engine:

* ``ocr_templates.fields`` -- a template now owns an ordered list of field
  definitions (key/label/type/role/box/pp) instead of relying on a fixed
  per-kind field list.  Backfilled from each template's ``ocr_rects`` + the
  built-in defaults for its kind.
* ``field_values`` / ``field_crops`` / ``field_confidence`` on both record
  tables -- hybrid storage: the canonical typed columns stay authoritative for
  mapping/export/dedup/search, while these JSON maps hold the full,
  template-driven field set.  Backfilled from the existing typed columns/crops.
* A built-in default template is seeded for any kind that has none.

Extraction still uses the existing typed columns; a later phase switches it onto
the template field defs.
"""
import json
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision = "0025_unified_template_fields"
down_revision = "0024_receipt_field_crops"
branch_labels = None
depends_on = None

# Field metadata (kept in sync with app.utils.registration_ocr; inlined here so
# the migration has no heavy OCR imports).
_CR_LBL = {
    "registration_no": "Reg No", "chassis_no": "Chassis No",
    "engine_no": "Engine No", "current_owner": "Current Owner",
    "absolute_owner": "Absolute Owner", "cylinder_capacity": "Cylinder Capacity",
    "class_of_vehicle": "Class of Vehicle", "fuel_type": "Fuel Type",
    "year_of_manufacture": "Year of Manufacture", "colour": "Colour",
    "make": "Make", "model": "Model", "country_of_origin": "Country of Origin",
    "provincial_council": "Provincial Council",
    "date_first_registration": "Date of First Registration",
}
_RC_LBL = {
    "slip_no": "Slip No", "receipt_no": "Receipt No", "receipt_date": "Date",
    "receipt_time": "Time", "vehicle_no": "Vehicle No",
    "application_no": "Application No", "applicant_name": "Applicant",
    "total_amount": "Total", "bank_charges": "Charges",
    "bank_total": "Bank Total",
}
_TYPE = {
    "registration_no": "plate", "chassis_no": "chassis", "engine_no": "engine",
    "vehicle_no": "plate", "receipt_date": "date", "total_amount": "currency",
    "bank_charges": "currency", "bank_total": "currency",
}
# Built-in (default) template field set per kind, in display order.
_CR_DEFAULT = ["registration_no", "chassis_no", "engine_no", "absolute_owner",
               "class_of_vehicle", "fuel_type", "cylinder_capacity",
               "year_of_manufacture", "colour"]
_RC_DEFAULT = ["slip_no", "receipt_no", "receipt_date", "vehicle_no",
               "total_amount", "bank_charges", "bank_total"]
# Record value columns backfilled into field_values per kind.
_CR_VALUE_KEYS = list(_CR_LBL.keys())
_RC_VALUE_KEYS = list(_RC_LBL.keys())
# field key -> crop-filename column, per kind.
_CR_CROP_COLS = {"chassis_no": "chassis_crop", "registration_no": "reg_crop",
                 "engine_no": "engine_crop"}
_RC_CROP_COLS = {"slip_no": "slip_crop", "receipt_no": "receipt_no_crop",
                 "receipt_date": "date_crop", "vehicle_no": "vehicle_crop",
                 "total_amount": "total_crop", "bank_charges": "charges_crop",
                 "bank_total": "bank_total_crop"}
# field key -> confidence column (CR only has per-field confidence today).
_CR_CONF_COLS = {"chassis_no": "chassis_no_confidence",
                 "registration_no": "registration_no_confidence",
                 "engine_no": "engine_no_confidence"}


def _defs(kind, rects):
    lbl = _CR_LBL if kind == "cr" else _RC_LBL
    keys = _CR_DEFAULT if kind == "cr" else _RC_DEFAULT
    rects = rects if isinstance(rects, dict) else {}
    return [{"key": k, "label": lbl.get(k, k), "type": _TYPE.get(k, "text"),
             "role": k, "box": rects.get(k), "pp": None} for k in keys]


def upgrade():
    op.add_column("ocr_templates", sa.Column("fields", sa.Text(), nullable=True))
    for tbl in ("cr_book_records", "receipt_records"):
        op.add_column(tbl, sa.Column("field_values", sa.Text(), nullable=True))
        op.add_column(tbl, sa.Column("field_crops", sa.Text(), nullable=True))
        op.add_column(tbl,
                      sa.Column("field_confidence", sa.Text(), nullable=True))

    conn = op.get_bind()

    # --- templates: derive fields from ocr_rects + built-in defaults ---
    # (materialize each SELECT before issuing UPDATEs: the pyodbc connection has
    # no MARS, so a result set must be fully read before the next command.)
    tpl_rows = conn.execute(sa.text(
        "SELECT id, kind, ocr_rects FROM ocr_templates")).mappings().all()
    for row in tpl_rows:
        try:
            rects = json.loads(row["ocr_rects"]) if row["ocr_rects"] else {}
        except (ValueError, TypeError):
            rects = {}
        conn.execute(
            sa.text("UPDATE ocr_templates SET fields=:f WHERE id=:id"),
            {"f": json.dumps(_defs(row["kind"] or "cr", rects)),
             "id": row["id"]})

    _backfill_records(conn, "cr_book_records", _CR_VALUE_KEYS,
                      _CR_CROP_COLS, _CR_CONF_COLS)
    _backfill_records(conn, "receipt_records", _RC_VALUE_KEYS,
                      _RC_CROP_COLS, {})

    # --- seed a built-in default template for any kind that has none ---
    now = datetime.utcnow()
    for kind, name in (("cr", "Default CR Book"), ("rc", "Default Receipt")):
        exists = conn.execute(
            sa.text("SELECT COUNT(*) FROM ocr_templates WHERE kind=:k"),
            {"k": kind}).scalar()
        if not exists:
            conn.execute(sa.text(
                "INSERT INTO ocr_templates "
                "(name, kind, ocr_pp, ocr_rects, fields, created_at, updated_at) "
                "VALUES (:name, :kind, NULL, NULL, :fields, :now, :now)"),
                {"name": name, "kind": kind,
                 "fields": json.dumps(_defs(kind, {})), "now": now})


def _backfill_records(conn, table, value_keys, crop_cols, conf_cols):
    cols = list(dict.fromkeys(
        value_keys + list(crop_cols.values()) + list(conf_cols.values())
        + ["id"]))
    select = ", ".join(cols)
    rows = conn.execute(
        sa.text(f"SELECT {select} FROM {table}")).mappings().all()
    for row in rows:
        vals = {k: str(row[k]) for k in value_keys
                if row.get(k) not in (None, "")}
        crops = {fk: row[col] for fk, col in crop_cols.items() if row.get(col)}
        conf = {fk: float(row[col]) for fk, col in conf_cols.items()
                if row.get(col) is not None}
        conn.execute(sa.text(
            f"UPDATE {table} SET field_values=:v, field_crops=:c, "
            f"field_confidence=:k WHERE id=:id"),
            {"v": json.dumps(vals),
             "c": json.dumps(crops) if crops else None,
             "k": json.dumps(conf) if conf else None, "id": row["id"]})


def downgrade():
    for tbl in ("cr_book_records", "receipt_records"):
        op.drop_column(tbl, "field_confidence")
        op.drop_column(tbl, "field_crops")
        op.drop_column(tbl, "field_values")
    op.drop_column("ocr_templates", "fields")
