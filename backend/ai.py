"""Vision analysis: image/PDF -> structured jewellery bill of materials.

Providers (BOM_AI_PROVIDER): "gemini" (default, free tier), "ollama" (local, free), "anthropic" (paid).

The model identifies *what* the piece is made of (components, metal, weights,
stones, settings, complexity). Prices are never taken on trust from the model
except as a last-resort fallback for exotic stones; the pricing engine owns money.
"""
import base64
import json
import logging
import os

import time

import httpx

from . import catalog as C

log = logging.getLogger("bom.ai")
PROVIDER = os.environ.get("BOM_AI_PROVIDER", "gemini").strip().lower()
GEMINI_MODELS = [m.strip() for m in os.environ.get(
    "BOM_GEMINI_MODEL", "gemini-3.5-flash,gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash-lite").split(",") if m.strip()]
GEMINI_TIMEOUT = float(os.environ.get("BOM_GEMINI_TIMEOUT", "90"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("BOM_OLLAMA_MODEL", "qwen2.5vl:7b")
ANTHROPIC_MODEL = os.environ.get("BOM_MODEL", "claude-opus-5-5")
ANTHROPIC_EFFORT = os.environ.get("BOM_EFFORT", "high")

OPS_TEXT = "; ".join(f"{k} (units = {v[0]})" for k, v in C.OPERATIONS.items())

SYSTEM = f"""You extract jewellery specs from one image into JSON. You never price, never do arithmetic
(no gram weights, no totals), never emit a currency symbol, rate or total. The server computes weight,
carat and cost from your measurements.

Tag every value with provenance: observed | declared | inferred | unknown.
- observed: directly visible and measurable in this image.
- declared: stated by the user in the request text (hints, measured weight, metal, ring size).
- inferred: your judgement from style, context or typical practice.
- unknown: cannot be determined; give your best range anyway and say so.

These are NEVER observed, at any image quality: karat, metal type from a greyscale/sketch image, stone
species, natural vs lab, colour grade, clarity, treatment, construction (solid/hollow/stamped), wall
thickness, weight. A sharper image does not make them knowable. Use inferred or unknown.

Works for any jewellery: rings, bands, earrings, pendants, necklaces, chains, bracelets, bangles, brooches,
sets, bridal pieces. Split the piece into components (a set = several components). quantity = number of
identical units (a pair of earrings is quantity 2 of one earring). Describe ONE unit's metal as geometric parts,
and give stone counts and operation units for ONE unit too (the server multiplies by quantity):
- ring_band: width_mm, thickness_mm, diameter_mm = inner diameter (0 if unknown; ring size drives it).
- bangle: width_mm, thickness_mm, diameter_mm = inner diameter.
- chain: length_mm, thickness_mm = wire gauge, style = link style.
- wire: length_mm, thickness_mm = wire diameter (hooks, frames, prongs as a group with count).
- sheet / cast_block: length_mm, width_mm, thickness_mm = envelope; fill_fraction = share of that envelope
  that is metal. The envelope includes the stones and air, so remove them: a prong head or halo around a
  large stone is mostly stone and air (fill 0.08-0.25), an openwork or filigree motif 0.2-0.45, a bezel cup
  0.3-0.5, a solid signet/plate 0.6-0.9. Heads, settings, pendant bodies, motifs, clasps and findings are
  cast_block. Describe the shank/band separately from the head; do not let parts overlap.
Every dimension is a {{low, high}} range in mm; use 0/0 for dimensions that do not apply.

Rules:
- Scale all dimensions from a reference (ring shank, prongs, ear post ~0.8mm, chain gauge, known stone sizes,
  any ruler) and report px_per_mm (0 if no reference). If ring size isn't declared, assume US 6.5 (women's) /
  US 10 (men's) and tag it inferred.
- Carat weight is never a fact. Give face-up mm (length, width ranges), assumed depth % range, and a carat
  range per stone.
- Count stones. State the visible count, the total count, and the symmetry rule used (e.g. "14 visible on
  front half, mirrored -> 28").
- Mandatory on any band over 5mm wide: construction and texture_origin (hand_chased | die_struck | cast_in |
  machined | unknown).
- List the labour operations needed, with units: {OPS_TEXT}. variant: setting type for stone_setting,
  hand/laser/machine for engraving, handmade/machine for chain_making, else "".
- Every inferred field needs an entry in assumptions[] with the alternative you rejected and which way it
  moves cost (up = the alternative would cost more). Empty assumptions[] = invalid output.
- Anything unknown that would change cost by >20% goes in unresolved[] with blocks_quote: true and what
  view or measurement would settle it.
- Prefer "unknown" over a plausible guess. Output JSON only, no prose.
Use only these names. Metals: {", ".join(C.METALS)}, unknown. Stones: {", ".join(C.STONES)}, unknown."""

S = {"type": "string"}
N = {"type": "number"}
I = {"type": "integer"}
B = {"type": "boolean"}


def _enum(values):
    return {"type": "string", "enum": list(values)}


def _obj(props):
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


PROV = _enum(C.PROVENANCE)
RANGE = _obj({"low": N, "high": N})
PSTR = _obj({"value": S, "provenance": PROV})


def _penum(values):
    return _obj({"value": _enum(values), "provenance": PROV})


SCHEMA = _obj({
    "is_jewellery": B,
    "title": S,
    "description": S,
    "image": _obj({"type": _enum(["photo", "sketch", "cad_render", "catalog", "technical_drawing", "other"]),
                   "quality": _enum(["excellent", "good", "fair", "poor"]),
                   "is_greyscale": B, "px_per_mm": N, "scale_reference": S}),
    "components": {"type": "array", "items": _obj({
        "name": S, "category": _enum(C.CATEGORIES), "quantity": I,
        "gender_style": _enum(["womens", "mens", "unisex", "unknown"]),
        "metal": _obj({
            "color": PSTR, "karat": PSTR,
            "metal_type": _penum(list(C.METALS) + ["unknown"]),
            "construction": _penum(["solid", "hollow", "stamped", "openwork", "unknown"]),
            "texture_origin": _penum(C.TEXTURE_ORIGINS),
        }),
        "geometry": _obj({
            "ring_size_us": _obj({"value": N, "provenance": PROV}),
            "wall_thickness_mm": _obj({"low": N, "high": N, "provenance": PROV}),
            "parts": {"type": "array", "items": _obj({
                "name": S, "shape": _enum(C.PART_SHAPES), "style": _enum(C.CHAIN_STYLES), "count": I,
                "length_mm": RANGE, "width_mm": RANGE, "thickness_mm": RANGE, "diameter_mm": RANGE,
                "fill_fraction": RANGE, "provenance": PROV,
            })},
        }),
    })},
    "stone_groups": {"type": "array", "items": _obj({
        "component_name": S, "role": _enum(C.STONE_ROLES),
        "species": _penum(list(C.STONES) + ["unknown"]),
        "origin": _penum(C.STONE_ORIGINS),
        "shape": _enum(C.SHAPES),
        "count": I, "count_visible": I, "count_rule": S, "count_provenance": PROV,
        "face_up_mm": _obj({"length": RANGE, "width": RANGE, "provenance": PROV}),
        "depth_pct_assumed": RANGE,
        "carat_each": RANGE,
        "color_grade": PSTR, "clarity_grade": PSTR, "treatment": PSTR,
        "quality_tier": _penum(list(C.QUALITIES) + ["unknown"]),
        "setting_type": _enum(C.SETTINGS),
    })},
    "operations": {"type": "array", "items": _obj({
        "component_name": S, "op": _enum(C.OPERATIONS), "variant": S, "units": N})},
    "assumptions": {"type": "array", "items": _obj({
        "field": S, "chose": S, "reason": S, "alternative": S, "cost_direction": _enum(["up", "down", "neutral"])})},
    "unresolved": {"type": "array", "items": _obj({"field": S, "blocks_quote": B, "resolved_by": S})},
})

NEVER_OBSERVED_METAL = ("karat", "construction")


def enforce_rules(a: dict) -> list:
    """Apply the never-observed rules in code and return validation problems for a retry."""
    problems = []
    grey = bool((a.get("image") or {}).get("is_greyscale")) or (a.get("image") or {}).get("type") == "sketch"
    for comp in a.get("components") or []:
        m = comp.get("metal") or {}
        for f in NEVER_OBSERVED_METAL:
            if (m.get(f) or {}).get("provenance") == "observed":
                m[f]["provenance"] = "inferred"
        if grey and (m.get("metal_type") or {}).get("provenance") == "observed":
            m["metal_type"]["provenance"] = "inferred"
        g = comp.get("geometry") or {}
        if (g.get("wall_thickness_mm") or {}).get("provenance") == "observed":
            g["wall_thickness_mm"]["provenance"] = "inferred"
        if not g.get("parts"):
            problems.append(f"component '{comp.get('name')}' has no geometric parts")
        wide = [p for p in g.get("parts") or [] if p.get("shape") in ("ring_band", "bangle")
                and float((p.get("width_mm") or {}).get("high") or 0) > 5]
        if wide and not (m.get("texture_origin") or {}).get("value"):
            problems.append(f"component '{comp.get('name')}' has a band over 5mm but no texture_origin")
    for sg in a.get("stone_groups") or []:
        for f in ("species", "origin", "color_grade", "clarity_grade", "treatment"):
            if (sg.get(f) or {}).get("provenance") == "observed":
                sg[f]["provenance"] = "inferred"
        fu = sg.get("face_up_mm") or {}
        if not float((fu.get("length") or {}).get("high") or 0):
            problems.append(f"stone group '{sg.get('role')} {(sg.get('species') or {}).get('value')}' has no face-up size")
    if not a.get("assumptions"):
        problems.append("assumptions[] is empty, which is invalid")
    if a.get("is_jewellery", True) and not a.get("components"):
        problems.append("no components listed")
    return problems


class AnalysisError(Exception):
    pass


def _user_text(hints: dict) -> str:
    lines = ["Extract the specs of this jewellery piece as JSON matching the schema."]
    if hints.get("stone_hints"):
        lines.append(f"User stone hints: {hints['stone_hints']}")
    if hints.get("gross_weight"):
        lines.append(f"User-measured GROSS weight (metal + stones): {hints['gross_weight']} g. "
                     "Keep your net metal estimate consistent with it.")
    if hints.get("metal"):
        lines.append(f"User-declared metal: {hints['metal']}.")
    if hints.get("ring_size"):
        lines.append(f"User-declared ring size: US {hints['ring_size']}.")
    if hints.get("labor_region"):
        lines.append(f"Manufacturing region: {hints['labor_region']}.")
    if hints.get("_feedback"):
        lines.append("Your previous output was invalid: " + "; ".join(hints["_feedback"]) + ". Fix these and resend.")
    return "\n".join(lines)


def _parse_json(text: str) -> dict:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        a, b = t.find("{"), t.rfind("}")
        try:
            data = json.loads(t[a:b + 1]) if a >= 0 and b > a else None
        except json.JSONDecodeError:
            data = None
    if not isinstance(data, dict):
        raise AnalysisError("The AI returned output that could not be read. Please retry.")
    return data


SCHEMA_TEXT = json.dumps(SCHEMA, separators=(",", ":"))


# ---------------------------------------------------------------- Gemini (free tier)
def _to_gemini_schema(node):
    """JSON Schema -> Gemini OpenAPI-style responseSchema (uppercase types, no additionalProperties)."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "additionalProperties":
                continue
            if k == "type":
                out[k] = v.upper()
            elif k in ("properties",):
                out[k] = {pk: _to_gemini_schema(pv) for pk, pv in v.items()}
            elif k == "items":
                out[k] = _to_gemini_schema(v)
            else:
                out[k] = v
        return out
    return node


GEMINI_SCHEMA = _to_gemini_schema(SCHEMA)


def _gemini(file_bytes, media_type, hints):
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise AnalysisError("No Gemini API key found. Get a free key at https://aistudio.google.com/apikey, "
                            "set GEMINI_API_KEY in .env and restart.")
    parts = [{"inlineData": {"mimeType": media_type, "data": base64.standard_b64encode(file_bytes).decode()}},
             {"text": _user_text(hints)}]
    body = {"systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": GEMINI_SCHEMA,
                                 "temperature": 0.2, "maxOutputTokens": 16384}}
    last, rate_limited = "No Gemini model is available for this key.", False
    schema_ok = True
    for round_no in range(2):  # second pass after a pause, in case every model was busy
        if round_no:
            time.sleep(15)
        for model in GEMINI_MODELS:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            for _ in range(2):  # second try only when the schema is rejected
                req = body if schema_ok else {**body, "generationConfig": {
                    k: v for k, v in body["generationConfig"].items() if k != "responseSchema"},
                    "contents": [{"role": "user", "parts": parts + [{"text": "JSON schema to follow exactly:\n" + SCHEMA_TEXT}]}]}
                try:
                    r = httpx.post(url, json=req, headers={"x-goog-api-key": key},
                                   timeout=httpx.Timeout(GEMINI_TIMEOUT, connect=15))
                except httpx.TimeoutException:
                    last = f"Gemini model {model} did not respond in time."
                    log.warning("gemini %s timed out, trying next model", model)
                    break
                except httpx.HTTPError:
                    raise AnalysisError("Could not reach the Gemini API. Check the network connection.")
                if r.status_code == 200:
                    data = r.json()
                    cands = data.get("candidates") or []
                    if not cands:
                        reason = (data.get("promptFeedback") or {}).get("blockReason", "no result")
                        raise AnalysisError(f"Gemini returned no analysis ({reason}).")
                    text = "".join(p.get("text", "") for p in (cands[0].get("content") or {}).get("parts", [])
                                   if not p.get("thought"))
                    if cands[0].get("finishReason") == "MAX_TOKENS" and not text.rstrip().endswith("}"):
                        raise AnalysisError("The AI response was cut off. Try a simpler or cropped image.")
                    log.info("gemini analysis done with %s", model)
                    return _parse_json(text)
                msg = ""
                try:
                    msg = r.json().get("error", {}).get("message", "")
                except ValueError:
                    pass
                if r.status_code == 400 and "API key" in msg:
                    raise AnalysisError("The Gemini API key is invalid. Create one at https://aistudio.google.com/apikey.")
                if r.status_code == 400 and schema_ok and ("schema" in msg.lower() or "field" in msg.lower()):
                    log.warning("gemini rejected schema (%s); retrying with schema in prompt", msg)
                    schema_ok = False
                    continue
                if r.status_code == 403:
                    raise AnalysisError(f"Gemini refused the request: {msg or 'permission denied'}")
                rate_limited = rate_limited or r.status_code == 429
                last = f"Gemini {model} error ({r.status_code}): {msg or 'unknown error'}"
                log.warning("%s; trying next model", last)
                break
    if rate_limited:
        raise AnalysisError("Gemini free-tier limit reached. Wait a minute (or until tomorrow for the daily limit) and retry.")
    raise AnalysisError(f"All Gemini models are busy right now. Please retry shortly. ({last})")


# ---------------------------------------------------------------- Ollama (local, free)
def _ollama(file_bytes, media_type, hints):
    if media_type == "application/pdf":
        raise AnalysisError("The local Ollama model cannot read PDFs. Upload a JPG, PNG or WebP image.")
    body = {"model": OLLAMA_MODEL, "stream": False, "format": SCHEMA, "options": {"temperature": 0.2},
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": _user_text(hints),
                          "images": [base64.standard_b64encode(file_bytes).decode()]}]}
    try:
        r = httpx.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=900)
    except httpx.HTTPError:
        raise AnalysisError(f"Ollama is not running at {OLLAMA_URL}. Install it from https://ollama.com and run "
                            f"'ollama pull {OLLAMA_MODEL}'.")
    if r.status_code != 200:
        raise AnalysisError(f"Ollama error ({r.status_code}): {r.text[:200]}")
    return _parse_json((r.json().get("message") or {}).get("content", ""))


# ---------------------------------------------------------------- Anthropic (paid, optional)
def _anthropic(file_bytes, media_type, hints):
    import anthropic
    data = base64.standard_b64encode(file_bytes).decode()
    kind = "document" if media_type == "application/pdf" else "image"
    block = {"type": kind, "source": {"type": "base64", "media_type": media_type, "data": data}}
    try:
        resp = anthropic.Anthropic(timeout=600, max_retries=2).beta.messages.create(
            model=ANTHROPIC_MODEL, max_tokens=16000, system=SYSTEM,
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            output_config={"effort": ANTHROPIC_EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": [block, {"type": "text", "text": _user_text(hints)}]}],
        )
    except anthropic.APIStatusError as e:
        raise AnalysisError(f"Anthropic error ({e.status_code}): {e.message}")
    except anthropic.APIConnectionError:
        raise AnalysisError("Could not reach the Anthropic API.")
    except TypeError as e:
        if "authentication" in str(e).lower():
            raise AnalysisError("No Anthropic credentials found. Set ANTHROPIC_API_KEY in .env and restart.")
        raise
    if resp.stop_reason in ("refusal", "max_tokens"):
        raise AnalysisError(f"The AI stopped early ({resp.stop_reason}).")
    return _parse_json(next((b.text for b in resp.content if b.type == "text"), ""))


PROVIDERS = {"gemini": _gemini, "ollama": _ollama, "anthropic": _anthropic}


def analyze(file_bytes: bytes, media_type: str, hints: dict) -> dict:
    fn = PROVIDERS.get(PROVIDER)
    if not fn:
        raise AnalysisError(f"Unknown BOM_AI_PROVIDER '{PROVIDER}'. Use gemini, ollama or anthropic.")
    result = fn(file_bytes, media_type, hints)
    problems = enforce_rules(result)
    if problems and result.get("is_jewellery", True):
        log.info("analysis invalid (%s); asking once more", problems)
        retry = fn(file_bytes, media_type, {**hints, "_feedback": problems})
        if len(enforce_rules(retry)) < len(problems):
            result = retry
    return result
