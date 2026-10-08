"""Job pipeline: intake (upload or URL) -> preprocess -> AI analysis -> priced quote."""
import io
import ipaddress
import json
import logging
import os
import random
import socket
import string
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image, ImageOps

from . import ai
from . import catalog as C
from . import db
from . import confidence as CONF
from . import geometry as G
from . import pricing

C_METAL_ID = {"hallmark_visible", "clear_color", "inferred_from_style", "guess"}
C_COUNT = {"counted_exact", "counted_extrapolated", "estimated_density"}
C_SIZE = {"measured_scale", "known_reference", "relative_estimate", "guess"}
C_TYPE = {"certain", "likely", "uncertain"}

log = logging.getLogger("bom.jobs")
MAX_BYTES = 20 * 1024 * 1024
MAX_SIDE = 1568
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
_pool = ThreadPoolExecutor(max_workers=int(os.environ.get("BOM_WORKERS", "3")))


class IntakeError(Exception):
    pass


# ---------------------------------------------------------------- credits
def get_credits():
    return db.kv_get("credits", int(os.environ.get("BOM_TRIAL_CREDITS", "25")))


def _spend_credit():
    db.kv_set("credits", max(get_credits() - 1, 0))


# ---------------------------------------------------------------- intake
def _sniff(data: bytes, declared: str | None) -> str:
    if data[:5] == b"%PDF-":
        return "application/pdf"
    try:
        fmt = Image.open(io.BytesIO(data)).format
    except Exception:
        raise IntakeError("Unsupported file. Use JPG, PNG, WebP or PDF.")
    mt = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif",
          "MPO": "image/jpeg"}.get(fmt)
    if not mt:
        raise IntakeError(f"Unsupported image format ({fmt}). Use JPG, PNG or WebP.")
    return mt


def _check_public_host(url: str):
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise IntakeError("Only public http(s) URLs are supported.")
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except socket.gaierror:
        raise IntakeError("Could not resolve the image URL host.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise IntakeError("That URL points to a private network address.")


def fetch_url(url: str) -> tuple[bytes, str]:
    with httpx.Client(timeout=20, follow_redirects=False,
                      headers={"User-Agent": "Mozilla/5.0 BOM-Quotation/1.0"}) as client:
        for _ in range(4):
            _check_public_host(url)
            with client.stream("GET", url) as r:
                if r.is_redirect:
                    url = urljoin(url, r.headers.get("location", ""))
                    continue
                if r.status_code != 200:
                    raise IntakeError(f"Image URL returned HTTP {r.status_code}.")
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > MAX_BYTES:
                        raise IntakeError("Image is larger than 20 MB.")
                return bytes(buf), url
    raise IntakeError("Too many redirects.")


def _prepare_image(data: bytes) -> tuple[bytes, bytes]:
    """Return (analysis_jpeg, thumbnail_jpeg)."""
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    if getattr(img, "is_animated", False):
        img.seek(0)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    full = img.copy()
    full.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    out = io.BytesIO()
    full.save(out, "JPEG", quality=90)
    thumb = img.copy()
    thumb.thumbnail((160, 160), Image.LANCZOS)
    t = io.BytesIO()
    thumb.save(t, "JPEG", quality=85)
    return out.getvalue(), t.getvalue()


def create_job(data: bytes | None, filename: str | None, url: str | None, inputs: dict) -> dict:
    if get_credits() <= 0:
        raise IntakeError("No analysis credits left.")
    if data is None and not url:
        raise IntakeError("Upload an image or paste an image URL.")
    if data is None:
        data, url = fetch_url(url.strip())
        filename = Path(urlparse(url).path).name or "image-from-url"
    if not data:
        raise IntakeError("The file is empty.")
    if len(data) > MAX_BYTES:
        raise IntakeError("File is larger than 20 MB.")
    media_type = _sniff(data, None)

    job_id = "job_" + uuid.uuid4().hex[:12]
    folder = db.UPLOAD_DIR / job_id
    folder.mkdir(parents=True)
    thumb_path = None
    if media_type == "application/pdf":
        file_path = folder / "source.pdf"
        file_path.write_bytes(data)
    else:
        try:
            full, thumb = _prepare_image(data)
        except Exception as e:
            raise IntakeError(f"Could not read the image: {e}")
        file_path = folder / "source.jpg"
        file_path.write_bytes(full)
        thumb_path = folder / "thumb.jpg"
        thumb_path.write_bytes(thumb)
        media_type = "image/jpeg"
    db.x("INSERT INTO jobs(id, filename, source_url, media_type, file_path, thumb_path, status, inputs, created_at)"
         " VALUES(?,?,?,?,?,?,?,?,?)",
         (job_id, filename or "upload", url, media_type, str(file_path), str(thumb_path) if thumb_path else None,
          "queued", json.dumps(inputs), db.now()))
    _pool.submit(_run, job_id)
    return job_summary(job_id)


def recover_interrupted():
    db.x("UPDATE jobs SET status='failed', error='Interrupted by a server restart. Please retry.', finished_at=?"
         " WHERE status IN ('queued','processing')", (db.now(),))


# ---------------------------------------------------------------- processing
def _quote_id():
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"qj_{int(time.time() * 1000)}_{rand}"


def _run(job_id):
    job = db.q("SELECT * FROM jobs WHERE id=?", (job_id,), one=True)
    db.x("UPDATE jobs SET status='processing', started_at=? WHERE id=?", (db.now(), job_id))
    try:
        inputs = json.loads(job["inputs"])
        analysis = ai.analyze(Path(job["file_path"]).read_bytes(), job["media_type"], inputs)
        if not analysis.get("is_jewellery", True) or not analysis.get("components"):
            raise ai.AnalysisError("No jewellery piece was recognised in this image.")
        quote = build_quote(job_id, analysis, inputs)
        _spend_credit()
        db.x("UPDATE jobs SET status='completed', finished_at=?, quote_id=? WHERE id=?",
             (db.now(), quote["id"], job_id))
    except ai.AnalysisError as e:
        db.x("UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=?", (str(e), db.now(), job_id))
    except Exception as e:
        log.exception("job %s failed", job_id)
        db.x("UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=?",
             (f"Unexpected error: {e}", db.now(), job_id))


def _num(v, default=0.0):
    try:
        return float(str(v).strip().split()[0]) if v not in (None, "") else default
    except (ValueError, IndexError):
        return default


def _int(v, default=0):
    return int(round(_num(v, default)))


def _clean_name(s, fallback):
    s = (s or "").strip()
    return s[:60] if s else fallback


def _pick(v, allowed, default):
    return v if v in allowed else default


def _pv(obj, allowed=None, default="unknown"):
    """(value, provenance) from a {value, provenance} object."""
    if not isinstance(obj, dict):
        return default, "unknown"
    v = obj.get("value")
    prov = _pick(obj.get("provenance"), C.PROVENANCE, "inferred")
    if allowed is not None and v not in allowed:
        return default, "unknown"
    return v, prov


def _mid(lo, hi):
    return (lo + hi) / 2


def state_from_analysis(analysis: dict, inputs: dict) -> tuple[dict, list]:
    warnings = []
    img = analysis.get("image") or {}
    declared_metal = inputs.get("metal") if inputs.get("metal") in C.METALS else None
    declared_size = _num(inputs.get("ring_size")) or None

    components, metals, comp_qty = [], [], {}
    for i, c in enumerate(x for x in analysis.get("components") or [] if isinstance(x, dict)):
        name = _clean_name(c.get("name"), f"Component {i + 1}")
        while name in comp_qty:
            name += " (2)"
        cat = _pick(c.get("category"), C.LABOR_BASE, "other")
        qty = max(_int(c.get("quantity"), 1), 1)
        comp_qty[name] = qty
        m = c.get("metal") or {}
        g = c.get("geometry") or {}
        mtype, mprov = _pv(m.get("metal_type"), C.METALS, C.DEFAULT_METAL)
        if declared_metal:
            mtype, mprov = declared_metal, "declared"
        construction, cprov = _pv(m.get("construction"), C.CONSTRUCTION, "unknown")
        texture, tprov = _pv(m.get("texture_origin"), C.TEXTURE_ORIGINS, "unknown")
        gender = _pick(c.get("gender_style"), C.DEFAULT_RING_SIZE, "unknown")
        rs_obj = g.get("ring_size_us") or {}
        ring_size, rs_prov = _num(rs_obj.get("value")), _pick(rs_obj.get("provenance"), C.PROVENANCE, "inferred")
        if declared_size:
            ring_size, rs_prov = declared_size, "declared"
        if not ring_size or ring_size < 1 or ring_size > 16:
            ring_size, rs_prov = C.DEFAULT_RING_SIZE[gender], "inferred"
        parts = [p for p in g.get("parts") or [] if isinstance(p, dict) and p.get("shape") in C.PART_SHAPES]
        comp = {"name": name, "category": cat, "quantity": qty, "gender_style": gender,
                "construction": construction, "construction_prov": cprov,
                "texture_origin": texture, "texture_prov": tprov,
                "ring_size": ring_size if cat in C.RING_CATEGORIES else None, "ring_size_prov": rs_prov,
                "parts": parts}
        lo, hi, trace = G.metal_weight_range(comp, mtype, ring_size)
        lo, hi = lo * qty, hi * qty
        if hi <= 0:  # the model gave no usable geometry: fall back to typical category range
            wl, wh = C.WEIGHT_RANGE.get(cat, C.WEIGHT_RANGE["other"])
            dens = C.METALS[mtype]["density"] / C.REFERENCE_DENSITY
            lo, hi = wl * dens * qty, min(wh, wl * 4) * dens * qty
            trace = ["no measurable geometry; typical range for the category used"]
            warnings.append(f"{name}: no measurable geometry was returned, so a typical weight range was used.")
        components.append(comp)
        metals.append({"component": name, "metal_type": mtype, "metal_prov": mprov,
                       "color": _pv(m.get("color"))[0], "karat": _pv(m.get("karat"))[0],
                       "weight_low": round(lo, 3), "weight_high": round(hi, 3), "net_weight_g": round(_mid(lo, hi), 3),
                       "weight_prov": "computed", "weight_trace": trace})

    names = set(comp_qty)
    stones = []
    for sg in analysis.get("stone_groups") or []:
        if not isinstance(sg, dict):
            continue
        comp = sg.get("component_name") if sg.get("component_name") in names else (next(iter(names)) if len(names) == 1 else None)
        mult = comp_qty.get(comp, 1)
        count = _int(sg.get("count")) * mult
        if count <= 0:
            continue
        species, sprov = _pv(sg.get("species"), C.STONES, "Other")
        origin, oprov = _pv(sg.get("origin"), C.STONE_ORIGINS, "unknown")
        quality, qprov = _pv(sg.get("quality_tier"), C.QUALITIES, "Standard")
        if (sg.get("quality_tier") or {}).get("value") == "unknown":
            qprov = "unknown"
        fu = sg.get("face_up_mm") or {}
        stone = {
            "id": pricing.new_stone_id(), "component": comp, "role": _pick(sg.get("role"), C.STONE_ROLES, "other"),
            "stone_type": species, "species_prov": sprov, "origin": origin, "origin_prov": oprov,
            "shape": _pick(sg.get("shape"), C.SHAPE_K, "other"), "qty": count,
            "count_visible": _int(sg.get("count_visible")) * mult, "count_rule": str(sg.get("count_rule") or ""),
            "count_prov": _pick(sg.get("count_provenance"), C.PROVENANCE, "inferred"),
            "face_up_mm": {"length": fu.get("length"), "width": fu.get("width")},
            "size_prov": _pick(fu.get("provenance"), C.PROVENANCE, "inferred"),
            "depth_pct": sg.get("depth_pct_assumed"),
            "ai_carat": sg.get("carat_each"),
            "color": _pv(sg.get("color_grade"))[0], "clarity": _pv(sg.get("clarity_grade"))[0],
            "treatment": _pv(sg.get("treatment"))[0], "grade_prov": _pv(sg.get("color_grade"))[1],
            "quality": quality, "quality_prov": qprov,
            "setting": _pick(sg.get("setting_type"), C.SETTING_COST, "other"),
        }
        stone["label"] = f"{stone['role'].title()} {species if species != 'Other' else 'stone'}"
        rng = G.carat_range(stone)
        if rng is None:
            ai_c = sg.get("carat_each") or {}
            rng = (_num(ai_c.get("low")), _num(ai_c.get("high")))
            warnings.append(f"{stone['label']}: no face-up size, so the AI's carat range was used.")
        else:
            ai_c = sg.get("carat_each") or {}
            ai_mid = _mid(_num(ai_c.get("low")), _num(ai_c.get("high")))
            if ai_mid > 0 and not (rng[0] * 0.5 <= ai_mid <= rng[1] * 2):
                warnings.append(f"{stone['label']}: AI carat guess {ai_mid:.3f} ct disagrees with the "
                                f"{rng[0]:.3f}-{rng[1]:.3f} ct computed from its measurements; measurements used.")
        if rng[1] <= 0:
            continue
        stone.update(carat_low=round(rng[0], 4), carat_high=round(rng[1], 4),
                     carat_each=round(_mid(*rng), 4), carat_prov="computed")
        stones.append(stone)

    # user-measured gross weight anchors the metal weight
    gross = inputs.get("gross_weight")
    scale_dev = None
    if gross:
        st_mid = sum(s["qty"] * s["carat_each"] for s in stones) * C.CARAT_TO_GRAMS
        st_unc = sum(s["qty"] * (s["carat_high"] - s["carat_low"]) for s in stones) * C.CARAT_TO_GRAMS / 2
        target = gross - st_mid
        est = sum(m["net_weight_g"] for m in metals)
        geo_lo, geo_hi = sum(m["weight_low"] for m in metals), sum(m["weight_high"] for m in metals)
        if target > 0 and est > 0:
            scale_dev = round(abs(est - target) / target, 3)
            if not (geo_lo <= target <= geo_hi):
                warnings.append(f"Measured gross weight implies {target:.2f} g of metal; the geometry estimate was "
                                f"{geo_lo:.2f}-{geo_hi:.2f} g. The measured weight is used.")
            for m in metals:
                share = m["net_weight_g"] / est
                m.update(net_weight_g=round(target * share, 3), weight_low=round(max(target - st_unc, 0.01) * share, 3),
                         weight_high=round((target + st_unc) * share, 3), weight_prov="declared")
        else:
            warnings.append(f"Entered gross weight {gross} g is below the estimated stone weight "
                            f"{st_mid:.2f} g, so the geometry estimate was kept.")

    for comp in components:
        comp["complexity"] = derive_complexity(comp["name"], analysis.get("operations") or [], stones)

    ops = []
    for o in analysis.get("operations") or []:
        if isinstance(o, dict) and o.get("op") in C.OPERATIONS and _num(o.get("units")) > 0:
            comp = o.get("component_name") if o.get("component_name") in names else (next(iter(names)) if len(names) == 1 else None)
            ops.append({"component": comp, "op": o["op"], "variant": str(o.get("variant") or "").strip().lower(),
                        "units": _num(o.get("units")) * (comp_qty.get(comp, 1) if o["op"] != "cad_modeling" else 1)})

    assumptions = [a for a in analysis.get("assumptions") or [] if isinstance(a, dict)]
    unresolved_ai = [{"field": str(u.get("field", "")), "blocks_quote": bool(u.get("blocks_quote")),
                      "resolved_by": str(u.get("resolved_by", ""))}
                     for u in analysis.get("unresolved") or [] if isinstance(u, dict)]
    state = {
        "metals": metals, "stones": stones, "components": components, "operations": ops,
        "assumptions": assumptions, "unresolved_ai": unresolved_ai, "finishing": [],
        "labor": {"override": False, "cost": None},
        "region": inputs.get("labor_region") if inputs.get("labor_region") in C.LABOR_REGIONS else "USA",
        "evidence": {
            "image_kind": _pick(img.get("type"), CONF.IMAGE_KIND, "other"),
            "image_quality": _pick(img.get("quality"), CONF.IMAGE_QUALITY, "good"),
            "is_greyscale": bool(img.get("is_greyscale")), "px_per_mm": _num(img.get("px_per_mm")),
            "scale_reference": str(img.get("scale_reference") or ""),
            "hints": [h.strip() for h in (inputs.get("stone_hints") or "").split(",") if h.strip()],
            "scale_deviation": scale_dev,
        },
    }
    return state, warnings


COMPLEX_OPS = {"hand_chasing", "repousse", "filigree", "enamel"}


def derive_complexity(comp_name, ops, stones):
    mine = [o for o in ops if isinstance(o, dict) and o.get("component_name") in (comp_name, None, "")] or \
        [o for o in ops if isinstance(o, dict)]
    n_stones = sum(s["qty"] for s in stones if s.get("component") in (comp_name, None))
    hand_hours = sum(_num(o.get("units")) for o in mine if o.get("op") == "hand_fabrication")
    if any(o.get("op") in COMPLEX_OPS for o in mine) or n_stones > 50 or hand_hours > 4:
        return "complex"
    if n_stones <= 1 and len(mine) <= 4:
        return "simple"
    return "medium"


def build_quote(job_id, analysis, inputs):
    state, warnings = state_from_analysis(analysis, inputs)
    priced = pricing.price_state(state)
    quote = {
        "id": _quote_id(), "job_id": job_id, "status": "success",
        "title": _clean_name(analysis.get("title"), "Jewellery Piece").title(),
        "description": analysis.get("description", ""), "image_kind": (analysis.get("image") or {}).get("type", ""),
        "style": "", "craftsmanship_notes": "",
        "warnings": warnings, "notes": [],
        "scenario_version": 0, "inputs": inputs, "created_at": db.now(), "analysis": analysis,
        **priced,
    }
    quote["baseline"] = pricing.extract_state(quote)
    save_quote(quote, new=True)
    return quote


def save_quote(quote, new=False):
    quote["updated_at"] = db.now()
    if new:
        db.x("INSERT INTO quotes(id, job_id, data, created_at, updated_at) VALUES(?,?,?,?,?)",
             (quote["id"], quote["job_id"], json.dumps(quote), quote["created_at"], quote["updated_at"]))
    else:
        db.x("UPDATE quotes SET data=?, updated_at=? WHERE id=?", (json.dumps(quote), quote["updated_at"], quote["id"]))


def load_quote(quote_id):
    row = db.q("SELECT data FROM quotes WHERE id=?", (quote_id,), one=True)
    return json.loads(row["data"]) if row else None


def public_quote(quote):
    """Strip internal fields and attach snapshot count."""
    out = {k: v for k, v in quote.items() if k not in ("analysis", "baseline")}
    if "confidence_detail" not in out:  # quote saved before evidence-based scoring existed
        out["confidence"], out["confidence_detail"] = CONF.compute(out)
    out["snapshot_count"] = db.q("SELECT COUNT(*) AS n FROM snapshots WHERE quote_id=?", (quote["id"],), one=True)["n"]
    return out


def job_summary(job_id):
    j = db.q("SELECT * FROM jobs WHERE id=?", (job_id,), one=True)
    if not j:
        return None
    end = j["finished_at"] or db.now()
    start = j["started_at"] or j["created_at"]
    out = {"id": j["id"], "filename": j["filename"], "status": j["status"], "error": j["error"],
           "created_at": j["created_at"], "duration_s": round(end - start) if j["started_at"] else 0,
           "has_thumbnail": bool(j["thumb_path"]), "is_pdf": j["media_type"] == "application/pdf",
           "quote_id": j["quote_id"], "inputs": json.loads(j["inputs"] or "{}")}
    if j["quote_id"]:
        qd = load_quote(j["quote_id"])
        if qd:
            out["quote_title"] = qd["title"]
            out["quote_total"] = qd["totals"]["total"]
    return out
