"""Deterministic measurement maths: metal weight and carat ranges from millimetres.

The AI only reports dimensions (with ranges and provenance). All arithmetic
happens here so it is repeatable and auditable.
"""
import math

from . import catalog as C


def _rng(d, default=(0.0, 0.0)):
    if not isinstance(d, dict):
        return default
    lo, hi = float(d.get("low") or 0), float(d.get("high") or 0)
    if lo <= 0 and hi <= 0:
        return default
    lo, hi = (lo or hi), (hi or lo)
    return (min(lo, hi), max(lo, hi))


def part_volume_mm3(part, ring_size, category):
    """(low, high) metal volume of one geometric part in mm^3, before construction factor."""
    shape = part.get("shape")
    L, W, T = _rng(part.get("length_mm")), _rng(part.get("width_mm")), _rng(part.get("thickness_mm"))
    D = _rng(part.get("diameter_mm"))
    fill = _rng(part.get("fill_fraction"), (1.0, 1.0))
    fill = (min(max(fill[0], 0.02), 1.0), min(max(fill[1], 0.02), 1.0))
    count = max(int(part.get("count") or 1), 1)
    out = []
    for i in (0, 1):
        if shape in ("ring_band", "bangle"):
            if D[i] > 0:
                inner = D[i]
            elif shape == "ring_band":
                inner = C.ring_inner_diameter_mm(ring_size)
            else:
                inner = C.DEFAULT_BANGLE_ID_MM
            v = math.pi * (inner + T[i]) * W[i] * T[i] * C.BAND_PROFILE_FACTOR
        elif shape in ("chain", "wire"):
            wire_d = T[i] or D[i]
            k = C.CHAIN_STYLES.get(part.get("style"), 2.5) if shape == "chain" else 1.0
            v = L[i] * math.pi * (wire_d / 2) ** 2 * k
        else:  # sheet, cast_block
            v = L[i] * W[i] * T[i]
        out.append(v * fill[i] * count)
    return tuple(out)


def metal_weight_range(component, metal_type, ring_size):
    """Return (low_g, high_g, trace list) for one unit of a component."""
    density = C.METALS[metal_type]["density"]
    construction = (component.get("construction") or "unknown")
    cf = C.CONSTRUCTION.get(construction, C.CONSTRUCTION["unknown"])
    lo = hi = 0.0
    trace = []
    for p in component.get("parts") or []:
        v = part_volume_mm3(p, ring_size, component.get("category"))
        lo += v[0]
        hi += v[1]
        trace.append(f"{p.get('name') or p.get('shape')}: {v[0]:.0f}-{v[1]:.0f} mm³")
    lo_g = lo / 1000 * density * cf[0]
    hi_g = hi / 1000 * density * cf[1]
    trace.append(f"{construction} construction x{cf[0]:.2f}-{cf[1]:.2f}, density {density} g/cm³")
    return lo_g, hi_g, trace


def carat_range(stone):
    """(low, high) carat per stone from face-up millimetres, depth % and specific gravity."""
    shape = stone.get("shape") if stone.get("shape") in C.SHAPE_K else "other"
    k = C.SHAPE_K[shape]
    sg = C.SG.get(stone.get("stone_type"), C.SG["Other"])
    fu = stone.get("face_up_mm") or {}
    L, W = _rng(fu.get("length")), _rng(fu.get("width"))
    if W == (0.0, 0.0):
        W = L
    if L == (0.0, 0.0):
        L = W
    dp = _rng(stone.get("depth_pct"), C.DEFAULT_DEPTH_PCT.get(shape, (58, 68)))
    if L[1] <= 0:
        return None
    lo = L[0] * W[0] * (W[0] * dp[0] / 100) * k * sg
    hi = L[1] * W[1] * (W[1] * dp[1] / 100) * k * sg
    return round(lo, 4), round(hi, 4)
