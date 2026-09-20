"""Upload forms for the Registration Updater module."""
from __future__ import annotations

from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileRequired, FileSize
from wtforms import BooleanField, SubmitField

from app.config import Config

# Accepted document types and the per-file size cap (single source of truth:
# Config.MAX_DOC_UPLOAD_MB, which also sizes the global MAX_CONTENT_LENGTH).
_ALLOWED = ["pdf", "png", "jpg", "jpeg", "tif", "tiff"]
_MAX_MB = Config.MAX_DOC_UPLOAD_MB
_MAX_BYTES = _MAX_MB * 1024 * 1024
_VALIDATORS = [
    FileRequired(),
    FileAllowed(_ALLOWED, "Upload a PDF or scanned image (pdf, png, jpg, tiff)."),
    FileSize(max_size=_MAX_BYTES,
             message=f"File must be {_MAX_MB} MB or smaller."),
]


class CRBookUploadForm(FlaskForm):
    file = FileField("CR Book file", validators=_VALIDATORS)
    # When ticked, the upload runs the format detection/review pass first;
    # otherwise processing (OCR) starts immediately.
    check_format = BooleanField("Check Format")
    submit_cr = SubmitField("Upload CR Book")


class ReceiptUploadForm(FlaskForm):
    file = FileField("Receipt file", validators=_VALIDATORS)
    submit_rc = SubmitField("Upload Receipts")
