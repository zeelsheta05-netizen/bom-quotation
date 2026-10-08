"""FastAPI app. The browser only sends user input and renders what this returns."""
import base64
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import catalog as C
from . import db, jobs, pricing

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="Image to BOM Quotation")
jobs.recover_interrupted()

# Optional password protection (HTTP Basic auth) for hosted / shared testing.
AUTH_USER = os.environ.get("BOM_USERNAME", "team")
AUTH_PASSWORD = os.environ.get("BOM_PASSWORD", "")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if not AUTH_PASSWORD or request.url.path == "/healthz":
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            user, _, pwd = base64.b64decode(header[6:]).decode().partition(":")
        except Exception:
            user, pwd = "", ""
        if secrets.compare_digest(user, AUTH_USER) and secrets.compare_digest(pwd, AUTH_PASSWORD):
            return await call_next(request)
    return Response("Authentication required", status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="BOM Quotation"'})


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.exception_handler(jobs.IntakeError)
def _intake_error(_, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


# ------------------------------------------------------------------ catalog / account
@app.get("/api/catalog")
def get_catalog():
    return {
        "metals": list(C.METALS),
        "stones": [s for s in C.STONES],
        "qualities": list(C.QUALITIES),
        "regions": [{"value": k, "label": f"{k} ({v}x)"} for k, v in C.LABOR_REGIONS.items()],
        "complexities": list(C.COMPLEXITY),
        "default_region": "India",
        "accepted": "image/jpeg,image/png,image/webp,image/gif,application/pdf",
    }


@app.get("/api/account")
def account():
    return {"credits": jobs.get_credits(), "plan": "trial"}


@app.get("/api/metal-rates")
def metal_rates():
    return pricing.all_metal_rates()


# ------------------------------------------------------------------ jobs
def _parse_weight(v: Optional[str]):
    if v is None or str(v).strip() == "":
        return None
    try:
        w = float(v)
    except ValueError:
        raise HTTPException(400, "Gross weight must be a number in grams.")
    if w <= 0 or w > 5000:
        raise HTTPException(400, "Gross weight must be between 0 and 5000 g.")
    return w


def _parse_ring_size(v):
    if v is None or str(v).strip() == "":
        return None
    try:
        size = float(v)
    except ValueError:
        raise HTTPException(400, "Ring size must be a US size number, e.g. 6.5.")
    if not 1 <= size <= 16:
        raise HTTPException(400, "Ring size must be between US 1 and 16.")
    return size


@app.post("/api/jobs")
async def create_job(file: Optional[UploadFile] = File(None), image_url: Optional[str] = Form(None),
                     labor_region: str = Form("India"), gross_weight: Optional[str] = Form(None),
                     stone_hints: Optional[str] = Form(None), metal: Optional[str] = Form(None),
                     ring_size: Optional[str] = Form(None)):
    if labor_region not in C.LABOR_REGIONS:
        raise HTTPException(400, "Unknown labor region.")
    data = await file.read(jobs.MAX_BYTES + 1) if file and file.filename else None
    inputs = {"labor_region": labor_region, "gross_weight": _parse_weight(gross_weight),
              "stone_hints": (stone_hints or "").strip()[:300],
              "metal": metal if metal in C.METALS else None, "ring_size": _parse_ring_size(ring_size)}
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(jobs.create_job, data, file.filename if data is not None else None,
                                   (image_url or "").strip() or None, inputs)


@app.get("/api/jobs")
def list_jobs():
    rows = db.q("SELECT id FROM jobs ORDER BY created_at DESC LIMIT 100")
    return [jobs.job_summary(r["id"]) for r in rows]


@app.get("/api/jobs/{job_id}/thumbnail")
def thumbnail(job_id: str):
    j = db.q("SELECT thumb_path FROM jobs WHERE id=?", (job_id,), one=True)
    if not j or not j["thumb_path"]:
        raise HTTPException(404)
    return FileResponse(j["thumb_path"], media_type="image/jpeg")


@app.get("/api/jobs/{job_id}/image")
def full_image(job_id: str):
    j = db.q("SELECT file_path, media_type FROM jobs WHERE id=?", (job_id,), one=True)
    if not j:
        raise HTTPException(404)
    return FileResponse(j["file_path"], media_type=j["media_type"])


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    j = db.q("SELECT quote_id FROM jobs WHERE id=?", (job_id,), one=True)
    if not j:
        raise HTTPException(404)
    if j["quote_id"]:
        db.x("DELETE FROM snapshots WHERE quote_id=?", (j["quote_id"],))
        db.x("DELETE FROM quotes WHERE id=?", (j["quote_id"],))
    db.x("DELETE FROM jobs WHERE id=?", (job_id,))
    return {"ok": True}


# ------------------------------------------------------------------ quotes
def _get_quote(quote_id):
    qd = jobs.load_quote(quote_id)
    if not qd:
        raise HTTPException(404, "Quote not found.")
    return qd


@app.get("/api/quotes/{quote_id}")
def get_quote(quote_id: str):
    return jobs.public_quote(_get_quote(quote_id))


class MetalEdit(BaseModel):
    metal_type: str
    net_weight_g: float = Field(ge=0, le=5000)
    verified: Optional[bool] = None


class StoneEdit(BaseModel):
    id: Optional[str] = None
    stone_type: str
    qty: int = Field(ge=0, le=10000)
    carat_each: float = Field(ge=0, le=500)
    quality: str
    verified: Optional[bool] = None


class Recalc(BaseModel):
    metals: Optional[list[MetalEdit]] = None
    stones: Optional[list[StoneEdit]] = None
    labor_cost: Optional[float] = Field(None, ge=0, le=10_000_000)
    labor_reset: bool = False
    region: Optional[str] = None


@app.post("/api/quotes/{quote_id}/recalculate")
def recalculate(quote_id: str, body: Recalc):
    quote = _get_quote(quote_id)
    edits = body.model_dump(exclude_none=True)
    state, notes = pricing.apply_edits(quote, edits)
    quote.update(pricing.price_state(state))
    quote["scenario_version"] += 1
    quote["notes"] = notes
    jobs.save_quote(quote)
    return jobs.public_quote(quote)


@app.post("/api/quotes/{quote_id}/reset")
def reset_quote(quote_id: str):
    quote = _get_quote(quote_id)
    quote.update(pricing.price_state(pricing.extract_state(quote["baseline"])))
    quote["scenario_version"] += 1
    quote["notes"] = ["Reset to the original AI estimate, repriced at current rates."]
    jobs.save_quote(quote)
    return jobs.public_quote(quote)


class SnapshotIn(BaseModel):
    label: Optional[str] = None


@app.post("/api/quotes/{quote_id}/snapshots")
def save_snapshot(quote_id: str, body: SnapshotIn):
    quote = _get_quote(quote_id)
    label = (body.label or "").strip()[:80] or f"Scenario v{quote['scenario_version']}"
    db.x("INSERT INTO snapshots(quote_id, label, data, created_at) VALUES(?,?,?,?)",
         (quote_id, label, json.dumps(quote), db.now()))
    return jobs.public_quote(quote)


@app.get("/api/quotes/{quote_id}/snapshots")
def list_snapshots(quote_id: str):
    rows = db.q("SELECT id, label, data, created_at FROM snapshots WHERE quote_id=? ORDER BY id DESC", (quote_id,))
    return [{"id": r["id"], "label": r["label"], "created_at": r["created_at"],
             "total": json.loads(r["data"])["totals"]["total"]} for r in rows]


@app.post("/api/quotes/{quote_id}/snapshots/{snap_id}/restore")
def restore_snapshot(quote_id: str, snap_id: int):
    quote = _get_quote(quote_id)
    row = db.q("SELECT data, label FROM snapshots WHERE id=? AND quote_id=?", (snap_id, quote_id), one=True)
    if not row:
        raise HTTPException(404, "Snapshot not found.")
    snap = json.loads(row["data"])
    quote.update(pricing.price_state(pricing.extract_state(snap)))
    quote["scenario_version"] += 1
    quote["notes"] = [f"Restored snapshot \"{row['label']}\", repriced at current rates."]
    jobs.save_quote(quote)
    return jobs.public_quote(quote)


@app.delete("/api/quotes/{quote_id}/snapshots/{snap_id}")
def delete_snapshot(quote_id: str, snap_id: int):
    db.x("DELETE FROM snapshots WHERE id=? AND quote_id=?", (snap_id, quote_id))
    return jobs.public_quote(_get_quote(quote_id))


# ------------------------------------------------------------------ settings
class InventoryIn(BaseModel):
    stone_type: str = Field(min_length=1, max_length=60)
    color: str = Field("", max_length=40)
    clarity: str = Field("", max_length=40)
    price_per_ct: float = Field(gt=0, le=10_000_000)


def _canonical_stone(name):
    for s in C.STONES:
        if s.lower() == name.strip().lower() or s.lower().replace("-", " ") == name.strip().lower():
            return s
    return name.strip().title()


@app.get("/api/settings/inventory")
def get_inventory():
    return db.q("SELECT * FROM inventory ORDER BY stone_type, id")


@app.post("/api/settings/inventory")
def add_inventory(body: InventoryIn):
    db.x("INSERT INTO inventory(stone_type, color, clarity, price_per_ct, created_at) VALUES(?,?,?,?,?)",
         (_canonical_stone(body.stone_type), body.color.strip(), body.clarity.strip(), body.price_per_ct, db.now()))
    return get_inventory()


@app.put("/api/settings/inventory/{row_id}")
def update_inventory(row_id: int, body: InventoryIn):
    db.x("UPDATE inventory SET stone_type=?, color=?, clarity=?, price_per_ct=? WHERE id=?",
         (_canonical_stone(body.stone_type), body.color.strip(), body.clarity.strip(), body.price_per_ct, row_id))
    return get_inventory()


@app.delete("/api/settings/inventory/{row_id}")
def delete_inventory(row_id: int):
    db.x("DELETE FROM inventory WHERE id=?", (row_id,))
    return get_inventory()


class LaborIn(BaseModel):
    piece_type: str = Field(min_length=1, max_length=40)
    simple: float = Field(ge=0, le=10_000_000)
    medium: float = Field(ge=0, le=10_000_000)
    complex: float = Field(ge=0, le=10_000_000)


@app.get("/api/settings/labor-rates")
def get_labor():
    return db.q("SELECT * FROM labor_rates ORDER BY piece_type")


@app.post("/api/settings/labor-rates")
def add_labor(body: LaborIn):
    name = body.piece_type.strip().title()
    if db.q("SELECT id FROM labor_rates WHERE piece_type=?", (name,), one=True):
        raise HTTPException(400, f"{name} already has labor rates. Edit the existing row.")
    db.x("INSERT INTO labor_rates(piece_type, simple, medium, complex, created_at) VALUES(?,?,?,?,?)",
         (name, body.simple, body.medium, body.complex, db.now()))
    return get_labor()


@app.put("/api/settings/labor-rates/{row_id}")
def update_labor(row_id: int, body: LaborIn):
    db.x("UPDATE labor_rates SET simple=?, medium=?, complex=? WHERE id=?",
         (body.simple, body.medium, body.complex, row_id))
    return get_labor()


@app.delete("/api/settings/labor-rates/{row_id}")
def delete_labor(row_id: int):
    db.x("DELETE FROM labor_rates WHERE id=?", (row_id,))
    return get_labor()


@app.get("/api/settings/preferences")
def get_prefs():
    return pricing.get_preferences()


class PrefsIn(BaseModel):
    prefer_inventory: Optional[bool] = None
    prefer_live_metal: Optional[bool] = None


@app.put("/api/settings/preferences")
def put_prefs(body: PrefsIn):
    return pricing.set_preferences(body.model_dump(exclude_none=True))


# ------------------------------------------------------------------ frontend
app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
