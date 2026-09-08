"""Helpers for module icon uploads and slug generation."""
from __future__ import annotations

import os
import re
import uuid

from flask import current_app
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename


def slugify(text: str) -> str:
    """Return a lowercase underscore slug suitable for a module key."""
    slug = re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")
    return slug or "module"


def allowed_icon(filename: str) -> bool:
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in current_app.config["ALLOWED_ICON_EXTENSIONS"]


def module_icon_dir() -> str:
    d = current_app.config["MODULE_ICON_DIR"]
    os.makedirs(d, exist_ok=True)
    return d


def save_module_icon(file: FileStorage) -> str:
    """Persist an uploaded icon and return its stored filename."""
    safe = secure_filename(file.filename) or "icon"
    stored = f"{uuid.uuid4().hex}_{safe}"
    file.save(os.path.join(module_icon_dir(), stored))
    return stored


def delete_module_icon(filename: str) -> None:
    if not filename:
        return
    try:
        os.remove(os.path.join(current_app.config["MODULE_ICON_DIR"], filename))
    except OSError:
        pass
