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
    return [
        bgr,
        cv2.cvtColor(otsu, cv2.COLOR_GRAY2BGR),
        cv2.cvtColor(adaptive, cv2.COLOR_GRAY2BGR),
        sharp,
    ]


def _rec_candidates(page, rect, zooms=(4, 6, 8, 10)) -> list[tuple[str, float]]:
    """Recognize a cropped region at several zooms x preprocessing variants."""
    out: list[tuple[str, float]] = []
    for z in zooms:
        for im in _preprocess_variants(_crop_bgr(page, rect, z)):
            out.extend(_rec_only(im))
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


# A VIN / chassis number never contains I, O or Q -- map common misreads.
_VIN_FIX = str.maketrans({"I": "1", "O": "0", "Q": "0"})


def _norm_chassis(t: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", (t or "")).upper().translate(_VIN_FIX)


def _norm_reg(t: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "", (t or "")).upper()


def _vote(cands: list[tuple[str, float]], prefer_len: int | None = None):
    """Length + per-character weighted majority vote across OCR passes.

    Groups candidates by length (with an optional prior for ``prefer_len``),
    then for each position picks the highest-weight character.  Returns
    ``(text, confidence)`` where confidence blends mean OCR score, per-character
    agreement and length agreement."""
    from collections import defaultdict

    cands = [(t, w) for t, w in cands if t]
    if not cands:
        return None, 0.0
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
    conf = round(mean_score * char_agree * len_agree, 4)
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
        text, conf = _vote(cands, prefer_len=17)
        cand_strs = [c for c, _ in cands if len(c) >= 15]
        if not text:
            return fallback, None, cand_strs
        return text, conf, cand_strs
    cands = [(_norm_reg(t), w) for t, w in raw
             if len(_norm_reg(t)) >= 4 and any(c.isdigit() for c in _norm_reg(t))]
    if fallback:
        fb = _norm_reg(fallback)
        if len(fb) >= 4:
            cands.append((fb, 0.5))
    text, conf = _vote(cands)
    if not text:
        return fallback, None, []
    return _fix_reg_lk(text), conf, []


def crop_value_png(page, line, full_zoom: float, disp_zoom: float = 5.0):
    """Render just a field's value region to a PNG (bytes) for display beside the
    captured text.  ``line`` is the OCR value line (coords in ``full_zoom``
    pixel space); a little padding keeps the value fully visible.  Returns
    ``None`` when the region is unavailable."""
    if page is None or line is None:
        return None
    import pymupdf
    rect = pymupdf.Rect(
        line["x0"] / full_zoom - 6, line["y0"] / full_zoom - 4,
        line["x1"] / full_zoom + 18, line["y1"] / full_zoom + 4)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(disp_zoom, disp_zoom), clip=rect)
    return pix.tobytes("png")


def _parse_cr_page(lines: list[dict], page=None, full_zoom: float = 3.0) -> dict:
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

    # light validation / cleanup on key identifiers (fallback values)
    if rec.get("chassis_no"):
        m = re.search(r"[A-Z0-9]{8,20}", rec["chassis_no"].replace(" ", ""))
        rec["chassis_no"] = m.group(0) if m else rec["chassis_no"]
    if rec.get("year_of_manufacture"):
        m = re.search(r"(19|20)\d{2}", rec["year_of_manufacture"])
        rec["year_of_manufacture"] = m.group(0) if m else None

    # ---- High-accuracy refinement of the two critical fields --------------
    # Chassis No and Registration No are re-read from a high-zoom crop with
    # multi-scale recognition-only OCR + character voting, correcting the
    # digit/letter confusions (5<->S, 8<->B, I->1, O->0, ...) the single
    # full-page pass makes.
    ch_lbl = _find_label(lines, _CR_LABELS["chassis_no"])
    ch_line = _line_below(lines, ch_lbl) if ch_lbl else None
    ch_val, ch_conf, ch_cands = _refine_field(page, ch_line, full_zoom, "chassis",
                                              rec.get("chassis_no"))
    if ch_val:
        rec["chassis_no"] = ch_val
    rec["chassis_no_confidence"] = ch_conf
    rec["_chassis_cands"] = ch_cands
    rec["_chassis_crop_png"] = crop_value_png(page, ch_line, full_zoom)

    rg_lbl = _find_label(lines, _CR_LABELS["registration_no"])
    rg_line = _line_below(lines, rg_lbl) if rg_lbl else None
    rg_val, rg_conf, _ = _refine_field(page, rg_line, full_zoom, "reg",
                                       rec.get("registration_no"))
    if rg_val:
        rec["registration_no"] = rg_val
    rec["registration_no_confidence"] = rg_conf
    rec["_reg_crop_png"] = crop_value_png(page, rg_line, full_zoom)

    rec["needs_review"] = bool(_chassis_review(rec.get("chassis_no"), ch_conf)
                               or _reg_review(rec.get("registration_no"), rg_conf))

    rec["raw_text"] = full
    rec["confidence"] = _mean_score(lines)
    return rec


def open_document(path: str):
    """Open a PDF/image with PyMuPDF; caller is responsible for closing it."""
    import pymupdf
    doc = pymupdf.open(path)
    return doc, doc.page_count


def extract_cr_page(page, zoom: float = 3.0) -> dict | None:
    """OCR + parse a single CR-book page (one vehicle).  None if page is blank."""
    lines = _ocr(_render_page_png(page, zoom))
    if not lines:
        return None
    return _parse_cr_page(lines, page=page, full_zoom=zoom)


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


def extract_receipt_page(page, zoom: float = 2.0) -> list[dict]:
    """OCR + parse one receipts page into its deposit-slip records."""
    lines = _ocr(_render_page_png(page, zoom))
    out: list[dict] = []
    for seg in _segment_receipts(lines):
        if not seg:
            continue
        rec = _parse_receipt_segment(seg)
        # skip empty noise segments (no slip and no vehicle/application)
        if not any(rec.get(k) for k in
                   ("slip_no", "vehicle_no", "application_no", "receipt_no")):
            continue
        out.append(rec)
    return out


def extract_receipt_records(path: str, zoom: float = 2.0) -> tuple[int, list[dict]]:
    doc, page_count = open_document(path)
    records: list[dict] = []
    seg_no = 0
    try:
        for i in range(page_count):
            for rec in extract_receipt_page(doc[i], zoom):
                seg_no += 1
                rec["segment_no"] = seg_no
                records.append(rec)
    finally:
        doc.close()
    return page_count, records
