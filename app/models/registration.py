"""Registration Updater module: document upload and extracted-record models.

Two document types are handled by module ``registration_updater``:

* CR books  -- Certificate of Registration of Motor Vehicle (one per PDF page).
* Receipts  -- bank deposit slips for the Dept. of Motor Traffic (many per file).

Each uploaded file is tracked in an ``*_uploads`` table (metadata + processing
status/counts) and its OCR-extracted rows in the matching ``*_records`` table.
"""
from __future__ import annotations

from datetime import datetime

from app.extensions import db

# Processing lifecycle for an uploaded document.
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_ERROR = "error"
UPLOAD_STATUSES = (STATUS_PENDING, STATUS_PROCESSING, STATUS_DONE, STATUS_ERROR)

# CR-book format pre-check (runs on upload, before the heavy OCR/Process step).
REVIEW_PENDING = "pending"        # not checked yet
REVIEW_DETECTING = "detecting"    # detection pass running
REVIEW_NEEDS = "needs_review"     # some pages are not-CR or an unknown format
REVIEW_READY = "ready"            # every page is a known CR-book format
REVIEW_ERROR = "error"

# Per-page classification produced by the detection pass.
CR_PAGE_KNOWN = "known"               # matches a known CR-book format
CR_PAGE_UNKNOWN = "unknown_format"    # is a CR book but no format matches
CR_PAGE_NOT_CR = "not_cr"             # too few CR field labels -> not a CR book


class _UploadMixin:
    """Shared columns for a tracked document upload."""

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    stored_path = db.Column(db.String(500), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    uploaded_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    # Identity of the source file: its original name (independent of any
    # ``copy N`` display rename) and its last-modified wall-clock time.  The
    # UPLOADED column shows source_mtime; (source_name, source_mtime) keys the
    # same-name/different-date re-upload handling.
    source_name = db.Column(db.String(255), nullable=True)
    source_mtime = db.Column(db.DateTime, nullable=True)

    processed = db.Column(db.Boolean, default=False, nullable=False)
    status = db.Column(db.String(20), default=STATUS_PENDING, nullable=False)
    page_count = db.Column(db.Integer, default=0, nullable=False)
    captured_records = db.Column(db.Integer, default=0, nullable=False)
    duplicate_records = db.Column(db.Integer, default=0, nullable=False)
    error = db.Column(db.Text, nullable=True)
    processed_at = db.Column(db.DateTime, nullable=True)

    # Live progress written incrementally by the OCR worker (per page).
    progress_done = db.Column(db.Integer, default=0, nullable=False)
    progress_total = db.Column(db.Integer, default=0, nullable=False)
    progress_message = db.Column(db.String(200), nullable=True)


class CRBookUpload(_UploadMixin, db.Model):
    __tablename__ = "cr_book_uploads"

    # Format/validity pre-check state (see REVIEW_* above).
    review_status = db.Column(db.String(20), default=REVIEW_PENDING, nullable=False)

    # OCR template driving extraction for this upload (image settings + value
    # boxes live on the template).  NULL = label-anchored extraction.
    template_id = db.Column(
        db.Integer, db.ForeignKey("ocr_templates.id"), nullable=True)
    template = db.relationship("OcrTemplate")

    records = db.relationship(
        "CRBookRecord", back_populates="upload",
        cascade="all, delete-orphan", passive_deletes=True,
    )
    pages = db.relationship(
        "CRUploadPage", back_populates="upload",
        cascade="all, delete-orphan", passive_deletes=True,
        order_by="CRUploadPage.page_no",
    )

    @property
    def review_pending_count(self) -> int:
        """Pages still needing the operator's attention (not-CR or unknown)."""
        return sum(1 for p in self.pages
                   if p.status in (CR_PAGE_NOT_CR, CR_PAGE_UNKNOWN))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CRBookUpload {self.id} {self.filename}>"


class ReceiptUpload(_UploadMixin, db.Model):
    __tablename__ = "receipt_uploads"

    records = db.relationship(
        "ReceiptRecord", back_populates="upload",
        cascade="all, delete-orphan", passive_deletes=True,
    )

    # OCR template driving extraction for this upload (image settings live on
    # the template).  NULL = default receipt extraction.
    template_id = db.Column(
        db.Integer, db.ForeignKey("ocr_templates.id"), nullable=True)
    template = db.relationship("OcrTemplate")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ReceiptUpload {self.id} {self.filename}>"


class CRBookRecord(db.Model):
    """One vehicle's Certificate of Registration (one CR-book PDF page)."""

    __tablename__ = "cr_book_records"

    id = db.Column(db.Integer, primary_key=True)
    upload_id = db.Column(
        db.Integer,
        db.ForeignKey("cr_book_uploads.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    page_no = db.Column(db.Integer, nullable=True)

    cr_number = db.Column(db.String(60), nullable=True)
    registration_no = db.Column(db.String(60), nullable=True, index=True)
    chassis_no = db.Column(db.String(60), nullable=True, index=True)
    engine_no = db.Column(db.String(60), nullable=True)
    # Filenames of the cropped scan of each value region (for visual
    # verification / correction on the records page).
    chassis_crop = db.Column(db.String(255), nullable=True)
    reg_crop = db.Column(db.String(255), nullable=True)
    engine_crop = db.Column(db.String(255), nullable=True)
    current_owner = db.Column(db.String(200), nullable=True)
    current_owner_address = db.Column(db.String(400), nullable=True)
    absolute_owner = db.Column(db.String(300), nullable=True)
    make = db.Column(db.String(80), nullable=True)
    model = db.Column(db.String(80), nullable=True)
    class_of_vehicle = db.Column(db.String(80), nullable=True)
    fuel_type = db.Column(db.String(40), nullable=True)
    cylinder_capacity = db.Column(db.String(40), nullable=True)
    year_of_manufacture = db.Column(db.String(10), nullable=True)
    colour = db.Column(db.String(60), nullable=True)
    country_of_origin = db.Column(db.String(80), nullable=True)
    provincial_council = db.Column(db.String(80), nullable=True)
    date_first_registration = db.Column(db.String(40), nullable=True)

    is_duplicate = db.Column(db.Boolean, default=False, nullable=False)
    confidence = db.Column(db.Float, nullable=True)
    # Per-field confidence for the two critical identifiers + review flag.
    chassis_no_confidence = db.Column(db.Float, nullable=True)
    registration_no_confidence = db.Column(db.Float, nullable=True)
    engine_no_confidence = db.Column(db.Float, nullable=True)
    needs_review = db.Column(db.Boolean, default=False, nullable=False)
    raw_text = db.Column(db.Text, nullable=True)
    # Name of the Template-picker template applied to this record (the CR
    # template resolved for the upload via the validation/fallback chain);
    # shown in View Records.
    applied_template = db.Column(db.String(120), nullable=True)
    # Template-defined field data (hybrid storage): JSON maps of field key ->
    # value / crop filename / confidence for every field of this record's
    # template.  The canonical typed columns above stay authoritative for
    # mapping/export/dedup/search; these hold the full, template-driven set.
    field_values = db.Column(db.Text, nullable=True)
    field_crops = db.Column(db.Text, nullable=True)
    field_confidence = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    upload = db.relationship("CRBookUpload", back_populates="records")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CRBookRecord {self.id} chassis={self.chassis_no}>"


class CRFormat(db.Model):
    """A recognised CR-book layout.  ``signature`` is JSON describing the field
    labels (and their normalised positions) used to match a page to this format;
    ``rule_set`` names the code rule-set used to OCR pages of this format."""

    __tablename__ = "cr_formats"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(60), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    signature = db.Column(db.Text, nullable=True)      # JSON: {labels, pos}
    rule_set = db.Column(db.String(60), nullable=False, default="_generic")
    is_builtin = db.Column(db.Boolean, default=False, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CRFormat {self.key}>"


class CRUploadPage(db.Model):
    """Per-page result of the CR-book detection pass for one upload."""

    __tablename__ = "cr_upload_pages"

    id = db.Column(db.Integer, primary_key=True)
    upload_id = db.Column(
        db.Integer,
        db.ForeignKey("cr_book_uploads.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    page_no = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), nullable=False, default=CR_PAGE_UNKNOWN)
    format_id = db.Column(
        db.Integer, db.ForeignKey("cr_formats.id", ondelete="SET NULL"),
        nullable=True,
    )
    core_hits = db.Column(db.Integer, default=0, nullable=False)
    signature = db.Column(db.Text, nullable=True)      # JSON: {labels, pos}
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    upload = db.relationship("CRBookUpload", back_populates="pages")
    format = db.relationship("CRFormat")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CRUploadPage u{self.upload_id} p{self.page_no} {self.status}>"


class ReceiptRecord(db.Model):
    """One Dept. of Motor Traffic bank deposit slip (receipt)."""

    __tablename__ = "receipt_records"

    id = db.Column(db.Integer, primary_key=True)
    upload_id = db.Column(
        db.Integer,
        db.ForeignKey("receipt_uploads.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    segment_no = db.Column(db.Integer, nullable=True)

    slip_no = db.Column(db.String(40), nullable=True, index=True)
    receipt_no = db.Column(db.String(40), nullable=True)
    receipt_date = db.Column(db.String(20), nullable=True)
    receipt_time = db.Column(db.String(20), nullable=True)
    vehicle_no = db.Column(db.String(40), nullable=True, index=True)
    application_no = db.Column(db.String(40), nullable=True, index=True)
    applicant_name = db.Column(db.String(200), nullable=True)
    total_amount = db.Column(db.Numeric(12, 2), nullable=True)
    bank_charges = db.Column(db.Numeric(12, 2), nullable=True)
    bank_total = db.Column(db.Numeric(12, 2), nullable=True)
    # Filename of the cropped scan of this receipt's region on the source page
    # (shown in the image viewer on the records page).
    image_crop = db.Column(db.String(255), nullable=True)
    # Filenames of the per-field value-region crops taken from the assigned OCR
    # template's boxes (shown above each value on the records page).
    slip_crop = db.Column(db.String(255), nullable=True)
    receipt_no_crop = db.Column(db.String(255), nullable=True)
    date_crop = db.Column(db.String(255), nullable=True)
    vehicle_crop = db.Column(db.String(255), nullable=True)
    total_crop = db.Column(db.String(255), nullable=True)
    charges_crop = db.Column(db.String(255), nullable=True)
    bank_total_crop = db.Column(db.String(255), nullable=True)

    is_duplicate = db.Column(db.Boolean, default=False, nullable=False)
    # Set when the Total/Charges/Bank Total figures do not balance (or a value
    # had to be derived/corrected), so an operator verifies the amounts.
    needs_review = db.Column(db.Boolean, default=False, nullable=False)
    confidence = db.Column(db.Float, nullable=True)
    raw_text = db.Column(db.Text, nullable=True)
    # Per-record extraction error (e.g. no template in the picker chain matched
    # this slip).  When set, the slip was skipped and flagged for the operator
    # instead of aborting the whole document.
    error = db.Column(db.String(200), nullable=True)
    # Name of the Template-picker template that actually validated/was applied to
    # this slip (the chain may pick a different template per slip).
    applied_template = db.Column(db.String(120), nullable=True)
    # Template-defined field data (hybrid storage): JSON maps of field key ->
    # value / crop filename / confidence for every field of this record's
    # template.  The canonical typed columns above stay authoritative for
    # mapping/export/dedup/search; these hold the full, template-driven set.
    field_values = db.Column(db.Text, nullable=True)
    field_crops = db.Column(db.Text, nullable=True)
    field_confidence = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    upload = db.relationship("ReceiptUpload", back_populates="records")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ReceiptRecord {self.id} slip={self.slip_no}>"


class OcrCorrection(db.Model):
    """Durable labeled data captured when an operator verifies/corrects a
    record's critical field.  Stores the model's original reading, the operator's
    final value and (a copy of) the value-region crop, so the system can report
    real accuracy, mine confusion rules / per-field priors, and export a training
    corpus for eventual recognizer fine-tuning.

    One row per (doc_kind, record_id, field): ``ocr_value`` is captured once (the
    model's output before the first correction) and ``corrected_value`` tracks the
    operator's latest verified value.  ``was_correct`` records whether the model
    already matched the operator's value.  The crop is copied into a persistent
    label store so it survives upload/record deletion."""

    __tablename__ = "ocr_corrections"
    __table_args__ = (
        db.UniqueConstraint("doc_kind", "record_id", "field",
                            name="uq_ocr_corr_rec_field"),
    )

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False,
                           index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    doc_kind = db.Column(db.String(4), nullable=False)       # 'cr' | 'rc'
    upload_id = db.Column(db.Integer, nullable=True)
    record_id = db.Column(db.Integer, nullable=True, index=True)
    page_no = db.Column(db.Integer, nullable=True)
    field = db.Column(db.String(30), nullable=False, index=True)
    ocr_value = db.Column(db.String(120), nullable=True)
    corrected_value = db.Column(db.String(120), nullable=True)
    was_correct = db.Column(db.Boolean, default=False, nullable=False)
    confidence = db.Column(db.Float, nullable=True)
    rule_set = db.Column(db.String(60), nullable=True)
    crop_file = db.Column(db.String(255), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<OcrCorrection {self.field} r{self.record_id} "
                f"ok={self.was_correct}>")


class MatchedRecord(db.Model):
    """A CR-book record matched to a registration receipt by vehicle number.

    Produced by the "Process Mapping" action, which pairs
    ``receipt_records.vehicle_no`` with ``cr_book_records.registration_no`` (on a
    normalised match key), snapshots both rows here and removes the originals from
    their source tables.  The snapshot is self-contained (no FKs to the source
    records) precisely because those rows are deleted once matched."""

    __tablename__ = "matched_records"

    id = db.Column(db.Integer, primary_key=True)
    matched_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False,
                           index=True)
    matched_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    match_key = db.Column(db.String(60), nullable=True, index=True)
    # Once a matched pair is written into an export file it is linked here and
    # hidden from the Mapped-records tab; revoking the export clears this so the
    # pair returns to Mapped records.
    export_document_id = db.Column(
        db.Integer,
        db.ForeignKey("export_documents.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    # --- CR-book side (snapshot) ---
    cr_source_upload_id = db.Column(db.Integer, nullable=True)
    cr_source_record_id = db.Column(db.Integer, nullable=True)
    registration_no = db.Column(db.String(60), nullable=True, index=True)
    cr_number = db.Column(db.String(60), nullable=True)
    chassis_no = db.Column(db.String(60), nullable=True)
    engine_no = db.Column(db.String(60), nullable=True)
    current_owner = db.Column(db.String(200), nullable=True)
    make = db.Column(db.String(80), nullable=True)
    model = db.Column(db.String(80), nullable=True)
    class_of_vehicle = db.Column(db.String(80), nullable=True)
    colour = db.Column(db.String(60), nullable=True)
    year_of_manufacture = db.Column(db.String(10), nullable=True)
    # Snapshot of the CR page / value-region crop filenames so a revoke can
    # restore the record's images (files persist under the source upload id).
    cr_page_no = db.Column(db.Integer, nullable=True)
    chassis_crop = db.Column(db.String(255), nullable=True)
    reg_crop = db.Column(db.String(255), nullable=True)
    engine_crop = db.Column(db.String(255), nullable=True)

    # --- Receipt side (snapshot) ---
    rc_source_upload_id = db.Column(db.Integer, nullable=True)
    rc_source_record_id = db.Column(db.Integer, nullable=True)
    vehicle_no = db.Column(db.String(40), nullable=True, index=True)
    slip_no = db.Column(db.String(40), nullable=True)
    receipt_no = db.Column(db.String(40), nullable=True)
    receipt_date = db.Column(db.String(20), nullable=True)
    applicant_name = db.Column(db.String(200), nullable=True)
    total_amount = db.Column(db.Numeric(12, 2), nullable=True)
    bank_charges = db.Column(db.Numeric(12, 2), nullable=True)
    bank_total = db.Column(db.Numeric(12, 2), nullable=True)
    rc_segment_no = db.Column(db.Integer, nullable=True)
    image_crop = db.Column(db.String(255), nullable=True)

    # Full field snapshot of each source record (every data column of
    # cr_book_records / receipt_records plus template custom fields), stored as
    # an ordered JSON list ``[{"k":key,"l":label,"v":value}, ...]`` so the
    # Mapped-records view can show every field both tables carry and drop the
    # columns that are empty for all matched rows.  The typed columns above stay
    # authoritative for export/dedup/search/revoke.
    cr_fields = db.Column(db.Text, nullable=True)
    rc_fields = db.Column(db.Text, nullable=True)
    # Verbatim copies of each source row's field_values / field_crops JSON so a
    # revoke restores the record whole (custom fields such as R55/M17 and their
    # value-region crops included), and a re-map snapshots them again.
    cr_field_values = db.Column(db.Text, nullable=True)
    cr_field_crops = db.Column(db.Text, nullable=True)
    rc_field_values = db.Column(db.Text, nullable=True)
    rc_field_crops = db.Column(db.Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MatchedRecord {self.id} reg={self.registration_no}>"


class ExportDocument(db.Model):
    """One generated SAP batch-input export file (RMV UPLOAD), produced from a
    selection of matched records.  The included ``matched_records`` are linked
    via ``export_document_id`` (not deleted), so revoking the export returns them
    to the Mapped-records tab and removes this row and its file."""

    __tablename__ = "export_documents"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False,
                           index=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    filename = db.Column(db.String(255), nullable=False)
    stored_path = db.Column(db.String(500), nullable=False)
    file_size = db.Column(db.Integer, default=0, nullable=False)
    records_count = db.Column(db.Integer, default=0, nullable=False)

    records = db.relationship("MatchedRecord", backref="export_document")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ExportDocument {self.id} n={self.records_count}>"


class ExportTemplate(db.Model):
    """The (single) configurable SAP export template used by Generate Export.

    ``config`` is a JSON document describing how one matched record expands into
    output rows: fixed ``constants``, per-document field mappings (``doc_fields``)
    and an ordered list of ``lines`` (each with a posting key, account, item-text
    source, an amount source and an include ``when`` condition).  Only one row is
    kept and edited via the Export-setting popup; a missing/blank config falls
    back to the built-in default (today's fixed four-line layout)."""

    __tablename__ = "export_templates"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, default="Default",
                     server_default="Default")
    config = db.Column(db.Text, nullable=True)
    updated_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ExportTemplate {self.id} {self.name!r}>"


class OcrTemplate(db.Model):
    """A reusable OCR template: named image-preprocessing settings plus (for CR
    books) per-field value-region rectangles.  An upload references one template
    (``template_id``); its settings/boxes drive Test OCR, Process and Reprocess.
    ``kind`` scopes the template to a document type: 'cr' (CR books, with value
    boxes) or 'rc' (receipts, image settings only)."""

    __tablename__ = "ocr_templates"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    # Document type this template applies to: 'cr' or 'rc'.
    kind = db.Column(db.String(4), nullable=False, default="cr",
                     server_default="cr")
    # JSON of a cleaned pp dict (image adjustments applied to every page).
    ocr_pp = db.Column(db.Text, nullable=True)
    # JSON: field -> [x0,y0,x1,y1] normalized value-region rectangles.
    ocr_rects = db.Column(db.Text, nullable=True)
    # JSON (receipts): up to 3 calibration anchors --
    # [{"box":[x0,y0,x1,y1], "patch":"<base64 png>"}].  Each is a small image
    # patch captured from a reference slip at a fixed, always-printed spot; at
    # extraction every slip is registered to the reference via these anchors so
    # the value boxes track its actual offset/scale.
    ocr_anchors = db.Column(db.Text, nullable=True)
    # JSON: up to 3 text/label calibration anchors --
    # [{"box":[x0,y0,x1,y1], "text":"EXPECTED"}].  Each marks a fixed printed
    # label; at extraction the label is located by OCR within its box and the
    # matched positions solve the same similarity transform as ``ocr_anchors``,
    # used when no image-patch anchors are set.
    ocr_text_anchors = db.Column(db.Text, nullable=True)
    # JSON: ordered list of this template's own field definitions --
    # [{"key","label","type","role","box":[x0,y0,x1,y1]|null,"pp":{...}|null}].
    # Supersedes the fixed per-kind field lists so each template carries its own
    # fields + positions; ``ocr_rects`` is kept as a derived {key: box} view for
    # back-compat with the current extractor.
    fields = db.Column(db.Text, nullable=True)
    # Template picker: validation + fallback chain.  When ``validate_enabled``
    # is set, processing OCRs ``validate_box`` (JSON normalized [x0,y0,x1,y1])
    # on the page using this template's image settings and only uses the
    # template if the read text contains ``validate_text``.  A second, optional
    # box (``validate_box2``/``validate_text2``) may also be configured; when it
    # is, BOTH boxes must match for the template to be used (logical AND).
    # Otherwise it falls back to ``fail_template_id`` and repeats.  If the chain
    # is exhausted with no match, processing stops with "No matched template
    # found.".
    validate_enabled = db.Column(db.Boolean, nullable=False, default=False,
                                 server_default=db.text("0"))
    validate_box = db.Column(db.Text, nullable=True)
    validate_text = db.Column(db.String(200), nullable=True)
    validate_box2 = db.Column(db.Text, nullable=True)
    validate_text2 = db.Column(db.String(200), nullable=True)
    fail_template_id = db.Column(
        db.Integer, db.ForeignKey("ocr_templates.id"), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<OcrTemplate {self.id} {self.name!r}>"


class OcrList(db.Model):
    """A named, global lookup list referenced from a field's Value as
    ``[name]``.  ``items`` is newline-separated; each non-empty line is one
    entry.  Consumed by the field "Apply mask" rules (pattern / partial match /
    number-of-characters) to refine an OCR capture."""

    __tablename__ = "ocr_lists"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    items = db.Column(db.Text, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)

    def item_lines(self) -> list[str]:
        return [ln.strip() for ln in (self.items or "").splitlines() if ln.strip()]

    def __repr__(self) -> str:  # pragma: no cover
        return f"<OcrList {self.name!r}>"


class OcrCustomField(db.Model):
    """Registry of user-added (non-canonical) record fields.  Each row means a
    real ``NVARCHAR`` column named ``key`` exists on the ``kind`` records table
    (``cr_book_records`` for 'cr', ``receipt_records`` for 'rc'), added on demand
    when the field is first defined in a template's Field-setting tab."""

    __tablename__ = "ocr_custom_fields"

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(4), nullable=False, default="cr")
    key = db.Column(db.String(64), nullable=False)
    label = db.Column(db.String(120), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("kind", "key", name="uq_ocr_custom_kind_key"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<OcrCustomField {self.kind}:{self.key}>"


class CRAutoSetting(db.Model):
    """Singleton settings for the automated CR-book folder watcher: the server
    folder scanned (recursively) for new PDFs and whether the watcher is on."""

    __tablename__ = "cr_auto_settings"

    id = db.Column(db.Integer, primary_key=True)
    folder = db.Column(db.String(500), nullable=True)
    enabled = db.Column(db.Boolean, nullable=False, default=False,
                        server_default=db.text("0"))
    # Seconds to wait after a scan's picked files finish processing before the
    # next scan.
    interval_seconds = db.Column(db.Integer, nullable=False, default=30,
                                 server_default=db.text("30"))
    updated_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CRAutoSetting enabled={self.enabled} folder={self.folder!r}>"


class CRAutoFile(db.Model):
    """Ledger of source PDFs already ingested by the CR-book folder watcher so a
    file is never picked up twice.  Identity is the absolute source path plus its
    size and modified-time: an edited/replaced file (changed size or mtime) is
    treated as new and re-ingested."""

    __tablename__ = "cr_auto_files"

    id = db.Column(db.Integer, primary_key=True)
    source_path = db.Column(db.String(700), nullable=False, index=True)
    size = db.Column(db.BigInteger, nullable=False, default=0)
    mtime = db.Column(db.BigInteger, nullable=False, default=0)
    upload_id = db.Column(
        db.Integer, db.ForeignKey("cr_book_uploads.id", ondelete="SET NULL"),
        nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("source_path", "size", "mtime",
                            name="uq_cr_auto_file_identity"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CRAutoFile {self.source_path!r}>"


class RCAutoSetting(db.Model):
    """Singleton settings for the automated receipts folder watcher: the server
    folder scanned (recursively) for new PDFs and whether the watcher is on."""

    __tablename__ = "rc_auto_settings"

    id = db.Column(db.Integer, primary_key=True)
    folder = db.Column(db.String(500), nullable=True)
    enabled = db.Column(db.Boolean, nullable=False, default=False,
                        server_default=db.text("0"))
    # Seconds to wait after a scan's picked files finish processing before the
    # next scan.
    interval_seconds = db.Column(db.Integer, nullable=False, default=30,
                                 server_default=db.text("30"))
    updated_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<RCAutoSetting enabled={self.enabled} folder={self.folder!r}>"


class RCAutoFile(db.Model):
    """Ledger of source PDFs already ingested by the receipts folder watcher so a
    file is never picked up twice.  Identity is the absolute source path plus its
    size and modified-time: an edited/replaced file (changed size or mtime) is
    treated as new and re-ingested."""

    __tablename__ = "rc_auto_files"

    id = db.Column(db.Integer, primary_key=True)
    source_path = db.Column(db.String(700), nullable=False, index=True)
    size = db.Column(db.BigInteger, nullable=False, default=0)
    mtime = db.Column(db.BigInteger, nullable=False, default=0)
    upload_id = db.Column(
        db.Integer, db.ForeignKey("receipt_uploads.id", ondelete="SET NULL"),
        nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("source_path", "size", "mtime",
                            name="uq_rc_auto_file_identity"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<RCAutoFile {self.source_path!r}>"
