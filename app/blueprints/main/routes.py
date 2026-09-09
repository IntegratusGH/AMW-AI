"""Main application routes: dashboard, operations, profile, settings."""
from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime

from flask import (
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import current_user, login_required
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
    ROLE_ADMIN,
    ROLE_OPERATOR,
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_PENDING,
    STATUS_PROCESSING,
    Category,
    CRBookRecord,
    CRBookUpload,
    Module,
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
            try:
                for i in range(total):
                    if kind == "cr":
                        rec = ocr.extract_cr_page(doc[i])
                        page_recs = [rec] if rec else []
                        for r in page_recs:
                            r["page_no"] = i + 1
                    else:
                        page_recs = ocr.extract_receipt_page(doc[i])

                    for r in page_recs:
                        if kind == "rc":
                            r["segment_no"] = captured + 1
                        key = (str(r.get(key_field) or "")).strip().upper()
                        is_dup = bool(key) and key in seen
                        if key:
                            seen.add(key)
                        if is_dup:
                            duplicates += 1
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
                if mask:
                    up.progress_message = "Applying batch chassis template\u2026"
                    db.session.commit()
                    ext_seen = {
                        (v[0] or "").strip().upper()
                        for v in db.session.query(key_col).filter(
                            key_col.isnot(None),
                            RecordModel.upload_id != upload_id).all()
                        if v[0]}
                    batch_seen = set()
                    duplicates = 0
                    for rec_obj, r in cr_pending:
                        new_ch, changed, recovered = ocr.apply_chassis_mask(
                            rec_obj.chassis_no, mask)
                        if changed:
                            rec_obj.chassis_no = new_ch
                        rec_obj.needs_review = bool(
                            recovered
                            or ocr.chassis_needs_review(
                                rec_obj.chassis_no, mask,
                                rec_obj.chassis_no_confidence)
                            or ocr._reg_review(
                                rec_obj.registration_no,
                                rec_obj.registration_no_confidence))
                        key = (rec_obj.chassis_no or "").strip().upper()
                        is_dup = bool(key) and (key in ext_seen or key in batch_seen)
                        if key:
                            batch_seen.add(key)
                        rec_obj.is_duplicate = is_dup
                        if is_dup:
                            duplicates += 1
                    up.duplicate_records = duplicates
                    db.session.commit()

            # Persist the cropped value-region scans (Chassis No / Reg No) so the
            # records page can show the original image beside each captured value.
            if kind == "cr" and cr_pending:
                crop_dir = os.path.join(
                    app.config["CR_BOOK_CROP_DIR"], str(upload_id))
                os.makedirs(crop_dir, exist_ok=True)
                for rec_obj, r in cr_pending:
                    for field, src in (("chassis", "_chassis_crop_png"),
                                       ("reg", "_reg_crop_png")):
                        png = r.get(src)
                        if not png:
                            continue
                        fn = f"{rec_obj.id}_{field}.png"
                        with open(os.path.join(crop_dir, fn), "wb") as fh:
                            fh.write(png)
                        setattr(rec_obj,
                                "chassis_crop" if field == "chassis" else "reg_crop",
                                fn)
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


@main_bp.route("/operations/registration-updater", methods=["GET", "POST"])
@login_required
def registration_updater():
    module = _require_module(REGISTRATION_UPDATER_KEY)
    cr_form = CRBookUploadForm()
    rc_form = ReceiptUploadForm()

    if cr_form.submit_cr.data and cr_form.validate_on_submit():
        name, path = _save_doc(
            cr_form.file.data, current_app.config["CR_BOOK_UPLOAD_DIR"])
        up = CRBookUpload(
            filename=name, stored_path=path,
            uploaded_by=current_user.id, status=STATUS_PENDING)
        db.session.add(up)
        record_audit("cr_book_uploaded", f"file={name}")
        db.session.commit()
        flash("CR Book uploaded. Click Process to extract records.", "success")
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
    # Keep the user on the tab they acted from: an explicit ?tab= wins, else fall
    # back to whichever form reported validation errors (default CR).
    active_tab = request.args.get("tab")
    if active_tab not in ("cr", "rc"):
        active_tab = "rc" if (rc_form.errors and not cr_form.errors) else "cr"
    return render_template(
        "main/registration_updater.html", module=module,
        cr_form=cr_form, rc_form=rc_form,
        cr_uploads=cr_uploads, rc_uploads=rc_uploads, active_tab=active_tab)


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
    """Serve the cropped value-region scan (chassis|reg) for one CR record."""
    _require_module(REGISTRATION_UPDATER_KEY)
    if field not in ("chassis", "reg"):
        abort(404)
    rec = db.session.get(CRBookRecord, record_id) or abort(404)
    if rec.upload_id != upload_id:
        abort(404)
    fn = rec.chassis_crop if field == "chassis" else rec.reg_crop
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


@main_bp.route("/operations/registration-updater/cr/<int:upload_id>"
               "/record/<int:record_id>/edit", methods=["POST"])
@login_required
def cr_edit_record(upload_id: int, record_id: int):
    """Save an operator's correction of a CR record's Chassis/Reg No."""
    _require_module(REGISTRATION_UPDATER_KEY)
    rec = db.session.get(CRBookRecord, record_id) or abort(404)
    if rec.upload_id != upload_id:
        abort(404)
    rec.registration_no = (request.form.get("registration_no") or "").strip()[:60] or None
    rec.chassis_no = (request.form.get("chassis_no") or "").strip()[:60] or None
    rec.needs_review = False  # an operator has verified/corrected this row
    record_audit("cr_record_edited", f"record_id={record_id}")
    db.session.commit()
    flash("Record updated.", "success")
    return redirect(url_for("main.upload_records", kind="cr", upload_id=upload_id))


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
