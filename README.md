# Image to BOM Quotation

Upload a jewellery photo, sketch, CAD render or PDF (or paste a public image URL) and get a structured,
editable Bill of Materials with a cost estimate.

## Run

```bash
cp .env.example .env        # put your free GEMINI_API_KEY in it (https://aistudio.google.com/apikey)
./run.sh                    # http://127.0.0.1:8000
```

## How it works

All data handling happens on the backend. The browser only sends user input and renders the JSON the
server returns. It does no pricing, filtering or business logic.

| Step | Where | What happens |
|---|---|---|
| Intake | `backend/jobs.py` | Upload or URL download, with private-IP blocking and a 20 MB limit. Format sniffing, EXIF rotation, resize to 1568 px, thumbnail. |
| AI analysis | `backend/ai.py` | Google Gemini free tier by default (vision, schema-constrained JSON). Local Ollama and paid Anthropic are optional providers via `BOM_AI_PROVIDER`. The model finds every component, the metal, dimensions, net weight from volume × density, stone groups (type, shape, setting, count, carat, colour, clarity, quality tier), complexity and finishing. Works for any piece type, any image kind, and multi-piece sets. |
| Sanity checks | `backend/jobs.py` | Weights outside plausible ranges per category and metal density get clamped, with a warning and lower confidence. A user-entered gross weight sets the metal weight: gross minus stone weight. |
| Pricing | `backend/pricing.py` | Metal uses live spot prices (gold-api.com) × purity × alloy premium, falling back to the last cached price, then reference prices. Stones use your inventory first (exact grade, then colour, then type average), then market tables with size and quality curves, then the AI's market estimate for unusual stones. Labor uses your labor rates per piece type and tier, else a model built from base fee, per-gram making charge, per-stone setting cost and finishing, times the region multiplier. |
| What-if edits | `POST /api/quotes/{id}/recalculate` | Every edit is repriced on the server as a new scenario version. Changing the metal while keeping the weight rescales the weight by density. A typed labor cost becomes a manual override until reset. |
| Snapshots | `/api/quotes/{id}/snapshots` | Save, list, restore (repriced at current rates) and delete. Reset returns to the original AI estimate. |
| Settings | `/api/settings/*` | Gemstone inventory, labor rates and source preferences, stored in SQLite under `data/`. |

Reference tables (metals, stones, regions, labor model) live in `backend/catalog.py`. Tune them to your market.

## Extraction contract (accuracy)

The AI only extracts specs. It never prices and never does arithmetic. Every value carries a provenance tag:
observed, declared, inferred or unknown. Karat, species, natural vs lab, grades, treatment, construction, wall
thickness and weight can never be "observed"; the server downgrades them if the model claims so.

| What | How it is computed (server side) |
|---|---|
| Metal weight | Geometric parts per component (ring band from ring size, bangle, chain, wire, sheet, cast block with fill fraction) x construction factor x alloy density, as a low/high range. A declared gross weight replaces it. |
| Carat | length x width x (width x depth %) x shape factor x specific gravity, as a low/high range. Changing the species recomputes carat from the same millimetres. |
| Labor | Priced per listed operation (casting, setting by type, engraving, polishing, plating...) x region, plus a per-gram making charge. Your rate card overrides it. |
| Quote | Low / mid / high totals. Any uncertainty that moves the total by more than 20% becomes a blocking open question, and the quote is "Indicative" until it is resolved. |
| Edits | Any value you edit, or a row you tick "Verified by me", becomes declared and its range collapses. |

Output is validated (assumptions present, geometry present, sizes present, bands over 5 mm have texture origin).
An invalid response is sent back to the model once with the problems listed.

## Public hosting (Render, free)

1. Push this repository to GitHub (private is fine).
2. On https://render.com sign in with GitHub, then **New + > Blueprint** and pick the repository.
   Render reads `render.yaml` and builds the `Dockerfile`.
3. When asked, fill in `GEMINI_API_KEY` (free key from https://aistudio.google.com/apikey) and a
   `BOM_PASSWORD` for testers. Testers log in with username `team` and that password.
4. Share the `https://bom-quotation-xxxx.onrender.com` link.

Free plan limits: the service sleeps after about 15 minutes idle (the next visit takes about a minute),
and its disk is reset on every deploy or restart, so saved quotes, inventory and labor rates are not kept.
