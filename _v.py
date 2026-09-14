from app import create_app
from app.models import CRBookUpload, ReceiptUpload, CRBookRecord, ReceiptRecord
from app.utils import registration_ocr as ocr
from app.blueprints.main.routes import _run_processing
import os, json, numpy as np, cv2
app=create_app()
with app.app_context():
    cr=next(u for u in CRBookUpload.query.all() if 'CR_10092026' in (u.filename or ''))
    rc=ReceiptUpload.query.get(7)
    doc1,_=ocr.open_document(cr.stored_path); doc2,_=ocr.open_document(rc.stored_path)
    print("CR  short_pt=%.0f -> descreen_zoom(5.0)=%.3f"%(min(doc1[0].rect.width,doc1[0].rect.height), ocr._descreen_zoom(doc1[0],5.0)))
    print("RC  short_pt=%.0f -> descreen_zoom(4.0)=%.3f"%(min(doc2[0].rect.width,doc2[0].rect.height), ocr._descreen_zoom(doc2[0],4.0)))
    doc1.close(); doc2.close()
    cruid=cr.id
# reprocess CR (should stay clean) and receipts (restore 4.0 res)
_run_processing(app,"cr",cruid)
for uid in (7,1,3,2): _run_processing(app,"rc",uid)
def marg(p):
    if not p or not os.path.exists(p): return "MISSING"
    im=cv2.imread(p,cv2.IMREAD_GRAYSCALE); h=im.shape[0]; m=max(1,int(h*0.22))
    mg=np.concatenate([im[:m],im[-m:]]); return f"{im.shape[1]}x{im.shape[0]} marg={100*np.sum(mg<128)/mg.size:4.1f}%"
with app.app_context():
    ccd=app.config["CR_BOOK_CROP_DIR"]; rcd=app.config["RECEIPT_CROP_DIR"]
    print("=== CR upload (should stay clean, ~2.5 zoom) ===")
    for r in CRBookRecord.query.filter_by(upload_id=cruid).all()[:3]:
        print(f"  id={r.id} reg:{marg(os.path.join(ccd,str(cruid),r.reg_crop))}  chassis:{marg(os.path.join(ccd,str(cruid),r.chassis_crop))}")
    print("=== Receipt upload 7 (should be back to 4.0 res ~320px) ===")
    for r in ReceiptRecord.query.filter_by(upload_id=7).all()[:3]:
        fc=json.loads(r.field_crops) if r.field_crops else {}
        print(f"  id={r.id} receipt_no:{marg(os.path.join(rcd,'7',fc.get('receipt_no','')))}  vehicle_no:{marg(os.path.join(rcd,'7',fc.get('vehicle_no','')))}")
