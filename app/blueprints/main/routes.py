"""Main application routes: dashboard, operations, profile, settings."""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import uuid
from datetime import datetime

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
    MatchedRecord,
    Module,
    OcrCorrection,
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


# Per document-type configuration for the generic processing worker.
_DOC_KINDS = {
    "cr": {
        "upload_model": CRBookUpload,
        "record_model": CRBookRecord,
        "key_field": "chassis_no",
        "noun": "CR book",
    },
    "rc": {
        "upload_model": ReceiptUpload,
        "record_model": ReceiptRecord,
        "key_field": "slip_no",
        "noun": "receipt",
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
            up.progress_message = f"Starting OCR on {total} page(s)\u2026"
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
            if kind == "cr":
                for p in CRUploadPage.query.filter_by(upload_id=upload_id).all():
                    page_rules[p.page_no] = (
                        p.format.rule_set if p.format else "_generic")
                if getattr(up, "ocr_pp", None):
                    try:
                        upload_pp = ocr.clean_pp(json.loads(up.ocr_pp)) or None
                    except (ValueError, TypeError):
                        upload_pp = None
            try:
                for i in range(total):
                    if kind == "cr":
                        rec = ocr.extract_cr_page(
                            doc[i],
                            rule_set=page_rules.get(i + 1, "oldcrbook"),
                            pp=upload_pp)
                        page_recs = [rec] if rec else []
                        for r in page_recs:
                            r["page_no"] = i + 1
                    else:
                        page_recs = ocr.extract_receipt_page(doc[i])

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

            # Batch reconciliation: receipt numbers are printed sequentially and
            # a batch is printed on a single day, so enforce both across the
            # captured records once the whole document has been OCR'd.
            if kind == "rc" and rc_pending:
                up.progress_message = "Reconciling receipt numbers/date\u2026"
                db.session.commit()
                seq = ocr.reconcile_receipt_numbers(
                    [ro.receipt_no for ro, _ in rc_pending])
                day = ocr.reconcile_receipt_dates(
                    [ro.receipt_date for ro, _ in rc_pending])
                for (ro, _), rn in zip(rc_pending, seq):
                    ro.receipt_no = rn
                    if day:
                        ro.receipt_date = day
                db.session.commit()

            # Persist each receipt's cropped page region for the image viewer.
            if kind == "rc" and rc_pending:
                crop_dir = os.path.join(
                    app.config["RECEIPT_CROP_DIR"], str(upload_id))
                os.makedirs(crop_dir, exist_ok=True)
                for rec_obj, r in rc_pending:
                    png = r.get("_image_crop_png")
                    if not png:
                        continue
                    fn = f"{rec_obj.id}.png"
                    with open(os.path.join(crop_dir, fn), "wb") as fh:
                        fh.write(png)
                    rec_obj.image_crop = fn
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
        up = CRBookUpload(
            filename=name, stored_path=path, uploaded_by=current_user.id,
            status=STATUS_PENDING if check_format else STATUS_PROCESSING,
            review_status=REVIEW_DETECTING if check_format else REVIEW_READY)
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
    export_documents = (ExportDocument.query
                        .order_by(ExportDocument.created_at.desc()).all())
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
        active_tab=active_tab)


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

    matched = 0
    used_rc: set[int] = set()
    cr_dec: dict[int, int] = {}
    rc_dec: dict[int, int] = {}
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
            rc_segment_no=rc.segment_no, image_crop=rc.image_crop))
        cr_dec[cr.upload_id] = cr_dec.get(cr.upload_id, 0) + 1
        rc_dec[rc.upload_id] = rc_dec.get(rc.upload_id, 0) + 1
        db.session.delete(cr)
        db.session.delete(rc)
        matched += 1
    # Reduce each source upload's captured count so the uploads list / records
    # page no longer counts rows that have moved into matched_records.
    _adjust_captured(CRBookUpload, cr_dec, -1)
    _adjust_captured(ReceiptUpload, rc_dec, -1)
    return matched


def _adjust_captured(model, counts: dict, sign: int) -> None:
    """Add ``sign * n`` to each listed upload's captured_records (clamped >= 0)."""
    for uid, n in counts.items():
        up = db.session.get(model, uid)
        if up is not None:
            up.captured_records = max(0, (up.captured_records or 0) + sign * n)


def _revoke_mapping(ids) -> int:
    """Reverse of ``_perform_mapping`` for the selected ``matched_records`` ids:
    recreate each snapshot's CR-book and receipt row in its source table (images
    included) and delete the matched row.  Adds to the session and returns the
    count; the caller commits."""
    if not ids:
        return 0
    rows = MatchedRecord.query.filter(MatchedRecord.id.in_(ids)).all()
    count = 0
    cr_inc: dict[int, int] = {}
    rc_inc: dict[int, int] = {}
    for m in rows:
        db.session.add(CRBookRecord(
            upload_id=m.cr_source_upload_id, page_no=m.cr_page_no,
            registration_no=m.registration_no, cr_number=m.cr_number,
            chassis_no=m.chassis_no, engine_no=m.engine_no,
            current_owner=m.current_owner, make=m.make, model=m.model,
            class_of_vehicle=m.class_of_vehicle, colour=m.colour,
            year_of_manufacture=m.year_of_manufacture,
            chassis_crop=m.chassis_crop, reg_crop=m.reg_crop,
            engine_crop=m.engine_crop))
        db.session.add(ReceiptRecord(
            upload_id=m.rc_source_upload_id, segment_no=m.rc_segment_no,
            vehicle_no=m.vehicle_no, slip_no=m.slip_no,
            receipt_no=m.receipt_no, receipt_date=m.receipt_date,
            applicant_name=m.applicant_name, total_amount=m.total_amount,
            bank_charges=m.bank_charges, bank_total=m.bank_total,
            image_crop=m.image_crop))
        if m.cr_source_upload_id:
            cr_inc[m.cr_source_upload_id] = cr_inc.get(m.cr_source_upload_id, 0) + 1
        if m.rc_source_upload_id:
            rc_inc[m.rc_source_upload_id] = rc_inc.get(m.rc_source_upload_id, 0) + 1
        db.session.delete(m)
        count += 1
    # Restore each source upload's captured count for the returned rows.
    _adjust_captured(CRBookUpload, cr_inc, +1)
    _adjust_captured(ReceiptUpload, rc_inc, +1)
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
# (NEWBS, WRBTR, SGTXT, NEWKO) for the four line items of every document.
_EXPORT_LINES = [
    ("40", "11010", "Vehicle Registration", "177205"),
    ("40", "25000", "Special No Fees", "177205"),
    ("40", "4500", "Number Plate Fees", "177205"),
    ("50", "40510", "Slip Value", "119682"),
]
_EXPORT_CONST = {"TCODE": "FB01", "BLART": "SA", "BUKRS": "2501",
                 "WAERS": "LKR", "PRCTR": "W111D1"}


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


def _write_rmv_export(path: str, records) -> None:
    """Write the matched ``records`` as a tab-separated RMV UPLOAD file: two
    header rows then, per record, four line items (TRNID increments per record)."""
    rows = [_EXPORT_HDR1, _EXPORT_HDR2]
    for i, m in enumerate(records, start=1):
        date = _to_ddmmyyyy(m.receipt_date)
        for newbs, wrbtr, sgtxt, newko in _EXPORT_LINES:
            rows.append([
                str(i), _EXPORT_CONST["TCODE"], date, _EXPORT_CONST["BLART"],
                _EXPORT_CONST["BUKRS"], date, _EXPORT_CONST["WAERS"],
                m.slip_no or "", m.receipt_no or "", newbs, wrbtr,
                m.chassis_no or "", sgtxt, newko, _EXPORT_CONST["PRCTR"]])
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
    return render_template(
        "main/registration_records.html", kind=kind, upload=up, records=records)


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
    """Render the first page of a CR upload with the given image adjustments so
    the upload's OCR-settings popup can show a live preview."""
    _require_module(REGISTRATION_UPDATER_KEY)
    from app.utils import registration_ocr as ocr
    up = db.session.get(CRBookUpload, upload_id) or abort(404)
    data = request.get_json(silent=True) or {}
    pp = ocr.clean_pp(data.get("pp"))
    doc, total = ocr.open_document(up.stored_path)
    try:
        png = ocr.render_page_png(doc[0], pp=pp, zoom=1.6)
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
    doc, total = ocr.open_document(up.stored_path)
    try:
        fields = ocr.test_cr_page(doc[0], pp=pp)
    finally:
        doc.close()
    return jsonify({"ok": True, "fields": fields})


@main_bp.route("/operations/registration-updater/cr/<int:upload_id>/ocr-settings",
               methods=["POST"])
@login_required
def cr_ocr_settings(upload_id: int):
    """Persist the upload-level OCR image adjustments and, if requested, kick a
    re-process so they take effect immediately."""
    _require_module(REGISTRATION_UPDATER_KEY)
    from app.utils import registration_ocr as ocr
    up = db.session.get(CRBookUpload, upload_id) or abort(404)
    data = request.get_json(silent=True) or {}
    pp = ocr.clean_pp(data.get("pp"))
    up.ocr_pp = json.dumps(pp) if pp else None
    db.session.commit()
    record_audit("cr_ocr_settings",
                 f"upload_id={upload_id} keys={sorted(pp)}")
    started = False
    if bool(data.get("reprocess")) and up.status != STATUS_PROCESSING \
            and up.review_status == REVIEW_READY:
        up.status = STATUS_PROCESSING
        up.error = None
        db.session.commit()
        _start_processing("cr", upload_id)
        started = True
    return jsonify({"ok": True, "reprocessing": started})


def _norm_match(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


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
