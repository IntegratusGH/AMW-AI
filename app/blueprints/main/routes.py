"""Main application routes: dashboard, operations, profile, settings."""
from __future__ import annotations

import base64
import copy
import json
import os
import re
import threading
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import (
    Response,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import case, func
from sqlalchemy import inspect as sqla_inspect
from sqlalchemy import text as sa_text
from werkzeug.utils import secure_filename

from app.blueprints.main import main_bp
from app.extensions import db
from app.forms import (
    CRBookUploadForm,
    ProfileForm,
    ReceiptUploadForm,
    SettingsForm,
)
from app.models import (
    CR_PAGE_KNOWN,
    CR_PAGE_NOT_CR,
    CR_PAGE_UNKNOWN,
    REVIEW_DETECTING,
    REVIEW_ERROR,
    REVIEW_NEEDS,
    REVIEW_READY,
    ROLE_ADMIN,
    ROLE_OPERATOR,
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_PENDING,
    STATUS_PROCESSING,
    Category,
    CRBookRecord,
    CRBookUpload,
    CRFormat,
    CRUploadPage,
    ExportDocument,
    ExportTemplate,
    MatchedRecord,
    Module,
    OcrCorrection,
    OcrCustomField,
    OcrList,
    OcrTemplate,
    ReceiptRecord,
    ReceiptUpload,
    User,
    record_audit,
    role_modules,
)

# Module keys that have a dedicated workspace page (used to make the Operations
# tile clickable and to guard access).
REGISTRATION_UPDATER_KEY = "registration_updater"


@main_bp.route("/")
@login_required
def index():
    return redirect(url_for("main.dashboard"))


@main_bp.route("/dashboard")
@login_required
def dashboard():
    stats = None
    if current_user.is_administrator:
        stats = {
            "total_users": User.query.count(),
            "active_users": User.query.filter_by(is_active=True).count(),
            "inactive_users": User.query.filter_by(is_active=False).count(),
        }
    return render_template("main/dashboard.html", stats=stats)


@main_bp.route("/operations")
@login_required
def operations():
    if not current_user.has_role(ROLE_OPERATOR, ROLE_ADMIN):
        abort(403)
    # Show only the active modules mapped to the current user's role, hiding
    # any whose category has been disabled.  Uncategorised modules are shown.
    mods = (
        Module.query.filter_by(is_active=True)
        .join(role_modules)
        .filter(role_modules.c.role_id == current_user.role_id)
        .outerjoin(Category, Module.category_id == Category.id)
        .filter(db.or_(Module.category_id.is_(None),
                       Category.is_active == True))  # noqa: E712
        .order_by(Category.sort_order, Category.name,
                  Module.sort_order, Module.name)
        .all()
    )
    # Group by category, preserving the ordered sequence.
    groups: list[dict] = []
    for m in mods:
        cat = m.category.name if m.category else "General"
        if not groups or groups[-1]["name"] != cat:
            groups.append({"name": cat, "modules": []})
        groups[-1]["modules"].append(m)
    # Modules that open a dedicated workspace page when their tile is clicked.
    module_links = {
        REGISTRATION_UPDATER_KEY: url_for("main.registration_updater"),
    }
    return render_template(
        "main/operations.html", groups=groups, module_links=module_links)


@main_bp.route("/media/module-icons/<path:filename>")
@login_required
def module_icon(filename: str):
    """Serve an uploaded module icon (rendered via <img>; nosniff for safety)."""
    resp = send_from_directory(current_app.config["MODULE_ICON_DIR"], filename)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# ===== Registration Updater (module id 1) ================================
def _require_module(key: str) -> Module:
    """Return the active module by key, enforcing the current role's access."""
    module = Module.query.filter_by(key=key, is_active=True).first()
    if not module:
        abort(404)
    allowed = (
        Module.query.filter_by(id=module.id)
        .join(role_modules)
        .filter(role_modules.c.role_id == current_user.role_id)
        .first()
    )
    if not allowed:
        abort(403)
    return module


def _save_doc(file, dest_dir: str) -> tuple[str, str]:
    os.makedirs(dest_dir, exist_ok=True)
    safe = secure_filename(file.filename) or "upload"
    stored = f"{uuid.uuid4().hex}_{safe}"
    path = os.path.join(dest_dir, stored)
    file.save(path)
    return file.filename or safe, path


# Template-box field -> record crop-filename column, per doc kind.  Used both to
# persist the per-field value-region crops and (via build_field_data) to fill the
# hybrid field_crops JSON.  ``_RC_CROP_ATTR`` is also consumed by the receipt
# crop-persist loop and the receipt_value_crop route.
_CR_CROP_ATTR = {
    "chassis_no": "chassis_crop",
    "registration_no": "reg_crop",
    "engine_no": "engine_crop",
}
# CR field -> per-field confidence column (only the three critical identifiers
# carry a refined per-field score today).
_CR_CONF_ATTR = {
    "chassis_no": "chassis_no_confidence",
    "registration_no": "registration_no_confidence",
    "engine_no": "engine_no_confidence",
}
_RC_CROP_ATTR = {
    "slip_no": "slip_crop",
    "receipt_no": "receipt_no_crop",
    "receipt_date": "date_crop",
    "vehicle_no": "vehicle_crop",
    "total_amount": "total_crop",
    "bank_charges": "charges_crop",
    "bank_total": "bank_total_crop",
}

# Per document-type configuration for the generic processing worker.  ``crop_attr``
# / ``conf_attr`` map template field keys onto the record's crop/confidence
# columns; ``granularity`` records the region strategy (one record per page vs
# one per detected segment).
_DOC_KINDS = {
    "cr": {
        "upload_model": CRBookUpload,
        "record_model": CRBookRecord,
        "key_field": "chassis_no",
        "noun": "CR book",
        "granularity": "page",
        "crop_attr": _CR_CROP_ATTR,
        "conf_attr": _CR_CONF_ATTR,
    },
    "rc": {
        "upload_model": ReceiptUpload,
        "record_model": ReceiptRecord,
        "key_field": "slip_no",
        "noun": "receipt",
        "granularity": "segment",
        "crop_attr": _RC_CROP_ATTR,
        "conf_attr": {},
    },
}


def _assign_columns(obj, data: dict) -> None:
    """Copy dict values onto matching model columns, truncating long strings."""
    for col in sqla_inspect(type(obj)).columns:
        name = col.key
        if name in data and data[name] is not None:
            val = data[name]
            length = getattr(col.type, "length", None)
            if isinstance(val, str) and length:
                val = val[:length]
            setattr(obj, name, val)


# Short URL token (used by the receipt_value_crop route) -> crop column.
_RC_CROP_TOKEN = {
    "slip": "slip_crop", "receipt": "receipt_no_crop", "date": "date_crop",
    "vehicle": "vehicle_crop", "total": "total_crop",
    "charges": "charges_crop", "banktotal": "bank_total_crop",
}


def _delete_cr_crops(app, rec) -> None:
    """Remove the cropped value-region files belonging to a CR record."""
    crop_dir = os.path.join(app.config["CR_BOOK_CROP_DIR"], str(rec.upload_id))
    for fn in (rec.chassis_crop, rec.reg_crop,
               getattr(rec, "engine_crop", None)):
        if fn:
            try:
                os.remove(os.path.join(crop_dir, fn))
            except OSError:
                pass


# ===== User-added (custom) record columns ================================
# Non-canonical fields defined in the Field-setting tab get a real NVARCHAR
# column on the kind's records table, created on demand and tracked in
# ocr_custom_fields.  Canonical (ORM-mapped) fields keep their typed columns.
_RECORD_TABLE = {"cr": "cr_book_records", "rc": "receipt_records"}
_RECORD_MODEL = {"cr": CRBookRecord, "rc": ReceiptRecord}
_VALID_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def _canonical_keys(kind: str) -> set:
    """Names of the real, ORM-mapped columns on a kind's records table."""
    return {c.key for c in sqla_inspect(_RECORD_MODEL[kind]).columns}


def _safe_field_key(key) -> str | None:
    key = (key or "").strip().lower()
    return key if _VALID_KEY_RE.match(key) else None


def _column_exists(table: str, col: str) -> bool:
    row = db.session.execute(sa_text(
        "SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_NAME=:t AND COLUMN_NAME=:c"),
        {"t": table, "c": col}).first()
    return row is not None


def ensure_record_column(kind: str, key: str, label: str | None = None) -> bool:
    """Ensure a real ``NVARCHAR(255)`` column ``key`` exists on the kind's
    records table for a user-added field, registering it in ocr_custom_fields.
    Canonical fields are left untouched.  Returns True when ``key`` is a managed
    custom column (new or existing), False when it is canonical or invalid.
    Idempotent; safe to call on every save."""
    skey = _safe_field_key(key)
    if not skey or skey in _canonical_keys(kind):
        return False
    table = _RECORD_TABLE.get(kind)
    if not table:
        return False
    if not _column_exists(table, skey):
        # skey is validated by _VALID_KEY_RE, so inlining the identifier is safe.
        db.session.execute(sa_text(
            f"ALTER TABLE [{table}] ADD [{skey}] NVARCHAR(255) NULL"))
    reg = OcrCustomField.query.filter_by(kind=kind, key=skey).first()
    if reg is None:
        db.session.add(OcrCustomField(
            kind=kind, key=skey, label=(label or skey),
            created_by=getattr(current_user, "id", None)))
    elif label and reg.label != label:
        reg.label = label
    db.session.commit()
    return True


def _persist_fields(tpl, data: dict, kind: str) -> None:
    """Clean + persist a template's field defs (mask/value/capture/box) from a
    request payload, creating any new custom record columns.  No-op when the
    payload carries no 'fields' key (so old clients that only send rects keep
    working)."""
    from app.utils import registration_ocr as ocr
    if "fields" not in data:
        return
    defs = ocr.clean_fields(data.get("fields"), kind)
    # Keep fields[].box in lockstep with ocr_rects for the built-in value
    # boxes (ocr_rects is what extraction reads), so the two stores can never
    # drift apart and mislead the editor.
    rects = ocr.clean_rects(data.get("rects"), kind)
    for d in defs:
        if d["key"] in rects:
            d["box"] = list(rects[d["key"]])
            d["capture"] = True
        if d.get("capture") and d["key"] not in _canonical_keys(kind):
            ensure_record_column(kind, d["key"], d.get("label"))
    tpl.fields = json.dumps(defs) if defs else None


def _list_lookup(name: str):
    """Resolve a global list name to its item lines (for [List name] masks)."""
    lst = OcrList.query.filter_by(name=(name or "").strip()).first()
    return lst.item_lines() if lst else None


def _lists_json() -> list:
    """All global lookup lists (name + raw items text) for the Field-setting
    tab's list manager and the Value [List name] picker."""
    return [{"id": l.id, "name": l.name, "items": l.items or ""}
            for l in OcrList.query.order_by(OcrList.name).all()]


def _slug_field(label: str) -> str:
    """Derive a valid record-column key from a user field label."""
    s = re.sub(r"[^a-z0-9]+", "_", (label or "").lower()).strip("_")[:63]
    if not s:
        s = "field"
    if not re.match(r"^[a-z]", s):
        s = "f_" + s
    return s[:63]


# Record columns that are internal plumbing, not user-facing OCR value fields.
_INTERNAL_RECORD_COLS = {
    "id", "upload_id", "page_no", "segment_no", "created_at",
    "is_duplicate", "needs_review", "confidence", "raw_text",
    "field_values", "field_crops", "field_confidence",
}


def _field_catalog(kind: str) -> list:
    """Master field list for a kind's records table: every real data column on
    the table (curated value fields first, then the remaining data columns) plus
    every user-added custom column.  Internal plumbing (ids, crops, confidence,
    JSON blobs, flags) is excluded.  The list is the same for all templates of
    the kind; a template's per-field capture/mask/value settings are overlaid
    client-side from its own ``fields``."""
    from app.utils import registration_ocr as ocr
    cols = [c.key for c in sqla_inspect(_RECORD_MODEL[kind]).columns]
    data_keys = [k for k in cols
                 if k not in _INTERNAL_RECORD_COLS
                 and not k.endswith("_crop") and not k.endswith("_confidence")]
    order = [k for k in (d["key"] for d in ocr.default_field_defs(kind))
             if k in data_keys]
    ordered = order + [k for k in data_keys if k not in order]
    cat = [{"key": k, "label": ocr.field_label(kind, k),
            "type": ocr.field_type(k), "role": k, "box": None, "pp": None,
            "capture": False, "mask": "none", "value": "", "canonical": True}
           for k in ordered]
    seen = set(ordered)
    for c in (OcrCustomField.query.filter_by(kind=kind)
              .order_by(OcrCustomField.label).all()):
        if c.key in seen:
            continue
        seen.add(c.key)
        cat.append({"key": c.key, "label": c.label or c.key, "type": "text",
                    "role": c.key, "box": None, "pp": None, "capture": False,
                    "mask": "none", "value": "", "canonical": False})
    return cat


def _tpl_pp(tpl):
    """Parsed image-settings dict for a template (empty when none)."""
    from app.utils import registration_ocr as ocr
    if not tpl or not tpl.ocr_pp:
        return None
    try:
        return ocr.clean_pp(json.loads(tpl.ocr_pp)) or None
    except (ValueError, TypeError):
        return None


def _apply_test_masks(kind, defs, fields, page, region, pp):
    """Apply the Field-setting Apply-mask rules to a Test-OCR result: refine each
    result field whose def carries a mask, and read+mask any custom capture
    field (non-canonical, boxed) so the test mirrors real processing.  Mutates
    and returns ``fields``."""
    from app.utils import registration_ocr as ocr
    by_key = {d["key"]: d for d in defs}
    for f in fields:
        d = by_key.get(f["key"])
        if d and (d.get("mask") or "none") != "none":
            val, ok = ocr.apply_field_mask(
                d["mask"], d.get("value"), f.get("value"), _list_lookup)
            f["value"] = val or ""
            f["mask_ok"] = ok
    if kind == "rc" and not region:
        return fields
    canon = _canonical_keys(kind)
    shown = {f["key"] for f in fields}
    for d in defs:
        if d["key"] in canon or d["key"] in shown:
            continue
        if not d.get("capture") or not d.get("box"):
            continue
        fpp = d.get("pp") or pp
        if kind == "cr":
            raw, conf = ocr.read_rect(page, d["box"], pp=fpp)
        else:
            raw, conf = ocr.read_slip_rect(page, region, d["box"], pp=fpp)
        val, ok = raw, True
        if (d.get("mask") or "none") != "none":
            val, ok = ocr.apply_field_mask(
                d["mask"], d.get("value"), raw, _list_lookup)
        fields.append({"key": d["key"], "label": d["label"], "value": val or "",
                       "confidence": round(float(conf), 3) if conf else None,
                       "mask_ok": ok})
    return fields


def _capture_custom_and_masks(kind, up, tpl, RecordModel, upload_id):
    """Post-extraction pass over every record of an upload: read each template
    capture-field box and apply its Apply-mask rule.  Custom (non-canonical)
    fields take their value from the box read + mask and are written to their
    real record column; canonical fields keep their tuned value but have the
    mask applied to validate/snap it.  A mask reporting 'not ok' flags the
    record for review (the correct-or-flagged guarantee).  Returns
    ``{record_id: {custom_key: value}}`` so the field_values mirror can include
    the custom fields."""
    from app.utils import registration_ocr as ocr
    out: dict = {}
    if tpl is None:
        return out
    defs = ocr.template_field_defs(tpl)
    canon = _canonical_keys(kind)
    str_cols = {c.key for c in sqla_inspect(_RECORD_MODEL[kind]).columns
                if isinstance(c.type, db.String)}
    cap_defs = [d for d in defs if d.get("box") and d.get("capture")
                and _safe_field_key(d["key"]) and d["key"] not in canon]
    mask_defs = [d for d in defs
                 if (d.get("mask") or "none") != "none" and d.get("value")]
    if not cap_defs and not mask_defs:
        return out
    for d in cap_defs:
        ensure_record_column(kind, d["key"], d.get("label"))
    recs = RecordModel.query.filter_by(upload_id=upload_id).all()
    if not recs:
        return out
    tpp = _tpl_pp(tpl)
    doc, total = ocr.open_document(up.stored_path)
    try:
        slips = _rc_slip_index(up) if kind == "rc" else None
        for rec in recs:
            if kind == "cr":
                pno = getattr(rec, "page_no", None) or 1
                page = doc[min(max(1, pno), total) - 1]
                region = None
            else:
                idx = (getattr(rec, "segment_no", None) or 1) - 1
                if not slips or not (0 <= idx < len(slips)):
                    continue
                s = slips[idx]
                page = doc[s["page"]]
                region = ocr._slip_region_pts(page, s["y0"], s["y1"], s["zoom"])
            flagged = False
            for d in cap_defs:
                fpp = d.get("pp") or tpp
                if kind == "cr":
                    raw, _c = ocr.read_rect(page, d["box"], pp=fpp)
                else:
                    raw, _c = ocr.read_slip_rect(page, region, d["box"], pp=fpp)
                val, ok = ocr.apply_field_mask(
                    d.get("mask"), d.get("value"), raw, _list_lookup)
                out.setdefault(rec.id, {})[d["key"]] = val or ""
                if not ok:
                    flagged = True
            for d in mask_defs:
                role = d.get("role") or d["key"]
                if role not in canon:
                    continue
                cur = getattr(rec, role, None)
                val, ok = ocr.apply_field_mask(
                    d.get("mask"), d.get("value"),
                    None if cur is None else str(cur), _list_lookup)
                if role in str_cols and val is not None \
                        and val != (None if cur is None else str(cur)):
                    setattr(rec, role, val[:255])
                if not ok:
                    flagged = True
            if flagged and hasattr(rec, "needs_review"):
                rec.needs_review = True
            cvals = {k: v for k, v in (out.get(rec.id) or {}).items()
                     if _safe_field_key(k)}
            if cvals:
                table = _RECORD_TABLE[kind]
                sets = ", ".join(f"[{k}]=:v_{k}" for k in cvals)
                params = {f"v_{k}": (v or None) for k, v in cvals.items()}
                params["rid"] = rec.id
                db.session.execute(sa_text(
                    f"UPDATE [{table}] SET {sets} WHERE id=:rid"), params)
        db.session.commit()
    finally:
        doc.close()
    return out


def _run_processing(app, kind: str, upload_id: int) -> None:
    """OCR a document page-by-page, committing records and live progress after
    each page so the polling UI can show it advancing."""
    cfg = _DOC_KINDS[kind]
    UploadModel = cfg["upload_model"]
    RecordModel = cfg["record_model"]
    key_field = cfg["key_field"]
    noun = cfg["noun"]
    with app.app_context():
        from app.utils import registration_ocr as ocr
        try:
            up = db.session.get(UploadModel, upload_id)
            if up is None:
                return
            # Re-process: drop any previously extracted rows and reset counters.
            RecordModel.query.filter_by(upload_id=upload_id).delete()
            import shutil
            shutil.rmtree(
                os.path.join(
                    app.config["CR_BOOK_CROP_DIR" if kind == "cr"
                               else "RECEIPT_CROP_DIR"], str(upload_id)),
                ignore_errors=True)
            up.captured_records = 0
            up.duplicate_records = 0
            up.page_count = 0
            up.progress_done = 0
            up.progress_total = 0
            up.progress_message = "Opening document\u2026"
            db.session.commit()

            doc, total = ocr.open_document(up.stored_path)
            up.page_count = total
            up.progress_total = total
            up.progress_message = (
                "Reading receipt roll \u2014 detecting slips\u2026"
                if kind == "rc"
                else f"Starting OCR on {total} page(s)\u2026")
            db.session.commit()

            key_col = getattr(RecordModel, key_field)
            seen = {
                (v[0] or "").strip().upper()
                for v in db.session.query(key_col).filter(key_col.isnot(None)).all()
                if v[0]
            }
            captured = 0
            duplicates = 0
            cr_pending = []  # (rec_obj, data) kept for the batch template pass
            rc_pending = []  # (rec_obj, data) kept for receipt image crops
            # Per-page OCR rule set chosen by the detection pass (per format).
            page_rules: dict[int, str] = {}
            upload_pp = None
            upload_rects = None
            upload_anchors = None
            # Extraction is driven by the upload's assigned OCR template: image
            # settings for both kinds, plus value boxes for CR.  No template =>
            # default extraction.
            tpl = up.template if getattr(up, "template_id", None) else None
            # Template picker.  Receipts resolve the template PER SLIP (each slip
            # restarts from the assigned template and walks the validation/
            # fallback chain), so build a per-slip resolver; CR resolves once for
            # the page.  Either path raises "No Match Template" when nothing
            # validates.
            def _rc_report(msg: str) -> None:
                up.progress_message = msg[:200]
                db.session.commit()

            rc_resolver = None
            if tpl is not None:
                if kind == "rc":
                    rc_resolver = _make_rc_slip_resolver(tpl, report=_rc_report)
                else:
                    tpl = _resolve_effective_template(kind, doc, tpl)
            if tpl and tpl.ocr_pp:
                try:
                    upload_pp = ocr.clean_pp(json.loads(tpl.ocr_pp)) or None
                except (ValueError, TypeError):
                    upload_pp = None
            if kind == "cr":
                for p in CRUploadPage.query.filter_by(upload_id=upload_id).all():
                    page_rules[p.page_no] = (
                        p.format.rule_set if p.format else "_generic")
                if tpl and tpl.ocr_rects:
                    try:
                        upload_rects = ocr.clean_rects(
                            json.loads(tpl.ocr_rects), "cr") or None
                    except (ValueError, TypeError):
                        upload_rects = None
            elif tpl and tpl.ocr_rects:
                try:
                    upload_rects = ocr.clean_rects(
                        json.loads(tpl.ocr_rects), "rc") or None
                except (ValueError, TypeError):
                    upload_rects = None
            if tpl and getattr(tpl, "ocr_anchors", None):
                try:
                    upload_anchors = ocr.clean_anchors(
                        json.loads(tpl.ocr_anchors)) or None
                except (ValueError, TypeError):
                    upload_anchors = None
            # Receipts are a single-page roll of many slips, so page-granular
            # progress never moves during the (long) slip OCR and the UI looks
            # stuck.  Report per-slip progress instead; rc_slip_base carries
            # slips completed on prior pages for the rare multi-page roll.
            rc_slip_base = 0

            def _rc_progress(done_in_page: int, slips_in_page: int) -> None:
                total_est = rc_slip_base + max(slips_in_page, done_in_page)
                up.progress_total = total_est or 1
                up.progress_done = rc_slip_base + done_in_page
                up.progress_message = (
                    f"Reading slip {rc_slip_base + done_in_page} "
                    f"of {total_est}\u2026")
                db.session.commit()

            try:
                for i in range(total):
                    # Unified per-page extractor dispatch (region strategy):
                    # CR yields 0/1 record per page, RC one per detected slip.
                    page_recs = ocr.extract_page(
                        kind, doc[i],
                        rule_set=page_rules.get(i + 1, "oldcrbook"),
                        pp=upload_pp, rects=upload_rects,
                        anchors=upload_anchors,
                        progress=(_rc_progress if kind == "rc" else None),
                        resolve=(rc_resolver if kind == "rc" else None))
                    if kind == "cr":
                        for r in page_recs:
                            r["page_no"] = i + 1

                    for r in page_recs:
                        if kind == "rc":
                            r["segment_no"] = captured + 1
                        # CR duplicates are resolved by Registration No in a
                        # dedicated replace pass after batch correction; here
                        # only receipts dedup (by slip no) inline.
                        if kind == "rc":
                            key = (str(r.get(key_field) or "")).strip().upper()
                            is_dup = bool(key) and key in seen
                            if key:
                                seen.add(key)
                            if is_dup:
                                duplicates += 1
                        else:
                            is_dup = False
                        rec_obj = RecordModel(upload_id=upload_id, is_duplicate=is_dup)
                        _assign_columns(rec_obj, r)
                        db.session.add(rec_obj)
                        if kind == "cr":
                            cr_pending.append((rec_obj, r))
                        elif kind == "rc":
                            rc_pending.append((rec_obj, r))
                        captured += 1

                    if kind == "rc":
                        rc_slip_base += len(page_recs)
                        up.progress_done = rc_slip_base
                        up.progress_total = max(
                            up.progress_total or 0, rc_slip_base) or 1
                    else:
                        up.progress_done = i + 1
                    up.captured_records = captured
                    up.duplicate_records = duplicates
                    up.progress_message = (
                        f"Processed page {i + 1} of {total} \u2014 "
                        f"{captured} {noun}(s) captured, {duplicates} duplicate(s)")
                    db.session.commit()
            finally:
                doc.close()

            # Batch chassis template: learn which positions are numeric across
            # the whole upload, then enforce it so systematic S->5 (etc.)
            # misreads are corrected against the batch's own consistent chars.
            if kind == "cr" and cr_pending:
                mask = ocr.build_chassis_mask(
                    [r.get("_chassis_cands") for _, r in cr_pending])
                emask = ocr.build_chassis_mask(
                    [r.get("_engine_cands") for _, r in cr_pending], min_len=6)
                if mask or emask:
                    up.progress_message = "Applying batch templates\u2026"
                    db.session.commit()
                    for rec_obj, r in cr_pending:
                        recovered = False
                        if mask:
                            new_ch, changed, recovered = ocr.apply_chassis_mask(
                                rec_obj.chassis_no, mask)
                            if changed:
                                rec_obj.chassis_no = new_ch
                        if emask:
                            new_en, echanged, _ = ocr.apply_chassis_mask(
                                rec_obj.engine_no, emask)
                            if echanged:
                                rec_obj.engine_no = new_en
                        chassis_bad = (
                            ocr.chassis_needs_review(
                                rec_obj.chassis_no, mask,
                                rec_obj.chassis_no_confidence)
                            if mask else ocr._chassis_review(
                                rec_obj.chassis_no,
                                rec_obj.chassis_no_confidence))
                        engine_bad = (
                            ocr.chassis_needs_review(
                                rec_obj.engine_no, emask,
                                rec_obj.engine_no_confidence)
                            if emask else ocr._engine_review(
                                rec_obj.engine_no,
                                rec_obj.engine_no_confidence))
                        rec_obj.needs_review = bool(
                            recovered or chassis_bad or engine_bad
                            or ocr._reg_review(
                                rec_obj.registration_no,
                                rec_obj.registration_no_confidence))
                    db.session.commit()

            # Persist the cropped value-region scans (Chassis No / Reg No) so the
            # records page can show the original image beside each captured value.
            if kind == "cr" and cr_pending:
                crop_dir = os.path.join(
                    app.config["CR_BOOK_CROP_DIR"], str(upload_id))
                os.makedirs(crop_dir, exist_ok=True)
                crop_fields = {
                    "chassis": ("_chassis_crop_png", "chassis_crop"),
                    "reg": ("_reg_crop_png", "reg_crop"),
                    "engine": ("_engine_crop_png", "engine_crop"),
                }
                for rec_obj, r in cr_pending:
                    for field, (src, attr) in crop_fields.items():
                        png = r.get(src)
                        if not png:
                            continue
                        fn = f"{rec_obj.id}_{field}.png"
                        with open(os.path.join(crop_dir, fn), "wb") as fh:
                            fh.write(png)
                        setattr(rec_obj, attr, fn)
                db.session.commit()

            # Registration-No replace/dedup: a record whose Registration No
            # already exists is treated as an update — the previously stored
            # record (an earlier upload, or earlier in this same batch) is
            # removed and this one kept in its place, counted as a duplicate
            # rather than a fresh capture.
            if kind == "cr" and cr_pending:
                up.progress_message = "Replacing existing Registration Nos\u2026"
                db.session.commit()
                prior_by_reg: dict[str, list] = {}
                for other in (RecordModel.query
                              .filter(RecordModel.upload_id != upload_id,
                                      RecordModel.registration_no.isnot(None))
                              .all()):
                    k = (other.registration_no or "").strip().upper()
                    if k:
                        prior_by_reg.setdefault(k, []).append(other)
                duplicates = 0
                kept_by_reg: dict[str, object] = {}
                for rec_obj, r in cr_pending:
                    key = (rec_obj.registration_no or "").strip().upper()
                    if not key:
                        rec_obj.is_duplicate = False
                        continue
                    replaced = False
                    for old in prior_by_reg.pop(key, []):
                        _delete_cr_crops(app, old)
                        db.session.delete(old)
                        replaced = True
                    earlier = kept_by_reg.get(key)
                    if earlier is not None:
                        _delete_cr_crops(app, earlier)
                        db.session.delete(earlier)
                        replaced = True
                    rec_obj.is_duplicate = replaced
                    kept_by_reg[key] = rec_obj
                    if replaced:
                        duplicates += 1
                db.session.flush()
                up.duplicate_records = duplicates
                up.captured_records = (
                    RecordModel.query.filter_by(upload_id=upload_id).count())
                captured = up.captured_records
                db.session.commit()

            # Persist each receipt's cropped page region for the image viewer,
            # plus the per-field value-region crops from the template's boxes.
            if kind == "rc" and rc_pending:
                crop_dir = os.path.join(
                    app.config["RECEIPT_CROP_DIR"], str(upload_id))
                os.makedirs(crop_dir, exist_ok=True)
                for rec_obj, r in rc_pending:
                    png = r.get("_image_crop_png")
                    if png:
                        fn = f"{rec_obj.id}.png"
                        with open(os.path.join(crop_dir, fn), "wb") as fh:
                            fh.write(png)
                        rec_obj.image_crop = fn
                    for field, attr in _RC_CROP_ATTR.items():
                        fpng = (r.get("_rc_field_crops") or {}).get(field)
                        if not fpng:
                            continue
                        fn = f"{rec_obj.id}_{field}.png"
                        with open(os.path.join(crop_dir, fn), "wb") as fh:
                            fh.write(fpng)
                        setattr(rec_obj, attr, fn)
                db.session.commit()

            # Custom (user-added) field capture + Apply-mask refinement.  Reads
            # each template capture box per record, applies the field's mask,
            # writes custom values to their real columns and flags uncertain
            # reads for review.  Returns the custom values for the JSON mirror.
            up.progress_message = "Applying field masks…"
            db.session.commit()
            custom_vals = _capture_custom_and_masks(
                kind, up, tpl, RecordModel, upload_id)

            # Hybrid field storage: mirror each record's final (batch-corrected)
            # canonical values, crops and confidences into the template-driven
            # field_values/field_crops/field_confidence JSON.  Driven by the
            # upload's template field defs (or the kind's built-in defaults),
            # keyed by field key, values read via each field's role.  Custom
            # field values (from the mask pass) are merged in on top.
            defs = (ocr.template_field_defs(tpl) if tpl
                    else ocr.default_field_defs(kind))
            crop_attr = cfg.get("crop_attr", {})
            conf_attr = cfg.get("conf_attr", {})
            for rec_obj in RecordModel.query.filter_by(
                    upload_id=upload_id).all():
                fv, fc, fk = ocr.build_field_data(
                    defs, rec_obj, crop_attr, conf_attr)
                for k, v in (custom_vals.get(rec_obj.id) or {}).items():
                    if v not in (None, ""):
                        fv[k] = v
                rec_obj.field_values = json.dumps(fv) if fv else None
                rec_obj.field_crops = json.dumps(fc) if fc else None
                rec_obj.field_confidence = json.dumps(fk) if fk else None
            db.session.commit()

            up.processed = True
            up.status = STATUS_DONE
            up.processed_at = datetime.utcnow()
            up.progress_message = (
                f"Completed \u2014 {captured} {noun}(s) captured, "
                f"{duplicates} duplicate(s)")
            up.error = None
            db.session.commit()
        except Exception as exc:  # pragma: no cover - background worker
            db.session.rollback()
            up = db.session.get(UploadModel, upload_id)
            if up is not None:
                up.status = STATUS_ERROR
                up.processed = False
                up.error = str(exc)[:1000]
                up.progress_message = f"Error: {str(exc)[:150]}"
                db.session.commit()


def _start_processing(kind: str, upload_id: int) -> None:
    app = current_app._get_current_object()
    threading.Thread(
        target=_run_processing, args=(app, kind, upload_id), daemon=True
    ).start()


# ===== CR-book format detection (runs on upload, before Process) ==========
def _load_sig(raw: str | None) -> dict:
    try:
        return json.loads(raw) if raw else {"labels": [], "pos": {}}
    except (ValueError, TypeError):
        return {"labels": [], "pos": {}}


def ensure_builtin_cr_formats() -> CRFormat:
    """Get-or-create the built-in 'OldCRBook' format (the current, tuned rules).
    Its positions are learned from the first CR page seen during detection."""
    fmt = CRFormat.query.filter_by(key="oldcrbook").first()
    if fmt is None:
        from app.utils import registration_ocr as ocr
        sig = {"labels": list(ocr._CR_LABELS.keys()), "pos": {}}
        fmt = CRFormat(key="oldcrbook", name="OldCRBook",
                       signature=json.dumps(sig), rule_set="oldcrbook",
                       is_builtin=True, is_active=True)
        db.session.add(fmt)
        db.session.commit()
    return fmt


def _recompute_cr_review_status(up: CRBookUpload) -> None:
    pages = CRUploadPage.query.filter_by(upload_id=up.id).all()
    needs = any(p.status in (CR_PAGE_NOT_CR, CR_PAGE_UNKNOWN) for p in pages)
    up.review_status = REVIEW_NEEDS if needs else REVIEW_READY


def _run_cr_detection(app, upload_id: int) -> None:
    """Label-only pass classifying every page of a CR-book upload as a known
    format, an unknown format, or not-a-CR-book, so the operator can resolve
    problems before the heavy Process step."""
    with app.app_context():
        from app.utils import registration_ocr as ocr
        up = db.session.get(CRBookUpload, upload_id)
        if up is None:
            return
        try:
            ensure_builtin_cr_formats()
            up.review_status = REVIEW_DETECTING
            up.progress_message = "Checking CR-book format\u2026"
            CRUploadPage.query.filter_by(upload_id=upload_id).delete()
            db.session.commit()

            doc, total = ocr.open_document(up.stored_path)
            up.page_count = total
            up.progress_total = total
            up.progress_done = 0
            db.session.commit()

            dets = []
            try:
                for i in range(total):
                    dets.append(ocr.detect_cr_page(doc[i]))
                    up.progress_done = i + 1
                    up.progress_message = f"Checked page {i + 1} of {total}\u2026"
                    db.session.commit()
            finally:
                doc.close()

            # Learn OldCRBook's layout from the first CR page ever seen so a
            # genuinely different layout no longer matches it.
            old = CRFormat.query.filter_by(key="oldcrbook").first()
            if old is not None and not _load_sig(old.signature).get("pos"):
                for d in dets:
                    if d["is_cr"]:
                        old.signature = json.dumps(d["signature"])
                        db.session.commit()
                        break

            active = CRFormat.query.filter_by(is_active=True).all()
            formats = [(f.key, _load_sig(f.signature)) for f in active]
            by_key = {f.key: f for f in active}
            for i, d in enumerate(dets):
                status, key = ocr.classify_cr_page(d, formats)
                fmt = by_key.get(key) if key else None
                db.session.add(CRUploadPage(
                    upload_id=upload_id, page_no=i + 1, status=status,
                    format_id=fmt.id if fmt else None,
                    core_hits=d["core_hits"],
                    signature=json.dumps(d["signature"])))
            _recompute_cr_review_status(up)
            up.progress_message = (
                f"{up.review_pending_count} page(s) need review"
                if up.review_status == REVIEW_NEEDS
                else "Format OK \u2014 ready to process")
            db.session.commit()
        except Exception as exc:  # pragma: no cover - background worker
            db.session.rollback()
            up = db.session.get(CRBookUpload, upload_id)
            if up is not None:
                up.review_status = REVIEW_ERROR
                up.error = str(exc)[:1000]
                up.progress_message = f"Detection error: {str(exc)[:150]}"
                db.session.commit()


def _start_cr_detection(upload_id: int) -> None:
    app = current_app._get_current_object()
    threading.Thread(
        target=_run_cr_detection, args=(app, upload_id), daemon=True
    ).start()


def _unique_format_key(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "format"
    if base in ("oldcrbook", "_generic"):
        base = f"{base}-fmt"
    key, n = base, 1
    while CRFormat.query.filter_by(key=key).first() is not None:
        n += 1
        key = f"{base}-{n}"
    return key[:60]


@main_bp.route("/operations/registration-updater", methods=["GET", "POST"])
@login_required
def registration_updater():
    module = _require_module(REGISTRATION_UPDATER_KEY)
    cr_form = CRBookUploadForm()
    rc_form = ReceiptUploadForm()

    if cr_form.submit_cr.data and cr_form.validate_on_submit():
        name, path = _save_doc(
            cr_form.file.data, current_app.config["CR_BOOK_UPLOAD_DIR"])
        check_format = bool(cr_form.check_format.data)
        # Inherit the OCR template from the previous CR upload so a same-layout
        # batch auto-processes with the tuned settings + value boxes.
        prev = (CRBookUpload.query
                .order_by(CRBookUpload.uploaded_at.desc()).first())
        up = CRBookUpload(
            filename=name, stored_path=path, uploaded_by=current_user.id,
            status=STATUS_PENDING if check_format else STATUS_PROCESSING,
            review_status=REVIEW_DETECTING if check_format else REVIEW_READY,
            template_id=(prev.template_id if prev else None))
        db.session.add(up)
        record_audit("cr_book_uploaded",
                     f"file={name} check_format={check_format}")
        db.session.commit()
        if check_format:
            _start_cr_detection(up.id)
            flash("CR Book uploaded \u2014 checking format against known CR "
                  "books\u2026", "success")
        else:
            _start_processing("cr", up.id)
            flash("CR Book uploaded \u2014 processing started (OCR runs in the "
                  "background). Refresh to see results.", "success")
        return redirect(url_for("main.registration_updater", tab="cr"))

    if rc_form.submit_rc.data and rc_form.validate_on_submit():
        name, path = _save_doc(
            rc_form.file.data, current_app.config["RECEIPT_UPLOAD_DIR"])
        up = ReceiptUpload(
            filename=name, stored_path=path,
            uploaded_by=current_user.id, status=STATUS_PENDING)
        db.session.add(up)
        record_audit("receipt_uploaded", f"file={name}")
        db.session.commit()
        # Pre-separate the roll into slips in the background so the OCR-settings
        # popup opens instantly when the operator gets to it.
        _start_rc_warm(up.id)
        flash("Receipts uploaded. Click Process to extract records.", "success")
        return redirect(url_for("main.registration_updater", tab="rc"))

    cr_uploads = CRBookUpload.query.order_by(CRBookUpload.uploaded_at.desc()).all()
    rc_uploads = ReceiptUpload.query.order_by(ReceiptUpload.uploaded_at.desc()).all()
    # Actual live row counts per upload (may be below captured_records once rows
    # have moved into matched_records or otherwise been removed).
    cr_counts = dict(
        db.session.query(CRBookRecord.upload_id, func.count(CRBookRecord.id))
        .group_by(CRBookRecord.upload_id).all())
    rc_counts = dict(
        db.session.query(ReceiptRecord.upload_id, func.count(ReceiptRecord.id))
        .group_by(ReceiptRecord.upload_id).all())
    matched_records = (MatchedRecord.query
                       .filter(MatchedRecord.export_document_id == None)
                       .order_by(MatchedRecord.matched_at.desc()).all())
    # Show every field both source tables carry, dropping columns that are empty
    # for all matched rows (attaches cr_map/rc_map to each row).
    map_cr_cols, map_rc_cols = _matched_view_columns(matched_records)
    export_documents = (ExportDocument.query
                        .order_by(ExportDocument.created_at.desc()).all())
    can_edit_export = current_user.is_ai_trainer or current_user.is_administrator
    export_samples = [{"id": m.id,
                       "label": m.registration_no or m.vehicle_no or f"#{m.id}"}
                      for m in matched_records[:50]]
    # Keep the user on the tab they acted from: an explicit ?tab= wins, else fall
    # back to whichever form reported validation errors (default CR).
    active_tab = request.args.get("tab")
    if active_tab not in ("cr", "rc", "map", "export"):
        active_tab = "rc" if (rc_form.errors and not cr_form.errors) else "cr"
    return render_template(
        "main/registration_updater.html", module=module,
        cr_form=cr_form, rc_form=rc_form,
        cr_uploads=cr_uploads, rc_uploads=rc_uploads,
        cr_counts=cr_counts, rc_counts=rc_counts,
        matched_records=matched_records, export_documents=export_documents,
        map_cr_cols=map_cr_cols, map_rc_cols=map_rc_cols,
        can_edit_export=can_edit_export,
        export_config=_get_export_config(), export_fields=_export_catalog(),
        export_default=_DEFAULT_EXPORT_CONFIG,
        export_samples=export_samples, active_tab=active_tab)


# Two-letter province prefixes used on some Sri Lankan registration numbers,
# written as "<CODE> " (code + a single space), e.g. "WP KA-1234".
_PROVINCE_CODES = frozenset(
    {"WP", "CP", "SP", "NP", "EP", "NW", "NC", "SG", "UV"})


def _strip_province_code(value: str) -> str:
    """Drop a leading province prefix (2 letters + a single space) when the two
    letters are a known province code, e.g. 'WP KA-1234' -> 'KA-1234'.  Values
    without such a prefix are returned unchanged (upper-cased, trimmed)."""
    v = (value or "").strip().upper()
    if len(v) >= 3 and v[2] == " " and v[:2] in _PROVINCE_CODES:
        return v[3:]
    return v


def _reg_match_key(value, omit_province: bool = False) -> str | None:
    """Normalise a registration / vehicle number to a comparison key: uppercase,
    stripped of every non-alphanumeric character (spaces, dashes, dots), so e.g.
    'CP BK-0999', 'CP-BK-0999' and 'cpbk0999' all map to 'CPBK0999'.  When
    ``omit_province`` is set, a leading province prefix (see
    ``_strip_province_code``) is removed first so 'WP KA-1234' matches
    'KA-1234'."""
    if not value:
        return None
    v = _strip_province_code(value) if omit_province else value.upper()
    return re.sub(r"[^A-Z0-9]", "", v) or None


def _json_val(v):
    """Coerce a record attribute to a JSON-serializable snapshot value."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)  # Decimal / datetime / etc.


def _nonempty(v) -> bool:
    return v is not None and str(v).strip() != ""


def _record_snapshot(kind: str, rec, catalog) -> list:
    """Ordered full-field snapshot of a source record: every catalog field
    (canonical data columns read from the ORM row, custom fields read from the
    record's field_values JSON), as ``[{"k","l","v"}, ...]``.  Any extra keys
    present in field_values but not in the catalog are appended."""
    try:
        fv = json.loads(rec.field_values) if rec.field_values else {}
    except (ValueError, TypeError):
        fv = {}
    if not isinstance(fv, dict):
        fv = {}
    out, seen = [], set()
    for d in catalog:
        k = d["key"]
        seen.add(k)
        v = getattr(rec, k, None) if d.get("canonical") else fv.get(k)
        out.append({"k": k, "l": d["label"], "v": _json_val(v)})
    for k, v in fv.items():
        if k not in seen:
            out.append({"k": k, "l": k.replace("_", " ").title(),
                        "v": _json_val(v)})
    return out


# Column order used to snapshot pre-0029 matched rows (no cr_fields/rc_fields)
# for display, mirroring the typed snapshot columns on MatchedRecord.
_MR_CR_FALLBACK = [
    ("registration_no", "Reg No"), ("cr_number", "CR Number"),
    ("chassis_no", "Chassis No"), ("engine_no", "Engine No"),
    ("current_owner", "Owner"), ("make", "Make"), ("model", "Model"),
    ("class_of_vehicle", "Class"), ("colour", "Colour"),
    ("year_of_manufacture", "Year"),
]
_MR_RC_FALLBACK = [
    ("vehicle_no", "Vehicle No"), ("slip_no", "Slip No"),
    ("receipt_no", "Receipt No"), ("receipt_date", "Date"),
    ("applicant_name", "Applicant"), ("total_amount", "Total"),
    ("bank_charges", "Charges"), ("bank_total", "Bank Total"),
]


def _matched_view_columns(rows):
    """Attach ``cr_map``/``rc_map`` (key->value) to each matched row and return
    ``(cr_cols, rc_cols)``: ordered ``(key, label)`` lists limited to fields that
    carry a value in at least one row.  Rows written before 0029 (no JSON
    snapshot) fall back to the typed snapshot columns."""
    def _parse(js, fallback):
        try:
            d = json.loads(js) if js else None
        except (ValueError, TypeError):
            d = None
        return d if isinstance(d, list) else fallback

    cr_order, cr_lab, cr_has = [], {}, set()
    rc_order, rc_lab, rc_has = [], {}, set()
    for m in rows:
        cl = _parse(m.cr_fields, [{"k": k, "l": l, "v": _json_val(getattr(m, k, None))}
                                  for k, l in _MR_CR_FALLBACK])
        rl = _parse(m.rc_fields, [{"k": k, "l": l, "v": _json_val(getattr(m, k, None))}
                                  for k, l in _MR_RC_FALLBACK])
        m.cr_map, m.rc_map = {}, {}
        for it in cl:
            k = it.get("k")
            if not k:
                continue
            if k not in cr_lab:
                cr_lab[k] = it.get("l") or k
                cr_order.append(k)
            m.cr_map[k] = it.get("v")
            if _nonempty(it.get("v")):
                cr_has.add(k)
        for it in rl:
            k = it.get("k")
            if not k:
                continue
            if k not in rc_lab:
                rc_lab[k] = it.get("l") or k
                rc_order.append(k)
            m.rc_map[k] = it.get("v")
            if _nonempty(it.get("v")):
                rc_has.add(k)
    cr_cols = [(k, cr_lab[k]) for k in cr_order if k in cr_has]
    rc_cols = [(k, rc_lab[k]) for k in rc_order if k in rc_has]
    return cr_cols, rc_cols


def _perform_mapping(user_id, omit_province: bool = False) -> int:
    """Core of the mapping action (no request/auth deps, so it is unit-testable):
    greedy one-to-one match of CR books to receipts on the normalised key, snapshot
    each pair into ``matched_records`` and delete the originals.  Adds rows to the
    session and returns the match count; the caller commits."""
    crs = (CRBookRecord.query
           .filter(CRBookRecord.registration_no.isnot(None))
           .order_by(CRBookRecord.id).all())
    rcs = (ReceiptRecord.query
           .filter(ReceiptRecord.vehicle_no.isnot(None))
           .order_by(ReceiptRecord.id).all())

    rc_by_key: dict[str, list] = {}
    for rc in rcs:
        key = _reg_match_key(rc.vehicle_no, omit_province)
        if key:
            rc_by_key.setdefault(key, []).append(rc)

    # Full field catalogs (canonical data columns + custom fields), computed
    # once, so each snapshot carries every field both tables hold.
    cr_cat = _field_catalog("cr")
    rc_cat = _field_catalog("rc")
    matched = 0
    used_rc: set[int] = set()
    for cr in crs:
        key = _reg_match_key(cr.registration_no, omit_province)
        if not key:
            continue
        rc = next((c for c in rc_by_key.get(key, ())
                   if c.id not in used_rc), None)
        if rc is None:
            continue
        used_rc.add(rc.id)
        db.session.add(MatchedRecord(
            matched_by=user_id, match_key=key,
            cr_source_upload_id=cr.upload_id, cr_source_record_id=cr.id,
            registration_no=cr.registration_no, cr_number=cr.cr_number,
            chassis_no=cr.chassis_no, engine_no=cr.engine_no,
            current_owner=cr.current_owner, make=cr.make, model=cr.model,
            class_of_vehicle=cr.class_of_vehicle, colour=cr.colour,
            year_of_manufacture=cr.year_of_manufacture,
            cr_page_no=cr.page_no, chassis_crop=cr.chassis_crop,
            reg_crop=cr.reg_crop, engine_crop=cr.engine_crop,
            rc_source_upload_id=rc.upload_id, rc_source_record_id=rc.id,
            vehicle_no=rc.vehicle_no, slip_no=rc.slip_no,
            receipt_no=rc.receipt_no, receipt_date=rc.receipt_date,
            applicant_name=rc.applicant_name, total_amount=rc.total_amount,
            bank_charges=rc.bank_charges, bank_total=rc.bank_total,
            rc_segment_no=rc.segment_no, image_crop=rc.image_crop,
            cr_field_values=cr.field_values, cr_field_crops=cr.field_crops,
            rc_field_values=rc.field_values, rc_field_crops=rc.field_crops,
            cr_fields=json.dumps(_record_snapshot("cr", cr, cr_cat)),
            rc_fields=json.dumps(_record_snapshot("rc", rc, rc_cat))))
        db.session.delete(cr)
        db.session.delete(rc)
        matched += 1
    # captured_records is the immutable "total captured" count, so mapping does
    # NOT decrement it; the uploads list shows live table counts separately.
    return matched


def _revoke_mapping(ids) -> int:
    """Reverse of ``_perform_mapping`` for the selected ``matched_records`` ids:
    recreate each snapshot's CR-book and receipt row in its source table (images
    included) and delete the matched row.  Adds to the session and returns the
    count; the caller commits."""
    if not ids:
        return 0
    rows = MatchedRecord.query.filter(MatchedRecord.id.in_(ids)).all()
    count = 0
    for m in rows:
        db.session.add(CRBookRecord(
            upload_id=m.cr_source_upload_id, page_no=m.cr_page_no,
            registration_no=m.registration_no, cr_number=m.cr_number,
            chassis_no=m.chassis_no, engine_no=m.engine_no,
            current_owner=m.current_owner, make=m.make, model=m.model,
            class_of_vehicle=m.class_of_vehicle, colour=m.colour,
            year_of_manufacture=m.year_of_manufacture,
            chassis_crop=m.chassis_crop, reg_crop=m.reg_crop,
            engine_crop=m.engine_crop,
            field_values=m.cr_field_values, field_crops=m.cr_field_crops))
        db.session.add(ReceiptRecord(
            upload_id=m.rc_source_upload_id, segment_no=m.rc_segment_no,
            vehicle_no=m.vehicle_no, slip_no=m.slip_no,
            receipt_no=m.receipt_no, receipt_date=m.receipt_date,
            applicant_name=m.applicant_name, total_amount=m.total_amount,
            bank_charges=m.bank_charges, bank_total=m.bank_total,
            image_crop=m.image_crop,
            field_values=m.rc_field_values, field_crops=m.rc_field_crops))
        db.session.delete(m)
        count += 1
    # captured_records is the immutable total, so revoke does not touch it.
    return count


@main_bp.route("/operations/registration-updater/mapping/process",
               methods=["POST"])
@login_required
def process_mapping():
    """Match receipts to CR books by vehicle number, move each matched pair into
    ``matched_records`` and delete the originals from their source tables.

    Matching is greedy one-to-one on the normalised key (see ``_reg_match_key``):
    each CR record is paired with the first still-unused receipt of the same key;
    unmatched records stay in their tables."""
    _require_module(REGISTRATION_UPDATER_KEY)
    omit_province = bool(request.form.get("omit_province"))
    matched = _perform_mapping(current_user.id, omit_province)
    record_audit("registration_mapping",
                 f"matched={matched} omit_province={omit_province}")
    db.session.commit()
    if matched:
        flash(f"Mapped {matched} record(s): moved to Matched Records and removed "
              f"from CR books / receipts.", "success")
    else:
        flash("No matching records found (no receipt vehicle number matched a CR "
              "book registration number).", "info")
    return redirect(url_for("main.registration_updater", tab="map"))


@main_bp.route("/operations/registration-updater/mapping/revoke",
               methods=["POST"])
@login_required
def revoke_mapping():
    """Return the selected matched records to their source tables: recreate each
    CR-book and receipt row (images included) and remove the matched row."""
    _require_module(REGISTRATION_UPDATER_KEY)
    ids = [int(x) for x in request.form.getlist("ids") if x.isdigit()]
    revoked = _revoke_mapping(ids)
    record_audit("registration_mapping_revoke", f"revoked={revoked}")
    db.session.commit()
    if revoked:
        flash(f"Revoked {revoked} mapping(s): records returned to CR books / "
              f"receipts.", "success")
    else:
        flash("No matched records selected to revoke.", "info")
    return redirect(url_for("main.registration_updater", tab="map"))


# --- Export (SAP batch-input "RMV UPLOAD") --------------------------------
# Fixed columns and the four standard line items per record, taken verbatim from
# SampleOutput/RMV UPLOAD.xls (a tab-separated file).  Amounts are fixed standard
# fees; the credit "Slip Value" is their sum.
_EXPORT_HDR1 = ["TRNID", "TCODE", "BLDAT", "BLART", "BUKRS", "BUDAT", "WAERS",
                "XBLNR", "BKTXT", "NEWBS", "WRBTR", "ZUONR", "SGTXT", "NEWKO",
                "PRCTR"]
_EXPORT_HDR2 = ["TRNID", "Transaction Code", "Date (batch input)",
                "Document Type", "Company Code", "Date (batch input)",
                "Currency Key", "Reference Document Number",
                "Document Header Text", "Posting Key for the Next Line Item",
                "Amount in Document Currency (Batch Input Field)",
                "Assignment Number", "Item Text",
                "Account or Matchcode for the Next Line Item", "Profit Center"]
# Built-in default template config: reproduces the previous fixed four-line RMV
# UPLOAD layout, so exports are unchanged until the template is edited.
_DEFAULT_EXPORT_CONFIG = {
    "constants": {"TCODE": "FB01", "BLART": "SA", "BUKRS": "2501",
                  "WAERS": "LKR", "PRCTR": "W111D1"},
    "doc_fields": {
        "BLDAT": {"src": "field", "key": "receipt_date", "fmt": "ddmmyyyy"},
        "BUDAT": {"src": "field", "key": "receipt_date", "fmt": "ddmmyyyy"},
        "XBLNR": {"src": "field", "key": "slip_no"},
        "BKTXT": {"src": "field", "key": "receipt_no"},
        "ZUONR": {"src": "field", "key": "chassis_no"},
    },
    "lines": [
        {"newbs": "40", "newko": "177205",
         "sgtxt": {"src": "const", "value": "Vehicle Registration"},
         "amount": {"src": "const", "value": "11010"},
         "when": {"type": "always"}},
        {"newbs": "40", "newko": "177205",
         "sgtxt": {"src": "const", "value": "Special No Fees"},
         "amount": {"src": "const", "value": "25000"},
         "when": {"type": "always"}},
        {"newbs": "40", "newko": "177205",
         "sgtxt": {"src": "const", "value": "Number Plate Fees"},
         "amount": {"src": "const", "value": "4500"},
         "when": {"type": "always"}},
        {"newbs": "50", "newko": "119682",
         "sgtxt": {"src": "const", "value": "Slip Value"},
         "amount": {"src": "sum_debits"},
         "when": {"type": "always"}},
    ],
}
# Fixed 15-column output layout; per-column source resolved from the config's
# constants / doc_fields / per-line values (TRNID and the line-level
# NEWBS/WRBTR/SGTXT/NEWKO are filled per row).
_EXPORT_CONST_COLS = ("TCODE", "BLART", "BUKRS", "WAERS", "PRCTR")
_EXPORT_DOC_COLS = ("BLDAT", "BUDAT", "XBLNR", "BKTXT", "ZUONR")


def _to_ddmmyyyy(value) -> str:
    """Convert a captured date string to DDMMYYYY (receipt dates are DD/MM/YYYY).
    Falls back to the digits present, or empty when none."""
    if not value:
        return ""
    parts = re.split(r"[/\-.]", str(value).strip())
    if len(parts) == 3:
        d, m, y = parts
        if len(y) == 2:
            y = "20" + y
        try:
            return f"{int(d):02d}{int(m):02d}{int(y):04d}"
        except ValueError:
            pass
    return re.sub(r"\D", "", str(value))


def _export_catalog() -> list:
    """Merged field list offered in the Export-setting popup: every CR-book and
    receipt field (canonical + custom) as ``{key,label,group}``."""
    out, seen = [], set()
    for kind, group in (("cr", "CR Book"), ("rc", "Receipt")):
        for d in _field_catalog(kind):
            if d["key"] in seen:
                continue
            seen.add(d["key"])
            out.append({"key": d["key"], "label": d["label"], "group": group})
    return out


def _get_export_config() -> dict:
    """Load the saved export template config, falling back to the built-in
    default; missing sections are backfilled from the default."""
    tpl = ExportTemplate.query.order_by(ExportTemplate.id).first()
    cfg = None
    if tpl and tpl.config:
        try:
            parsed = json.loads(tpl.config)
            if isinstance(parsed, dict):
                cfg = parsed
        except (ValueError, TypeError):
            cfg = None
    if cfg is None:
        return copy.deepcopy(_DEFAULT_EXPORT_CONFIG)
    cfg.setdefault("constants", {})
    cfg.setdefault("doc_fields", {})
    if not isinstance(cfg.get("lines"), list):
        cfg["lines"] = []
    for col, val in _DEFAULT_EXPORT_CONFIG["constants"].items():
        cfg["constants"].setdefault(col, val)
    return cfg


def _export_values(m) -> dict:
    """Flat ``key -> value`` view of a matched record for export: every field
    from the CR and receipt snapshots (custom fields included), with typed
    columns as a fallback for pre-snapshot rows."""
    vals: dict = {}
    for js in (m.cr_fields, m.rc_fields):
        try:
            arr = json.loads(js) if js else []
        except (ValueError, TypeError):
            arr = []
        if isinstance(arr, list):
            for it in arr:
                k = it.get("k")
                if k and vals.get(k) in (None, ""):
                    vals[k] = it.get("v")
    for k in ("registration_no", "chassis_no", "engine_no", "vehicle_no",
              "slip_no", "receipt_no", "receipt_date", "total_amount",
              "bank_charges", "bank_total"):
        if vals.get(k) in (None, ""):
            vals[k] = _json_val(getattr(m, k, None))
    return vals


def _amount_dec(value) -> Decimal:
    """Parse a captured/constant amount into a Decimal (0 on failure)."""
    try:
        return Decimal(re.sub(r"[^0-9.\-]", "", str(value)) or "0")
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _fmt_amount(dec: Decimal) -> str:
    """Format an amount: no decimals when whole, else two decimals."""
    dec = dec or Decimal(0)
    if dec == dec.to_integral_value():
        return str(int(dec))
    return f"{dec:.2f}"


def _resolve_source(spec, vals) -> str:
    """Resolve a value-source spec (constant / field, with optional date format)
    to a string.  ``sum_debits`` is handled by the caller, not here."""
    if not isinstance(spec, dict):
        return ""
    src = spec.get("src")
    if src == "const":
        return str(spec.get("value") or "")
    if src == "field":
        v = vals.get(spec.get("key"))
        s = "" if v is None else str(v)
        if spec.get("fmt") == "ddmmyyyy":
            return _to_ddmmyyyy(s)
        return s
    return ""


def _eval_when(when, vals) -> bool:
    """Evaluate a line's include condition against a record's values."""
    t = (when or {}).get("type", "always")
    if t == "always":
        return True
    v = vals.get((when or {}).get("key"))
    if t == "has_value":
        return _nonempty(v)
    if t == "empty":
        return not _nonempty(v)
    if t == "compare":
        if not _nonempty(v):
            return False
        try:
            a = float(re.sub(r"[^0-9.\-]", "", str(v)) or "nan")
            b = float(when.get("value"))
        except (ValueError, TypeError):
            return False
        op = when.get("op")
        return {">": a > b, ">=": a >= b, "=": a == b,
                "!=": a != b}.get(op, False)
    return True


def _build_record_rows(m, trnid: int, cfg: dict) -> list:
    """Expand one matched record into its output rows per the template config."""
    vals = _export_values(m)
    consts = cfg.get("constants", {})
    doc = {col: _resolve_source(cfg.get("doc_fields", {}).get(col, {}), vals)
           for col in _EXPORT_DOC_COLS}
    included, debit_sum = [], Decimal(0)
    for ln in cfg.get("lines", []):
        if not _eval_when(ln.get("when"), vals):
            continue
        amt_spec = ln.get("amount", {})
        if amt_spec.get("src") == "sum_debits":
            amt = None
        else:
            amt = _amount_dec(_resolve_source(amt_spec, vals))
            if str(ln.get("newbs", "")) == "40":
                debit_sum += amt
        included.append((ln, amt))
    rows = []
    for ln, amt in included:
        if amt is None:  # sum_debits placeholder
            amt = debit_sum
        rows.append([
            str(trnid), consts.get("TCODE", ""), doc["BLDAT"],
            consts.get("BLART", ""), consts.get("BUKRS", ""), doc["BUDAT"],
            consts.get("WAERS", ""), doc["XBLNR"], doc["BKTXT"],
            str(ln.get("newbs", "")), _fmt_amount(amt), doc["ZUONR"],
            _resolve_source(ln.get("sgtxt", {}), vals),
            str(ln.get("newko", "")), consts.get("PRCTR", "")])
    return rows


def _write_rmv_export(path: str, records) -> None:
    """Write the matched ``records`` as a tab-separated RMV UPLOAD file: two
    header rows then, per record, the template's line items (TRNID increments
    per record)."""
    cfg = _get_export_config()
    rows = [_EXPORT_HDR1, _EXPORT_HDR2]
    for i, m in enumerate(records, start=1):
        rows.extend(_build_record_rows(m, i, cfg))
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("".join("\t".join(r) + "\r\n" for r in rows))


@main_bp.route("/operations/registration-updater/export/generate",
               methods=["POST"])
@login_required
def generate_export():
    """Write the selected matched records to a new RMV UPLOAD export file, link
    them to it (removing them from the Mapped-records tab) and record it."""
    _require_module(REGISTRATION_UPDATER_KEY)
    ids = [int(x) for x in request.form.getlist("ids") if x.isdigit()]
    records = []
    if ids:
        records = (MatchedRecord.query
                   .filter(MatchedRecord.id.in_(ids),
                           MatchedRecord.export_document_id == None)
                   .order_by(MatchedRecord.id).all())
    if not records:
        flash("No matched records selected to export.", "info")
        return redirect(url_for("main.registration_updater", tab="map"))

    export_dir = current_app.config["EXPORT_DIR"]
    os.makedirs(export_dir, exist_ok=True)
    doc = ExportDocument(
        created_by=current_user.id, filename="pending", stored_path="pending",
        records_count=len(records))
    db.session.add(doc)
    db.session.flush()  # assign id for the filename
    filename = f"RMV-UPLOAD-{doc.id:05d}.xls"
    path = os.path.join(export_dir, filename)
    _write_rmv_export(path, records)
    doc.filename = filename
    doc.stored_path = path
    doc.file_size = os.path.getsize(path)
    for m in records:
        m.export_document_id = doc.id
    record_audit("registration_export_generate",
                 f"export={doc.id} records={len(records)}")
    db.session.commit()
    flash(f"Export #{doc.id} created with {len(records)} record(s).", "success")
    return redirect(url_for("main.registration_updater", tab="export"))


@main_bp.route("/operations/registration-updater/export/<int:export_id>/revoke",
               methods=["POST"])
@login_required
def revoke_export(export_id: int):
    """Delete an export: return its records to the Mapped-records tab and remove
    the export row and its file."""
    _require_module(REGISTRATION_UPDATER_KEY)
    doc = db.session.get(ExportDocument, export_id) or abort(404)
    n = 0
    for m in list(doc.records):
        m.export_document_id = None
        n += 1
    try:
        if doc.stored_path and os.path.exists(doc.stored_path):
            os.remove(doc.stored_path)
    except OSError:
        pass
    db.session.delete(doc)
    record_audit("registration_export_revoke",
                 f"export={export_id} records={n}")
    db.session.commit()
    flash(f"Export #{export_id} revoked: {n} record(s) returned to Mapped "
          f"records.", "success")
    return redirect(url_for("main.registration_updater", tab="export"))


@main_bp.route("/operations/registration-updater/export/<int:export_id>/download")
@login_required
def download_export(export_id: int):
    """Download a generated RMV UPLOAD export file."""
    _require_module(REGISTRATION_UPDATER_KEY)
    doc = db.session.get(ExportDocument, export_id) or abort(404)
    if not doc.stored_path or not os.path.exists(doc.stored_path):
        abort(404)
    return send_file(doc.stored_path, as_attachment=True,
                     download_name=doc.filename,
                     mimetype="application/vnd.ms-excel")


def _clean_source_spec(spec, allow_sum=False) -> dict:
    """Sanitise a value-source spec posted from the Export-setting popup."""
    spec = spec if isinstance(spec, dict) else {}
    src = spec.get("src")
    if src == "field":
        out = {"src": "field", "key": str(spec.get("key") or "")}
        if spec.get("fmt") == "ddmmyyyy":
            out["fmt"] = "ddmmyyyy"
        return out
    if allow_sum and src == "sum_debits":
        return {"src": "sum_debits"}
    return {"src": "const", "value": str(spec.get("value") or "")}


def _clean_export_config(raw) -> dict:
    """Validate/normalise a posted export config into the stored shape."""
    raw = raw if isinstance(raw, dict) else {}
    constants = {}
    for col in _EXPORT_CONST_COLS:
        constants[col] = str((raw.get("constants") or {}).get(col, ""))
    doc_fields = {}
    for col in _EXPORT_DOC_COLS:
        doc_fields[col] = _clean_source_spec((raw.get("doc_fields") or {}).get(col))
    lines = []
    for ln in (raw.get("lines") or []):
        ln = ln if isinstance(ln, dict) else {}
        when = ln.get("when") if isinstance(ln.get("when"), dict) else {}
        wtype = when.get("type") if when.get("type") in (
            "always", "has_value", "empty", "compare") else "always"
        clean_when = {"type": wtype}
        if wtype in ("has_value", "empty", "compare"):
            clean_when["key"] = str(when.get("key") or "")
        if wtype == "compare":
            clean_when["op"] = when.get("op") if when.get("op") in (
                ">", ">=", "=", "!=") else ">"
            clean_when["value"] = str(when.get("value") or "")
        lines.append({
            "newbs": "50" if str(ln.get("newbs")) == "50" else "40",
            "newko": str(ln.get("newko") or ""),
            "sgtxt": _clean_source_spec(ln.get("sgtxt")),
            "amount": _clean_source_spec(ln.get("amount"), allow_sum=True),
            "when": clean_when,
        })
    return {"constants": constants, "doc_fields": doc_fields, "lines": lines}


@main_bp.route("/operations/registration-updater/export/settings",
               methods=["POST"])
@login_required
def export_settings_save():
    """Persist the single export template config (AI Trainers / admins only)."""
    _require_module(REGISTRATION_UPDATER_KEY)
    _require_trainer()
    cfg = _clean_export_config(request.get_json(silent=True))
    tpl = ExportTemplate.query.order_by(ExportTemplate.id).first()
    if tpl is None:
        tpl = ExportTemplate(name="Default")
        db.session.add(tpl)
    tpl.config = json.dumps(cfg)
    tpl.updated_by = current_user.id
    record_audit("registration_export_settings", f"lines={len(cfg['lines'])}")
    db.session.commit()
    return {"ok": True}


@main_bp.route("/operations/registration-updater/export/settings/preview",
               methods=["POST"])
@login_required
def export_settings_preview():
    """Render one matched record through a (possibly unsaved) config so the popup
    can show a live single-record preview."""
    _require_module(REGISTRATION_UPDATER_KEY)
    _require_trainer()
    body = request.get_json(silent=True) or {}
    cfg = _clean_export_config(body.get("config"))
    ids = body.get("record_ids")
    if not isinstance(ids, list):
        ids = [body["record_id"]] if body.get("record_id") else []
    records = []
    for rid in ids:
        try:
            m = db.session.get(MatchedRecord, int(rid))
        except (ValueError, TypeError):
            m = None
        if m is not None:
            records.append(m)
    if not records:
        m = (MatchedRecord.query
             .filter(MatchedRecord.export_document_id == None)
             .order_by(MatchedRecord.matched_at.desc()).first())
        if m is not None:
            records.append(m)
    if not records:
        return {"ok": True, "headers": _EXPORT_HDR1, "rows": [],
                "label": "", "empty": True}
    rows = []
    for i, m in enumerate(records, start=1):
        rows.extend(_build_record_rows(m, i, cfg))
    label = ", ".join(m.registration_no or m.vehicle_no or f"#{m.id}"
                       for m in records)
    return {"ok": True, "headers": _EXPORT_HDR1, "rows": rows, "label": label,
            "count": len(records)}


@main_bp.route("/operations/registration-updater/<kind>/<int:upload_id>/process",
               methods=["POST"])
@login_required
def process_upload(kind: str, upload_id: int):
    _require_module(REGISTRATION_UPDATER_KEY)
    if kind not in _DOC_KINDS:
        abort(404)
    up = db.session.get(_DOC_KINDS[kind]["upload_model"], upload_id) or abort(404)
    if up.status == STATUS_PROCESSING:
        flash("This file is already being processed.", "info")
        return redirect(url_for("main.registration_updater", tab=kind))
    if kind == "cr" and up.review_status != REVIEW_READY:
        flash("Resolve the CR-book format review before processing.", "info")
        return redirect(url_for("main.cr_review", upload_id=upload_id))
    up.status = STATUS_PROCESSING
    up.error = None
    db.session.commit()
    record_audit(f"{kind}_process", f"upload_id={upload_id}")
    _start_processing(kind, upload_id)
    flash("Processing started (OCR runs in the background). Refresh to see "
          "results.", "success")
    return redirect(url_for("main.registration_updater", tab=kind))


@main_bp.route("/operations/registration-updater/status")
@login_required
def registration_status():
    _require_module(REGISTRATION_UPDATER_KEY)

    def _ser(u, kind):
        return {
            "kind": kind, "id": u.id, "status": u.status,
            "processed": bool(u.processed),
            "captured": u.captured_records, "duplicates": u.duplicate_records,
            "page_count": u.page_count,
            "progress_done": u.progress_done, "progress_total": u.progress_total,
            "message": u.progress_message,
            "has_records": u.captured_records > 0,
            "review": getattr(u, "review_status", None),
        }

    data = ([_ser(u, "cr") for u in CRBookUpload.query.all()]
            + [_ser(u, "rc") for u in ReceiptUpload.query.all()])
    return {"uploads": data}


@main_bp.route("/operations/registration-updater/<kind>/<int:upload_id>/records")
@login_required
def upload_records(kind: str, upload_id: int):
    _require_module(REGISTRATION_UPDATER_KEY)
    if kind not in _DOC_KINDS:
        abort(404)
    up = db.session.get(_DOC_KINDS[kind]["upload_model"], upload_id) or abort(404)
    RecordModel = _DOC_KINDS[kind]["record_model"]
    records = (
        RecordModel.query.filter_by(upload_id=upload_id)
        .order_by(RecordModel.id).all()
    )
    # Template-defined custom fields (non-canonical) associated with this
    # upload, shown as extra columns from each record's field_values JSON.
    from app.utils import registration_ocr as ocr
    canon = _canonical_keys(kind)
    defs = (ocr.template_field_defs(up.template)
            if getattr(up, "template_id", None) else [])
    label_by_key = {d["key"]: d["label"] for d in defs}
    extra_keys = [d["key"] for d in defs
                  if d["key"] not in canon and d.get("capture")]
    for r in records:
        try:
            r.fv = json.loads(r.field_values) if r.field_values else {}
        except (ValueError, TypeError):
            r.fv = {}
        for k in r.fv:
            if k not in canon and k not in extra_keys:
                extra_keys.append(k)
                label_by_key.setdefault(k, k.replace("_", " ").title())
    extra_fields = [(k, label_by_key.get(k, k)) for k in extra_keys]
    return render_template(
        "main/registration_records.html", kind=kind, upload=up,
        records=records, extra_fields=extra_fields)


@main_bp.route("/operations/registration-updater/cr/<int:upload_id>"
               "/crop/<int:record_id>/<field>")
@login_required
def cr_value_crop(upload_id: int, record_id: int, field: str):
    """Serve the cropped value-region scan (chassis|reg|engine) for one record."""
    _require_module(REGISTRATION_UPDATER_KEY)
    if field not in ("chassis", "reg", "engine"):
        abort(404)
    rec = db.session.get(CRBookRecord, record_id) or abort(404)
    if rec.upload_id != upload_id:
        abort(404)
    fn = {"chassis": rec.chassis_crop, "reg": rec.reg_crop,
          "engine": rec.engine_crop}.get(field)
    if not fn:
        abort(404)
    crop_dir = os.path.join(current_app.config["CR_BOOK_CROP_DIR"], str(upload_id))
    resp = send_from_directory(crop_dir, fn)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@main_bp.route("/operations/registration-updater/rc/<int:upload_id>"
               "/image/<int:record_id>")
@login_required
def receipt_image(upload_id: int, record_id: int):
    """Serve the cropped page-region scan of one receipt for the image viewer."""
    _require_module(REGISTRATION_UPDATER_KEY)
    rec = db.session.get(ReceiptRecord, record_id) or abort(404)
    if rec.upload_id != upload_id or not rec.image_crop:
        abort(404)
    crop_dir = os.path.join(current_app.config["RECEIPT_CROP_DIR"], str(upload_id))
    resp = send_from_directory(crop_dir, rec.image_crop)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@main_bp.route("/operations/registration-updater/rc/<int:upload_id>"
               "/crop/<int:record_id>/<field>")
@login_required
def receipt_value_crop(upload_id: int, record_id: int, field: str):
    """Serve one receipt's per-field value-region crop (from the template box)."""
    _require_module(REGISTRATION_UPDATER_KEY)
    attr = _RC_CROP_TOKEN.get(field)
    if not attr:
        abort(404)
    rec = db.session.get(ReceiptRecord, record_id) or abort(404)
    fn = getattr(rec, attr, None)
    if rec.upload_id != upload_id or not fn:
        abort(404)
    crop_dir = os.path.join(current_app.config["RECEIPT_CROP_DIR"], str(upload_id))
    resp = send_from_directory(crop_dir, fn)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# Critical CR fields captured as labeled data -> (crop attr, confidence attr).
_CR_LABEL_FIELDS = {
    "registration_no": ("reg_crop", "registration_no_confidence"),
    "chassis_no": ("chassis_crop", "chassis_no_confidence"),
    "engine_no": ("engine_crop", "engine_no_confidence"),
}


def _cr_page_format_key(rec) -> str | None:
    """Rule-set / format key of the CR page a record came from, for grouping
    accuracy by format.  None when the page row is gone."""
    if not rec.page_no:
        return None
    page = (CRUploadPage.query
            .filter_by(upload_id=rec.upload_id, page_no=rec.page_no).first())
    return page.format.rule_set if page and page.format else None


def _norm_field_value(field: str, value) -> str:
    """Normalise a value the same way the OCR pipeline does, so cosmetic
    differences do not count as an operator correction."""
    if not value:
        return ""
    from app.utils import registration_ocr as ocr
    if field == "registration_no":
        return ocr._fix_reg_lk(ocr._norm_reg(value))
    if field == "chassis_no":
        return ocr._norm_chassis(value)
    if field == "engine_no":
        return ocr._norm_engine(value)
    return value.strip().upper()


def _capture_cr_label(rec, field: str, new_value, fmt_key) -> None:
    """Upsert one labeled-data row for a CR record's critical field and copy its
    value-region crop into the persistent label store.  Must be called BEFORE the
    record's field is overwritten, so ``ocr_value`` (captured once) is the model's
    output rather than the operator's value."""
    if not new_value:
        return
    crop_attr, conf_attr = _CR_LABEL_FIELDS[field]
    label = OcrCorrection.query.filter_by(
        doc_kind="cr", record_id=rec.id, field=field).first()
    if label is None:
        label = OcrCorrection(
            doc_kind="cr", upload_id=rec.upload_id, record_id=rec.id,
            page_no=rec.page_no, field=field, ocr_value=getattr(rec, field),
            confidence=getattr(rec, conf_attr), rule_set=fmt_key)
        db.session.add(label)
    label.corrected_value = new_value
    label.was_correct = (_norm_field_value(field, label.ocr_value)
                         == _norm_field_value(field, new_value))
    label.user_id = current_user.id
    label.created_at = datetime.utcnow()
    crop_fn = getattr(rec, crop_attr, None)
    if crop_fn and not label.crop_file:
        src = os.path.join(current_app.config["CR_BOOK_CROP_DIR"],
                           str(rec.upload_id), crop_fn)
        if os.path.exists(src):
            import shutil
            label_dir = current_app.config["OCR_LABEL_DIR"]
            os.makedirs(label_dir, exist_ok=True)
            dst_fn = f"cr_{rec.id}_{field}.png"
            try:
                shutil.copyfile(src, os.path.join(label_dir, dst_fn))
                label.crop_file = dst_fn
            except OSError:
                pass


@main_bp.route("/operations/registration-updater/cr/<int:upload_id>"
               "/record/<int:record_id>/edit", methods=["POST"])
@login_required
def cr_edit_record(upload_id: int, record_id: int):
    """Save an operator's correction of a CR record's Reg/Chassis/Engine No and
    capture the verified values as durable labeled data (feedback loop)."""
    _require_module(REGISTRATION_UPDATER_KEY)
    rec = db.session.get(CRBookRecord, record_id) or abort(404)
    if rec.upload_id != upload_id:
        abort(404)
    new_reg = (request.form.get("registration_no") or "").strip()[:60] or None
    new_chassis = (request.form.get("chassis_no") or "").strip()[:60] or None
    new_engine = (request.form.get("engine_no") or "").strip()[:60] or None
    # Capture labels BEFORE overwriting so ocr_value is the model's output.
    fmt_key = _cr_page_format_key(rec)
    _capture_cr_label(rec, "registration_no", new_reg, fmt_key)
    _capture_cr_label(rec, "chassis_no", new_chassis, fmt_key)
    _capture_cr_label(rec, "engine_no", new_engine, fmt_key)
    rec.registration_no = new_reg
    rec.chassis_no = new_chassis
    rec.engine_no = new_engine
    rec.needs_review = False  # an operator has verified/corrected this row
    record_audit("cr_record_edited", f"record_id={record_id}")
    db.session.commit()
    flash("Record updated.", "success")
    return redirect(url_for("main.upload_records", kind="cr", upload_id=upload_id))


def _require_trainer() -> None:
    """Gate the OCR training views to AI Trainers and Administrators."""
    if not (current_user.is_ai_trainer or current_user.is_administrator):
        abort(403)


@main_bp.route("/operations/registration-updater/training")
@login_required
def ocr_training():
    """OCR feedback dashboard: real accuracy from operator corrections plus
    mined confusion rules / per-field priors (report-only)."""
    _require_trainer()
    from app.utils import ocr_feedback as fb
    from datetime import timedelta

    correct_sum = func.sum(case((OcrCorrection.was_correct == True, 1), else_=0))

    # Per-field accuracy over all captured labels.
    field_stats, overall_c, overall_t = [], 0, 0
    for field, total, correct in (
            db.session.query(OcrCorrection.field,
                             func.count(OcrCorrection.id), correct_sum)
            .group_by(OcrCorrection.field).all()):
        total, correct = int(total or 0), int(correct or 0)
        overall_c += correct
        overall_t += total
        field_stats.append({
            "field": field, "total": total, "correct": correct,
            "pct": round(100 * correct / total, 1) if total else None})
    field_stats.sort(key=lambda s: s["field"])
    overall = {"total": overall_t, "correct": overall_c,
               "pct": round(100 * overall_c / overall_t, 1) if overall_t else None}

    # Per-format accuracy.
    fmt_stats = []
    for rule_set, total, correct in (
            db.session.query(OcrCorrection.rule_set,
                             func.count(OcrCorrection.id), correct_sum)
            .group_by(OcrCorrection.rule_set).all()):
        total, correct = int(total or 0), int(correct or 0)
        fmt_stats.append({
            "rule_set": rule_set or "(unknown)", "total": total,
            "correct": correct,
            "pct": round(100 * correct / total, 1) if total else None})
    fmt_stats.sort(key=lambda s: s["total"], reverse=True)

    # 14-day daily trend, bucketed in Python (DB-agnostic).
    since = datetime.utcnow() - timedelta(days=14)
    buckets: dict[str, list[int]] = {}
    for ts, ok in (db.session.query(OcrCorrection.created_at,
                                    OcrCorrection.was_correct)
                   .filter(OcrCorrection.created_at >= since).all()):
        b = buckets.setdefault(ts.date().isoformat(), [0, 0])
        b[1] += 1
        b[0] += 1 if ok else 0
    trend = [{"day": d, "correct": c, "total": t,
              "pct": round(100 * c / t, 1) if t else None}
             for d, (c, t) in sorted(buckets.items())]

    # Mined confusion rules + per-field char priors (report-only).
    changed = (db.session.query(OcrCorrection.field, OcrCorrection.ocr_value,
                                OcrCorrection.corrected_value)
               .filter(OcrCorrection.was_correct == False,
                       OcrCorrection.ocr_value.isnot(None),
                       OcrCorrection.corrected_value.isnot(None)).all())
    confusions = fb.top_confusions(fb.char_confusions(changed), limit=20)
    priors = fb.field_char_priors(
        db.session.query(OcrCorrection.field, OcrCorrection.corrected_value)
        .filter(OcrCorrection.corrected_value.isnot(None)).all())
    crop_count = (db.session.query(func.count(OcrCorrection.id))
                  .filter(OcrCorrection.crop_file.isnot(None)).scalar())

    # Trainer tab: CR batches to view, saved positional templates, field list.
    from app.utils import registration_ocr as ocr
    cr_batches = [
        {"id": up.id, "filename": up.filename, "page_count": up.page_count or 0}
        for up in (CRBookUpload.query
                   .filter(CRBookUpload.page_count > 0)
                   .order_by(CRBookUpload.uploaded_at.desc()).all())]
    cr_templates = _serialize_cr_templates()
    cr_fields = ocr.cr_field_options()

    return render_template(
        "main/ocr_training.html", overall=overall, field_stats=field_stats,
        fmt_stats=fmt_stats, trend=trend, confusions=confusions,
        priors=priors, crop_count=int(crop_count or 0),
        cr_batches=cr_batches, cr_templates=cr_templates, cr_fields=cr_fields)


@main_bp.route("/operations/registration-updater/training/export")
@login_required
def ocr_training_export():
    """Download the captured labeled data (manifest.csv + crops) as a ZIP for
    offline recognizer fine-tuning."""
    _require_trainer()
    import csv
    import io
    import zipfile

    labels = OcrCorrection.query.order_by(OcrCorrection.id).all()
    label_dir = current_app.config["OCR_LABEL_DIR"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        sio = io.StringIO()
        writer = csv.writer(sio)
        writer.writerow([
            "id", "created_at", "doc_kind", "field", "ocr_value",
            "corrected_value", "was_correct", "confidence", "rule_set",
            "crop_file", "page_no", "upload_id", "record_id"])
        for lab in labels:
            writer.writerow([
                lab.id,
                lab.created_at.isoformat() if lab.created_at else "",
                lab.doc_kind, lab.field, lab.ocr_value or "",
                lab.corrected_value or "", int(lab.was_correct),
                lab.confidence if lab.confidence is not None else "",
                lab.rule_set or "", lab.crop_file or "", lab.page_no or "",
                lab.upload_id or "", lab.record_id or ""])
        zf.writestr("manifest.csv", sio.getvalue())
        seen = set()
        for lab in labels:
            if lab.crop_file and lab.crop_file not in seen:
                path = os.path.join(label_dir, lab.crop_file)
                if os.path.exists(path):
                    zf.write(path, arcname=f"crops/{lab.crop_file}")
                    seen.add(lab.crop_file)
    record_audit("ocr_labels_exported", f"count={len(labels)}")
    db.session.commit()
    fname = f"ocr-labels-{datetime.utcnow():%Y%m%d-%H%M%S}.zip"
    return Response(
        buf.getvalue(), mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename={fname}"})


def _serialize_cr_templates() -> list[dict]:
    """Saved positional templates (CR formats that carry trainer-drawn value
    rectangles) for the Trainer editor's 'apply existing template' picker."""
    out = []
    for fmt in (CRFormat.query.filter_by(is_active=True)
                .order_by(CRFormat.name).all()):
        sig = _load_sig(fmt.signature)
        rects = sig.get("rects") or {}
        if not rects:
            continue
        out.append({"id": fmt.id, "name": fmt.name, "rects": rects,
                    "validation": sig.get("validation") or None,
                    "field_pp": sig.get("field_pp") or {}})
    return out


@main_bp.route("/operations/registration-updater/training/cr/<int:upload_id>"
               "/page/<int:page_no>.png")
@login_required
def tr_cr_page_image(upload_id: int, page_no: int):
    """Render a CR-book page for the Trainer template editor (trainer-gated)."""
    _require_trainer()
    up = db.session.get(CRBookUpload, upload_id) or abort(404)
    from app.utils import registration_ocr as ocr
    doc, total = ocr.open_document(up.stored_path)
    try:
        if page_no < 1 or page_no > total:
            abort(404)
        png = ocr._render_page_png(doc[page_no - 1], 2.0)
    finally:
        doc.close()
    resp = Response(png, mimetype="image/png")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@main_bp.route("/operations/registration-updater/cr/<int:upload_id>/ocr-preview",
               methods=["POST"])
@login_required
def cr_ocr_preview(upload_id: int):
    """Render a page of a CR upload with the given image adjustments so the
    upload's OCR-settings popup can show a live preview (page nav supported)."""
    _require_module(REGISTRATION_UPDATER_KEY)
    from app.utils import registration_ocr as ocr
    up = db.session.get(CRBookUpload, upload_id) or abort(404)
    data = request.get_json(silent=True) or {}
    pp = ocr.clean_pp(data.get("pp"))
    doc, total = ocr.open_document(up.stored_path)
    try:
        idx = max(1, min(total, int(data.get("page") or 1))) - 1
        png = ocr.render_page_png(doc[idx], pp=pp, zoom=1.6)
    finally:
        doc.close()
    resp = Response(png, mimetype="image/png")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@main_bp.route("/operations/registration-updater/cr/<int:upload_id>/ocr-test",
               methods=["POST"])
@login_required
def cr_ocr_test(upload_id: int):
    """Run CR OCR on the first page with the given image adjustments and return
    the captured fields with confidence, for the OCR-settings "Test OCR" button."""
    _require_module(REGISTRATION_UPDATER_KEY)
    from app.utils import registration_ocr as ocr
    up = db.session.get(CRBookUpload, upload_id) or abort(404)
    data = request.get_json(silent=True) or {}
    pp = ocr.clean_pp(data.get("pp"))
    rects = ocr.clean_rects(data.get("rects")) or None
    anchors = _cr_anchor_patches(
        up, data.get("ref_slip") or data.get("page") or 1,
        data.get("anchors")) or None
    doc, total = ocr.open_document(up.stored_path)
    try:
        idx = max(1, min(total, int(data.get("page") or 1))) - 1
        fields = ocr.test_cr_page(doc[idx], pp=pp, rects=rects, anchors=anchors)
        _apply_test_masks("cr", ocr.clean_fields(data.get("fields"), "cr"),
                          fields, doc[idx], None, pp)
    finally:
        doc.close()
    return jsonify({"ok": True, "fields": fields})


@main_bp.route("/operations/registration-updater/cr/<int:upload_id>/validate-read",
               methods=["POST"])
@login_required
def cr_validate_read(upload_id: int):
    """OCR the validate box on the current page and return the captured text (and
    whether it satisfies the given validate pattern) for the Template picker."""
    _require_module(REGISTRATION_UPDATER_KEY)
    from app.utils import registration_ocr as ocr
    up = db.session.get(CRBookUpload, upload_id) or abort(404)
    data = request.get_json(silent=True) or {}
    box = _parse_box(data.get("box"))
    if not box:
        return jsonify({"ok": False, "error": "Draw the validate box first."}), 400
    pp = ocr.clean_pp(data.get("pp"))
    doc, total = ocr.open_document(up.stored_path)
    try:
        idx = max(1, min(total, int(data.get("page") or 1))) - 1
        text, conf = ocr.read_rect(doc[idx], box, pp=pp or None)
    finally:
        doc.close()
    expected = (data.get("text") or "").strip()
    matched = bool(expected) and _validate_contains(expected, text)
    return jsonify({"ok": True, "text": text or "", "confidence": conf,
                    "matched": matched})


@main_bp.route("/operations/registration-updater/cr/<int:upload_id>/ocr-boxes",
               methods=["POST"])
@login_required
def cr_ocr_boxes(upload_id: int):
    """Auto-detect value-region boxes for the first page so the OCR-settings
    popup can seed its movable/resizable rectangles."""
    _require_module(REGISTRATION_UPDATER_KEY)
    from app.utils import registration_ocr as ocr
    up = db.session.get(CRBookUpload, upload_id) or abort(404)
    data = request.get_json(silent=True) or {}
    pp = ocr.clean_pp(data.get("pp"))
    doc, total = ocr.open_document(up.stored_path)
    try:
        idx = max(1, min(total, int(data.get("page") or 1))) - 1
        boxes = ocr.detect_cr_field_boxes(doc[idx], pp=pp)
    finally:
        doc.close()
    return jsonify({"ok": True, "boxes": boxes})


def _tpl_json(t: "OcrTemplate") -> dict:
    """Serialize a template (with parsed pp/rects) for the popup."""
    from app.utils import registration_ocr as ocr
    try:
        pp = ocr.clean_pp(json.loads(t.ocr_pp)) if t.ocr_pp else {}
    except (ValueError, TypeError):
        pp = {}
    try:
        rects = (ocr.clean_rects(json.loads(t.ocr_rects), t.kind or "cr")
                 if t.ocr_rects else {})
    except (ValueError, TypeError):
        rects = {}
    try:
        anchors = (ocr.clean_anchors(json.loads(t.ocr_anchors))
                   if getattr(t, "ocr_anchors", None) else [])
    except (ValueError, TypeError):
        anchors = []
    try:
        vbox = json.loads(t.validate_box) if getattr(t, "validate_box", None) else None
        if not (isinstance(vbox, (list, tuple)) and len(vbox) == 4):
            vbox = None
    except (ValueError, TypeError):
        vbox = None
    return {"id": t.id, "name": t.name, "kind": t.kind or "cr",
            "pp": pp, "rects": rects,
            "fields": ocr.template_field_defs(t),
            "anchors": [a["box"] for a in anchors],
            "validate_enabled": bool(getattr(t, "validate_enabled", False)),
            "validate_box": [float(n) for n in vbox] if vbox else None,
            "validate_text": getattr(t, "validate_text", None) or "",
            "fail_template_id": getattr(t, "fail_template_id", None)}


def _clean_validation(data: dict, self_id=None):
    """Parse the Template-picker validation fields from a request payload into
    ``(enabled, box|None, text|None, fail_template_id|None)``.  The box is a
    validated normalized ``[x0,y0,x1,y1]``; a fail template equal to the
    template itself is dropped to avoid a trivial self-cycle."""
    enabled = bool(data.get("validate_enabled"))
    box = data.get("validate_box")
    cbox = None
    if isinstance(box, (list, tuple)) and len(box) == 4:
        try:
            xs = [max(0.0, min(1.0, float(n))) for n in box]
            if xs[2] > xs[0] and xs[3] > xs[1]:
                cbox = [round(xs[0], 4), round(xs[1], 4),
                        round(xs[2], 4), round(xs[3], 4)]
        except (TypeError, ValueError):
            cbox = None
    text = (data.get("validate_text") or "").strip() or None
    fid = data.get("fail_template_id")
    try:
        fid = int(fid) if fid not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        fid = None
    if self_id is not None and fid == self_id:
        fid = None
    return enabled, cbox, text, fid


def _apply_validation(tpl, data) -> None:
    """Write the parsed Template-picker validation fields onto ``tpl``."""
    enabled, box, text, fid = _clean_validation(data, self_id=tpl.id)
    tpl.validate_enabled = enabled
    tpl.validate_box = json.dumps(box) if box else None
    tpl.validate_text = text
    tpl.fail_template_id = fid


def _resolve_effective_template(kind, doc, base_tpl):
    """Walk the Template-picker validation/fallback chain and return the template
    to extract with.  For each template that has validation enabled, apply its
    image settings and OCR its validate box on the first page; the template is
    chosen when the read text contains its validate text.  Templates without
    validation enabled (or misconfigured) are used as-is.  Raises ValueError
    ("No matched template found.") when validation is enabled and the chain is
    exhausted without a match."""
    from app.utils import registration_ocr as ocr
    page = doc[0]
    # Receipt boxes are normalized to a single deposit slip (not the whole roll),
    # so read the validate box on the first detected slip's frame.  Segmentation
    # is template-independent, so compute the slip region once.
    region = ocr.first_slip_region(page) if kind == "rc" else None

    def _read(box, pp):
        if kind == "rc":
            txt, _ = ocr.read_slip_rect(page, region, box, pp=pp or None)
            return txt
        txt, _ = ocr.read_rect(page, box, pp=pp or None)
        return txt

    t = base_tpl
    seen: set[int] = set()
    while t is not None and t.id not in seen:
        seen.add(t.id)
        if not getattr(t, "validate_enabled", False):
            return t
        box = None
        if t.validate_box:
            try:
                b = json.loads(t.validate_box)
                if isinstance(b, (list, tuple)) and len(b) == 4:
                    box = [float(n) for n in b]
            except (ValueError, TypeError):
                box = None
        expected = (t.validate_text or "").strip()
        if not box or not expected:
            return t  # validation enabled but unconfigured -> use as-is
        if kind == "rc" and region is None:
            return t  # no slip detected to validate against -> use as-is
        try:
            pp = ocr.clean_pp(json.loads(t.ocr_pp)) if t.ocr_pp else None
        except (ValueError, TypeError):
            pp = None
        text = _read(box, pp)
        if _validate_contains(expected, text):
            return t
        nxt_id = getattr(t, "fail_template_id", None)
        t = db.session.get(OcrTemplate, nxt_id) if nxt_id else None
    raise ValueError("No Match Template")


def _rc_chain_head(base_tpl):
    """Return the head of ``base_tpl``'s validation/fallback chain by following
    predecessors (receipt templates whose ``fail_template_id`` points at the
    current template) back to the root, so every slip starts from the FIRST
    template in the chain regardless of which template the upload was assigned.
    Cycle-safe."""
    head = base_tpl
    seen: set[int] = {head.id}
    while True:
        pred = (OcrTemplate.query
                .filter_by(kind="rc", fail_template_id=head.id)
                .first())
        if pred is None or pred.id in seen:
            break
        head = pred
        seen.add(head.id)
    return head


def _make_rc_slip_resolver(base_tpl, report=None):
    """Return a per-slip resolver for the RC Template picker.  For each slip the
    callback restarts from the FIRST template in the chain (the head, found by
    walking the assigned template's predecessors) and walks its validation/
    fallback chain: it applies a template's image settings and OCRs its validate
    box on that slip, and the first template whose read contains its validate
    text supplies the slip's ``(pp, rects, anchors)``.  Templates without
    validation are used as-is.  Raises ValueError("No Match Template") when a
    slip matches nothing in the chain.  ``report`` (if given) is called with a
    short status string as each template is validated so the progress bar can
    show which template matched."""
    from app.utils import registration_ocr as ocr
    base_tpl = _rc_chain_head(base_tpl)

    def _pp(t):
        try:
            return ocr.clean_pp(json.loads(t.ocr_pp)) if t.ocr_pp else None
        except (ValueError, TypeError):
            return None

    def _rects(t):
        if not t.ocr_rects:
            return None
        try:
            return ocr.clean_rects(json.loads(t.ocr_rects), "rc") or None
        except (ValueError, TypeError):
            return None

    def _anchors(t):
        if not getattr(t, "ocr_anchors", None):
            return None
        try:
            return ocr.clean_anchors(json.loads(t.ocr_anchors)) or None
        except (ValueError, TypeError):
            return None

    def _box(t):
        if not t.validate_box:
            return None
        try:
            b = json.loads(t.validate_box)
            return ([float(n) for n in b]
                    if isinstance(b, (list, tuple)) and len(b) == 4 else None)
        except (ValueError, TypeError):
            return None

    def _slip(idx, total):
        return f"slip {idx} of {total}" if idx and total else "slip"

    def resolve(page, region, idx=0, total=0):
        t = base_tpl
        seen: set[int] = set()
        while t is not None and t.id not in seen:
            seen.add(t.id)
            box = _box(t)
            expected = (t.validate_text or "").strip()
            if (not getattr(t, "validate_enabled", False)
                    or not box or not expected):
                if report:
                    report(f"Using {t.name} \u2014 {_slip(idx, total)}\u2026")
                return _pp(t), _rects(t), _anchors(t)
            if report:
                report(f"Validating {t.name} \u2014 {_slip(idx, total)}\u2026")
            text, _ = ocr.read_slip_rect(page, region, box, pp=_pp(t) or None)
            if _validate_contains(expected, text):
                if report:
                    report(f"Validated {t.name} \u2014 {_slip(idx, total)}")
                return _pp(t), _rects(t), _anchors(t)
            nxt = getattr(t, "fail_template_id", None)
            t = db.session.get(OcrTemplate, nxt) if nxt else None
        raise ValueError("No Match Template")

    return resolve


def _rc_anchor_patches(up, ref_slip, anchor_boxes) -> list[dict]:
    """Crop image patches for the given normalized anchor boxes from a receipt
    upload's reference slip, returning [{box, patch(b64)}] for storage/matching.
    Used by receipt Test OCR (live) and the settings save."""
    from app.utils import registration_ocr as ocr
    boxes = []
    for b in (anchor_boxes or []):
        try:
            boxes.append([float(v) for v in b][:4])
        except (TypeError, ValueError):
            continue
    if not boxes:
        return []
    slips = _rc_slip_index(up)
    idx = int(ref_slip or 1) - 1
    if not (0 <= idx < len(slips)):
        return []
    s = slips[idx]
    doc, _ = ocr.open_document(up.stored_path)
    try:
        page = doc[s["page"]]
        region = ocr._slip_region_pts(page, s["y0"], s["y1"], s["zoom"])
        return ocr.build_anchor_patches(page, region, boxes)
    finally:
        doc.close()


def _cr_anchor_patches(up, ref_page, anchor_boxes) -> list[dict]:
    """Crop image patches for the given normalized anchor boxes from a CR
    upload's reference page (full-page frame), returning [{box, patch(b64)}].
    Used by CR Test OCR (live) and the settings save."""
    from app.utils import registration_ocr as ocr
    boxes = []
    for b in (anchor_boxes or []):
        try:
            boxes.append([float(v) for v in b][:4])
        except (TypeError, ValueError):
            continue
    if not boxes:
        return []
    doc, total = ocr.open_document(up.stored_path)
    try:
        idx = max(1, min(total, int(ref_page or 1))) - 1
        page = doc[idx]
        region = (0.0, 0.0, page.rect.width, page.rect.height)
        return ocr.build_anchor_patches(page, region, boxes)
    finally:
        doc.close()


@main_bp.route("/operations/registration-updater/cr/<int:upload_id>/ocr-init")
@login_required
def cr_ocr_init(upload_id: int):
    """Popup bootstrap: page count, the upload's current template, and all
    templates (with their settings/boxes) for the dropdown."""
    _require_module(REGISTRATION_UPDATER_KEY)
    from app.utils import registration_ocr as ocr
    up = db.session.get(CRBookUpload, upload_id) or abort(404)
    doc, total = ocr.open_document(up.stored_path)
    doc.close()
    templates = [_tpl_json(t) for t in OcrTemplate.query
                 .filter_by(kind="cr")
                 .order_by(OcrTemplate.updated_at.desc()).all()]
    return jsonify({"ok": True, "pages": total,
                    "template_id": up.template_id, "templates": templates,
                    "lists": _lists_json(), "field_catalog": _field_catalog("cr")})


@main_bp.route("/operations/registration-updater/ocr-templates",
               methods=["GET", "POST"])
@login_required
def cr_ocr_templates():
    """List OCR templates (GET) or create a new one from the current settings
    and value boxes (POST {name, pp, rects})."""
    _require_module(REGISTRATION_UPDATER_KEY)
    from app.utils import registration_ocr as ocr
    if request.method == "GET":
        kind = "rc" if request.args.get("kind") == "rc" else "cr"
        templates = [_tpl_json(t) for t in OcrTemplate.query
                     .filter_by(kind=kind)
                     .order_by(OcrTemplate.updated_at.desc()).all()]
        return jsonify({"ok": True, "templates": templates})
    data = request.get_json(silent=True) or {}
    kind = "rc" if data.get("kind") == "rc" else "cr"
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Template name is required."}), 400
    if OcrTemplate.query.filter_by(name=name).first():
        return jsonify({"ok": False,
                        "error": "A template with that name already exists."}), 400
    pp = ocr.clean_pp(data.get("pp"))
    rects = ocr.clean_rects(data.get("rects"), kind)
    v_enabled, v_box, v_text, v_fail = _clean_validation(data)
    t = OcrTemplate(name=name, kind=kind, created_by=current_user.id,
                    ocr_pp=json.dumps(pp) if pp else None,
                    ocr_rects=json.dumps(rects) if rects else None,
                    validate_enabled=v_enabled,
                    validate_box=json.dumps(v_box) if v_box else None,
                    validate_text=v_text, fail_template_id=v_fail)
    db.session.add(t)
    db.session.commit()
    _persist_fields(t, data, kind)
    db.session.commit()
    record_audit("ocr_template_create", f"id={t.id} name={name!r}")
    return jsonify({"ok": True, "template": _tpl_json(t)})


@main_bp.route("/operations/registration-updater/ocr-templates/<int:tid>",
               methods=["POST"])
@login_required
def cr_ocr_template_update(tid: int):
    """Save the current settings/boxes into an existing template (POST
    {pp, rects, name?})."""
    _require_module(REGISTRATION_UPDATER_KEY)
    from app.utils import registration_ocr as ocr
    t = db.session.get(OcrTemplate, tid) or abort(404)
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if name and name != t.name:
        if OcrTemplate.query.filter_by(name=name).first():
            return jsonify({"ok": False,
                            "error": "A template with that name already exists."}), 400
        t.name = name
    pp = ocr.clean_pp(data.get("pp"))
    rects = ocr.clean_rects(data.get("rects"), t.kind or "cr")
    t.ocr_pp = json.dumps(pp) if pp else None
    t.ocr_rects = json.dumps(rects) if rects else None
    _apply_validation(t, data)
    _persist_fields(t, data, t.kind or "cr")
    db.session.commit()
    record_audit("ocr_template_update",
                 f"id={t.id} pp={sorted(pp)} rects={sorted(rects)}")
    return jsonify({"ok": True, "template": _tpl_json(t)})


@main_bp.route("/operations/registration-updater/ocr-lists",
               methods=["GET", "POST"])
@login_required
def ocr_lists():
    """List global lookup lists (GET) or upsert one by name (POST {name,
    items}).  ``items`` is stored verbatim (newline-separated entries)."""
    _require_module(REGISTRATION_UPDATER_KEY)
    if request.method == "GET":
        return jsonify({"ok": True, "lists": _lists_json()})
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()[:120]
    if not name:
        return jsonify({"ok": False, "error": "List name is required."}), 400
    items = data.get("items")
    if isinstance(items, list):
        items = "\n".join(str(x) for x in items)
    items = (items or "").strip() or None
    lst = OcrList.query.filter_by(name=name).first()
    if lst is None:
        lst = OcrList(name=name, items=items,
                      created_by=getattr(current_user, "id", None))
        db.session.add(lst)
    else:
        lst.items = items
    db.session.commit()
    record_audit("ocr_list_save", f"id={lst.id} name={name!r}")
    return jsonify({"ok": True, "lists": _lists_json()})


@main_bp.route("/operations/registration-updater/ocr-lists/<int:lid>/delete",
               methods=["POST"])
@login_required
def ocr_list_delete(lid: int):
    """Delete a global lookup list."""
    _require_module(REGISTRATION_UPDATER_KEY)
    lst = db.session.get(OcrList, lid) or abort(404)
    name = lst.name
    db.session.delete(lst)
    db.session.commit()
    record_audit("ocr_list_delete", f"id={lid} name={name!r}")
    return jsonify({"ok": True, "lists": _lists_json()})


@main_bp.route("/operations/registration-updater/ocr-fields", methods=["POST"])
@login_required
def ocr_fields():
    """Add a custom field to a kind's records table (POST {kind, label}): create
    a real NVARCHAR column shared by all templates of that kind.  Returns the
    refreshed field catalog and the new field's key."""
    _require_module(REGISTRATION_UPDATER_KEY)
    data = request.get_json(silent=True) or {}
    kind = data.get("kind")
    if kind not in _RECORD_TABLE:
        return jsonify({"ok": False, "error": "Unknown document kind."}), 400
    label = (data.get("label") or "").strip()[:120]
    if not label:
        return jsonify({"ok": False, "error": "Field label is required."}), 400
    base = _slug_field(label)
    key, i = base, 2
    canon = _canonical_keys(kind)
    while key in canon or _column_exists(_RECORD_TABLE[kind], key):
        key = f"{base}_{i}"[:63]
        i += 1
    if not ensure_record_column(kind, key, label):
        return jsonify({"ok": False, "error": "Could not create field."}), 400
    record_audit("ocr_field_add", f"kind={kind} key={key!r} label={label!r}")
    return jsonify({"ok": True, "key": key, "catalog": _field_catalog(kind)})


@main_bp.route("/operations/registration-updater/ocr-fields/<key>/delete",
               methods=["POST"])
@login_required
def ocr_field_delete(key: str):
    """Delete a custom field (POST {kind}): drop its column from the kind's
    records table, unregister it, and strip it from every template's fields.
    Built-in (canonical) fields cannot be deleted."""
    _require_module(REGISTRATION_UPDATER_KEY)
    data = request.get_json(silent=True) or {}
    kind = data.get("kind")
    skey = _safe_field_key(key)
    if kind not in _RECORD_TABLE or not skey:
        return jsonify({"ok": False, "error": "Invalid request."}), 400
    if skey in _canonical_keys(kind):
        return jsonify({"ok": False,
                        "error": "Built-in fields cannot be deleted."}), 400
    table = _RECORD_TABLE[kind]
    if _column_exists(table, skey):
        db.session.execute(sa_text(
            f"ALTER TABLE [{table}] DROP COLUMN [{skey}]"))
    OcrCustomField.query.filter_by(kind=kind, key=skey).delete()
    for t in OcrTemplate.query.filter_by(kind=kind).all():
        if not t.fields:
            continue
        try:
            defs = json.loads(t.fields)
        except (ValueError, TypeError):
            continue
        kept = [d for d in defs if d.get("key") != skey]
        if len(kept) != len(defs):
            t.fields = json.dumps(kept) if kept else None
    db.session.commit()
    record_audit("ocr_field_delete", f"kind={kind} key={skey!r}")
    return jsonify({"ok": True, "catalog": _field_catalog(kind)})


@main_bp.route("/operations/registration-updater/cr/<int:upload_id>/ocr-settings",
               methods=["POST"])
@login_required
def cr_ocr_settings(upload_id: int):
    """Save the current image settings + value boxes into the selected OCR
    template, assign that template to this upload, and (if requested) kick a
    re-process so it takes effect immediately."""
    _require_module(REGISTRATION_UPDATER_KEY)
    from app.utils import registration_ocr as ocr
    up = db.session.get(CRBookUpload, upload_id) or abort(404)
    data = request.get_json(silent=True) or {}
    tid = data.get("template_id")
    if tid in (None, "", 0, "0"):
        return jsonify({"ok": False,
                        "error": "Select or create a template first."}), 400
    tpl = db.session.get(OcrTemplate, int(tid))
    if tpl is None:
        return jsonify({"ok": False, "error": "Template not found."}), 400
    pp = ocr.clean_pp(data.get("pp"))
    rects = ocr.clean_rects(data.get("rects"))
    anchors = ocr.clean_anchors(_cr_anchor_patches(
        up, data.get("ref_slip") or 1, data.get("anchors")))
    tpl.ocr_pp = json.dumps(pp) if pp else None
    tpl.ocr_rects = json.dumps(rects) if rects else None
    tpl.ocr_anchors = json.dumps(anchors) if anchors else None
    _apply_validation(tpl, data)
    _persist_fields(tpl, data, "cr")
    up.template_id = tpl.id
    db.session.commit()
    record_audit("cr_ocr_settings",
                 f"upload_id={upload_id} template_id={tpl.id} "
                 f"pp={sorted(pp)} rects={sorted(rects)} anchors={len(anchors)}")
    started = False
    if bool(data.get("reprocess")) and up.status != STATUS_PROCESSING \
            and up.review_status == REVIEW_READY:
        up.status = STATUS_PROCESSING
        up.error = None
        db.session.commit()
        _start_processing("cr", upload_id)
        started = True
    return jsonify({"ok": True, "reprocessing": started,
                    "template": _tpl_json(tpl)})


# Per-upload receipt slip index (the "separated document"), cached so the popup's
# slip navigation doesn't re-run segmentation on every step.  Keyed by upload
# id; invalidated when the source file's mtime changes.
_RC_SLIP_CACHE: dict[int, dict] = {}
_RC_SLIP_LOCKS: dict[int, threading.Lock] = {}
_RC_SLIP_LOCKS_GUARD = threading.Lock()


def _rc_slip_lock(upload_id: int) -> threading.Lock:
    """Per-upload lock so a background warm-up and an early popup open never run
    the (expensive) full-document segmentation twice."""
    with _RC_SLIP_LOCKS_GUARD:
        lk = _RC_SLIP_LOCKS.get(upload_id)
        if lk is None:
            lk = _RC_SLIP_LOCKS[upload_id] = threading.Lock()
        return lk


def _rc_slip_cache_file(up) -> str:
    """Slip-index cache path, co-located with the uploaded file so it survives
    re-processing (which clears the crop dir)."""
    return f"{up.stored_path}.slips.json"


def _rc_slip_index(up) -> list[dict]:
    """Return the slip index for a receipt upload (page + full-page bounds per
    slip).  Separating the receipt roll requires OCR'ing the whole document,
    which is slow, so the result is cached in memory and on disk (keyed by the
    source file's mtime).  The OCR-settings popup therefore pays this cost at
    most once per file version, even across service restarts."""
    from app.utils import registration_ocr as ocr
    try:
        mtime = os.path.getmtime(up.stored_path)
    except OSError:
        mtime = None
    cached = _RC_SLIP_CACHE.get(up.id)
    if cached and cached.get("mtime") == mtime:
        return cached["slips"]
    cache_file = _rc_slip_cache_file(up)
    with _rc_slip_lock(up.id):
        cached = _RC_SLIP_CACHE.get(up.id)  # re-check after acquiring the lock
        if cached and cached.get("mtime") == mtime:
            return cached["slips"]
        try:
            with open(cache_file, encoding="utf-8") as fh:
                disk = json.load(fh)
            if disk.get("mtime") == mtime:
                _RC_SLIP_CACHE[up.id] = disk
                return disk["slips"]
        except (OSError, ValueError):
            pass
        slips = ocr.segment_document(up.stored_path)
        entry = {"mtime": mtime, "slips": slips}
        _RC_SLIP_CACHE[up.id] = entry
        try:
            with open(cache_file, "w", encoding="utf-8") as fh:
                json.dump(entry, fh)
        except OSError:
            pass
        return slips


def _warm_rc_slips(app, upload_id: int) -> None:
    """Pre-compute the slip index in the background right after upload so the
    OCR-settings popup is ready by the time the operator opens it."""
    with app.app_context():
        up = db.session.get(ReceiptUpload, upload_id)
        if up is None:
            return
        try:
            _rc_slip_index(up)
        except Exception:  # pragma: no cover - warm-up is best-effort
            current_app.logger.exception(
                "Receipt slip pre-segmentation failed (upload %s)", upload_id)


def _start_rc_warm(upload_id: int) -> None:
    app = current_app._get_current_object()
    threading.Thread(
        target=_warm_rc_slips, args=(app, upload_id), daemon=True).start()


@main_bp.route("/operations/registration-updater/rc/<int:upload_id>/ocr-init")
@login_required
def rc_ocr_init(upload_id: int):
    """Receipts OCR-settings popup bootstrap: separate the document into slips,
    then return the slip index (page per slip), the upload's current template,
    and all receipt ('rc') templates for the dropdown."""
    _require_module(REGISTRATION_UPDATER_KEY)
    from app.utils import registration_ocr as ocr
    up = db.session.get(ReceiptUpload, upload_id) or abort(404)
    doc, total = ocr.open_document(up.stored_path)
    doc.close()
    slips = _rc_slip_index(up)
    templates = [_tpl_json(t) for t in OcrTemplate.query
                 .filter_by(kind="rc")
                 .order_by(OcrTemplate.updated_at.desc()).all()]
    return jsonify({"ok": True, "pages": total,
                    "slip_count": len(slips),
                    "slips": [{"page": s["page"] + 1} for s in slips],
                    "template_id": up.template_id, "templates": templates,
                    "lists": _lists_json(), "field_catalog": _field_catalog("rc")})


@main_bp.route("/operations/registration-updater/rc/<int:upload_id>/ocr-preview",
               methods=["POST"])
@login_required
def rc_ocr_preview(upload_id: int):
    """Render one separated slip with the given image adjustments for the
    popup's live preview (slip nav supported)."""
    _require_module(REGISTRATION_UPDATER_KEY)
    from app.utils import registration_ocr as ocr
    up = db.session.get(ReceiptUpload, upload_id) or abort(404)
    data = request.get_json(silent=True) or {}
    pp = ocr.clean_pp(data.get("pp"))
    slips = _rc_slip_index(up)
    idx = int(data.get("slip") or 1) - 1
    doc, total = ocr.open_document(up.stored_path)
    try:
        if 0 <= idx < len(slips):
            s = slips[idx]
            png = ocr.render_slip_png(doc[s["page"]], s["y0"], s["y1"],
                                      s["zoom"], pp=pp)
            if png is None:
                png = ocr.render_page_png(doc[s["page"]], pp=pp, zoom=1.6)
        else:
            png = ocr.render_page_png(doc[0], pp=pp, zoom=1.6)
    finally:
        doc.close()
    resp = Response(png, mimetype="image/png")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@main_bp.route("/operations/registration-updater/rc/<int:upload_id>/ocr-test",
               methods=["POST"])
@login_required
def rc_ocr_test(upload_id: int):
    """Run receipt OCR on one separated slip with the given image adjustments
    and return that slip's fields (plus the page's slip count) for "Test OCR"."""
    _require_module(REGISTRATION_UPDATER_KEY)
    from app.utils import registration_ocr as ocr
    up = db.session.get(ReceiptUpload, upload_id) or abort(404)
    data = request.get_json(silent=True) or {}
    pp = ocr.clean_pp(data.get("pp"))
    rects = ocr.clean_rects(data.get("rects"), "rc")
    slips = _rc_slip_index(up)
    idx = int(data.get("slip") or 1) - 1
    # Live calibration: crop anchor patches from the popup's reference slip so
    # Test OCR on any slip reflects the same per-slip registration extraction
    # will use.
    anchors = _rc_anchor_patches(
        up, data.get("ref_slip") or 1, data.get("anchors")) or None
    doc, total = ocr.open_document(up.stored_path)
    try:
        if 0 <= idx < len(slips):
            s = slips[idx]
            page = doc[s["page"]]
            fields = ocr.test_receipt_slip(page, s["k"],
                                           pp=pp, rects=rects, anchors=anchors)
            region = ocr._slip_region_pts(page, s["y0"], s["y1"], s["zoom"])
        else:
            page = doc[0]
            fields = ocr.test_receipt_page(page, pp=pp)
            region = ocr.first_slip_region(page)
        _apply_test_masks("rc", ocr.clean_fields(data.get("fields"), "rc"),
                          fields, page, region, pp)
    finally:
        doc.close()
    return jsonify({"ok": True, "fields": fields})


@main_bp.route("/operations/registration-updater/rc/<int:upload_id>/validate-read",
               methods=["POST"])
@login_required
def rc_validate_read(upload_id: int):
    """OCR the validate box on the selected slip's frame and return the captured
    text (and whether it satisfies the validate pattern) for the Template
    picker.  Receipt boxes are slip-normalized, so the box is read within the
    chosen slip's region -- exactly as per-slip resolution does at processing."""
    _require_module(REGISTRATION_UPDATER_KEY)
    from app.utils import registration_ocr as ocr
    up = db.session.get(ReceiptUpload, upload_id) or abort(404)
    data = request.get_json(silent=True) or {}
    box = _parse_box(data.get("box"))
    if not box:
        return jsonify({"ok": False, "error": "Draw the validate box first."}), 400
    pp = ocr.clean_pp(data.get("pp"))
    slips = _rc_slip_index(up)
    idx = int(data.get("slip") or 1) - 1
    doc, total = ocr.open_document(up.stored_path)
    try:
        if 0 <= idx < len(slips):
            s = slips[idx]
            region = ocr._slip_region_pts(
                doc[s["page"]], s["y0"], s["y1"], s["zoom"])
            text, conf = ocr.read_slip_rect(
                doc[s["page"]], region, box, pp=pp or None)
        else:
            text, conf = None, 0.0
    finally:
        doc.close()
    expected = (data.get("text") or "").strip()
    matched = bool(expected) and _validate_contains(expected, text)
    return jsonify({"ok": True, "text": text or "", "confidence": conf,
                    "matched": matched})


@main_bp.route("/operations/registration-updater/rc/<int:upload_id>/ocr-settings",
               methods=["POST"])
@login_required
def rc_ocr_settings(upload_id: int):
    """Save the current image settings into the selected receipt OCR template,
    assign that template to this upload, and (if requested) kick a re-process."""
    _require_module(REGISTRATION_UPDATER_KEY)
    from app.utils import registration_ocr as ocr
    up = db.session.get(ReceiptUpload, upload_id) or abort(404)
    data = request.get_json(silent=True) or {}
    tid = data.get("template_id")
    if tid in (None, "", 0, "0"):
        return jsonify({"ok": False,
                        "error": "Select or create a template first."}), 400
    tpl = db.session.get(OcrTemplate, int(tid))
    if tpl is None or tpl.kind != "rc":
        return jsonify({"ok": False, "error": "Template not found."}), 400
    pp = ocr.clean_pp(data.get("pp"))
    rects = ocr.clean_rects(data.get("rects"), "rc")
    anchors = ocr.clean_anchors(_rc_anchor_patches(
        up, data.get("ref_slip") or 1, data.get("anchors")))
    tpl.ocr_pp = json.dumps(pp) if pp else None
    tpl.ocr_rects = json.dumps(rects) if rects else None
    tpl.ocr_anchors = json.dumps(anchors) if anchors else None
    _apply_validation(tpl, data)
    _persist_fields(tpl, data, "rc")
    up.template_id = tpl.id
    db.session.commit()
    record_audit("rc_ocr_settings",
                 f"upload_id={upload_id} template_id={tpl.id} "
                 f"pp={sorted(pp)} anchors={len(anchors)}")
    started = False
    if bool(data.get("reprocess")) and up.status != STATUS_PROCESSING:
        up.status = STATUS_PROCESSING
        up.error = None
        db.session.commit()
        _start_processing("rc", upload_id)
        started = True
    return jsonify({"ok": True, "reprocessing": started,
                    "template": _tpl_json(tpl)})


def _norm_match(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _lev(a: str, b: str) -> int:
    """Levenshtein edit distance between two short strings."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _fuzzy_contains(expected: str | None, text: str | None) -> bool:
    """True when ``text`` contains ``expected`` after normalization, tolerating
    minor OCR noise.  Validate labels are short, stable words (e.g. "Cash"), and
    a single-character misread ('Cash' -> 'Cast') must not fail the batch, so an
    exact substring test is backed by a small edit-distance search over same-
    length (and +/-1) windows of the read text."""
    e = _norm_match(expected)
    t = _norm_match(text)
    if not e or not t:
        return False
    if e in t:
        return True
    tol = max(1, len(e) // 4)
    for le in (len(e), len(e) - 1, len(e) + 1):
        if le <= 0:
            continue
        for i in range(0, len(t) - le + 1):
            if _lev(t[i:i + le], e) <= tol:
                return True
    return False


_LOGIC_OPS = {"and", "or", "not"}


def _is_wild(pattern: str) -> bool:
    """True when a validate pattern uses Access-style wildcards (``* ? # [ ]``)
    or logical operators (and/or/not), so it must be matched as a pattern rather
    than as a plain, OCR-noise-tolerant label."""
    if re.search(r"[*?#\[\]]", pattern):
        return True
    return any(tok.lower() in _LOGIC_OPS for tok in pattern.split())


def _like_to_regex(pattern: str) -> str:
    """Translate one Access-style ``Like`` pattern into an anchored regex:
    ``*``=any run, ``?``=one letter, ``#``=one digit, ``[abc]``/``[a-c]``=class,
    ``[!abc]``=negated class.  Other characters are matched literally."""
    out = ["^"]
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            out.append(".*")
        elif c == "?":
            out.append("[A-Za-z]")
        elif c == "#":
            out.append("[0-9]")
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                inner = pattern[i + 1:j]
                neg = inner.startswith("!")
                if neg:
                    inner = inner[1:]
                inner = inner.replace("\\", "\\\\").replace("^", "\\^")
                out.append("[" + ("^" if neg else "") + inner + "]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    out.append("$")
    return "".join(out)


def _wild_match_one(pattern: str, text: str) -> bool:
    try:
        return re.match(_like_to_regex(pattern), text, re.IGNORECASE) is not None
    except re.error:
        return False


def _wild_eval(tokens: list[str], text: str) -> bool:
    """Evaluate a boolean expression of Like-patterns joined by and/or/not
    (precedence not > and > or).  Each operand is a single whitespace-free
    pattern token, e.g. ``*c* or *b*`` / ``not *a*``."""
    pos = 0

    def parse_or():
        nonlocal pos
        val = parse_and()
        while pos < len(tokens) and tokens[pos].lower() == "or":
            pos += 1
            rhs = parse_and()
            val = val or rhs
        return val

    def parse_and():
        nonlocal pos
        val = parse_not()
        while pos < len(tokens) and tokens[pos].lower() == "and":
            pos += 1
            rhs = parse_not()
            val = val and rhs
        return val

    def parse_not():
        nonlocal pos
        if pos < len(tokens) and tokens[pos].lower() == "not":
            pos += 1
            return not parse_not()
        return parse_operand()

    def parse_operand():
        nonlocal pos
        if pos >= len(tokens):
            return False
        tok = tokens[pos]
        pos += 1
        return _wild_match_one(tok, text)

    return parse_or()


def _validate_contains(expected: str | None, text: str | None) -> bool:
    """True when the OCR ``text`` satisfies the ``expected`` validate pattern.
    Plain labels use an OCR-noise-tolerant fuzzy contains; patterns using
    Access-style wildcards (``* ? # [ ]``) or logical operators (and/or/not) are
    matched exactly per those rules (case-insensitive)."""
    exp = (expected or "").strip()
    if not exp:
        return False
    if _is_wild(exp):
        txt = re.sub(r"\s+", " ", (text or "")).strip()
        toks = exp.split()
        if any(tok.lower() in _LOGIC_OPS for tok in toks):
            return _wild_eval(toks, txt)
        return _wild_match_one(exp, txt)
    return _fuzzy_contains(exp, text)


def _parse_box(raw):
    """Parse/clamp a normalized ``[x0,y0,x1,y1]`` box from request JSON; return
    None when malformed or degenerate."""
    try:
        b = [float(n) for n in (raw or [])]
    except (TypeError, ValueError):
        return None
    if len(b) != 4 or not (b[2] > b[0] and b[3] > b[1]):
        return None
    return [max(0.0, min(1.0, n)) for n in b]


@main_bp.route("/operations/registration-updater/training/template/apply",
               methods=["POST"])
@login_required
def tr_template_apply():
    """Crop the given rectangles on one page and OCR them, returning the read
    value/confidence per field plus the validation-box check.  Used by the
    editor's 'Apply / Check' action to verify a template against a page."""
    _require_trainer()
    data = request.get_json(silent=True) or {}
    from app.utils import registration_ocr as ocr
    up = db.session.get(CRBookUpload, int(data.get("upload_id") or 0)) or abort(404)
    page_no = int(data.get("page_no") or 1)
    rects = {k: v for k, v in (data.get("rects") or {}).items()
             if isinstance(v, (list, tuple)) and len(v) == 4}
    field_pp = {k: ocr.clean_pp(v)
                for k, v in (data.get("field_pp") or {}).items()}
    validation = data.get("validation") or None
    doc, total = ocr.open_document(up.stored_path)
    try:
        if page_no < 1 or page_no > total:
            abort(404)
        page = doc[page_no - 1]
        results = ocr.extract_by_rects(page, rects, pp_map=field_pp)
        vout = None
        if validation and isinstance(validation.get("rect"), (list, tuple)) \
                and len(validation["rect"]) == 4:
            text, conf = ocr.read_rect(page, validation["rect"],
                                       pp=ocr.clean_pp(validation.get("pp")))
            expected = validation.get("value") or ""
            ok = bool(expected) and _norm_match(expected) in _norm_match(text) \
                if text else False
            vout = {"value": text, "confidence": conf,
                    "expected": expected, "ok": ok}
    finally:
        doc.close()
    return jsonify({"results": results, "validation": vout})


@main_bp.route("/operations/registration-updater/training/field/preview",
               methods=["POST"])
@login_required
def tr_field_preview():
    """Render one box's crop with the given adjustments (and, if requested, OCR
    it) so the settings popup can show the processed image + confidence."""
    _require_trainer()
    from app.utils import registration_ocr as ocr
    data = request.get_json(silent=True) or {}
    up = db.session.get(CRBookUpload, int(data.get("upload_id") or 0)) or abort(404)
    page_no = int(data.get("page_no") or 1)
    name = (data.get("name") or "field").strip() or "field"
    rect = data.get("rect")
    if not (isinstance(rect, (list, tuple)) and len(rect) == 4):
        return jsonify({"error": "No box."}), 400
    pp = ocr.clean_pp(data.get("pp"))
    run_ocr = bool(data.get("ocr"))
    doc, total = ocr.open_document(up.stored_path)
    try:
        if page_no < 1 or page_no > total:
            abort(404)
        page = doc[page_no - 1]
        png = ocr.render_crop_png(page, rect, pp=pp)
        result = None
        if run_ocr:
            result = ocr.extract_by_rects(
                page, {name: rect}, pp_map={name: pp}).get(name)
    finally:
        doc.close()
    return jsonify({
        "crop": "data:image/png;base64," + base64.b64encode(png).decode(),
        "ocr": result})


def _apply_template_payload(sig: dict, data: dict) -> dict:
    """Populate a template signature's rects / field_pp / validation (incl. its
    per-box adjustments) from an editor payload.  Shared by save + update."""
    from app.utils import registration_ocr as ocr
    rects = {k: [float(n) for n in v]
             for k, v in (data.get("rects") or {}).items()
             if isinstance(v, (list, tuple)) and len(v) == 4}
    sig["rects"] = rects
    field_pp = {}
    for k, v in (data.get("field_pp") or {}).items():
        cleaned = ocr.clean_pp(v)
        if k in rects and cleaned:
            field_pp[k] = cleaned
    if field_pp:
        sig["field_pp"] = field_pp
    else:
        sig.pop("field_pp", None)
    validation = data.get("validation") or None
    if validation and isinstance(validation.get("rect"), (list, tuple)) \
            and len(validation["rect"]) == 4 and (validation.get("value") or "").strip():
        v = {"name": (validation.get("name") or "").strip(),
             "value": validation["value"].strip(),
             "rect": [float(n) for n in validation["rect"]]}
        vpp = ocr.clean_pp(validation.get("pp"))
        if vpp:
            v["pp"] = vpp
        sig["validation"] = v
    else:
        sig.pop("validation", None)
    return rects


@main_bp.route("/operations/registration-updater/training/template/save",
               methods=["POST"])
@login_required
def tr_template_save():
    """Save the current rectangles + validation box as a new CR format (template).
    Restricted to AI Trainers and Administrators (same as the Trainer tab)."""
    _require_trainer()
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Enter a template name."}), 400
    if not (data.get("rects") or {}):
        return jsonify({"error": "Draw at least one field rectangle."}), 400
    up = db.session.get(CRBookUpload, int(data.get("upload_id") or 0)) or abort(404)
    page_no = int(data.get("page_no") or 1)
    from app.utils import registration_ocr as ocr
    doc, total = ocr.open_document(up.stored_path)
    try:
        page = doc[max(0, min(page_no, total) - 1)]
        det = ocr.detect_cr_page(page)
        sig = det["signature"]
    finally:
        doc.close()
    rects = _apply_template_payload(sig, data)
    if not rects:
        return jsonify({"error": "Draw at least one field rectangle."}), 400
    key = _unique_format_key(name)
    fmt = CRFormat(key=key, name=name[:120], signature=json.dumps(sig),
                   rule_set=key, is_builtin=False, is_active=True,
                   created_by=current_user.id)
    db.session.add(fmt)
    record_audit("cr_template_saved", f"key={key} fields={len(rects)}")
    db.session.commit()
    return jsonify({"id": fmt.id, "name": fmt.name, "rects": rects,
                    "validation": sig.get("validation"),
                    "field_pp": sig.get("field_pp") or {}})


@main_bp.route("/operations/registration-updater/training/template/update",
               methods=["POST"])
@login_required
def tr_template_update():
    """Overwrite an existing template's value rectangles + validation box in
    place (keeps its name/key/layout signature).  Trainer/Admin only."""
    _require_trainer()
    data = request.get_json(silent=True) or {}
    fmt = db.session.get(CRFormat, int(data.get("template_id") or 0)) or abort(404)
    if not (data.get("rects") or {}):
        return jsonify({"error": "Draw at least one field rectangle."}), 400
    sig = _load_sig(fmt.signature)
    rects = _apply_template_payload(sig, data)
    if not rects:
        return jsonify({"error": "Draw at least one field rectangle."}), 400
    fmt.signature = json.dumps(sig)
    record_audit("cr_template_updated", f"key={fmt.key} fields={len(rects)}")
    db.session.commit()
    return jsonify({"id": fmt.id, "name": fmt.name, "rects": rects,
                    "validation": sig.get("validation"),
                    "field_pp": sig.get("field_pp") or {}})


@main_bp.route("/operations/registration-updater/cr/<int:upload_id>/review")
@login_required
def cr_review(upload_id: int):
    """Review screen for the CR-book format/validity pre-check."""
    _require_module(REGISTRATION_UPDATER_KEY)
    up = db.session.get(CRBookUpload, upload_id) or abort(404)
    pages = up.pages
    formats = (CRFormat.query.filter_by(is_active=True)
               .order_by(CRFormat.name).all())
    return render_template(
        "main/cr_review.html", upload=up, pages=pages, formats=formats)


@main_bp.route("/operations/registration-updater/cr/<int:upload_id>/recheck",
               methods=["POST"])
@login_required
def cr_recheck(upload_id: int):
    """Re-run the CR-book format/validity detection pass for an upload."""
    _require_module(REGISTRATION_UPDATER_KEY)
    up = db.session.get(CRBookUpload, upload_id) or abort(404)
    up.review_status = REVIEW_DETECTING
    up.progress_message = "Checking CR-book format\u2026"
    db.session.commit()
    _start_cr_detection(up.id)
    flash("Re-checking CR-book format\u2026", "success")
    return redirect(url_for("main.cr_review", upload_id=upload_id))


@main_bp.route("/operations/registration-updater/cr/<int:upload_id>"
               "/page-image/<int:page_no>")
@login_required
def cr_page_image(upload_id: int, page_no: int):
    """Render a CR-book page live (for the review image viewer)."""
    _require_module(REGISTRATION_UPDATER_KEY)
    up = db.session.get(CRBookUpload, upload_id) or abort(404)
    from app.utils import registration_ocr as ocr
    doc, total = ocr.open_document(up.stored_path)
    try:
        if page_no < 1 or page_no > total:
            abort(404)
        png = ocr._render_page_png(doc[page_no - 1], 2.0)
    finally:
        doc.close()
    resp = Response(png, mimetype="image/png")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@main_bp.route("/operations/registration-updater/cr/<int:upload_id>"
               "/remove-page/<int:page_no>", methods=["POST"])
@login_required
def cr_remove_page(upload_id: int, page_no: int):
    """Drop a page from the stored CR-book PDF and re-index the page rows."""
    _require_module(REGISTRATION_UPDATER_KEY)
    up = db.session.get(CRBookUpload, upload_id) or abort(404)
    import pymupdf
    doc = pymupdf.open(up.stored_path)
    try:
        if page_no < 1 or page_no > doc.page_count:
            abort(404)
        doc.delete_page(page_no - 1)
        tmp = up.stored_path + ".tmp"
        doc.save(tmp, garbage=3, deflate=True)
    finally:
        doc.close()
    os.replace(tmp, up.stored_path)
    CRUploadPage.query.filter_by(upload_id=upload_id, page_no=page_no).delete()
    for p in (CRUploadPage.query
              .filter(CRUploadPage.upload_id == upload_id,
                      CRUploadPage.page_no > page_no).all()):
        p.page_no -= 1
    up.page_count = max(0, (up.page_count or 1) - 1)
    _recompute_cr_review_status(up)
    record_audit("cr_page_removed", f"upload_id={upload_id} page={page_no}")
    db.session.commit()
    flash(f"Removed page {page_no} from '{up.filename}'.", "success")
    return redirect(url_for("main.cr_review", upload_id=upload_id))


@main_bp.route("/operations/registration-updater/cr/<int:upload_id>"
               "/define-format/<int:page_no>", methods=["POST"])
@login_required
def cr_define_format(upload_id: int, page_no: int):
    """Create/assign a CR-book format for an unknown-format page (and any other
    unknown pages in this upload whose signature matches it).

    Restricted to AI Trainers: only they may train the model on a new format."""
    _require_module(REGISTRATION_UPDATER_KEY)
    if not current_user.is_ai_trainer:
        flash("The uploaded format is not trained in the AI model. "
              "Please contact AI Trainer", "danger")
        return redirect(url_for("main.cr_review", upload_id=upload_id))
    up = db.session.get(CRBookUpload, upload_id) or abort(404)
    page = (CRUploadPage.query
            .filter_by(upload_id=upload_id, page_no=page_no).first() or abort(404))
    from app.utils import registration_ocr as ocr

    existing_id = (request.form.get("existing_format_id") or "").strip()
    if existing_id:
        fmt = db.session.get(CRFormat, int(existing_id)) or abort(404)
    else:
        name = (request.form.get("format_name") or "").strip()
        if not name:
            flash("Enter a name for the new format.", "danger")
            return redirect(url_for("main.cr_review", upload_id=upload_id))
        fmt = CRFormat(
            key=_unique_format_key(name), name=name[:120],
            signature=page.signature, rule_set=_unique_format_key(name),
            is_builtin=False, is_active=True, created_by=current_user.id)
        # rule_set must equal the (unique) key so code rules can key off it later.
        fmt.rule_set = fmt.key
        db.session.add(fmt)
        db.session.flush()

    target_sig = _load_sig(fmt.signature)
    assigned = 0
    for p in CRUploadPage.query.filter_by(
            upload_id=upload_id, status=CR_PAGE_UNKNOWN).all():
        if p.page_no == page_no or \
                ocr.format_match_score(_load_sig(p.signature), target_sig) >= 0.55:
            p.status = CR_PAGE_KNOWN
            p.format_id = fmt.id
            assigned += 1
    _recompute_cr_review_status(up)
    record_audit("cr_format_assigned", f"key={fmt.key} pages={assigned}")
    db.session.commit()
    flash(f"Assigned {assigned} page(s) to format '{fmt.name}'.", "success")
    return redirect(url_for("main.cr_review", upload_id=upload_id))


@main_bp.route("/operations/registration-updater/<kind>/<int:upload_id>/delete",
               methods=["POST"])
@login_required
def delete_upload(kind: str, upload_id: int):
    _require_module(REGISTRATION_UPDATER_KEY)
    if kind not in _DOC_KINDS:
        abort(404)
    up = db.session.get(_DOC_KINDS[kind]["upload_model"], upload_id) or abort(404)
    if kind == "rc":
        _RC_SLIP_CACHE.pop(upload_id, None)
        try:
            os.remove(_rc_slip_cache_file(up))
        except OSError:
            pass
    try:
        if up.stored_path and os.path.exists(up.stored_path):
            os.remove(up.stored_path)
    except OSError:
        pass
    import shutil
    shutil.rmtree(
        os.path.join(
            current_app.config["CR_BOOK_CROP_DIR" if kind == "cr"
                               else "RECEIPT_CROP_DIR"], str(upload_id)),
        ignore_errors=True)
    name = up.filename
    db.session.delete(up)  # records cascade via FK/relationship
    record_audit(f"{kind}_upload_deleted", f"file={name}")
    db.session.commit()
    flash(f"Deleted '{name}' and its extracted records.", "success")
    return redirect(url_for("main.registration_updater", tab=kind))


@main_bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    form = ProfileForm(obj=current_user)
    if form.validate_on_submit():
        existing = User.query.filter(
            User.email == form.email.data.strip(), User.id != current_user.id
        ).first()
        if existing:
            flash("That email address is already in use.", "danger")
            return render_template("main/profile.html", form=form)
        current_user.full_name = form.full_name.data
        current_user.email = form.email.data.strip()
        record_audit("profile_updated", "self-service")
        db.session.commit()
        flash("Your profile has been updated.", "success")
        return redirect(url_for("main.profile"))
    return render_template("main/profile.html", form=form)


@main_bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    form = SettingsForm(obj=current_user)
    if form.validate_on_submit():
        current_user.theme = form.theme.data
        db.session.commit()
        flash("Your settings have been saved.", "success")
        return redirect(url_for("main.settings"))
    return render_template("main/settings.html", form=form)


@main_bp.route("/theme", methods=["POST"])
@login_required
def set_theme():
    """Quick theme toggle from the top bar; returns to the previous page."""
    theme = request.form.get("theme", "light")
    if theme not in {"light", "dark"}:
        theme = "light"
    current_user.theme = theme
    db.session.commit()
    return redirect(request.referrer or url_for("main.dashboard"))
