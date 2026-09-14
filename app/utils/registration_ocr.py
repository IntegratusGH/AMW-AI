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

import json
import os
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
# Scan-enhancement toggle (rollback-safe)
# ---------------------------------------------------------------------------
# Accuracy enhancements for poor-quality (grey, low-contrast, trilingual) scans
# are all gated behind this single switch.  Set OCR_ENHANCE=0 in the .env and
# restart the service to revert to the exact prior behaviour with no rebuild.
def _enh_on() -> bool:
    return os.getenv("OCR_ENHANCE", "1").strip().lower() not in (
        "0", "no", "false", "off")


# A page whose estimated paper background (85th-percentile brightness) is at or
# above this stays untouched by flat-fielding; only measurably grey scans are
# normalised.  Clean scans (~245) sit well above the default and are never
# altered.
_OCR_BG_WHITE_MIN = float(os.getenv("OCR_BG_WHITE_MIN", "215"))


def _normalize_page_bgr(bgr):
    """Flatten uneven illumination and lift a grey background so the text
    detector sees near-white paper.  Returns the input unchanged when
    enhancement is off or the page is already clean (near-identity), so good
    scans are never degraded."""
    import cv2
    import numpy as np
    if not _enh_on():
        return bgr
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    if float(np.percentile(gray, 85)) >= _OCR_BG_WHITE_MIN:
        return bgr
    bg = cv2.medianBlur(gray, 31)
    flat = cv2.divide(gray, bg, scale=255)
    return cv2.cvtColor(flat, cv2.COLOR_GRAY2BGR)


def _latin_ratio(text: str) -> float:
    """Fraction of alphabetic characters that are ASCII (Latin).  Trilingual CR
    labels rendered as Sinhala/Tamil noise score low; English values score 1.0.
    Returns 1.0 when there are no letters (a digits-only value is all-Latin)."""
    letters = [c for c in (text or "") if c.isalpha()]
    if not letters:
        return 1.0
    return sum(c.isascii() for c in letters) / len(letters)


def _is_script_noise(text: str) -> bool:
    """A line dominated by non-Latin (Sinhala/Tamil) script with no usable Latin
    alphanumeric run - never a field value on these English-keyed forms."""
    if _latin_ratio(text) >= 0.5:
        return False
    return not re.search(r"[A-Za-z0-9]{4,}", text or "")


# ---------------------------------------------------------------------------
# Rendering + OCR primitives
# ---------------------------------------------------------------------------
# Upload-level OCR adjustments active for the current processing thread.  Set
# by extract_cr_page() so both the coarse page render and the refinement crops
# preprocess consistently; isolated per worker thread (one thread per upload).
_pp_ctx = threading.local()


def _active_pp():
    return getattr(_pp_ctx, "pp", None)


def _page_bgr(page, pp=None, zoom: float = 2.0):
    """Render a full page to a BGR image with ``pp`` applied.  Single source of
    truth for the pp pipeline so the OCR-settings preview and the persisted
    value-region crops are produced identically (the crops are sliced out of
    this full-page render so page-adaptive steps -- Otsu/autocontrast/removebg
    -- see the whole page, exactly as the popup does)."""
    import cv2
    import numpy as np
    import pymupdf
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)
    if pix.n == 4:
        bgr = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    elif pix.n == 1:
        bgr = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    else:
        bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    if pp:
        bgr = _apply_pp(bgr, pp)
    return bgr


def _slice_png(page_bgr, rect, zoom: float):
    """Slice a PDF-point ``rect`` out of a page BGR rendered at ``zoom`` and
    encode it as PNG bytes (clamped to the image bounds).  Returns None when the
    region is empty."""
    import cv2
    h, w = page_bgr.shape[:2]
    x0 = max(0, min(w, int(round(rect.x0 * zoom))))
    y0 = max(0, min(h, int(round(rect.y0 * zoom))))
    x1 = max(0, min(w, int(round(rect.x1 * zoom))))
    y1 = max(0, min(h, int(round(rect.y1 * zoom))))
    if x1 <= x0 or y1 <= y0:
        return None
    return cv2.imencode(".png", page_bgr[y0:y1, x0:x1])[1].tobytes()


def render_page_png(page, pp=None, zoom: float = 2.0) -> bytes:
    """Render a full page as PNG with an explicit ``pp`` applied (used by the
    CR upload OCR-settings preview).  ``pp`` is a cleaned adjustment dict."""
    import cv2
    return cv2.imencode(".png", _page_bgr(page, pp, zoom))[1].tobytes()


def _encode_pix_png(pix) -> bytes:
    """Encode a rendered pixmap to PNG, applying page-normalization (when the
    OCR_ENHANCE flag is on) and any thread-local ``pp`` image adjustments."""
    pp = _active_pp()
    if not _enh_on() and not pp:
        return pix.tobytes("png")
    import cv2
    import numpy as np
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)
    if pix.n == 4:
        bgr = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    elif pix.n == 1:
        bgr = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    else:
        bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    out = _normalize_page_bgr(bgr) if _enh_on() else bgr
    if pp:
        out = _apply_pp(out, pp)
    if out is bgr:
        return pix.tobytes("png")
    return cv2.imencode(".png", out)[1].tobytes()


def _render_page_png(page, zoom: float) -> bytes:
    import pymupdf
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
    return _encode_pix_png(pix)


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


def _ocr_page(page, zoom: float = 2.0, tile_px: int = 2000,
              overlap_px: int = 250, max_px: int = 3000) -> list[dict]:
    """OCR a full page.  Very tall pages (e.g. a continuous receipt-roll strip
    with many slips stacked vertically) exceed the text detector's input size
    and get downscaled until most text is unreadable, so render and OCR them in
    overlapping vertical tiles and merge the lines into full-page pixel
    coordinates.  Normal-height pages take the single-pass path unchanged."""
    import pymupdf
    if page.rect.height * zoom <= max_px:
        return _ocr(_render_page_png(page, zoom))
    w_pt = page.rect.width
    tile_pt = tile_px / zoom
    step_pt = (tile_px - overlap_px) / zoom
    merged: list[dict] = []
    y = 0.0
    while y < page.rect.height:
        y1 = min(y + tile_pt, page.rect.height)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom),
                              clip=pymupdf.Rect(0, y, w_pt, y1))
        off = y * zoom
        for ln in _ocr(_encode_pix_png(pix)):
            ln["y0"] += off; ln["y1"] += off; ln["cy"] += off
            merged.append(ln)
        if y1 >= page.rect.height:
            break
        y += step_pt
    merged.sort(key=lambda d: (round(d["y0"]), d["x0"]))
    # Drop duplicates introduced by the tile overlaps (same text, same place).
    dedup: list[dict] = []
    for ln in merged:
        if any(o["text"] == ln["text"]
               and abs(o["cy"] - ln["cy"]) < 20 * zoom
               and abs(o["x0"] - ln["x0"]) < 30 * zoom
               for o in dedup[-40:]):
            continue
        dedup.append(ln)
    return dedup


def _get_recognizer():
    return _get_engine().text_recognizer


def _rec_only(img) -> list[tuple[str, float]]:
    """Recognition-only on a single-line crop (skips detection entirely)."""
    rec = _get_recognizer()
    with _engine_lock:
        res = rec([img])
    items = res[0] if isinstance(res, tuple) and res and isinstance(res[0], list) else res
    out: list[tuple[str, float]] = []
    for it in (items or []):
        if isinstance(it, (list, tuple)) and len(it) >= 2 and isinstance(it[0], str):
            try:
                out.append((it[0], float(it[1])))
            except (TypeError, ValueError):
                out.append((it[0], 0.0))
    return out


def _crop_bgr(page, rect, zoom: float):
    import numpy as np
    import cv2
    import pymupdf
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=rect)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        return cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    if pix.n == 1:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)


def _deskew(bgr):
    """Correct a small rotation on a text crop so the recognizer sees a level
    line.  Estimates the dominant text angle from the binarised foreground and
    only rotates for a small, safe correction (<=10 deg); otherwise returns the
    crop unchanged."""
    import cv2
    import numpy as np
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    th = cv2.threshold(gray, 0, 255,
                       cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    coords = np.column_stack(np.where(th > 0))
    if coords.shape[0] < 20:
        return bgr
    angle = cv2.minAreaRect(coords.astype(np.float32))[-1]
    if angle > 45:
        angle -= 90
    if abs(angle) < 0.3 or abs(angle) > 10:
        return bgr
    h, w = bgr.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle, 1.0)
    return cv2.warpAffine(bgr, m, (w, h), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE)


def _fit_height(img, lo: int = 24, hi: int = 64, target: int = 48):
    """Normalise a crop's height into the band the recognizer is trained on
    (~32-48 px) so neither an over-zoomed nor an under-zoomed pass feeds it
    poorly-conditioned text."""
    import cv2
    h = img.shape[0]
    if h == 0 or lo <= h <= hi:
        return img
    scale = target / float(h)
    w = max(1, int(round(img.shape[1] * scale)))
    interp = cv2.INTER_AREA if h > target else cv2.INTER_CUBIC
    return cv2.resize(img, (w, target), interpolation=interp)


def _preprocess_variants(bgr):
    """Return several renderings of a crop so systematic misreads (e.g. 5<->S)
    differ across at least one variant, giving the vote a chance to correct."""
    import cv2
    import numpy as np
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    adaptive = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10)
    sharp = cv2.filter2D(bgr, -1, np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]]))
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    variants = [
        bgr,
        cv2.cvtColor(otsu, cv2.COLOR_GRAY2BGR),
        cv2.cvtColor(adaptive, cv2.COLOR_GRAY2BGR),
        sharp,
        cv2.cvtColor(clahe, cv2.COLOR_GRAY2BGR),
    ]
    if _enh_on() and float(np.percentile(gray, 85)) < _OCR_BG_WHITE_MIN:
        # Flat-field variant: divide out the (grey) background so faint values
        # on poor scans vote alongside the others.  Only added for measurably
        # grey crops, so clean scans keep their exact prior candidate set (and
        # confidence) unchanged.
        bg = cv2.medianBlur(gray, 31)
        flat = cv2.divide(gray, bg, scale=255)
        variants.append(cv2.cvtColor(flat, cv2.COLOR_GRAY2BGR))
    return variants


# --- per-box image adjustments (trainer template settings) ----------------
# Optional pre-OCR tuning a trainer can attach to a template box to lift the
# recognizer's confidence on that specific field/scan (all keys optional; a
# neutral value leaves the crop unchanged).
_PP_KEYS = ("brightness", "contrast", "sharpness", "gamma", "scale",
            "grayscale", "invert", "denoise", "removebg", "clahe",
            "autocontrast", "morph", "colorkeep", "colorkeep_v", "fft",
            "threshold")


def _fft_descreen(gray):
    """Suppress a periodic background (guilloche) with an FFT notch filter:
    transform to the frequency domain, zero the strong off-centre peaks a
    repeating pattern produces (protecting the low-frequency text structure at
    the centre), then invert.  Returns a uint8 grayscale image."""
    import cv2
    import numpy as np
    g = gray.astype(np.float32)
    f = np.fft.fftshift(np.fft.fft2(g))
    mag = np.abs(f)
    h, w = g.shape
    cy, cx = h // 2, w // 2
    yy, xx = np.ogrid[:h, :w]
    r = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    r0 = max(8.0, 0.10 * min(h, w))          # protect DC + low freq (text)
    annulus = r > r0
    if not annulus.any():
        return gray
    # Isolated spikes (periodic guilloche) stand far above their local spectral
    # neighbourhood; broad text energy does not.  Flag only points that both
    # tower over a blurred local background AND are globally strong, so a clean
    # (non-patterned) crop is left essentially unchanged.
    bg = cv2.GaussianBlur(mag, (0, 0), 3.0)
    ratio = mag / (bg + 1e-6)
    strong = float(np.percentile(mag[annulus], 99.5))
    peaks = annulus & (ratio > 4.0) & (mag > strong)
    if not peaks.any():
        return gray
    # widen each peak so its immediate skirt is removed too, then re-protect
    # the centre so text structure is never touched.
    peaks = cv2.dilate(peaks.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    peaks &= annulus
    f[peaks] = 0
    rec = np.abs(np.fft.ifft2(np.fft.ifftshift(f)))
    return cv2.normalize(rec, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)


def clean_pp(pp) -> dict:
    """Keep only known, non-neutral adjustment keys so templates persist just
    the settings the trainer actually changed."""
    if not isinstance(pp, dict):
        return {}
    out = {}
    for k in _PP_KEYS:
        v = pp.get(k)
        if v in (None, "", "off", "Off", False):
            continue
        out[k] = v
    return out


def _apply_pp(bgr, pp: dict):
    """Apply a box's trainer-defined adjustments to a BGR crop before OCR.
    Order: brightness/contrast -> gamma -> remove-background -> CLAHE ->
    auto-contrast -> FFT de-screen -> colour-keep -> morphological open ->
    denoise -> sharpen -> grayscale/threshold -> invert.  Always returns a
    3-channel BGR image."""
    import cv2
    import numpy as np
    img = bgr

    def _f(key, dflt):
        try:
            return float(pp.get(key, dflt))
        except (TypeError, ValueError):
            return dflt

    def _b(key):
        v = pp.get(key)
        return v is True or str(v).strip().lower() in ("1", "true", "yes", "on")

    alpha = max(0.0, 1.0 + _f("contrast", 0.0) / 100.0)   # -100..100
    beta = _f("brightness", 0.0) * 1.27                   # -100..100
    if alpha != 1.0 or beta != 0.0:
        img = cv2.convertScaleAbs(img, alpha=alpha, beta=beta)
    gamma = _f("gamma", 1.0)
    if gamma > 0 and abs(gamma - 1.0) > 1e-3:
        table = np.array([((i / 255.0) ** (1.0 / gamma)) * 255
                          for i in range(256)], dtype=np.uint8)
        img = cv2.LUT(img, table)
    if _b("removebg"):
        # Flatten uneven paper/shadow: estimate the background by morphological
        # closing (fills dark strokes with surrounding paper) then divide it
        # out, leaving text on clean white.  Kernel ~ crop height so strokes
        # vanish but the illumination gradient survives the estimate.
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        ksz = int(max(9, min(gray.shape[0], gray.shape[1])))
        ksz = min(ksz | 1, 51)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ksz, ksz))
        bg = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, kernel)
        flat = cv2.divide(gray, bg, scale=255)
        img = cv2.cvtColor(flat, cv2.COLOR_GRAY2BGR)
    if _b("clahe"):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        eq = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
        img = cv2.cvtColor(eq, cv2.COLOR_GRAY2BGR)
    if _b("autocontrast"):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        eq = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
        img = cv2.cvtColor(eq, cv2.COLOR_GRAY2BGR)
    if _b("fft"):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        img = cv2.cvtColor(_fft_descreen(gray), cv2.COLOR_GRAY2BGR)
    if _b("colorkeep"):
        # Keep near-black, low-saturation pixels (the text) and drop lighter or
        # coloured guilloche.  ``colorkeep_v`` is the max brightness (V) kept.
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        vmax = _f("colorkeep_v", 130.0)
        if vmax <= 0:
            vmax = 130.0
        mask = (hsv[:, :, 2] <= vmax) & (hsv[:, :, 1] <= 90)
        out = np.where(mask, 0, 255).astype(np.uint8)
        img = cv2.cvtColor(out, cv2.COLOR_GRAY2BGR)
    morph = int(_f("morph", 0.0))
    if morph > 0:
        # Opening on the dark=foreground text mask with a small kernel erases
        # lines thinner than the kernel (guilloche) while keeping thicker text.
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        _, fg = cv2.threshold(gray, 0, 255,
                              cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        k = max(1, min(morph, 7))
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        opened = cv2.morphologyEx(fg, cv2.MORPH_OPEN, kernel)
        img = cv2.cvtColor(255 - opened, cv2.COLOR_GRAY2BGR)
    if _b("denoise"):
        img = cv2.medianBlur(img, 3)
    sharp = _f("sharpness", 0.0)                          # 0..3
    if sharp > 0:
        blur = cv2.GaussianBlur(img, (0, 0), 1.0)
        img = cv2.addWeighted(img, 1.0 + sharp, blur, -sharp, 0)
    th = pp.get("threshold")
    ths = "" if th is None else str(th).strip().lower()
    if ths and ths != "off":
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if ths == "auto":
            _, bw = cv2.threshold(gray, 0, 255,
                                  cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        elif ths == "adaptive":
            bw = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY, 31, 10)
        else:
            try:
                tval = int(float(th))
            except (TypeError, ValueError):
                tval = 128
            _, bw = cv2.threshold(gray, max(0, min(255, tval)), 255,
                                  cv2.THRESH_BINARY)
        img = cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR)
    elif _b("grayscale"):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        img = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    if _b("invert"):
        img = 255 - img
    return img


def _rec_candidates(page, rect, zooms=(4, 6, 9), pp=None) -> list[tuple[str, float]]:
    """Recognize a cropped region at several zooms x preprocessing variants.
    Each crop is deskewed and height-normalised so the recognizer sees text at
    a consistent, well-conditioned scale.  ``pp`` applies a box's trainer-
    defined adjustments (and its ``scale`` multiplies the render zooms)."""
    if pp is None:
        pp = _active_pp()
    zs = zooms
    if pp and pp.get("scale"):
        try:
            sc = float(pp["scale"])
            if sc and sc != 1.0:
                zs = tuple(max(1.0, z * sc) for z in zooms)
        except (TypeError, ValueError):
            pass
    out: list[tuple[str, float]] = []
    for z in zs:
        base = _deskew(_crop_bgr(page, rect, z))
        if pp:
            base = _apply_pp(base, pp)
        for im in _preprocess_variants(base):
            out.extend(_rec_only(_fit_height(im)))
    return out


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
    # Tolerate OCR misreads of the trailing "No." (e.g. "Chassis Na.") and of
    # the field word itself ("Registratlon", "Registratian", "Registrationt")
    # by keying on the stable stem.  "registrat" also appears in the page title
    # "CERTIFICATE OF REGISTRATION OF MOTOR VEHICLE"; that line is filtered out
    # via _CR_LABEL_EXCLUDE below.
    "registration_no": ["registrat"],
    "chassis_no": ["chassis"],
    "engine_no": ["engine"],
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

# Per-field label lines to skip while matching (normalised substrings).  The
# broad "registrat" stem would otherwise catch the "CERTIFICATE OF REGISTRATION
# OF MOTOR VEHICLE" title, which sits above the real "Registration No." label.
_CR_LABEL_EXCLUDE = {
    "registration_no": ["certificate", "motorveh"],
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
    if any(frag in n for frag in _ALL_LABEL_FRAGMENTS):
        return True
    # Non-Latin (Sinhala/Tamil) label noise is never a value on these forms.
    return bool(_enh_on() and _is_script_noise(line["text"]))


# --- CR-book format detection --------------------------------------------
# The core identity labels a genuine CR book always carries.  A page with
# fewer than _CR_MIN_CORE of these present is treated as "not a CR book".
_CR_CORE_LABELS = ["registration_no", "chassis_no", "engine_no",
                   "absolute_owner", "class_of_vehicle"]
_CR_MIN_CORE = 2
# Similarity thresholds for matching a page to a known format's signature.
_FMT_MATCH_THRESHOLD = 0.55
_FMT_POS_TOL = 0.10


def cr_page_signature(lines: list[dict], page_w: float, page_h: float) -> dict:
    """Signature of a CR page: which field labels are present and where (each
    label centre as a fraction of page size).  Used to match a page to a
    format and to seed a new format the operator names."""
    labels: list[str] = []
    pos: dict[str, list[float]] = {}
    pw = page_w or 1.0
    ph = page_h or 1.0
    for key, frags in _CR_LABELS.items():
        lbl = _find_label(lines, frags, _CR_LABEL_EXCLUDE.get(key))
        if lbl:
            labels.append(key)
            pos[key] = [round(lbl["cx"] / pw, 4), round(lbl["cy"] / ph, 4)]
    return {"labels": labels, "pos": pos}


def detect_cr_page(page, zoom: float = 2.0) -> dict:
    """Fast label-only pass classifying a single page for the review step.
    Returns {signature, core_hits, is_cr}."""
    lines = _ocr_page(page, zoom)
    sig = cr_page_signature(
        lines, page.rect.width * zoom, page.rect.height * zoom)
    core = sum(1 for k in _CR_CORE_LABELS if k in sig["labels"])
    return {"signature": sig, "core_hits": core, "is_cr": core >= _CR_MIN_CORE}


def format_match_score(page_sig: dict, fmt_sig: dict) -> float:
    """0..1 similarity between a page signature and a format signature.  Label
    overlap (Jaccard) for a format with no learned positions; a blend of label
    overlap and positional agreement once positions are known, so two layouts
    sharing field names but differing in placement are still told apart."""
    pl = set(page_sig.get("labels") or [])
    fl = set(fmt_sig.get("labels") or [])
    if not pl or not fl:
        return 0.0
    jac = len(pl & fl) / len(pl | fl)
    fpos = fmt_sig.get("pos") or {}
    if not fpos:
        return jac
    ppos = page_sig.get("pos") or {}
    matched = 0
    for k in (pl & fl):
        a, b = fpos.get(k), ppos.get(k)
        if a and b and abs(a[0] - b[0]) <= _FMT_POS_TOL \
                and abs(a[1] - b[1]) <= _FMT_POS_TOL:
            matched += 1
    return 0.5 * jac + 0.5 * (matched / len(fl))


def classify_cr_page(detect: dict, formats: list[tuple[str, dict]]):
    """Return (status, format_key): known / unknown_format / not_cr.  ``formats``
    is a list of (key, signature_dict) for the active known formats."""
    if not detect.get("is_cr"):
        return ("not_cr", None)
    best_key, best_score = None, 0.0
    for key, sig in formats:
        s = format_match_score(detect["signature"], sig)
        if s > best_score:
            best_key, best_score = key, s
    if best_key and best_score >= _FMT_MATCH_THRESHOLD:
        return ("known", best_key)
    return ("unknown_format", None)


# Per-format OCR rule sets.  "oldcrbook" is the current, tuned extractor; a
# newly detected format uses "_generic" (same pipeline) until dedicated rules
# are written for it and added here keyed by the format's slug.
CR_RULE_SETS = {
    "oldcrbook": {"labels": _CR_LABELS},
    "_generic": {"labels": _CR_LABELS},
}


def get_rule_set(key: str | None) -> dict:
    return CR_RULE_SETS.get(key or "", CR_RULE_SETS["_generic"])


def _line_below(lines: list[dict], label: dict, max_dy: float = 90.0) -> dict | None:
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
    return best


def _value_below(lines: list[dict], label: dict, max_dy: float = 90.0) -> str | None:
    best = _line_below(lines, label, max_dy)
    return best["text"].strip() if best else None


def _line_same_row(lines: list[dict], label: dict) -> dict | None:
    """Nearest non-label value box on the same row as the label - used as a
    fallback when nothing is detected directly below it, as on trilingual forms
    where the value sits beside (or fused with) the label row."""
    band = max(0.6 * (label["y1"] - label["y0"]), 12.0)
    best, best_dx = None, None
    for ln in lines:
        if ln is label or not ln["text"]:
            continue
        if abs(ln["cy"] - label["cy"]) > band:
            continue
        if _looks_like_label(ln):
            continue
        dx = abs(ln["cx"] - label["cx"])
        if best_dx is None or dx < best_dx:
            best, best_dx = ln, dx
    return best


def _find_label(lines: list[dict], fragments: list[str],
                exclude: list[str] | None = None) -> dict | None:
    for ln in lines:
        n = _norm(ln["text"])
        if exclude and any(x in n for x in exclude):
            continue
        if any(frag in n for frag in fragments):
            return ln
    return None


def _clean(v: str | None) -> str | None:
    if not v:
        return None
    v = re.sub(r"\s{2,}", " ", v).strip(" .:-/")
    return v or None


# A VIN / chassis number never contains I, O or Q -- map common misreads.
_VIN_FIX = str.maketrans({"I": "1", "O": "0", "Q": "0"})


def _norm_chassis(t: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", (t or "")).upper().translate(_VIN_FIX)


def _norm_reg(t: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "", (t or "")).upper()


def _norm_engine(t: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", (t or "")).upper()


# Structure validators used to *boost* (never remove) OCR candidates that match
# each field's known shape, nudging the vote toward well-formed readings.
def _chassis_valid(t: str) -> bool:
    return len(t) == 17 and not any(c in "IOQ" for c in t)


def _reg_valid(t: str) -> bool:
    s = t.replace("-", "")
    return bool(re.fullmatch(r"[A-Z]{2,5}\d{1,4}", s)
                or re.fullmatch(r"\d{2,3}\d{4}", s))


def _engine_valid(t: str) -> bool:
    return 5 <= len(t) <= 17 and any(c.isdigit() for c in t)


def _reg_wellformed(s: str) -> bool:
    """Province-aware plate check used to choose between a coarse and a refined
    reading (stricter than _reg_valid, which is structure-only): a province-
    prefix plate must carry a real 2-letter province code."""
    s = (s or "").upper()
    m = re.fullmatch(r"([A-Z]{2}) [A-Z]{2,3}-\d{1,4}", s)
    if m:
        return m.group(1) in _LK_PROVINCES
    return bool(re.fullmatch(r"[A-Z]{2,3}-\d{1,4}", s)
                or re.fullmatch(r"\d{1,3}-\d{4}", s))


def _prefer_coarse(refined, r_conf, coarse, c_conf, wellformed):
    """Choose between the refined (multi-zoom crop) reading and the coarse full-
    page reading.  A high-zoom crop can amplify noise on faint text and vote a
    consistent-but-malformed value (e.g. a plate whose 2-letter prefix is not a
    real province); when the refined reading is malformed and the coarse one is
    well-formed, keep the coarse reading.  Otherwise keep the refined reading,
    which corrects the systematic digit/letter confusions the single full-page
    pass makes.  Returns ``(text, conf)``."""
    if refined and coarse and refined != coarse \
            and wellformed(coarse) and not wellformed(refined):
        return coarse, c_conf
    return refined, r_conf


def _vote(cands: list[tuple[str, float]], prefer_len: int | None = None,
         validator=None):
    """Length + per-character weighted majority vote across OCR passes.

    Groups candidates by length (with an optional prior for ``prefer_len``),
    then for each position picks the highest-weight character.  When a
    ``validator`` is given, candidates matching the field's structure are
    up-weighted (never dropped) so a well-formed reading wins ties.  Returns
    ``(text, confidence)`` where confidence blends mean OCR score, per-character
    agreement and length agreement."""
    from collections import defaultdict

    cands = [(t, w) for t, w in cands if t]
    if not cands:
        return None, 0.0
    if validator is not None:
        cands = [(t, w * 1.25 if validator(t) else w) for t, w in cands]
    lenw: dict[int, float] = defaultdict(float)
    for t, w in cands:
        lenw[len(t)] += w
    if prefer_len and prefer_len in lenw:
        lenw[prefer_len] *= 1.15
    best_len = max(lenw, key=lambda k: lenw[k])
    grp = [(t, w) for t, w in cands if len(t) == best_len]
    total_all = sum(w for _, w in cands) or 1.0
    len_agree = sum(w for _, w in grp) / total_all
    chars, agrees = [], []
    for i in range(best_len):
        cw: dict[str, float] = defaultdict(float)
        for t, w in grp:
            cw[t[i]] += w
        ch = max(cw, key=lambda k: cw[k])
        tot = sum(cw.values()) or 1.0
        chars.append(ch)
        agrees.append(cw[ch] / tot)
    char_agree = sum(agrees) / len(agrees) if agrees else 0.0
    mean_score = sum(w for _, w in grp) / len(grp) if grp else 0.0
    # ``mean_score`` can exceed 1.0 when validator up-weighting is applied; clamp
    # so confidence stays in [0, 1] for the review thresholds.  Length
    # disagreement across passes (common for variable-length fields like engine
    # numbers, where a trailing digit is sometimes dropped/added) should discount
    # confidence, not annihilate it - per-character agreement already captures
    # how reliable the chosen reading is - so cap the length penalty at 50%.
    conf = round(min(mean_score, 1.0) * char_agree * (0.5 + 0.5 * len_agree), 4)
    return "".join(chars), conf


def _chassis_review(ch: str | None, conf) -> bool:
    """Flag a chassis for human check: low confidence, non-17 length, or a
    digit-confusable letter (S/B/G/Z/D) in the numeric serial tail."""
    if not ch or conf is None:
        return True
    if len(ch) != 17 or conf < 0.70:
        return True
    return any(c in "SBGZD" for c in ch[-7:])


def _reg_review(rg: str | None, conf) -> bool:
    """Flag a registration no for check unless it is a valid Sri Lankan plate
    with adequate confidence: modern '[A-Z]{2,5}-<digits>' (a 4-5 letter block
    must start with a known province code) or legacy numeric 'NN-NNNN'."""
    if not rg or conf is None or conf < 0.55:
        return True
    s = rg.upper()
    # Province-prefix: 'PP LLL-NNNN' (province must be a known code).
    m = re.fullmatch(r"([A-Z]{2}) [A-Z]{2,3}-\d{1,4}", s)
    if m:
        return m.group(1) not in _LK_PROVINCES
    # National series 'LLL-NNNN' (2-3 letters) or legacy numeric 'NN-NNNN'.
    if re.fullmatch(r"[A-Z]{2,3}-\d{1,4}", s) or re.fullmatch(r"\d{1,3}-\d{4}", s):
        return False
    return True


def _engine_review(en: str | None, conf) -> bool:
    """Flag an engine no for human check on low confidence, an implausible
    length, or a value with no digits (engine serials always carry digits).
    The threshold is lower than the other fields because engine numbers have no
    fixed structure to validate against, so their vote confidence runs lower."""
    if not en or conf is None or conf < 0.55:
        return True
    if not (5 <= len(en) <= 17):
        return True
    return not any(c.isdigit() for c in en)


# Well-known OCR digit<->letter confusions (letter -> the digit it is usually a
# misread of).  Used to correct characters in positions that are structurally
# numeric (chassis serial tail, registration plate-number group).
_DIGIT_MAP = {"S": "5", "B": "8", "G": "6", "Z": "2",
              "O": "0", "I": "1", "D": "0", "Q": "0"}


def _to_digits(s: str) -> str:
    return "".join(_DIGIT_MAP.get(c, c) for c in s)


# Inverse map (digit -> the letter it is usually a misread of), used to correct
# the alphabetic zone of a Sri Lankan plate (province + class letters).
_LETTER_MAP = {"0": "O", "1": "I", "2": "Z", "4": "A",
               "5": "S", "6": "G", "8": "B"}


def _to_letters(s: str) -> str:
    return "".join(_LETTER_MAP.get(c, c) for c in s)


# Two-letter provincial identification codes used on Sri Lankan plates
# (2000-2022), plus the legacy transitional NE code.
_LK_PROVINCES = {"WP", "CP", "SP", "NP", "EP", "NW", "NC", "UP", "SG", "NE"}


def _fix_reg_number(rg: str | None) -> str | None:
    """Map digit-confusable letters -> digits in the trailing plate-number
    group (which is always numeric), e.g. 'WPBLM-1S21' -> 'WPBLM-1521'."""
    if not rg:
        return rg
    m = re.search(r"([0-9SBGZOIDQ]{2,4})$", rg)
    if not m:
        return rg
    fixed = _to_digits(m.group(1))
    return rg[:m.start(1)] + fixed if fixed.isdigit() else rg


def _fix_reg_lk(rg: str | None) -> str | None:
    """Normalise a captured value to the Sri Lankan plate format: province-prefix
    plates as 'PP LLL-NNNN' (e.g. 'NW BLM-1516'), the national series as
    'LLL-NNNN' (e.g. 'CAB-1234') and legacy plates as numeric 'NN-NNNN'.  OCR
    confusions are corrected per zone - digit-confusable letters in the serial
    become digits, letter-confusable digits in the alphabetic block become
    letters."""
    if not rg:
        return rg
    s = re.sub(r"[^A-Za-z0-9-]", "", rg).upper()
    if "-" in s:
        left, right = s.rsplit("-", 1)
    else:
        m = re.search(r"([0-9]{1,4})$", s)
        left, right = (s[:m.start()], m.group(1)) if (m and m.start()) else (s, "")
    left = left.replace("-", "")
    right = right.replace("-", "")
    num = _to_digits(right)
    # Purely-numeric leading zone => legacy numeric province code; otherwise it
    # is the alphabetic (province + class letters) block.
    lead = _to_digits(left) if (left and not any(c.isalpha() for c in left)) \
        else _to_letters(left)
    if lead and num:
        # Province-prefix plate: separate the 2-letter province from the class
        # letters with a space (e.g. 'NWBLM' + '1516' -> 'NW BLM-1516').
        if len(lead) >= 4 and lead[:2] in _LK_PROVINCES:
            return f"{lead[:2]} {lead[2:]}-{num}"
        return f"{lead}-{num}"
    return (lead + num) or s


def build_chassis_mask(cand_lists, min_records: int = 4, min_len: int = 11):
    """Learn a chassis 'template' from a whole batch.  For each position it
    records, across every recognition pass of every record whose length matches
    the dominant one:
      * ``digitp`` - probability the character is a digit (letter positions stay
        near 0%, numeric positions near 100%; S<->5 misreads sit in between);
      * ``consensus`` - the modal character (the fleet's fixed skeleton, e.g.
        the ``ME1RG``/``T``/leading-``0`` positions);
      * ``freq`` - how dominant that modal character is (anchor strength).

    Returns a dict, or ``None`` when the batch is too small / heterogeneous."""
    from collections import Counter
    lens: Counter = Counter()
    per_record_lens = []
    for cl in cand_lists:
        ls = {len(s) for s in (cl or [])}
        per_record_lens.append(ls)
        for s in (cl or []):
            lens[len(s)] += 1
    if not lens:
        return None
    length = lens.most_common(1)[0][0]
    if length < min_len:
        return None
    if sum(1 for ls in per_record_lens if length in ls) < min_records:
        return None
    strings = [s for cl in cand_lists for s in (cl or []) if len(s) == length]
    if not strings:
        return None
    n = len(strings)
    digitp, consensus, freq = [], [], []
    for i in range(length):
        col = Counter(s[i] for s in strings)
        ch, cnt = col.most_common(1)[0]
        consensus.append(ch)
        freq.append(cnt / n)
        digitp.append(sum(1 for s in strings if s[i].isdigit()) / n)
    return {"length": length, "digitp": digitp,
            "consensus": consensus, "freq": freq}


def _recover_zeros(value: str, mask):
    """Reinsert dropped zeros so a short chassis matches the batch length.
    Handles filled/touching 0s the recognizer omitted (e.g. 'T46587' for
    'T0046587').  Only '0' is ever inserted, and only where the batch consensus
    is '0', so no digit is invented.  Returns ``(value, recovered)``."""
    import math
    length = mask["length"]
    cons = mask["consensus"]
    freq = mask["freq"]
    n = len(value)
    if n >= length or (length - n) > 4:
        return value, False
    need = length - n
    inf = math.inf
    dp = [[inf] * (length + 1) for _ in range(n + 1)]
    bk = [[None] * (length + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0
    for j in range(length + 1):
        for i in range(n + 1):
            c = dp[i][j]
            if c == inf:
                continue
            if i < n and j < length:  # consume value[i] at template pos j
                cost = (0.05 if freq[j] < 0.60
                        else (0.0 if value[i] == cons[j] else 0.4))
                if c + cost < dp[i + 1][j + 1]:
                    dp[i + 1][j + 1] = c + cost
                    bk[i + 1][j + 1] = ("m", i, j)
            if j < length and cons[j] == "0":  # insert a dropped zero
                if c + 0.2 < dp[i][j + 1]:
                    dp[i][j + 1] = c + 0.2
                    bk[i][j + 1] = ("i", i, j)
    if dp[n][length] == inf:
        return value, False
    out, inserts, i, j = [], 0, n, length
    while (i, j) != (0, 0):
        step = bk[i][j]
        if step is None:
            return value, False
        kind, pi, pj = step
        if kind == "m":
            out.append(value[pi])
        else:
            out.append("0")
            inserts += 1
        i, j = pi, pj
    out.reverse()
    res = "".join(out)
    if len(res) != length or inserts != need:
        return value, False
    return res, True


# At a near-constant position, only these consensus -> misread swaps are trusted
# as OCR confusions safe to snap back.  Deliberately excludes 0<->6 so genuine
# 'T0645' style serials are never clobbered; targets the reported filled-0
# (read as 1 or 8) case.
_ANCHOR_FIX = {
    "0": {"1", "8", "D", "O", "Q"},
    "1": {"7", "I"},
    "5": {"S"},
    "8": {"B"},
    "6": {"G"},
    "2": {"Z"},
}


def _enforce_anchors(value: str, mask, freq_min: float = 0.85):
    """At near-constant 'anchor' positions (one character dominates the batch),
    snap a disagreeing character back to the consensus - but only when it is a
    recognised OCR confusion of that consensus (e.g. an ink-filled 0 read as 1
    or 8 in the fixed leading 'T0' region).  Genuinely variable positions and
    non-confusion characters are left untouched.  Returns ``(value, changed)``."""
    length = mask["length"]
    cons = mask["consensus"]
    freq = mask["freq"]
    if len(value) != length:
        return value, False
    out = list(value)
    changed = False
    for i in range(length):
        c = cons[i]
        if freq[i] >= freq_min and out[i] != c and out[i] in _ANCHOR_FIX.get(c, ()):
            out[i] = c
            changed = True
    return "".join(out), changed


def apply_chassis_mask(value: str | None, mask, thr: float = 0.40):
    """Reconcile one chassis against the batch template: recover dropped zeros,
    snap constant anchor positions to consensus, then map digit-confusable
    letters to digits at numeric positions.  Returns
    ``(value, changed, recovered)`` where ``recovered`` means the length had to
    be reconstructed (kept flagged for a human check)."""
    if not mask or not value:
        return value, False, False
    orig = value
    value, recovered = _recover_zeros(value, mask)
    value, _ = _enforce_anchors(value, mask)
    length, digitp = mask["length"], mask["digitp"]
    if len(value) == length:
        out = list(value)
        for i, ch in enumerate(out):
            if digitp[i] >= thr and ch in _DIGIT_MAP:
                out[i] = _DIGIT_MAP[ch]
        value = "".join(out)
    return value, value != orig, recovered


def chassis_needs_review(value: str | None, mask, conf,
                         thr: float = 0.40, alpha_thr: float = 0.10) -> bool:
    """Template-aware review flag: after enforcement, a numeric position still
    not a digit (an un-mappable letter) or a letter position holding a digit
    means the record does not fit the batch template -> needs a check."""
    if not value:
        return True
    if not mask:
        return _chassis_review(value, conf)
    length, digitp = mask["length"], mask["digitp"]
    if len(value) != length:
        return True
    for i, ch in enumerate(value):
        if digitp[i] >= thr and not ch.isdigit():
            return True
        if digitp[i] <= alpha_thr and not ch.isalpha():
            return True
    return False


def _refine_field(page, line, full_zoom, kind, fallback):
    """High-accuracy value for a critical field via multi-scale recognition-only
    OCR of its cropped region + voting.  Returns ``(value, confidence, cands)``
    where ``cands`` are the normalised chassis candidate strings (for batch
    template learning) and ``[]`` for other fields."""
    if page is None or line is None:
        return fallback, None, []
    import pymupdf
    # A user-drawn box defines its own bounds: crop it tightly and symmetrically
    # so extraction follows the rectangle instead of drifting into neighbours.
    if line.get("rect"):
        rect = pymupdf.Rect(
            line["x0"] / full_zoom - 2, line["y0"] / full_zoom - 2,
            line["x1"] / full_zoom + 2, line["y1"] / full_zoom + 2)
    else:
        rect = pymupdf.Rect(
            line["x0"] / full_zoom - 4, line["y0"] / full_zoom - 3,
            line["x1"] / full_zoom + 16, line["y1"] / full_zoom + 3)
    raw = _rec_candidates(page, rect)
    if kind == "chassis":
        cands = [(_norm_chassis(t), w) for t, w in raw if len(_norm_chassis(t)) >= 6]
        if fallback:
            fb = _norm_chassis(fallback)
            if len(fb) >= 6:
                cands.append((fb, 0.5))
        text, conf = _vote(cands, prefer_len=17, validator=_chassis_valid)
        cand_strs = [c for c, _ in cands if len(c) >= 15]
        if not text:
            return fallback, None, cand_strs
        fb = _norm_chassis(fallback) if fallback else ""
        text, conf = _prefer_coarse(text, conf, fb, line.get("score"),
                                    _chassis_valid)
        return text, conf, cand_strs
    if kind == "engine":
        cands = [(_norm_engine(t), w) for t, w in raw if len(_norm_engine(t)) >= 5]
        if fallback:
            fb = _norm_engine(fallback)
            if len(fb) >= 5:
                cands.append((fb, 0.5))
        text, conf = _vote(cands, validator=_engine_valid)
        cand_strs = [c for c, _ in cands if len(c) >= 6]
        if not text:
            return fallback, None, cand_strs
        fb = _norm_engine(fallback) if fallback else ""
        text, conf = _prefer_coarse(text, conf, fb, line.get("score"),
                                    _engine_valid)
        return text, conf, cand_strs
    cands = [(_norm_reg(t), w) for t, w in raw
             if len(_norm_reg(t)) >= 4 and any(c.isdigit() for c in _norm_reg(t))]
    if fallback:
        fb = _norm_reg(fallback)
        if len(fb) >= 4:
            cands.append((fb, 0.5))
    text, conf = _vote(cands, validator=_reg_valid)
    if not text:
        return fallback, None, []
    refined = _fix_reg_lk(text)
    coarse = _fix_reg_lk(_norm_reg(fallback)) if fallback else ""
    text, conf = _prefer_coarse(refined, conf, coarse, line.get("score"),
                               _reg_wellformed)
    return text, conf, []


def _line_from_norm(page, box, full_zoom: float) -> dict:
    """Synthesize an OCR value "line" (full_zoom pixel-space bbox) from a
    normalized ``[x0,y0,x1,y1]`` rectangle so the label-anchored refinement and
    crop helpers can operate on a user-drawn box."""
    x0, y0, x1, y1 = box
    W, H = page.rect.width, page.rect.height
    px0, py0 = x0 * W * full_zoom, y0 * H * full_zoom
    px1, py1 = x1 * W * full_zoom, y1 * H * full_zoom
    return {"x0": px0, "y0": py0, "x1": px1, "y1": py1,
            "cx": (px0 + px1) / 2.0, "cy": (py0 + py1) / 2.0,
            "text": "", "score": None, "rect": True}


def crop_value_png(page, line, full_zoom: float, disp_zoom: float = 5.0,
                   page_bgr=None):
    """Render just a field's value region to a PNG (bytes) for display beside the
    captured text.  ``line`` is the OCR value line (coords in ``full_zoom``
    pixel space); a little padding keeps the value fully visible.  When
    ``page_bgr`` (a full page rendered with the upload's pp at ``disp_zoom``) is
    given, the region is sliced out of it so the stored crop matches the
    OCR-settings popup; otherwise the raw page region is rendered.  Returns
    ``None`` when the region is unavailable."""
    if page is None or line is None:
        return None
    import pymupdf
    if line.get("rect"):
        rect = pymupdf.Rect(
            line["x0"] / full_zoom - 2, line["y0"] / full_zoom - 2,
            line["x1"] / full_zoom + 2, line["y1"] / full_zoom + 2)
    else:
        rect = pymupdf.Rect(
            line["x0"] / full_zoom - 6, line["y0"] / full_zoom - 4,
            line["x1"] / full_zoom + 18, line["y1"] / full_zoom + 4)
    if page_bgr is not None:
        return _slice_png(page_bgr, rect, disp_zoom)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(disp_zoom, disp_zoom), clip=rect)
    return pix.tobytes("png")


def _parse_cr_page(lines: list[dict], page=None, full_zoom: float = 3.0,
                   rule_set: str = "oldcrbook", rects: dict | None = None) -> dict:
    rec: dict = {}
    for field, frags in get_rule_set(rule_set)["labels"].items():
        lbl = _find_label(lines, frags, _CR_LABEL_EXCLUDE.get(field))
        if not lbl:
            continue
        val = _value_below(lines, lbl)
        if val is None and _enh_on():
            sr = _line_same_row(lines, lbl)
            val = sr["text"].strip() if sr else None
        rec[field] = _clean(val)

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

    # light validation / cleanup on key identifiers (fallback values)
    if rec.get("chassis_no"):
        m = re.search(r"[A-Z0-9]{8,20}", rec["chassis_no"].replace(" ", ""))
        rec["chassis_no"] = m.group(0) if m else rec["chassis_no"]
    if rec.get("year_of_manufacture"):
        m = re.search(r"(19|20)\d{2}", rec["year_of_manufacture"])
        rec["year_of_manufacture"] = m.group(0) if m else None

    # ---- Rectangle override for coarse fields ------------------------------
    # When the upload defines value-region boxes, crop-and-OCR each coarse field
    # from its box (the three critical fields are handled by the refinement
    # pass below, which also uses the box).
    if rects and page is not None:
        for _f in ("absolute_owner", "class_of_vehicle", "fuel_type",
                   "cylinder_capacity", "year_of_manufacture", "colour"):
            _box = rects.get(_f)
            if _box:
                # Strictly follow the box: its OCR read is the value (even if
                # empty), never the label-anchored guess.
                _v, _ = read_rect(page, _box)
                rec[_f] = _v or None

    # ---- High-accuracy refinement of the two critical fields --------------
    # Chassis No and Registration No are re-read from a high-zoom crop with
    # multi-scale recognition-only OCR + character voting, correcting the
    # digit/letter confusions (5<->S, 8<->B, I->1, O->0, ...) the single
    # full-page pass makes.
    # When an upload pp is active, render the whole page once with it so each
    # value crop is sliced from the same pp-processed image the popup shows.
    _crop_pp = _active_pp()
    _crop_page_bgr = (_page_bgr(page, _crop_pp, 5.0)
                      if (_crop_pp and page is not None) else None)
    if rects and rects.get("chassis_no") and page is not None:
        ch_line = _line_from_norm(page, rects["chassis_no"], full_zoom)
        ch_fb, ch_rect = None, True   # box-only: no label fallback
    else:
        ch_lbl = _find_label(lines, _CR_LABELS["chassis_no"])
        ch_line = _line_below(lines, ch_lbl) if ch_lbl else None
        if ch_line is None and ch_lbl and _enh_on():
            ch_line = _line_same_row(lines, ch_lbl)
        ch_fb, ch_rect = rec.get("chassis_no"), False
    ch_val, ch_conf, ch_cands = _refine_field(page, ch_line, full_zoom, "chassis",
                                              ch_fb)
    if ch_val or ch_rect:
        rec["chassis_no"] = ch_val
    rec["chassis_no_confidence"] = ch_conf
    rec["_chassis_cands"] = ch_cands
    rec["_chassis_crop_png"] = crop_value_png(page, ch_line, full_zoom,
                                              page_bgr=_crop_page_bgr)

    if rects and rects.get("registration_no") and page is not None:
        rg_line = _line_from_norm(page, rects["registration_no"], full_zoom)
        rg_fb, rg_rect = None, True   # box-only
    else:
        rg_lbl = _find_label(lines, _CR_LABELS["registration_no"],
                             _CR_LABEL_EXCLUDE["registration_no"])
        rg_line = _line_below(lines, rg_lbl) if rg_lbl else None
        if rg_line is None and rg_lbl and _enh_on():
            rg_line = _line_same_row(lines, rg_lbl)
        rg_fb, rg_rect = rec.get("registration_no"), False
    rg_val, rg_conf, _ = _refine_field(page, rg_line, full_zoom, "reg",
                                       rg_fb)
    if rg_val or rg_rect:
        rec["registration_no"] = rg_val
    rec["registration_no_confidence"] = rg_conf
    rec["_reg_crop_png"] = crop_value_png(page, rg_line, full_zoom,
                                          page_bgr=_crop_page_bgr)

    if rects and rects.get("engine_no") and page is not None:
        en_line = _line_from_norm(page, rects["engine_no"], full_zoom)
        en_fb, en_rect = None, True   # box-only
    else:
        en_lbl = _find_label(lines, _CR_LABELS["engine_no"])
        en_line = _line_below(lines, en_lbl) if en_lbl else None
        if en_line is None and en_lbl and _enh_on():
            en_line = _line_same_row(lines, en_lbl)
        en_fb, en_rect = rec.get("engine_no"), False
    en_val, en_conf, en_cands = _refine_field(page, en_line, full_zoom, "engine",
                                              en_fb)
    if en_val or en_rect:
        rec["engine_no"] = en_val
    rec["engine_no_confidence"] = en_conf
    rec["_engine_cands"] = en_cands
    rec["_engine_crop_png"] = crop_value_png(page, en_line, full_zoom,
                                             page_bgr=_crop_page_bgr)

    rec["needs_review"] = bool(_chassis_review(rec.get("chassis_no"), ch_conf)
                               or _reg_review(rec.get("registration_no"), rg_conf)
                               or _engine_review(rec.get("engine_no"), en_conf))

    rec["raw_text"] = full
    rec["confidence"] = _mean_score(lines)
    return rec


# ---------------------------------------------------------------------------
# Template (positional) extraction: crop trainer-defined rectangles + OCR
# ---------------------------------------------------------------------------
# Human-readable labels for the CR field keys, surfaced in the Trainer editor.
CR_FIELD_LABELS = {
    "registration_no": "Reg No",
    "chassis_no": "Chassis No",
    "engine_no": "Engine No",
    "current_owner": "Current Owner",
    "absolute_owner": "Absolute Owner",
    "cylinder_capacity": "Cylinder Capacity",
    "class_of_vehicle": "Class of Vehicle",
    "fuel_type": "Fuel Type",
    "year_of_manufacture": "Year of Manufacture",
    "colour": "Colour",
    "make": "Make",
    "model": "Model",
    "country_of_origin": "Country of Origin",
    "provincial_council": "Provincial Council",
    "date_first_registration": "Date of First Registration",
}

# Per-field normalizer/validator used when reading a template crop.  Fields not
# listed are treated as free text (best single read, no structure validation).
_FIELD_NORMALIZERS = {
    "chassis_no": _norm_chassis,
    "registration_no": _norm_reg,
    "engine_no": _norm_engine,
}
_FIELD_VALIDATORS = {
    "chassis_no": _chassis_valid,
    "registration_no": _reg_valid,
    "engine_no": _engine_valid,
}


def cr_field_options() -> list[dict]:
    """Field keys + labels for the template editor's data-name picker."""
    return [{"key": k, "label": CR_FIELD_LABELS.get(k, k)}
            for k in _CR_LABELS]


def _encode_rect_png(page, rect, pp=None, zoom: float = 6.0) -> bytes:
    """Render one absolute PDF-point rectangle to a PNG (with optional pp)."""
    import cv2
    bgr = _crop_bgr(page, rect, zoom)
    if pp:
        bgr = _apply_pp(bgr, pp)
    return cv2.imencode(".png", bgr)[1].tobytes()


def render_crop_png(page, box, pp=None, zoom: float = 6.0) -> bytes:
    """Render one normalized rectangle as a PNG for the settings preview
    (optionally with the box's trainer adjustments applied)."""
    import pymupdf
    x0, y0, x1, y1 = box
    W, H = page.rect.width, page.rect.height
    rect = pymupdf.Rect(x0 * W, y0 * H, x1 * W, y1 * H)
    return _encode_rect_png(page, rect, pp=pp, zoom=zoom)


def read_rect(page, box, pp=None) -> tuple[str | None, float]:
    """OCR one normalized rectangle ``[x0,y0,x1,y1]`` (fractions of page size)
    with the extensive multi-zoom/variant pass.  Returns ``(text, confidence)``
    of the best free-text read (no field structure assumed)."""
    import pymupdf
    x0, y0, x1, y1 = box
    W, H = page.rect.width, page.rect.height
    rect = pymupdf.Rect(x0 * W, y0 * H, x1 * W, y1 * H)
    cands = [(_clean(t), s) for t, s in _rec_candidates(page, rect, pp=pp)
             if _clean(t)]
    if not cands:
        return None, 0.0
    text, conf = max(cands, key=lambda c: c[1])
    return text, round(float(conf), 3)


def extract_by_rects(page, rects: dict, pp_map: dict | None = None) -> dict:
    """Extract each field by cropping its trainer-defined normalized rectangle
    and running the extensive OCR pass on the crop.  ``rects`` maps a field key
    to ``[x0,y0,x1,y1]`` (fractions of page size); ``pp_map`` optionally maps a
    field key to its per-box image adjustments.  Returns a dict mapping each
    field key to ``{value, confidence, valid}`` (``valid`` is None for free-text
    fields that have no structure validator)."""
    import pymupdf
    W, H = page.rect.width, page.rect.height
    out: dict[str, dict] = {}
    for name, box in rects.items():
        try:
            x0, y0, x1, y1 = box
        except (TypeError, ValueError):
            continue
        rect = pymupdf.Rect(x0 * W, y0 * H, x1 * W, y1 * H)
        cands = _rec_candidates(page, rect, pp=(pp_map or {}).get(name))
        norm = _FIELD_NORMALIZERS.get(name)
        valid = _FIELD_VALIDATORS.get(name)
        if norm:
            cn = [(norm(t), s) for t, s in cands if norm(t)]
            value, conf = _vote(cn, validator=valid) if cn else (None, 0.0)
        else:
            cn = [(_clean(t), s) for t, s in cands if _clean(t)]
            value, conf = (max(cn, key=lambda c: c[1]) if cn else (None, 0.0))
        out[name] = {
            "value": value,
            "confidence": round(float(conf), 3),
            "valid": (bool(valid(value)) if (valid and value) else None),
        }
    return out


def open_document(path: str):
    """Open a PDF/image with PyMuPDF; caller is responsible for closing it."""
    import pymupdf
    doc = pymupdf.open(path)
    return doc, doc.page_count


def extract_cr_page(page, zoom: float = 3.0,
                    rule_set: str = "oldcrbook", pp=None,
                    rects: dict | None = None) -> dict | None:
    """OCR + parse a single CR-book page (one vehicle).  None if page is blank.
    ``rule_set`` selects the per-format extractor (see CR_RULE_SETS).  ``pp``
    is the upload-level image adjustment applied to the whole page and every
    refinement crop for this page.  ``rects`` (field -> normalized box) makes
    each value be cropped from its box instead of located by its label."""
    _pp_ctx.pp = pp or None
    try:
        lines = _ocr(_render_page_png(page, zoom))
        if not lines:
            return None
        return _parse_cr_page(lines, page=page, full_zoom=zoom,
                              rule_set=rule_set, rects=rects)
    finally:
        _pp_ctx.pp = None


# Fields shown (in order) by the CR upload OCR-settings "Test OCR" button.
_CR_TEST_FIELDS = [
    ("registration_no", "Reg No"),
    ("chassis_no", "Chassis No"),
    ("engine_no", "Engine No"),
    ("absolute_owner", "Absolute Owner"),
    ("class_of_vehicle", "Class"),
    ("fuel_type", "Fuel"),
    ("cylinder_capacity", "CC"),
    ("year_of_manufacture", "Year"),
    ("colour", "Colour"),
]
_CR_TEST_CONF = {
    "registration_no": "registration_no_confidence",
    "chassis_no": "chassis_no_confidence",
    "engine_no": "engine_no_confidence",
}


def _line_score_for(lines: list[dict], value) -> float | None:
    """Best OCR score among lines whose text contains (or is contained by) the
    value -- a best-effort confidence for coarse fields with no refined score."""
    nv = _norm(value or "")
    if not nv:
        return None
    best = None
    for ln in lines:
        nt = _norm(ln.get("text"))
        if nt and (nv in nt or nt in nv):
            s = ln.get("score")
            if s and (best is None or s > best):
                best = s
    return best


def clean_rects(rects, kind: str = "cr") -> dict:
    """Keep only known field keys (for the given document ``kind``) whose value
    is a valid normalized rectangle ``[x0,y0,x1,y1]`` (0..1, x0<x1, y0<y1)."""
    if not isinstance(rects, dict):
        return {}
    fields = _RC_TEST_FIELDS if kind == "rc" else _CR_TEST_FIELDS
    allowed = {k for k, _ in fields}
    out: dict = {}
    for k, v in rects.items():
        if k not in allowed:
            continue
        try:
            x0, y0, x1, y1 = (max(0.0, min(1.0, float(n))) for n in v)
        except (TypeError, ValueError):
            continue
        if x1 <= x0 or y1 <= y0:
            continue
        out[k] = [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)]
    return out


def detect_cr_field_boxes(page, pp=None, zoom: float = 3.0,
                          rule_set: str = "oldcrbook") -> dict:
    """Auto-detect a normalized value-region box for each display field from the
    label + value-line layout.  Fields whose value line is not found get a
    stacked default box so every field is still editable."""
    _pp_ctx.pp = pp or None
    try:
        lines = _ocr(_render_page_png(page, zoom))
    finally:
        _pp_ctx.pp = None
    W, H = page.rect.width, page.rect.height
    labels = get_rule_set(rule_set)["labels"]

    def _norm_box(ln):
        return [round(ln["x0"] / zoom / W, 4), round(ln["y0"] / zoom / H, 4),
                round(ln["x1"] / zoom / W, 4), round(ln["y1"] / zoom / H, 4)]

    boxes: dict = {}
    for key, _label in _CR_TEST_FIELDS:
        frags = labels.get(key) or _CR_LABELS.get(key)
        if not (lines and frags):
            continue
        lbl = _find_label(lines, frags, _CR_LABEL_EXCLUDE.get(key))
        if not lbl:
            continue
        vline = _line_below(lines, lbl) or _line_same_row(lines, lbl)
        if vline:
            boxes[key] = _norm_box(vline)
    missing = 0
    for key, _label in _CR_TEST_FIELDS:
        if key not in boxes:
            y = 0.10 + 0.06 * missing
            boxes[key] = [0.05, round(y, 4), 0.35, round(y + 0.045, 4)]
            missing += 1
    return boxes


def test_cr_page(page, pp=None, zoom: float = 3.0,
                 rule_set: str = "oldcrbook", rects: dict | None = None) -> list[dict]:
    """OCR one CR-book page with ``pp`` applied and return the display fields
    (see _CR_TEST_FIELDS) as ``{key,label,value,confidence}`` for the upload
    OCR-settings "Test OCR" button.  When ``rects`` is given each value is read
    from its box.  Confidence is the refined per-field score for the three
    critical identifiers and a best-effort box/line score otherwise."""
    _pp_ctx.pp = pp or None
    try:
        lines = _ocr(_render_page_png(page, zoom))
        rec = (_parse_cr_page(lines, page=page, full_zoom=zoom,
                              rule_set=rule_set, rects=rects) if lines else {})
    finally:
        _pp_ctx.pp = None
    out: list[dict] = []
    for key, label in _CR_TEST_FIELDS:
        value = rec.get(key)
        if key in _CR_TEST_CONF:
            conf = rec.get(_CR_TEST_CONF[key])
        elif rects and rects.get(key):
            _, conf = read_rect(page, rects[key])
        else:
            conf = _line_score_for(lines if lines else [], value)
        out.append({
            "key": key, "label": label, "value": value or "",
            "confidence": (round(float(conf), 3) if conf else None),
        })
    return out


def extract_cr_records(path: str, zoom: float = 3.0) -> tuple[int, list[dict]]:
    doc, page_count = open_document(path)
    records: list[dict] = []
    try:
        for i in range(page_count):
            rec = extract_cr_page(doc[i], zoom)
            if rec is None:
                continue
            rec["page_no"] = i + 1
            records.append(rec)
    finally:
        doc.close()
    return page_count, records


# ---------------------------------------------------------------------------
# Receipt extraction (many bank deposit slips per file)
# ---------------------------------------------------------------------------
# Deposit-slip serial: a leading 0 then 6 digits.  Also accept 7 (i.e. an
# 8-digit token) to tolerate the occasional doubled-digit OCR glitch so the
# slip still anchors its receipt instead of the whole receipt being dropped.
_SLIP_RE = re.compile(r"^0\d{6,7}$")
_AMOUNT_RE = re.compile(r"\d[\d,]*\.\d{2}")
# Vehicle plate: 2-3 letters, then a digit group, tolerating OCR separator
# noise between them (e.g. 'BLM--1625', 'BLM 1625').
_VEHICLE_RE = re.compile(r"\b([A-Z]{2,3})[-\s.]{0,3}(\d{3,4})([A-Z]?)\b")


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


def _norm_amount(t: str) -> str | None:
    """Canonicalise a recognised amount token to 'digits.dd' (or None)."""
    if not t:
        return None
    m = _AMOUNT_RE.search(t.replace(" ", ""))
    return m.group(0).replace(",", "") if m else None


def _find_amount(seg: list[dict], fragments: list[str]):
    """Return ``(Decimal|None, line)`` for the amount on, or on the same row to
    the right of / just below, a line matching a label fragment.  The line is
    returned so the value cell can be re-OCR'd for accuracy.  Scanning continues
    past a matching label that has no amount nearby (e.g. the 'CASH PAYING'
    header for the 'cash' fragment), so a later real label still resolves."""
    for i, ln in enumerate(seg):
        n = _norm(ln["text"])
        if not any(f in n for f in fragments):
            continue
        m = _AMOUNT_RE.search(ln["text"])
        if m:
            return _to_decimal(m.group(0)), ln
        ly0, ly1, lx1 = ln["y0"], ln["y1"], ln.get("x1", 0)
        cy = (ly0 + ly1) / 2
        best = None  # (row_dist, x0), decimal, line
        for o in seg:
            if o is ln:
                continue
            oy = (o["y0"] + o["y1"]) / 2
            # same row (tight, so an adjacent row's amount does not bleed in) and
            # to the right of the label; pick the closest row, then leftmost.
            if abs(oy - cy) <= 14 and o.get("x0", 0) >= lx1 - 40:
                mm = _AMOUNT_RE.search(o["text"])
                if mm:
                    key = (abs(oy - cy), o.get("x0", 0))
                    if best is None or key < best[0]:
                        best = (key, _to_decimal(mm.group(0)), o)
        if best:
            return best[1], best[2]
        for nxt in seg[i + 1:i + 3]:
            mm = _AMOUNT_RE.search(nxt["text"])
            if mm:
                return _to_decimal(mm.group(0)), nxt
    return None, None


def _refine_vehicle(page, line, full_zoom, fallback):
    """High-accuracy vehicle number via multi-scale recognition-only OCR of the
    plate line + voting, normalised to the Sri Lankan plate format.  Returns
    ``(value, confidence)``.  This field is critical, so it is recognised across
    more zoom levels than the general refinement."""
    if page is None or line is None:
        return _fix_reg_lk(fallback), None
    import pymupdf
    rect = pymupdf.Rect(
        line["x0"] / full_zoom - 6, line["y0"] / full_zoom - 4,
        line["x1"] / full_zoom + 18, line["y1"] / full_zoom + 4)
    raw = _rec_candidates(page, rect, zooms=(3, 4, 6, 8, 10))
    cands: list[tuple[str, float]] = []
    for t, w in raw:
        v = _norm_reg(t).replace("-", "")
        if 5 <= len(v) <= 8 and any(c.isalpha() for c in v) \
                and any(c.isdigit() for c in v):
            cands.append((v, w))
    if fallback:
        fb = _norm_reg(fallback).replace("-", "")
        if 5 <= len(fb) <= 8:
            cands.append((fb, 0.5))
    text, conf = _vote(cands)
    if not text:
        return _fix_reg_lk(fallback), None
    refined = _fix_reg_lk(text)
    coarse = _fix_reg_lk(_norm_reg(fallback)) if fallback else ""
    refined, conf = _prefer_coarse(refined, conf, coarse, line.get("score"),
                                   _reg_wellformed)
    return refined, conf


def _refine_amount(page, line, full_zoom, fallback):
    """High-accuracy currency amount via multi-scale recognition-only OCR of the
    amount cell + value-frequency voting (a single wrong digit changes the whole
    value, so whole-value votes beat per-character ones here).  Returns
    ``(Decimal|None, confidence)``."""
    if page is None or line is None:
        return fallback, None
    import pymupdf
    from collections import defaultdict
    rect = pymupdf.Rect(
        line["x0"] / full_zoom - 8, line["y0"] / full_zoom - 4,
        line["x1"] / full_zoom + 12, line["y1"] / full_zoom + 4)
    raw = _rec_candidates(page, rect, zooms=(3, 4, 6, 8, 10))
    votes: dict[str, float] = defaultdict(float)
    for t, w in raw:
        v = _norm_amount(t)
        if v is not None:
            votes[v] += w
    if fallback is not None:
        votes[f"{fallback:.2f}"] += 0.5
    if not votes:
        return fallback, None
    best = max(votes, key=lambda k: votes[k])
    tot = sum(votes.values()) or 1.0
    return _to_decimal(best), round(votes[best] / tot, 4)


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
    # vehicle no: capture the matching line so it can be re-OCR'd at multiple
    # zooms for accuracy (this is a critical field).
    for ln in seg:
        mm = _VEHICLE_RE.search(ln["text"].upper())
        if mm:
            rec["vehicle_no"] = f"{mm.group(1)}-{mm.group(2)}{mm.group(3)}"
            rec["_vehicle_line"] = ln
            break
    if "vehicle_no" not in rec:
        mm = _VEHICLE_RE.search(text.upper())
        if mm:
            rec["vehicle_no"] = f"{mm.group(1)}-{mm.group(2)}{mm.group(3)}"
    m = re.search(r"\b(RK\d{6,})\b", text, re.I)
    if m:
        rec["application_no"] = m.group(1).upper()

    # receipt no: the numeric token on the same row to the right of the "Receipt
    # No" label (the layout the printer uses), else on the line just below it.
    for i, ln in enumerate(seg):
        if "receiptno" in _norm(ln["text"]):
            ly0, ly1, lx1 = ln["y0"], ln["y1"], ln.get("x1", 0)
            best = None
            for o in seg:
                if o is ln:
                    continue
                oy = (o["y0"] + o["y1"]) / 2
                if ly0 - 14 <= oy <= ly1 + 14 and o.get("x0", 0) >= lx1 - 20:
                    mm = re.fullmatch(r"\d{1,4}", o["text"].strip())
                    if mm and (best is None or o.get("x0", 0) < best[0]):
                        best = (o.get("x0", 0), mm.group(0))
            if best:
                rec["receipt_no"] = best[1]
            else:
                for nxt in seg[i + 1:i + 3]:
                    mm = re.search(r"\b(\d{1,4})\b", nxt["text"])
                    if mm:
                        rec["receipt_no"] = mm.group(1)
                        break
            break

    # amounts: capture each value's line so the cell can be re-OCR'd at multiple
    # zooms, and so the Total/Charges/Bank Total formula can be cross-checked.
    ch_amt, ch_line = _find_amount(seg, ["charg"])
    bt_amt, bt_line = _find_amount(seg, ["banktota", "anktota", "anetota"])
    tt_amt, tt_line = _find_amount(seg, ["vtota", "rmvtota", "rnvtota"])
    if tt_amt is None:
        tt_amt, tt_line = _find_amount(seg, ["cash", "amount", "aaount", "anount"])
    rec["bank_charges"] = ch_amt
    rec["bank_total"] = bt_amt
    rec["total_amount"] = tt_amt
    rec["_amt_lines"] = {"bank_charges": ch_line, "bank_total": bt_line,
                         "total_amount": tt_line}
    return rec


# A receipt's visual anchors on the Peoples-Bank slip: the bold "PEOPLE'S"
# header (top) and the "Cashier" footer (bottom).  A single receipt runs from
# just above its PEOPLE'S header to just after the following Cashier line.
def _is_people(text: str) -> bool:
    return "people" in _norm(text)


def _is_cashier(text: str) -> bool:
    return "cashier" in _norm(text)


# Minimum vertical gap (px @ OCR zoom) between two headers to treat them as
# separate receipts; closer ones are a split/duplicated header for one slip.
_RECEIPT_MIN_GAP = 200


def _segment_by_slip(lines: list[dict]) -> list[list[dict]]:
    """Fallback: split OCR lines into per-receipt groups on the slip number."""
    anchors = [i for i, ln in enumerate(lines) if _SLIP_RE.match(ln["text"].strip())]
    if not anchors:
        return [lines] if lines else []
    segments = []
    for idx, start in enumerate(anchors):
        end = anchors[idx + 1] if idx + 1 < len(anchors) else len(lines)
        ctx = max(0, start - 6) if idx == 0 else anchors[idx - 1] + 1
        seg_start = ctx if idx == 0 else start
        segments.append(lines[seg_start:end])
    return segments


def _segment_receipts(lines: list[dict]) -> list[list[dict]]:
    """Split OCR lines into per-receipt groups anchored on the bold PEOPLE'S
    header, so each slip is bounded by its own header/footer.  Falls back to
    slip-number anchoring when no headers are detected."""
    people = []
    for i, ln in enumerate(lines):
        if _is_people(ln["text"]):
            # collapse a split/duplicated header for the same slip
            if people and (ln["y0"] - lines[people[-1]]["y0"]) < _RECEIPT_MIN_GAP:
                continue
            people.append(i)
    if not people:
        return _segment_by_slip(lines)
    segments = []
    for k, start in enumerate(people):
        end = people[k + 1] if k + 1 < len(people) else len(lines)
        segments.append(lines[start:end])
    return segments


def crop_receipt_png(page, y0_px: float, y1_px: float, ocr_zoom: float,
                     disp_zoom: float = 2.0, pad_above_pt: float = 8.0,
                     pad_below_pt: float = 10.0) -> bytes | None:
    """Render one receipt's region (full page width, from a little above its
    PEOPLE'S header to a little after its Cashier footer) to a PNG.  Bounds are
    given in full-page pixel space at ``ocr_zoom``; converted to PDF points."""
    import pymupdf
    y0 = max(0.0, y0_px / ocr_zoom - pad_above_pt)
    y1 = min(page.rect.height, y1_px / ocr_zoom + pad_below_pt)
    if y1 <= y0:
        return None
    pix = page.get_pixmap(matrix=pymupdf.Matrix(disp_zoom, disp_zoom),
                          clip=pymupdf.Rect(0, y0, page.rect.width, y1))
    return pix.tobytes("png")


def _seg_ybounds(seg: list[dict]) -> tuple[float, float]:
    """Full-page pixel bounds of one receipt segment: from its top line down to
    the Cashier footer (or its last line when no Cashier is found)."""
    y0_px = min(ln["y0"] for ln in seg)
    csh = [ln for ln in seg if _is_cashier(ln["text"])]
    y1_px = csh[-1]["y1"] if csh else max(ln["y1"] for ln in seg)
    return y0_px, y1_px


def _is_real_segment(seg: list[dict]) -> bool:
    """A segment is a real slip only if it carries at least one anchor field."""
    rec = _parse_receipt_segment(seg)
    return any(rec.get(k) for k in
              ("slip_no", "vehicle_no", "application_no", "receipt_no"))


def _real_segments(lines: list[dict]) -> list[list[dict]]:
    """Segment a page's OCR lines and keep only real slips, top-to-bottom."""
    return [seg for seg in _segment_receipts(lines)
            if seg and _is_real_segment(seg)]


def _process_receipt_segment(page, seg: list[dict], zoom: float,
                             rects=None, pp=None) -> dict:
    """Parse one receipt segment into a record.

    When ``rects`` is given (an assigned template's boxes), extraction is
    strictly box-driven: every displayed field comes from its own box (and its
    shown crop is that same box), while fields without a box are left empty --
    so the table reflects exactly the template's cropped reads.  With no boxes,
    fall back to full auto extraction (label anchoring + multi-scale vehicle /
    amount refinement).  No post-OCR rules (sequential numbering, batch date,
    amount formula) are applied."""
    rec = _parse_receipt_segment(seg)
    # crop from the PEOPLE'S header (segment top) to the Cashier footer.
    y0_px, y1_px = _seg_ybounds(seg)
    if rects:
        region = _slip_region_pts(page, y0_px, y1_px, zoom)
        vals, crops = read_receipt_rects(page, region, rects, pp=pp)
        # Populate boxed fields from their box; clear every other extractable
        # field so nothing outside the template leaks in from label anchoring.
        for f in _RC_BOX_FIELDS:
            rec[f] = vals.get(f) if f in rects else None
        rec["application_no"] = None
        rec["receipt_time"] = None
        rec["needs_review"] = False
        rec["_rc_field_crops"] = crops
        rec.pop("_vehicle_line", None)
        rec.pop("_amt_lines", None)
        rec["_image_crop_png"] = crop_receipt_png(page, y0_px, y1_px, zoom)
        return rec
    # No template boxes: refine the vehicle number with multi-scale OCR + voting.
    vline = rec.pop("_vehicle_line", None)
    v, _vconf = _refine_vehicle(page, vline, zoom, rec.get("vehicle_no"))
    if v:
        rec["vehicle_no"] = v
    # refine the currency amounts the same way (each read on its own merits;
    # no Bank Total = Total + Charges cross-check is applied).
    al = rec.pop("_amt_lines", {}) or {}
    tt, _ = _refine_amount(page, al.get("total_amount"), zoom,
                           rec.get("total_amount"))
    ch, _ = _refine_amount(page, al.get("bank_charges"), zoom,
                           rec.get("bank_charges"))
    bt, _ = _refine_amount(page, al.get("bank_total"), zoom,
                           rec.get("bank_total"))
    rec["total_amount"] = tt
    rec["bank_charges"] = ch
    rec["bank_total"] = bt
    rec["needs_review"] = False
    rec["_image_crop_png"] = crop_receipt_png(page, y0_px, y1_px, zoom)
    return rec


def extract_receipt_page(page, zoom: float = 2.0, pp=None,
                         rects=None) -> list[dict]:
    """OCR + parse one receipts page into its deposit-slip records.  Slip
    detection and label anchoring run on the RAW page so segmentation matches
    the OCR-settings popup (segment_document) and any drawn boxes align; ``pp``
    (a cleaned image-adjustment dict) is applied only to the per-box value reads
    within each slip, driven by ``rects`` (field -> normalized box)."""
    lines = _ocr_page(page, zoom)
    return [_process_receipt_segment(page, seg, zoom, rects=rects, pp=pp)
            for seg in _real_segments(lines)]


def extract_receipt_records(path: str, zoom: float = 2.0,
                            pp=None, rects=None) -> tuple[int, list[dict]]:
    doc, page_count = open_document(path)
    records: list[dict] = []
    seg_no = 0
    try:
        for i in range(page_count):
            for rec in extract_receipt_page(doc[i], zoom, pp=pp, rects=rects):
                seg_no += 1
                rec["segment_no"] = seg_no
                records.append(rec)
    finally:
        doc.close()
    return page_count, records


# Fields surfaced by the receipts OCR-settings "Test OCR" button.
_RC_TEST_FIELDS = [
    ("receipt_no", "Receipt No"),
    ("vehicle_no", "Vehicle No"),
    ("receipt_date", "Date"),
    ("total_amount", "Total"),
    ("bank_charges", "Charges"),
    ("bank_total", "Bank Total"),
    ("slip_no", "Slip No"),
]
# Receipt fields that can be captured via a template box (i.e. every field the
# strict template-box extraction populates).
_RC_BOX_FIELDS = [k for k, _ in _RC_TEST_FIELDS]


# ===== Unified template field model (doc-type agnostic) ===================
# A template owns an ordered list of field definitions, each of which is a dict:
#   {"key", "label", "type", "role", "box":[x0,y0,x1,y1]|None, "pp":{...}|None}
# ``type`` selects the normalizer/validator + accuracy strategy; ``role`` maps
# the field onto a record's canonical typed column so Mapping/Export/Dedup keep
# working regardless of how a template labels a field.  These helpers are the
# single source of truth for both doc kinds; the extractor is switched onto them
# in a later phase (this phase only adds + backfills the data).
_RC_FIELD_LABELS = {
    "slip_no": "Slip No", "receipt_no": "Receipt No", "receipt_date": "Date",
    "receipt_time": "Time", "vehicle_no": "Vehicle No",
    "application_no": "Application No", "applicant_name": "Applicant",
    "total_amount": "Total", "bank_charges": "Charges",
    "bank_total": "Bank Total",
}
# Field key -> extraction/normalization type (default "text").
_FIELD_TYPE = {
    "registration_no": "plate", "chassis_no": "chassis",
    "engine_no": "engine", "vehicle_no": "plate", "receipt_date": "date",
    "total_amount": "currency", "bank_charges": "currency",
    "bank_total": "currency",
}
# Built-in (default) field set per doc kind, in display order.
_DEFAULT_FIELD_KEYS = {
    "cr": [k for k, _ in _CR_TEST_FIELDS],
    "rc": _RC_BOX_FIELDS,
}


def field_label(kind: str, key: str) -> str:
    labels = _RC_FIELD_LABELS if kind == "rc" else CR_FIELD_LABELS
    return labels.get(key) or key.replace("_", " ").title()


def field_type(key: str) -> str:
    return _FIELD_TYPE.get(key, "text")


def default_field_defs(kind: str) -> list[dict]:
    """Built-in field definitions for a doc kind (no boxes / pp yet)."""
    return [{"key": k, "label": field_label(kind, k), "type": field_type(k),
             "role": k, "box": None, "pp": None}
            for k in _DEFAULT_FIELD_KEYS.get(kind, [])]


def clean_fields(fields, kind: str = "cr") -> list[dict]:
    """Validate/normalize a template's ``fields`` list into clean field defs.
    Non-dict / keyless / duplicate entries are dropped; boxes are validated like
    clean_rects; missing label/type/role are filled from the built-ins."""
    if not isinstance(fields, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for f in fields:
        if not isinstance(f, dict):
            continue
        key = str(f.get("key") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        cbox = None
        box = f.get("box")
        if isinstance(box, (list, tuple)) and len(box) == 4:
            try:
                x0, y0, x1, y1 = (max(0.0, min(1.0, float(n))) for n in box)
                if x1 > x0 and y1 > y0:
                    cbox = [round(x0, 4), round(y0, 4),
                            round(x1, 4), round(y1, 4)]
            except (TypeError, ValueError):
                cbox = None
        out.append({
            "key": key,
            "label": str(f.get("label") or field_label(kind, key)),
            "type": str(f.get("type") or field_type(key)),
            "role": str(f.get("role") or key),
            "box": cbox,
            "pp": (clean_pp(f.get("pp")) or None),
        })
    return out


def fields_from_rects(kind: str, rects, pp=None) -> list[dict]:
    """Build a fields list from a legacy ``ocr_rects`` {key: box} map: start from
    the built-in defs for the kind, attach each field's box, and append any extra
    boxed keys not in the built-in set."""
    rects = rects if isinstance(rects, dict) else {}
    defs = default_field_defs(kind)
    by_key = {d["key"]: d for d in defs}
    for key, box in rects.items():
        d = by_key.get(key)
        if d is None:
            d = {"key": key, "label": field_label(kind, key),
                 "type": field_type(key), "role": key, "box": None, "pp": None}
            defs.append(d)
            by_key[key] = d
        d["box"] = box
    return clean_fields(defs, kind)


def template_field_defs(template) -> list[dict]:
    """Return a template's field defs: parse its ``fields`` JSON when present,
    else derive them from the legacy ``ocr_rects`` + built-ins."""
    kind = getattr(template, "kind", "cr") or "cr"
    raw = getattr(template, "fields", None)
    if raw:
        try:
            defs = clean_fields(json.loads(raw), kind)
            if defs:
                return defs
        except (ValueError, TypeError):
            pass
    rects = {}
    raw_rects = getattr(template, "ocr_rects", None)
    if raw_rects:
        try:
            rects = clean_rects(json.loads(raw_rects), kind)
        except (ValueError, TypeError):
            rects = {}
    return fields_from_rects(kind, rects)


def extract_page(kind: str, page, *, rule_set: str = "oldcrbook",
                 pp=None, rects=None) -> list[dict]:
    """Unified per-page extractor dispatch (region strategy): returns the list
    of record dicts a page yields for either doc kind -- CR is page-granular
    (0/1 record per page), RC is segment-granular (one per detected slip).
    ``rule_set`` is honoured by CR and ignored by RC."""
    if kind == "rc":
        return extract_receipt_page(page, pp=pp, rects=rects)
    rec = extract_cr_page(page, rule_set=rule_set, pp=pp, rects=rects)
    return [rec] if rec else []


def build_field_data(defs, rec_obj, crop_attr=None, conf_attr=None):
    """Build a record's hybrid (field_values, field_crops, field_confidence)
    maps from its template's field defs.  Each field's value/confidence is read
    from the record's canonical column via the field's ``role`` (so
    template-defined fields mirror onto the typed columns Mapping/Export/Dedup
    rely on); crop filenames come from the kind's crop-attr map.  Returns three
    dicts keyed by field key (empty when the field has no data)."""
    crop_attr = crop_attr or {}
    conf_attr = conf_attr or {}
    values: dict = {}
    crops: dict = {}
    conf: dict = {}
    for d in defs:
        key = d.get("key")
        if not key:
            continue
        role = d.get("role") or key
        val = getattr(rec_obj, role, None)
        if val not in (None, ""):
            values[key] = str(val)
        cattr = crop_attr.get(key)
        if cattr:
            fn = getattr(rec_obj, cattr, None)
            if fn:
                crops[key] = fn
        kattr = conf_attr.get(key)
        if kattr:
            c = getattr(rec_obj, kattr, None)
            if c is not None:
                conf[key] = round(float(c), 3)
    return values, crops, conf


def test_receipt_page(page, pp=None) -> list[dict]:
    """Run receipt OCR on one page with the given image adjustments and return
    the first detected slip's fields (plus a slip count) for the receipts
    OCR-settings "Test OCR" button."""
    recs = extract_receipt_page(page, pp=pp)
    fields = [{"key": "_count", "label": "Slips on page",
               "value": str(len(recs)), "confidence": None}]
    rec = recs[0] if recs else {}
    for key, label in _RC_TEST_FIELDS:
        v = rec.get(key)
        fields.append({"key": key, "label": label,
                       "value": "" if v is None else str(v),
                       "confidence": None})
    return fields


def segment_document(path: str, zoom: float = 2.0,
                     pp=None) -> list[dict]:
    """Separate a receipts document into individual slips.  Returns one entry
    per detected slip (top-to-bottom, across all pages) with the page index and
    the slip's full-page pixel bounds so callers can render/OCR it in isolation:
        {"page": int, "k": int, "y0": float, "y1": float, "zoom": float}
    ``k`` is the slip's 0-based index among the real slips on its page."""
    doc, page_count = open_document(path)
    slips: list[dict] = []
    try:
        for p in range(page_count):
            page = doc[p]
            _pp_ctx.pp = pp or None
            try:
                lines = _ocr_page(page, zoom)
            finally:
                _pp_ctx.pp = None
            for k, seg in enumerate(_real_segments(lines)):
                y0_px, y1_px = _seg_ybounds(seg)
                slips.append({"page": p, "k": k,
                              "y0": y0_px, "y1": y1_px, "zoom": zoom})
    finally:
        doc.close()
    return slips


# Padding (PDF points) added around a slip's detected header/footer bounds when
# rendering/reading it in isolation.  Shared by the live preview and rect-based
# extraction so boxes drawn on the preview map to the same region on extraction.
_SLIP_PAD_ABOVE_PT = 8.0
_SLIP_PAD_BELOW_PT = 10.0


def _slip_region_pts(page, y0_px: float, y1_px: float,
                     ocr_zoom: float) -> tuple[float, float, float, float]:
    """Full-width slip region in PDF points (with standard padding)."""
    y0 = max(0.0, y0_px / ocr_zoom - _SLIP_PAD_ABOVE_PT)
    y1 = min(page.rect.height, y1_px / ocr_zoom + _SLIP_PAD_BELOW_PT)
    return 0.0, y0, page.rect.width, y1


def render_slip_png(page, y0_px: float, y1_px: float, ocr_zoom: float,
                    pp=None, disp_zoom: float = 2.0) -> bytes | None:
    """Render one separated slip's region to a PNG, applying ``pp`` image
    adjustments (used by the receipts OCR-settings live preview)."""
    import pymupdf
    rx0, ry0, rx1, ry1 = _slip_region_pts(page, y0_px, y1_px, ocr_zoom)
    if ry1 <= ry0:
        return None
    _pp_ctx.pp = pp or None
    try:
        pix = page.get_pixmap(matrix=pymupdf.Matrix(disp_zoom, disp_zoom),
                              clip=pymupdf.Rect(rx0, ry0, rx1, ry1))
        return _encode_pix_png(pix)
    finally:
        _pp_ctx.pp = None


def _parse_receipt_field(name: str, text: str | None):
    """Normalize a rect-cropped OCR read into the receipt field's value type."""
    if not text:
        return None
    t = text.strip()
    if name in ("total_amount", "bank_charges", "bank_total"):
        return _to_decimal(_norm_amount(t))
    if name == "receipt_date":
        m = re.search(r"(\d{2})[/\s.]{0,2}(\d{2})[/\s.]{0,2}(\d{4})", t)
        return f"{m.group(1)}/{m.group(2)}/{m.group(3)}" if m else None
    if name == "vehicle_no":
        mm = _VEHICLE_RE.search(t.upper())
        return f"{mm.group(1)}-{mm.group(2)}{mm.group(3)}" if mm else None
    if name == "slip_no":
        m = re.search(r"0\d{6,7}", t.replace(" ", ""))
        return m.group(0) if m else None
    if name == "receipt_no":
        m = re.search(r"\d{1,4}", t)
        return m.group(0) if m else None
    return t or None


def read_receipt_rects(page, region_pts, rects, pp=None) -> tuple[dict, dict]:
    """OCR each normalized rect (relative to one slip's ``region_pts`` in PDF
    points) and normalize per receipt field.  Returns ``(values, crops)`` where
    ``values`` maps field -> value and ``crops`` maps field -> PNG bytes of the
    box (so the records page can show the scan above each value)."""
    import pymupdf
    rx0, ry0, rx1, ry1 = region_pts
    rw, rh = (rx1 - rx0), (ry1 - ry0)
    values: dict = {}
    crops: dict = {}
    # When a pp is active, render the whole page once with it so each field crop
    # is sliced from the same pp-processed image the OCR-settings popup shows.
    page_bgr = _page_bgr(page, pp, 4.0) if pp else None
    for name, box in (rects or {}).items():
        try:
            x0, y0, x1, y1 = box
        except (TypeError, ValueError):
            continue
        rect = pymupdf.Rect(rx0 + x0 * rw, ry0 + y0 * rh,
                            rx0 + x1 * rw, ry0 + y1 * rh)
        cands = [(_clean(t), s) for t, s in
                 _rec_candidates(page, rect, pp=pp) if _clean(t)]
        raw = max(cands, key=lambda c: c[1])[0] if cands else None
        values[name] = _parse_receipt_field(name, raw)
        # Slice the crop from the pp-processed page (matching the popup) when a
        # pp is set; otherwise render the raw box (the true scan).
        crops[name] = (_slice_png(page_bgr, rect, 4.0) if page_bgr is not None
                       else _encode_rect_png(page, rect, pp=None, zoom=4.0))
    return values, crops


def test_receipt_slip(page, k: int, pp=None, zoom: float = 2.0,
                      rects=None) -> list[dict]:
    """OCR one page with the given image adjustments and return the ``k``-th
    separated slip's fields (plus the page's slip count) for "Test OCR"."""
    _pp_ctx.pp = pp or None
    try:
        lines = _ocr_page(page, zoom)
    finally:
        _pp_ctx.pp = None
    segs = _real_segments(lines)
    fields = [{"key": "_count", "label": "Slips on page",
               "value": str(len(segs)), "confidence": None}]
    rec = (_process_receipt_segment(page, segs[k], zoom, rects=rects, pp=pp)
           if 0 <= k < len(segs) else {})
    for key, label in _RC_TEST_FIELDS:
        v = rec.get(key)
        fields.append({"key": key, "label": label,
                       "value": "" if v is None else str(v),
                       "confidence": None})
    return fields
