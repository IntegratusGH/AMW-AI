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
    needs_review = db.Column(db.Boolean, default=False, nullable=False)
    raw_text = db.Column(db.Text, nullable=True)
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

    is_duplicate = db.Column(db.Boolean, default=False, nullable=False)
    # Set when the Total/Charges/Bank Total figures do not balance (or a value
    # had to be derived/corrected), so an operator verifies the amounts.
    needs_review = db.Column(db.Boolean, default=False, nullable=False)
    confidence = db.Column(db.Float, nullable=True)
    raw_text = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    upload = db.relationship("ReceiptUpload", back_populates="records")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ReceiptRecord {self.id} slip={self.slip_no}>"
