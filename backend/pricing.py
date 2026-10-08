"""Deterministic pricing engine.

The AI produces an *analysis* (what the piece is made of). This module turns an
editable BOM state into money. Both the first quote and every recalculation run
through `price_state`, so the numbers are always computed the same way.
"""
import logging
import threading
import time
import uuid

import httpx

from . import catalog as C
from . import confidence as CONF
from . import geometry as G
from . import db

log = logging.getLogger("bom.pricing")
_spot_cache: dict[str, tuple[float, float]] = {}  # symbol -> (price, fetched_at)
_spot_lock = threading.Lock()
SPOT_TTL = 600


# ---------------------------------------------------------------- preferences
def get_preferences():
    return db.kv_get("preferences", {"prefer_inventory": True, "prefer_live_metal": True})


def set_preferences(p):
    cur = get_preferences()
    cur.update({k: bool(v) for k, v in p.items() if k in cur})
    db.kv_set("preferences", cur)
    return cur


# ---------------------------------------------------------------- metal rates
def _fetch_live(symbol):
    r = httpx.get(f"https://api.gold-api.com/price/{symbol}", timeout=6)
    r.raise_for_status()
    price = float(r.json()["price"])
    if price <= 0:
        raise ValueError("non-positive price")
    return price


def spot_price(symbol, prefer_live=True):
    """Return (usd_per_troy_oz, source_label)."""
    if prefer_live:
        with _spot_lock:
            hit = _spot_cache.get(symbol)
            if hit and time.time() - hit[1] < SPOT_TTL:
                return hit[0], "Live spot"
        try:
            price = _fetch_live(symbol)
            with _spot_lock:
                _spot_cache[symbol] = (price, time.time())
            db.kv_set(f"spot:{symbol}", {"price": price, "at": time.time()})
            return price, "Live spot"
        except Exception as e:  # network down, API changed, etc.
            log.warning("live metal price failed for %s: %s", symbol, e)
    saved = db.kv_get(f"spot:{symbol}")
    if saved:
        return saved["price"], "Cached spot"
    return C.FALLBACK_SPOT[symbol], "Reference fallback"


def metal_price_per_gram(metal_type, prefer_live=True):
    m = C.METALS[metal_type]
    spot, source = spot_price(m["symbol"], prefer_live)
    return spot / C.TROY_OZ_G * m["purity"] * (1 + m["premium"]), source, spot


def all_metal_rates():
    prefs = get_preferences()
    out = []
    for name, m in C.METALS.items():
        ppg, src, spot = metal_price_per_gram(name, prefs["prefer_live_metal"])
        out.append({"metal_type": name, "price_per_g": round(ppg, 2), "spot_per_oz": spot,
                    "symbol": m["symbol"], "source": src})
    return out


# ---------------------------------------------------------------- gemstones
def _norm(s):
    return (s or "").strip().lower()


def _inventory_match(stone):
    rows = [r for r in db.q("SELECT * FROM inventory") if _norm(r["stone_type"]) == _norm(stone["stone_type"])]
    if not rows:
        return None, None
    color, clarity = _norm(stone.get("color")), _norm(stone.get("clarity"))
    exact = [r for r in rows if _norm(r["color"]) == color and _norm(r["clarity"]) == clarity]
    if exact:
        return exact[0]["price_per_ct"], "Inventory (exact grade)"
    by_color = [r for r in rows if color and _norm(r["color"]) == color]
    if by_color:
        return sum(r["price_per_ct"] for r in by_color) / len(by_color), "Inventory (color match)"
    return sum(r["price_per_ct"] for r in rows) / len(rows), "Inventory (type average)"


def stone_price_per_ct(stone, prefs):
    if prefs["prefer_inventory"]:
        price, src = _inventory_match(stone)
        if price is not None:
            return price, src
    stype = stone["stone_type"]
    qmult = C.QUALITIES.get(stone.get("quality"), 1.0)
    carat = max(float(stone.get("carat_each") or 0), 0.001)
    if stype in C.STONES and stype != "Other":
        base, k = C.STONES[stype]
        size = min(max(carat ** k, C.SIZE_FACTOR_MIN), C.SIZE_FACTOR_MAX)
        return base * size * qmult, "Market reference"
    ai = stone.get("ai_price_per_ct")
    if ai and ai > 0:
        return float(ai) * qmult, "AI market estimate"
    base, k = C.STONES["Other"]
    return base * qmult, "Generic reference"


# ---------------------------------------------------------------- labor
def _norm_piece(s):
    s = _norm(s).replace("-", "_").replace(" ", "_")
    return s[:-1] if s.endswith("s") else s


def _user_labor_rate(category):
    cat = _norm_piece(category)
    rows = db.q("SELECT * FROM labor_rates")
    for r in rows:
        if _norm_piece(r["piece_type"]) == cat:
            return r
    for r in rows:
        if cat.endswith(_norm_piece(r["piece_type"])) or _norm_piece(r["piece_type"]).endswith(cat):
            return r
    return None


def _op_cost(op):
    unit, rate = C.OPERATIONS[op["op"]]
    if op["op"] == "stone_setting":
        rate = C.SETTING_COST.get(op.get("variant", "").replace("-", "_").replace(" ", "_"), C.SETTING_COST["other"])
    mult = C.OP_VARIANT_MULT.get(op.get("variant"), 1.0) if op["op"] in ("engraving", "chain_making") else 1.0
    return rate * mult * op["units"]


def compute_labor(state):
    """Returns (mid, low, high, breakdown)."""
    region_mult = C.LABOR_REGIONS.get(state.get("region"), 1.0)
    comps = state.get("components") or [{"name": "Piece", "category": "other", "complexity": "medium", "quantity": 1}]
    ops = state.get("operations") or []
    breakdown, mid, lo, hi = [], 0.0, 0.0, 0.0
    for comp in comps:
        qty = max(int(comp.get("quantity") or 1), 1)
        tier = comp.get("complexity") if comp.get("complexity") in C.COMPLEXITY else "medium"
        user = _user_labor_rate(comp.get("category", "other"))
        if user and user.get(tier) is not None:
            cost = float(user[tier]) * qty
            breakdown.append({"component": comp["name"], "source": f"Your labor rates ({user['piece_type']}, {tier})",
                              "cost": round(cost, 2), "items": []})
            mid, lo, hi = mid + cost, lo + cost, hi + cost
            continue
        weight = sum(m["net_weight_g"] for m in state["metals"] if m.get("component") == comp["name"]) \
            or sum(m["net_weight_g"] for m in state["metals"]) / len(comps)
        mine = [o for o in ops if o.get("component") in (comp["name"], None)] if len(comps) > 1 else ops
        if mine:
            items = [{"op": o["op"], "variant": o.get("variant", ""), "units": o["units"],
                      "unit": C.OPERATIONS[o["op"]][0].split(" (")[0], "cost": round(_op_cost(o) * region_mult, 2)} for o in mine]
            making = C.MAKING_PER_GRAM_OPS * weight * region_mult
            items.append({"op": "metal_working", "variant": "", "units": round(weight, 2), "unit": "grams",
                          "cost": round(making, 2)})
            cost = sum(i["cost"] for i in items)
            source = f"Operations ({state.get('region')} x{region_mult})"
            c_lo, c_hi = cost * 0.8, cost * 1.25
        else:  # no operations listed: generic labor model
            cmult = C.COMPLEXITY[tier]
            base = C.LABOR_BASE.get(comp.get("category"), C.LABOR_BASE["other"]) * cmult * qty
            making = C.MAKING_PER_GRAM * weight * cmult
            comp_stones = [s for s in state["stones"] if s.get("component") in (comp["name"], None)] \
                if len(comps) > 1 else state["stones"]
            setting = sum(C.SETTING_COST.get(s.get("setting"), C.SETTING_COST["other"]) * int(s["qty"]) for s in comp_stones)
            cost = (base + making + setting) * region_mult
            items = []
            source = f"Labor model ({tier}, {state.get('region')} x{region_mult})"
            c_lo, c_hi = cost * 0.7, cost * 1.4
        breakdown.append({"component": comp["name"], "source": source, "cost": round(cost, 2), "items": items})
        mid, lo, hi = mid + cost, lo + c_lo, hi + c_hi
    finishing = sum(C.FINISHING_COST.get(f, 0) for f in state.get("finishing") or []) * region_mult
    if finishing:
        breakdown.append({"component": "Finishing", "source": ", ".join(state["finishing"]), "cost": round(finishing, 2),
                          "items": []})
        mid, lo, hi = mid + finishing, lo + finishing, hi + finishing
    return round(mid, 2), round(lo, 2), round(hi, 2), breakdown


# ---------------------------------------------------------------- full quote
def new_stone_id():
    return "st_" + uuid.uuid4().hex[:8]


STATE_KEYS = ("metals", "stones", "components", "operations", "assumptions", "unresolved_ai", "finishing",
              "labor", "region", "evidence")


def extract_state(q):
    st = {k: q.get(k) for k in STATE_KEYS}
    for k in ("components", "operations", "assumptions", "unresolved_ai", "finishing"):
        st[k] = st[k] or []
    st["labor"] = st["labor"] or {"override": False, "cost": None}
    st["evidence"] = st["evidence"] or {"image_kind": q.get("image_kind"), "ai_confidence": q.get("confidence")}
    return st


def _normalize_rows(state):
    """Fill range/provenance fields for quotes created before ranges existed."""
    for m in state["metals"]:
        w = float(m.get("net_weight_g") or 0)
        m.setdefault("weight_low", w)
        m.setdefault("weight_high", w)
        m.setdefault("weight_prov", "inferred")
        m.setdefault("metal_prov", "inferred")
    for s in state["stones"]:
        c = float(s.get("carat_each") or 0)
        s.setdefault("carat_low", c)
        s.setdefault("carat_high", c)
        s.setdefault("carat_prov", "inferred")
        for f in ("species_prov", "count_prov", "size_prov", "quality_prov", "grade_prov"):
            s.setdefault(f, "inferred")
        s.setdefault("origin", "natural")
        s.setdefault("origin_prov", "inferred")


def _priced_type(stone):
    t = stone["stone_type"]
    if stone.get("origin") == "lab_grown" and t == "Diamond":
        return "Lab-Diamond"
    if stone.get("origin") == "simulant":
        return "Cubic-Zirconia"
    return t


def _stone_cost(stone, carat, quality, prefs, stype=None):
    probe = {**stone, "carat_each": carat, "quality": quality, "stone_type": stype or _priced_type(stone)}
    ppc, src = stone_price_per_ct(probe, prefs)
    return int(stone["qty"]) * carat * ppc, ppc, src


TIERS = ["Commercial", "Standard", "Premium"]


def price_state(state):
    """Compute every derived number for an editable BOM state. Returns a new dict."""
    prefs = get_preferences()
    _normalize_rows(state)
    metals = []
    for m in state["metals"]:
        ppg, src, _ = metal_price_per_gram(m["metal_type"], prefs["prefer_live_metal"])
        w = round(max(float(m["net_weight_g"]), 0.0), 3)
        lo, hi = min(m["weight_low"], w), max(m["weight_high"], w)
        metals.append({**m, "net_weight_g": w, "weight_low": round(lo, 3), "weight_high": round(hi, 3),
                       "price_per_g": round(ppg, 2), "cost": round(w * ppg, 2),
                       "cost_low": round(lo * ppg, 2), "cost_high": round(hi * ppg, 2), "source": src})
    stones = []
    for s in state["stones"]:
        mid_c = float(s["carat_each"])
        lo_c, hi_c = min(s["carat_low"], mid_c), max(s["carat_high"], mid_c)
        q = s.get("quality") if s.get("quality") in C.QUALITIES else "Standard"
        if s.get("quality_prov") == "unknown":
            q_lo, q_hi = "Commercial", "Premium"
        else:
            q_lo = q_hi = q
        cost, ppc, src = _stone_cost(s, mid_c, q, prefs)
        cost_lo, _, _ = _stone_cost(s, lo_c, q_lo, prefs)
        cost_hi, _, _ = _stone_cost(s, hi_c, q_hi, prefs)
        if s["stone_type"] == "Diamond" and s.get("origin") == "unknown":  # could be lab-grown
            cost_lo = min(cost_lo, _stone_cost(s, lo_c, q_lo, prefs, "Lab-Diamond")[0])
        if s["stone_type"] == "Other" and s.get("species_prov") != "declared":
            cost_lo, cost_hi = cost_lo * 0.3, cost_hi * 3
        stones.append({**s, "carat_low": round(lo_c, 4), "carat_high": round(hi_c, 4),
                       "total_carat": round(int(s["qty"]) * mid_c, 3), "price_per_ct": round(ppc, 2),
                       "cost": round(cost, 2), "cost_low": round(min(cost_lo, cost), 2),
                       "cost_high": round(max(cost_hi, cost), 2), "source": src,
                       "priced_as": _priced_type(s)})
    priced = {**state, "metals": metals, "stones": stones}

    auto_mid, auto_lo, auto_hi, breakdown = compute_labor(priced)
    labor = dict(state.get("labor") or {})
    if labor.get("override") and labor.get("cost") is not None:
        l_mid = l_lo = l_hi = float(labor["cost"])
    else:
        l_mid, l_lo, l_hi = auto_mid, auto_lo, auto_hi
    priced["labor"] = {"cost": round(l_mid, 2), "cost_low": round(l_lo, 2), "cost_high": round(l_hi, 2),
                       "auto_cost": auto_mid, "override": bool(labor.get("override")), "breakdown": breakdown}

    t_metal = round(sum(m["cost"] for m in metals), 2)
    t_stone = round(sum(s["cost"] for s in stones), 2)
    net = sum(m["net_weight_g"] for m in metals)
    stone_wt = sum(s["total_carat"] for s in stones) * C.CARAT_TO_GRAMS
    total = round(t_metal + t_stone + priced["labor"]["cost"], 2)
    priced["totals"] = {
        "metal": t_metal, "stones": t_stone, "labor": priced["labor"]["cost"], "total": total,
        "total_low": round(sum(m["cost_low"] for m in metals) + sum(s["cost_low"] for s in stones) + l_lo, 2),
        "total_high": round(sum(m["cost_high"] for m in metals) + sum(s["cost_high"] for s in stones) + l_hi, 2),
        "net_weight_g": round(net, 3), "stone_weight_g": round(stone_wt, 3),
        "gross_weight_g": round(net + stone_wt, 2),
        "gross_weight_low": round(sum(m["weight_low"] for m in metals)
                                  + sum(s["qty"] * s["carat_low"] for s in stones) * C.CARAT_TO_GRAMS, 2),
        "gross_weight_high": round(sum(m["weight_high"] for m in metals)
                                   + sum(s["qty"] * s["carat_high"] for s in stones) * C.CARAT_TO_GRAMS, 2),
        "total_carat": round(sum(s["total_carat"] for s in stones), 3),
    }
    priced["unresolved"] = find_unresolved(priced)
    priced["status"] = "indicative" if any(u["blocks_quote"] for u in priced["unresolved"]) else "firm"
    priced["confidence"], priced["confidence_detail"] = CONF.compute(priced)
    return priced


def find_unresolved(q):
    """Sensitivity analysis: anything whose uncertainty swings the total by >20% blocks a firm quote."""
    total = max(q["totals"]["total"], 0.01)
    out = []

    def add(field, swing, resolved_by, source="computed"):
        out.append({"field": field, "swing": round(swing, 2), "share": round(swing / total, 3),
                    "blocks_quote": swing / total > 0.2, "resolved_by": resolved_by, "source": source})

    for m in q["metals"]:
        comp = m.get("component") or "metal"
        if m.get("weight_prov") not in ("declared",):
            add(f"{comp}: metal weight {m['weight_low']:.2f}-{m['weight_high']:.2f} g",
                m["cost_high"] - m["cost_low"], "Weigh the piece (enter gross weight) or give exact dimensions / ring size.")
        if m.get("metal_prov") in ("inferred", "unknown"):
            add(f"{comp}: metal {m['metal_type']} is {m['metal_prov']}", m["cost"] * 0.3,
                "Check the hallmark or do an XRF / acid test, or declare the metal.")
    for s in q["stones"]:
        name = s.get("label") or s["stone_type"]
        if s.get("carat_prov") != "declared" and s["carat_high"] > s["carat_low"]:
            add(f"{name}: carat {s['carat_low']:.3f}-{s['carat_high']:.3f} each",
                max(s["cost_high"] - s["cost_low"], 0) * 0.6,
                "Measure length, width and depth with a gauge, or weigh the loose stone.")
        if s.get("species_prov") in ("inferred", "unknown") and s["cost"] / total > 0.1:
            add(f"{name}: species {s['stone_type']} is {s.get('species_prov')}", s["cost"] * 0.5,
                "Gemmological test (refractometer / spectroscope) or a lab report.")
        if s["stone_type"] == "Diamond" and s.get("origin") in ("unknown",) and s.get("origin_prov") != "declared":
            add(f"{name}: natural vs lab-grown unknown", s["cost"] - s["cost_low"],
                "Diamond tester with lab-grown detection, or a grading report.")
        if s.get("quality_prov") == "unknown":
            add(f"{name}: colour/clarity grade unknown", s["cost_high"] - s["cost_low"],
                "Grade under a loupe in daylight, or a grading report.")
    all_metal_decl = all(m.get("metal_prov") == "declared" and m.get("weight_prov") == "declared" for m in q["metals"])
    all_stone_decl = all(s.get("verified") for s in q["stones"])
    stone_words = ("stone", "species", "carat", "clarity", "colour", "color", "treatment", "origin", "diamond", "emerald")
    for u in q.get("unresolved_ai") or []:
        f = u["field"].lower()
        if all_metal_decl and any(w in f for w in ("metal", "karat", "weight", "construction", "wall")):
            continue
        if all_stone_decl and any(w in f for w in stone_words):
            continue
        out.append({**u, "swing": None, "share": None, "source": "ai"})
    out.sort(key=lambda u: (not u["blocks_quote"], -(u["swing"] or 0)))
    return out


def _mark(row, field):
    row["user_fields"] = sorted(set(row.get("user_fields") or []) | {field})


def apply_edits(quote, edits):
    """Merge a user's what-if edits into the stored state. Returns (new_state, notes).

    Any value the user changes becomes 'declared': its range collapses to that
    value. Ticking a row as verified declares every value in it.
    """
    state = extract_state(quote)
    _normalize_rows(state)
    notes = []
    if "region" in edits and edits["region"] in C.LABOR_REGIONS:
        state["region"] = edits["region"]

    if "metals" in edits:
        new_metals = []
        for i, e in enumerate(edits["metals"]):
            old = dict(state["metals"][i]) if i < len(state["metals"]) else {"component": None}
            row = dict(old)
            mtype = e.get("metal_type") if e.get("metal_type") in C.METALS else old.get("metal_type", C.DEFAULT_METAL)
            weight = float(e.get("net_weight_g", old.get("net_weight_g", 0)) or 0)
            if old.get("metal_type") and mtype != old["metal_type"]:
                _mark(row, "metal_type")
                row["metal_prov"] = "declared"
            if old.get("net_weight_g") is not None and abs(weight - old["net_weight_g"]) > 1e-6:
                _mark(row, "net_weight_g")
                row.update(weight_prov="declared", weight_low=weight, weight_high=weight)
            elif old.get("metal_type") and mtype != old["metal_type"] and row.get("weight_prov") != "declared":
                ratio = C.METALS[mtype]["density"] / C.METALS[old["metal_type"]]["density"]
                weight = round(weight * ratio, 3)
                row["weight_low"] = round(row.get("weight_low", weight) * ratio, 3)
                row["weight_high"] = round(row.get("weight_high", weight) * ratio, 3)
                notes.append(f"Net weight rescaled x{ratio:.2f} for {mtype} density (same design volume).")
            if e.get("verified") is not None:
                row["verified"] = bool(e["verified"])
                if row["verified"]:
                    row.update(metal_prov="declared", weight_prov="declared", weight_low=weight, weight_high=weight)
            row.update({"metal_type": mtype, "net_weight_g": max(weight, 0.0)})
            new_metals.append(row)
        state["metals"] = new_metals or state["metals"]

    if "stones" in edits:
        by_id = {s["id"]: s for s in state["stones"]}
        default_comp = state["components"][0]["name"] if state["components"] else None
        new_stones = []
        for e in edits["stones"]:
            existing = by_id.get(e.get("id"))
            if existing:
                base = dict(existing)
            else:
                base = {"id": new_stone_id(), "label": "Added stone", "role": "other", "shape": "round", "setting": "prong",
                        "color": "", "clarity": "", "component": default_comp, "added_by_user": True, "origin": "natural",
                        "user_fields": ["stone_type", "qty", "carat_each", "quality"], "verified": True}
                for f in ("species_prov", "count_prov", "size_prov", "quality_prov", "grade_prov", "origin_prov", "carat_prov"):
                    base[f] = "declared"
            if e.get("stone_type") in C.STONES and existing and e["stone_type"] != existing.get("stone_type"):
                base.update(color="", clarity="", species_prov="declared", stone_type=e["stone_type"])
                _mark(base, "stone_type")
                if base.get("carat_prov") != "declared" and base.get("face_up_mm"):
                    rng = G.carat_range(base)  # same millimetres, new specific gravity
                    if rng:
                        base.update(carat_low=rng[0], carat_high=rng[1], carat_each=round((rng[0] + rng[1]) / 2, 4))
                        notes.append(f"Carat recomputed from the measured size for {e['stone_type']} density.")
            elif e.get("stone_type") in C.STONES:
                base["stone_type"] = e["stone_type"]
            qty = max(int(float(e.get("qty", base.get("qty", 1)) or 0)), 0)
            carat = max(float(e.get("carat_each", base.get("carat_each", 0.1)) or 0), 0.0)
            if existing and qty != existing.get("qty"):
                _mark(base, "qty")
                base["count_prov"] = "declared"
            if existing is None:
                base.update(carat_each=carat, carat_low=carat, carat_high=carat)
            elif abs(carat - float(existing.get("carat_each") or 0)) > 1e-9:
                _mark(base, "carat_each")
                base.update(carat_prov="declared", carat_each=carat, carat_low=carat, carat_high=carat)
            base["qty"] = qty
            if e.get("quality") in C.QUALITIES:
                if existing and e["quality"] != existing.get("quality"):
                    _mark(base, "quality")
                    base["quality_prov"] = "declared"
                base["quality"] = e["quality"]
            base.setdefault("quality", "Standard")
            if e.get("verified") is not None:
                base["verified"] = bool(e["verified"])
                if base["verified"]:
                    for f in ("species_prov", "count_prov", "quality_prov", "grade_prov", "carat_prov"):
                        base[f] = "declared"
                    if base.get("origin") == "unknown":
                        base["origin"] = "natural"
                    base["origin_prov"] = "declared"
                    base.update(carat_low=base["carat_each"], carat_high=base["carat_each"])
            new_stones.append(base)
        state["stones"] = new_stones

    if edits.get("labor_reset"):
        state["labor"] = {"override": False, "cost": None}
    elif edits.get("labor_cost") is not None:
        cost = max(float(edits["labor_cost"]), 0.0)
        if abs(cost - float(quote["labor"]["cost"])) > 0.005:
            state["labor"] = {"override": True, "cost": cost}
    return state, notes
