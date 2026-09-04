"""Custom WTForms validators."""
from __future__ import annotations

import re

from wtforms.validators import ValidationError

_PATTERNS = (
    (re.compile(r"[a-z]"), "a lowercase letter"),
    (re.compile(r"[A-Z]"), "an uppercase letter"),
    (re.compile(r"[0-9]"), "a digit"),
    (re.compile(r"[^A-Za-z0-9]"), "a special character"),
)


class StrongPassword:
    """Enforce a reasonable password policy."""

    def __init__(self, min_length: int = 8):
        self.min_length = min_length

    def __call__(self, form, field):
        value = field.data or ""
        if len(value) < self.min_length:
            raise ValidationError(
                f"Password must be at least {self.min_length} characters long."
            )
        missing = [label for pattern, label in _PATTERNS if not pattern.search(value)]
        if missing:
            raise ValidationError("Password must contain " + ", ".join(missing) + ".")
