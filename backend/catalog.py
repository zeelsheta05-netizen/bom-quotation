"""Reference data: metals, gemstones, quality tiers, labor regions and labor model.

Everything the UI shows in dropdowns comes from here via /api/catalog, so the
frontend never hard-codes business data.
"""

TROY_OZ_G = 31.1035

# name -> spot symbol, purity (fine metal fraction), alloy premium, density g/cm3
METALS = {
    "White Gold 14k": {"symbol": "XAU", "purity": 0.585, "premium": 0.04, "density": 12.6},
    "White Gold 18k": {"symbol": "XAU", "purity": 0.750, "premium": 0.04, "density": 14.7},
    "Rose Gold 14k": {"symbol": "XAU", "purity": 0.585, "premium": 0.02, "density": 13.0},
    "Rose Gold 18k": {"symbol": "XAU", "purity": 0.750, "premium": 0.02, "density": 15.0},
    "Palladium 950": {"symbol": "XPD", "purity": 0.950, "premium": 0.06, "density": 12.0},
    "Platinum 900": {"symbol": "XPT", "purity": 0.900, "premium": 0.06, "density": 21.5},
    "Platinum 950": {"symbol": "XPT", "purity": 0.950, "premium": 0.06, "density": 21.4},
    "Silver 999": {"symbol": "XAG", "purity": 0.999, "premium": 0.05, "density": 10.49},
    "Silver 925": {"symbol": "XAG", "purity": 0.925, "premium": 0.05, "density": 10.36},
    "Gold 10k": {"symbol": "XAU", "purity": 0.417, "premium": 0.02, "density": 11.6},
    "Gold 14k": {"symbol": "XAU", "purity": 0.585, "premium": 0.02, "density": 13.1},
    "Gold 18k": {"symbol": "XAU", "purity": 0.750, "premium": 0.02, "density": 15.6},
    "Gold 22k": {"symbol": "XAU", "purity": 0.916, "premium": 0.01, "density": 17.7},
    "Gold 24k": {"symbol": "XAU", "purity": 0.999, "premium": 0.00, "density": 19.3},
}
DEFAULT_METAL = "Gold 18k"
REFERENCE_DENSITY = METALS["Gold 14k"]["density"]

# Used only when the live API is unreachable and no previous live value is cached (USD / troy oz).
FALLBACK_SPOT = {"XAU": 4100.0, "XAG": 48.0, "XPT": 1600.0, "XPD": 1150.0}

# Gemstones: base USD per carat for a 1 ct "Standard" stone, and a size exponent.
# price_per_ct = base * clamp(carat_each ** k) * quality multiplier
STONES = {
    "Alexandrite": (6000, 0.50), "Amethyst": (30, 0.10), "Aquamarine": (350, 0.25),
    "Citrine": (30, 0.10), "Coral": (60, 0.10), "Cubic-Zirconia": (15, 0.0),
    "Diamond": (5500, 0.60), "Emerald": (2800, 0.45), "Garnet": (90, 0.15),
    "Jade": (200, 0.20), "Lab-Diamond": (900, 0.30), "Lapis": (10, 0.0),
    "Moissanite": (400, 0.10), "Moonstone": (50, 0.10), "Morganite": (250, 0.20),
    "Onyx": (8, 0.0), "Opal": (300, 0.20), "Pearl": (150, 0.20),
    "Peridot": (120, 0.15), "Ruby": (3500, 0.50), "Sapphire": (1800, 0.45),
    "Spinel": (900, 0.35), "Tanzanite": (600, 0.30), "Topaz": (40, 0.10),
    "Tourmaline": (300, 0.25), "Turquoise": (15, 0.0), "Other": (50, 0.10),
}
SIZE_FACTOR_MIN, SIZE_FACTOR_MAX = 0.12, 3.5
CARAT_TO_GRAMS = 0.2

QUALITIES = {"Premium": 1.6, "Standard": 1.0, "Commercial": 0.55}

LABOR_REGIONS = {"USA": 1.0, "Europe": 1.3, "Dubai": 1.2, "India": 0.7}

# Default labor model (USA baseline). Base fee per component category.
LABOR_BASE = {
    "ring": 120, "band": 80, "engagement_ring": 180, "earrings": 140, "stud_earrings": 90,
    "pendant": 110, "necklace": 260, "choker": 240, "chain": 140, "bracelet": 200,
    "bangle": 180, "cuff": 190, "anklet": 150, "brooch": 180, "cufflinks": 140,
    "nose_pin": 50, "maang_tikka": 160, "tiara": 600, "watch": 300, "other": 150,
}
COMPLEXITY = {"simple": 0.8, "medium": 1.0, "complex": 1.6}
MAKING_PER_GRAM = 4.5  # USD per gram of metal, scaled by complexity
SETTING_COST = {  # USD per stone set
    "prong": 12, "bezel": 25, "pave": 4, "micro_pave": 3, "channel": 8, "invisible": 15,
    "flush": 10, "tension": 40, "bead": 4, "cluster": 6, "glued_or_strung": 1, "other": 10,
}
FINISHING_COST = {"rhodium_plating": 25, "enamel": 60, "engraving": 40, "filigree": 80,
                  "milgrain": 20, "two_tone": 35, "oxidizing": 15, "hand_polish": 10}

# Plausible net-metal weight range per category in Gold 14k equivalent grams.
WEIGHT_RANGE = {
    "ring": (1.2, 30), "band": (1.0, 25), "engagement_ring": (1.5, 12), "earrings": (0.6, 40),
    "stud_earrings": (0.3, 8), "pendant": (0.5, 40), "necklace": (2.5, 300), "choker": (5, 250),
    "chain": (1.5, 200), "bracelet": (2.5, 150), "bangle": (4, 150), "cuff": (5, 150),
    "anklet": (1.5, 60), "brooch": (2, 60), "cufflinks": (3, 40), "nose_pin": (0.1, 3),
    "maang_tikka": (2, 60), "tiara": (20, 500), "watch": (20, 250), "other": (0.3, 500),
}
CATEGORIES = list(LABOR_BASE)
SETTINGS = list(SETTING_COST)
FINISHES = list(FINISHING_COST)


# ====================================================================== geometry & gemology
PROVENANCE = ["observed", "declared", "inferred", "unknown"]

# Specific gravity per species (for carat-from-millimetres).
SG = {
    "Alexandrite": 3.73, "Amethyst": 2.65, "Aquamarine": 2.72, "Citrine": 2.65, "Coral": 2.65,
    "Cubic-Zirconia": 5.7, "Diamond": 3.52, "Emerald": 2.72, "Garnet": 3.9, "Jade": 3.3,
    "Lab-Diamond": 3.52, "Lapis": 2.8, "Moissanite": 3.22, "Moonstone": 2.57, "Morganite": 2.8,
    "Onyx": 2.6, "Opal": 2.1, "Pearl": 2.7, "Peridot": 3.34, "Ruby": 4.0, "Sapphire": 4.0,
    "Spinel": 3.6, "Tanzanite": 3.35, "Topaz": 3.53, "Tourmaline": 3.06, "Turquoise": 2.7, "Other": 3.0,
}
# carat = length_mm * width_mm * depth_mm * K * SG   (standard gemmological estimation factors)
SHAPE_K = {
    "round": 0.0018, "oval": 0.0020, "cushion": 0.0022, "emerald": 0.0025, "radiant": 0.0024,
    "asscher": 0.0025, "princess": 0.0023, "pear": 0.0018, "marquise": 0.0016, "heart": 0.0021,
    "baguette": 0.0026, "trillion": 0.0016, "cabochon_oval": 0.0026, "cabochon_round": 0.0026,
    "bead": 0.0026, "other": 0.0020,
}
DEFAULT_DEPTH_PCT = {"round": (59, 63), "oval": (58, 64), "cushion": (60, 70), "emerald": (60, 70),
                     "princess": (64, 75), "cabochon_oval": (40, 60), "cabochon_round": (40, 60), "bead": (100, 100)}
SHAPES = list(SHAPE_K)
STONE_ROLES = ["center", "side", "accent", "halo", "pave", "drop", "cluster", "strand", "other"]
STONE_ORIGINS = ["natural", "lab_grown", "simulant", "unknown"]

CONSTRUCTION = {  # fraction of the solid envelope that is metal (low, high)
    "solid": (1.0, 1.0), "hollow": (0.35, 0.6), "stamped": (0.45, 0.75), "openwork": (0.45, 0.8), "unknown": (0.5, 1.0),
}
TEXTURE_ORIGINS = ["hand_chased", "die_struck", "cast_in", "machined", "none", "unknown"]
PART_SHAPES = ["ring_band", "bangle", "chain", "wire", "sheet", "cast_block"]
CHAIN_STYLES = {"none": 2.5, "cable": 2.0, "curb": 2.6, "figaro": 2.4, "rope": 4.0, "box": 3.0, "snake": 3.5,
                "ball": 1.5, "wheat": 3.5, "franco": 3.8, "other": 2.5}  # wire length per chain length
BAND_PROFILE_FACTOR = 0.9      # court/half-round vs a rectangular section
DEFAULT_RING_SIZE = {"womens": 6.5, "mens": 10.0, "unisex": 7.5, "unknown": 6.5}
DEFAULT_BANGLE_ID_MM = 60.0
RING_CATEGORIES = {"ring", "band", "engagement_ring"}


def ring_inner_diameter_mm(us_size):
    return 11.63 + 0.8128 * us_size


# ====================================================================== labor operations (USA base, USD)
# op -> (unit description for the AI, rate per unit)
OPERATIONS = {
    "casting": ("pieces cast", 20), "cad_modeling": ("models", 60), "hand_fabrication": ("bench hours", 45),
    "stone_setting": ("stones set (variant = setting type)", None), "engraving": ("cm of engraving", 8),
    "hand_chasing": ("cm2 chased", 12), "repousse": ("cm2", 15), "filigree": ("cm2 of filigree", 18),
    "enamel": ("cm2 enamelled", 10), "milgrain": ("cm of milgrain", 4), "texture_finish": ("pieces", 15),
    "polishing": ("pieces", 12), "rhodium_plating": ("pieces", 25), "plating": ("pieces", 20),
    "oxidizing": ("pieces", 10), "soldering_assembly": ("solder joints", 3), "chain_making": ("cm of chain", 1.5),
    "stringing": ("strands", 15), "finding_attach": ("findings (clasp, post, hook)", 6),
    "die_striking": ("pieces", 5), "machining": ("pieces", 20),
}
OP_VARIANT_MULT = {"hand": 1.0, "laser": 0.3, "machine": 0.4, "handmade": 4.0}
MAKING_PER_GRAM_OPS = 1.5  # metal working charge added automatically from the computed weight
