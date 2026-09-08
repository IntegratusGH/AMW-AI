"""Offline OCR + field extraction for the Registration Updater module.

Rendering uses PyMuPDF; OCR uses RapidOCR (ONNX PP-OCR) which is fully offline
(models ship inside the wheel).  Both are imported lazily so the rest of the
app (and the test suite) does not require these heavy packages to be present.

Extraction is best-effort: the source documents are scanned, bilingual
(Sinhala/Tamil/English) forms, so field parsing keys off the English label
fragments and spatial layout.  The full OCR text is always kept in ``raw_text``
so nothing is lost, and ``confidence`` records the mean OCR score.
"""
from __future__ import annotations

import re
import threading
from decimal import Decimal, InvalidOperation

# Heavy engines are created lazily and shared; OCR is serialised with a lock
# (one document is processed at a time in a background thread).
_engine = None
_engine_lock = threading.Lock()


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


# ---------------------------------------------------------------------------
# Rendering + OCR primitives
# ---------------------------------------------------------------------------
def _render_page_png(page, zoom: float) -> bytes:
    import pymupdf
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
    return pix.tobytes("png")


def _ocr(png_bytes: bytes) -> list[dict]:
    """Return OCR lines as dicts: text, score, x0/y0/x1/y1, cx/cy."""
    engine = _get_engine()
    with _engine_lock:
        result, _ = engine(png_bytes)
    lines: list[dict] = []
    for item in (result or []):
        box, text, score = item[0], item[1], item[2]
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        try:
            sc = float(score)
        except (TypeError, ValueError):
            sc = 0.0
        lines.append({
            "text": (text or "").strip(),
            "score": sc,
            "x0": min(xs), "y0": min(ys), "x1": max(xs), "y1": max(ys),
            "cx": sum(xs) / 4.0, "cy": sum(ys) / 4.0,
        })
    lines.sort(key=lambda d: (round(d["y0"]), d["x0"]))
    return lines


def _norm(s: str) -> str:
    """Lowercase, alphanumerics only -- for fuzzy label matching."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _mean_score(lines: list[dict]) -> float | None:
    scores = [ln["score"] for ln in lines if ln.get("score")]
    return round(sum(scores) / len(scores), 4) if scores else None


# ---------------------------------------------------------------------------
# CR book extraction (one vehicle per PDF page)
# ---------------------------------------------------------------------------
# English label fragments (already normalised) used to locate each field's
# value, which on this form sits just below (or to the right of) the label.
_CR_LABELS = {
    "registration_no": ["registrationno"],
    "chassis_no": ["chassisno"],
    "engine_no": ["engineno"],
    "current_owner": ["currentow", "currentowher", "currentowner"],
    "absolute_owner": ["absoluteow", "absoluteowner"],
    "cylinder_capacity": ["cylindercap"],
    "class_of_vehicle": ["classofvehic"],
    "fuel_type": ["fueltype"],
    "year_of_manufacture": ["yearofmanufact", "arofmanufact"],
    "colour": ["colour"],
    "make": ["make"],
    "model": ["model"],
    "country_of_origin": ["countryoforig", "untryoforig"],
    "provincial_council": ["provincialcoun", "rovincialcoun"],
    "date_first_registration": ["firstregistra", "dateoffirstreg"],
}

# Any line whose normalised text contains one of these is itself a label and so
# must never be taken as a value.
_ALL_LABEL_FRAGMENTS = [f for frags in _CR_LABELS.values() for f in frags] + [
    "conditions", "specialnotes", "taxationclass", "statuswhenreg",
    "manufacturesdesc", "wheelbase", "overhang", "typeofbody", "previousowner",
    "seatingcapacity", "weight", "tyresize", "length", "provinciat",
    "taxespayable", "certificate", "commissioner", "referencedate",
]


def _looks_like_label(line: dict) -> bool:
    n = _norm(line["text"])
    return any(frag in n for frag in _ALL_LABEL_FRAGMENTS)


def _value_below(lines: list[dict], label: dict, max_dy: float = 90.0) -> str | None:
    """Nearest non-label line beneath the label with horizontal overlap."""
    best, best_dy = None, None
    for ln in lines:
        dy = ln["cy"] - label["cy"]
        if dy <= 2 or dy > max_dy:
            continue
        # horizontal overlap with the label column
        if ln["x1"] < label["x0"] - 20 or ln["x0"] > label["x1"] + 200:
            continue
        if _looks_like_label(ln) or not ln["text"]:
            continue
        if best_dy is None or dy < best_dy:
            best, best_dy = ln, dy
    return best["text"].strip() if best else None


def _find_label(lines: list[dict], fragments: list[str]) -> dict | None:
    for ln in lines:
        n = _norm(ln["text"])
        if any(frag in n for frag in fragments):
            return ln
    return None


def _clean(v: str | None) -> str | None:
    if not v:
        return None
    v = re.sub(r"\s{2,}", " ", v).strip(" .:-/")
    return v or None


def _parse_cr_page(lines: list[dict]) -> dict:
    rec: dict = {}
    for field, frags in _CR_LABELS.items():
        lbl = _find_label(lines, frags)
        if not lbl:
            continue
        rec[field] = _clean(_value_below(lines, lbl))

    full = "\n".join(ln["text"] for ln in lines)

    # current owner may span two lines (name + address)
    owner_lbl = _find_label(lines, _CR_LABELS["current_owner"])
    if owner_lbl:
        below = [ln for ln in lines
                 if 2 < (ln["cy"] - owner_lbl["cy"]) <= 140
                 and not _looks_like_label(ln) and ln["text"]]
        below.sort(key=lambda d: d["cy"])
        if below:
            rec["current_owner"] = _clean(below[0]["text"])
            if len(below) > 1:
                rec["current_owner_address"] = _clean(
                    " ".join(b["text"] for b in below[1:3]))

    # top-of-page CR number (e.g. "LOT FR-0249364") and reference date
    m = re.search(r"l[o0]t\s*fr[\s-]*([0-9osl]{5,8})", full, re.I)
    if m:
        rec["cr_number"] = "LOT FR-" + m.group(1).upper()
    m = re.search(r"reference\s*date\s*[:.]?\s*([0-9]{6,8})", full, re.I)
    if m:
        rec["date_first_registration"] = rec.get("date_first_registration") or m.group(1)

    # light validation / cleanup on key identifiers
    if rec.get("chassis_no"):
        m = re.search(r"[A-Z0-9]{8,20}", rec["chassis_no"].replace(" ", ""))
        rec["chassis_no"] = m.group(0) if m else rec["chassis_no"]
    if rec.get("year_of_manufacture"):
        m = re.search(r"(19|20)\d{2}", rec["year_of_manufacture"])
        rec["year_of_manufacture"] = m.group(0) if m else None

    rec["raw_text"] = full
    rec["confidence"] = _mean_score(lines)
    return rec


def extract_cr_records(path: str, zoom: float = 3.0) -> tuple[int, list[dict]]:
    import pymupdf
    doc = pymupdf.open(path)
    page_count = doc.page_count
    records: list[dict] = []
    try:
        for i in range(page_count):
            lines = _ocr(_render_page_png(doc[i], zoom))
            if not lines:
                continue
            rec = _parse_cr_page(lines)
            rec["page_no"] = i + 1
            records.append(rec)
    finally:
        doc.close()
    return page_count, records


# ---------------------------------------------------------------------------
# Receipt extraction (many bank deposit slips per file)
# ---------------------------------------------------------------------------
_SLIP_RE = re.compile(r"^0\d{6}$")
_AMOUNT_RE = re.compile(r"\d[\d,]*\.\d{2}")


def _to_decimal(s: str | None):
    if not s:
        return None
    try:
        return Decimal(s.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None


def _amount_after(seg: list[dict], fragments: list[str]):
    """First amount on, or just below, a line matching one of the fragments."""
    for i, ln in enumerate(seg):
        n = _norm(ln["text"])
        if any(f in n for f in fragments):
            m = _AMOUNT_RE.search(ln["text"])
            if m:
                return _to_decimal(m.group(0))
            for nxt in seg[i + 1:i + 3]:
                m = _AMOUNT_RE.search(nxt["text"])
                if m:
                    return _to_decimal(m.group(0))
    return None


def _parse_receipt_segment(seg: list[dict]) -> dict:
    text = "\n".join(ln["text"] for ln in seg)
    rec: dict = {"raw_text": text, "confidence": _mean_score(seg)}

    for ln in seg:
        if _SLIP_RE.match(ln["text"].strip()):
            rec["slip_no"] = ln["text"].strip()
            break

    m = re.search(r"\b(\d{2}:\d{2}:\d{2})\b", text)
    if m:
        rec["receipt_time"] = m.group(1)
    m = re.search(r"\b(\d{2})[/\s.]{0,2}(\d{2})[/\s.]{0,2}(\d{4})\b", text)
    if m:
        rec["receipt_date"] = f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
    m = re.search(r"\b([A-Z]{2,3}-\d{3,4}[A-Z]?)\b", text)
    if m:
        rec["vehicle_no"] = m.group(1)
    m = re.search(r"\b(RK\d{6,})\b", text, re.I)
    if m:
        rec["application_no"] = m.group(1).upper()

    # receipt no: small integer on the line after the "Receipt No" label
    for i, ln in enumerate(seg):
        if "receiptno" in _norm(ln["text"]):
            for nxt in seg[i + 1:i + 3]:
                mm = re.search(r"\b(\d{1,5})\b", nxt["text"])
                if mm:
                    rec["receipt_no"] = mm.group(1)
                    break
            break

    rec["bank_charges"] = _amount_after(seg, ["servicecharg", "charges"])
    rec["bank_total"] = _amount_after(seg, ["banktotal", "anetotal", "tanktatal"])
    rec["total_amount"] = _amount_after(seg, ["rmvtotal", "rhvtotal", "rwvtotal"]) \
        or _amount_after(seg, ["amount", "cash"])
    return rec


def _segment_receipts(lines: list[dict]) -> list[list[dict]]:
    """Split OCR lines into per-receipt groups anchored on the slip number."""
    anchors = [i for i, ln in enumerate(lines) if _SLIP_RE.match(ln["text"].strip())]
    if not anchors:
        return [lines] if lines else []
    segments = []
    for idx, start in enumerate(anchors):
        end = anchors[idx + 1] if idx + 1 < len(anchors) else len(lines)
        # include a little context above the anchor (header lines)
        ctx = max(0, start - 6) if idx == 0 else anchors[idx - 1] + 1
        seg_start = ctx if idx == 0 else start
        segments.append(lines[seg_start:end])
    return segments


def extract_receipt_records(path: str, zoom: float = 2.0) -> tuple[int, list[dict]]:
    import pymupdf
    doc = pymupdf.open(path)
    page_count = doc.page_count
    records: list[dict] = []
    seg_no = 0
    try:
        for i in range(page_count):
            lines = _ocr(_render_page_png(doc[i], zoom))
            for seg in _segment_receipts(lines):
                if not seg:
                    continue
                rec = _parse_receipt_segment(seg)
                # skip empty noise segments (no slip and no vehicle/application)
                if not any(rec.get(k) for k in
                           ("slip_no", "vehicle_no", "application_no", "receipt_no")):
                    continue
                seg_no += 1
                rec["segment_no"] = seg_no
                records.append(rec)
    finally:
        doc.close()
    return page_count, records
