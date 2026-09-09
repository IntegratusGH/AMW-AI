"""Pure helpers for the OCR correction feedback loop.

Mine character-level confusion rules and per-field character priors from the
operator corrections captured in ``ocr_corrections``.  Kept dependency-free
(stdlib only) so it is cheap to import and easy to unit-test in isolation from
the heavy OCR pipeline.
"""
from __future__ import annotations

import difflib
from collections import Counter


def char_confusions(pairs):
    """Tally per-field character substitutions between the model's reading and
    the operator's correction.

    ``pairs`` is an iterable of ``(field, ocr_value, corrected_value)``.  Only
    unambiguous single-character substitutions (equal-length ``replace``
    segments) are counted, so each ``(ocr_char -> correct_char)`` reflects a real
    confusion rather than an alignment artefact.  Returns
    ``{field: Counter{(ocr_char, correct_char): count}}``.
    """
    per_field: dict[str, Counter] = {}
    for field, ocr_value, corrected in pairs:
        a = (ocr_value or "").strip()
        b = (corrected or "").strip()
        if not a or not b or a == b:
            continue
        counter = per_field.setdefault(field, Counter())
        sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag != "replace":
                continue
            seg_a, seg_b = a[i1:i2], b[j1:j2]
            if len(seg_a) != len(seg_b):
                continue  # length changed -> ambiguous alignment, skip
            for ca, cb in zip(seg_a, seg_b):
                if ca != cb:
                    counter[(ca, cb)] += 1
    return per_field


def top_confusions(per_field, limit=20):
    """Flatten ``{field: Counter}`` into display rows sorted most-frequent first:
    ``[{"field", "ocr", "correct", "count"}, ...]``."""
    rows = []
    for field, counter in per_field.items():
        for (a, b), n in counter.items():
            rows.append({"field": field, "ocr": a, "correct": b, "count": n})
    rows.sort(key=lambda r: r["count"], reverse=True)
    return rows[:limit] if limit else rows


def field_char_priors(items, limit=12):
    """Per-field character frequency over the operator-verified values - a simple
    prior over which characters actually occur in each field.  ``items`` is an
    iterable of ``(field, corrected_value)``.  Returns
    ``{field: [(char, count), ...]}`` most-frequent first (top ``limit``)."""
    per_field: dict[str, Counter] = {}
    for field, value in items:
        for ch in (value or "").strip():
            if ch == " ":
                continue
            per_field.setdefault(field, Counter())[ch] += 1
    return {f: c.most_common(limit) for f, c in per_field.items()}
