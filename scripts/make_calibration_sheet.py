"""Generate a Reg-No / Chassis-No labeling crop sheet for calibration.

Renders fixed form-relative regions (the CR certificate layout is fixed) at the
native scan resolution for a sample of pages across every uploaded CR book, then
writes an HTML contact sheet + a CSV template so the correct values can be filled
in by eye.  Output: <repo>/calibration/.
"""
from __future__ import annotations

import base64
import csv
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import cv2  # noqa: E402
import pymupdf  # noqa: E402

from app import create_app  # noqa: E402
from app.models import CRBookUpload  # noqa: E402
from app.utils import registration_ocr as ocr  # noqa: E402

NATIVE_ZOOM = 4.17  # 595 pt page rendered to the native 2480 px (~300 DPI)
READ_UP = 1.6       # extra upscale so 1-bit text is comfortable to read

# Fixed, generous form-relative regions (fractions of page size).  Each includes
# the field label row for context so the value is unambiguous.
REG_ROI = (0.030, 0.082, 0.470, 0.155)
CHS_ROI = (0.500, 0.082, 0.870, 0.155)


def _crop_png_b64(page, roi) -> str:
    W, H = page.rect.width, page.rect.height
    rect = pymupdf.Rect(roi[0] * W, roi[1] * H, roi[2] * W, roi[3] * H)
    bgr = ocr._crop_bgr(page, rect, NATIVE_ZOOM)
    big = cv2.resize(bgr, None, fx=READ_UP, fy=READ_UP,
                     interpolation=cv2.INTER_CUBIC)
    png = cv2.imencode(".png", big)[1].tobytes()
    return base64.b64encode(png).decode("ascii")


def _sample_pages(total: int) -> list[int]:
    if total <= 3:
        return list(range(total))
    return sorted({0, total // 2, total - 1})


def main() -> None:
    app = create_app()
    out_dir = BASE_DIR / "calibration"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    html_parts: list[str] = []

    with app.app_context():
        uploads = CRBookUpload.query.order_by(CRBookUpload.id).all()
        for u in uploads:
            if not os.path.exists(u.stored_path):
                continue
            doc, total = ocr.open_document(u.stored_path)
            name = os.path.basename(u.stored_path)
            for pi in _sample_pages(total):
                page = doc[pi]
                reg_b64 = _crop_png_b64(page, REG_ROI)
                chs_b64 = _crop_png_b64(page, CHS_ROI)
                key = f"up{u.id}_p{pi}"
                rows.append({
                    "upload_id": u.id, "page_index": pi, "file": name,
                    "correct_reg": "", "correct_chassis": "",
                })
                html_parts.append(f"""
      <tr>
        <td class="k">{key}<br><span class="f">{name}</span></td>
        <td><div class="lbl">Registration No</div>
            <img src="data:image/png;base64,{reg_b64}"></td>
        <td><div class="lbl">Chassis No</div>
            <img src="data:image/png;base64,{chs_b64}"></td>
      </tr>""")
            doc.close()

    csv_path = out_dir / "labels.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "upload_id", "page_index", "file",
            "correct_reg", "correct_chassis"])
        w.writeheader()
        w.writerows(rows)

    html = """<!doctype html><html><head><meta charset="utf-8">
<title>AMW-AI CR calibration sheet</title>
<style>
 body{font-family:Segoe UI,Arial,sans-serif;margin:16px;background:#fafafa}
 h1{font-size:18px} p{color:#555}
 table{border-collapse:collapse;width:100%}
 td{border:1px solid #ccc;padding:8px;vertical-align:top;background:#fff}
 td.k{white-space:nowrap;font-weight:600;font-size:13px}
 td.k .f{font-weight:400;color:#888;font-size:11px}
 .lbl{font-size:12px;color:#0a58ca;margin-bottom:4px}
 img{display:block;max-width:520px;border:1px solid #eee}
</style></head><body>
<h1>CR Book calibration sheet</h1>
<p>Read each printed value and record it in <code>calibration/labels.csv</code>
(columns <b>correct_reg</b>, <b>correct_chassis</b>), or paste the values back in
chat keyed by the id in the first column (e.g. <code>up20_p0</code>).</p>
<table>
 <tr><th>Page</th><th>Registration No</th><th>Chassis No</th></tr>
""" + "".join(html_parts) + "\n</table></body></html>"

    html_path = out_dir / "contact_sheet.html"
    html_path.write_text(html, encoding="utf-8")

    print(f"Sampled {len(rows)} pages across {len(uploads)} uploads.")
    print(f"Contact sheet: {html_path}")
    print(f"CSV template:  {csv_path}")


if __name__ == "__main__":
    main()
