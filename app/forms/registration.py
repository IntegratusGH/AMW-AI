"""Upload forms for the Registration Updater module."""
from __future__ import annotations

from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileRequired, FileSize
from wtforms import SubmitField

# Accepted document types and the per-file size cap (matches the app's global
# MAX_CONTENT_LENGTH ceiling; see Config.MAX_DOC_UPLOAD_MB).
_ALLOWED = ["pdf", "png", "jpg", "jpeg", "tif", "tiff"]
_MAX_BYTES = 30 * 1024 * 1024
_VALIDATORS = [
    FileRequired(),
    FileAllowed(_ALLOWED, "Upload a PDF or scanned image (pdf, png, jpg, tiff)."),
    FileSize(max_size=_MAX_BYTES, message="File must be 30 MB or smaller."),
]


class CRBookUploadForm(FlaskForm):
    file = FileField("CR Book file", validators=_VALIDATORS)
    submit_cr = SubmitField("Upload CR Book")


class ReceiptUploadForm(FlaskForm):
    file = FileField("Receipt file", validators=_VALIDATORS)
    submit_rc = SubmitField("Upload Receipts")
