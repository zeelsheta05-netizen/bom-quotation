"""Confidence scoring from price-range width, provenance and open questions.

score = (0.55 * range score + 0.30 * evidence score + 0.15 * image score) x open-question penalty

* range score    - how tight the low..high quote is. Every uncertainty (weight range, carat
                   range, unknown grade, natural-vs-lab) already widens the money range, so this
                   is the most direct measure of how far the real cost can be from the estimate.
* evidence score - cost-weighted provenance: declared/verified 1.0, observed 0.9, computed from
                   observed measurements 0.8, inferred 0.55, unknown 0.25.
* image score    - image type x quality (a sketch can never give observed karat or grades).
* penalty        - x0.85 for each open question that blocks a firm quote (max three).
"""

IMAGE_KIND = {"photo": 1.0, "catalog": 0.9, "cad_render": 0.92, "technical_drawing": 0.88, "sketch": 0.65, "other": 0.5}
IMAGE_QUALITY = {"excellent": 1.0, "good": 0.9, "fair": 0.75, "poor": 0.55}
COVERAGE = {"multiple_angles": 1.0, "single_clear": 0.88, "partial": 0.7, "obstructed": 0.55}  # legacy quotes
PROV = {"declared": 1.0, "observed": 0.9, "computed": 0.8, "inferred": 0.55, "unknown": 0.25}


def _p(v):
    return PROV.get(v, 0.55)


def _metal_prov(m):
    if m.get("verified"):
        return 1.0
    weight = _p(m.get("weight_prov"))
    return 0.4 * _p(m.get("metal_prov")) + 0.6 * weight


def _stone_prov(s):
    if s.get("verified"):
        return 1.0
    carat = _p(s.get("carat_prov"))
    if s.get("carat_prov") == "computed":  # computed carat is only as good as the measurement behind it
        carat = 0.8 if s.get("size_prov") == "observed" else 0.6
    origin = 1.0 if s.get("stone_type") != "Diamond" else _p(s.get("origin_prov"))
    return (0.25 * _p(s.get("species_prov")) + 0.15 * _p(s.get("count_prov")) + 0.3 * carat
            + 0.2 * _p(s.get("quality_prov")) + 0.1 * origin)


def compute(q):
    t = q["totals"]
    total = max(t["total"], 0.01)
    lo, hi = t.get("total_low", total), t.get("total_high", total)
    rel = max(hi - lo, 0) / (2 * total)
    range_score = 1 / (1 + 3 * rel)

    lab = q["labor"]
    if lab.get("override"):
        labor_p = 1.0
    else:
        parts = [(max(b["cost"], 0.01), 0.95 if b["source"].startswith("Your labor") else
                  0.7 if b["source"].startswith("Operations") else 0.55) for b in lab["breakdown"]]
        labor_p = sum(w * s for w, s in parts) / sum(w for w, _ in parts) if parts else 0.6
    rows = [(m["cost"], _metal_prov(m)) for m in q["metals"]] + [(s["cost"], _stone_prov(s)) for s in q["stones"]] \
        + [(lab["cost"], labor_p)]
    wsum = sum(max(w, 0) for w, _ in rows) or 1
    evidence = sum(max(w, 0) * s for w, s in rows) / wsum

    ev = q.get("evidence") or {}
    kind = ev.get("image_kind") or q.get("image_kind") or "other"
    quality = ev.get("image_quality") or "good"
    image = IMAGE_KIND.get(kind, 0.5) * IMAGE_QUALITY.get(quality, 0.8)

    unresolved = q.get("unresolved") or []
    blocking = [u for u in unresolved if u.get("blocks_quote")]
    penalty = 0.85 ** min(len(blocking), 3)
    score = round(min(max((0.55 * range_score + 0.30 * evidence + 0.15 * image) * penalty, 0.05), 0.98), 2)
    level = "High" if score >= 0.8 else "Medium" if score >= 0.6 else "Low"

    tips = [f"{u['resolved_by']} ({u['field']})" for u in unresolved if u.get("resolved_by")][:4]
    if not any(m.get("weight_prov") == "declared" for m in q["metals"]):
        tips.append("Enter the measured gross weight to replace the geometry estimate.")
    if not lab.get("override") and labor_p < 0.9:
        cats = sorted({c["category"].replace("_", " ") for c in q.get("components") or []})
        tips.append(f"Add your labor rates for {', '.join(cats) or 'this piece type'} in Settings.")
    tips = list(dict.fromkeys(tips))[:5]

    declared_share = sum(max(w, 0) for w, s in rows if s >= 0.99) / wsum
    factors = [
        {"key": "range", "name": "Price range width", "score": round(range_score, 2),
         "note": f"±{rel:.0%} around the estimate"},
        {"key": "evidence", "name": "Evidence quality", "score": round(evidence, 2),
         "note": f"{declared_share:.0%} of cost declared or verified by you"},
        {"key": "image", "name": "Image", "score": round(image, 2), "note": f"{kind.replace('_', ' ')}, {quality}"},
        {"key": "open", "name": "Open questions", "score": round(penalty, 2),
         "note": f"{len(blocking)} blocking, {len(unresolved) - len(blocking)} minor"},
    ]
    notes = []
    sd = ev.get("scale_deviation")
    if sd is not None and sd > 0.5:
        notes.append(f"The image-based size estimate differed from your measured weight by {sd:.0%}; "
                     "stone sizes may be off by a similar scale.")
    return score, {"score": score, "level": level, "factors": factors, "tips": tips, "notes": notes,
                   "verified_share": round(declared_share, 2), "range_pct": round(rel, 3)}
