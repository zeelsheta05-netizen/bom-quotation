// Thin client: collects input, calls the API, renders what the server returns.
// No pricing or business logic lives here.
const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (n) => new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(n || 0);
const fmtDate = (t) => new Date(t * 1000).toLocaleDateString();

let catalog = null;
const state = { file: null, jobs: new Map(), expanded: new Set(), quotes: new Map(), pollTimer: null, seenStatus: new Map() };

async function api(path, opts = {}) {
  const init = { ...opts };
  if (opts.json !== undefined) {
    init.body = JSON.stringify(opts.json);
    init.headers = { "Content-Type": "application/json" };
  }
  const r = await fetch(path, init);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const d = data.detail;
    throw new Error(Array.isArray(d) ? d.map((x) => x.msg).join("; ") : d || `Request failed (${r.status})`);
  }
  return data;
}

function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), 3200);
}

const options = (list, selected) => list.map((v) => {
  const val = typeof v === "string" ? v : v.value, label = typeof v === "string" ? v : v.label;
  return `<option value="${esc(val)}"${val === selected ? " selected" : ""}>${esc(label)}</option>`;
}).join("");

// ------------------------------------------------------------------ analyze form
function setFile(file) {
  state.file = file;
  const empty = $("#dz-empty"), prev = $("#dz-preview");
  if (!file) {
    empty.hidden = false; prev.hidden = true; $("#file-input").value = "";
  } else {
    empty.hidden = true; prev.hidden = false;
    $("#dz-name").textContent = file.name;
    const isPdf = file.type === "application/pdf" || /\.pdf$/i.test(file.name);
    $("#dz-pdf").hidden = !isPdf; $("#dz-img").hidden = isPdf;
    if (!isPdf) $("#dz-img").src = URL.createObjectURL(file);
    $("#image-url").value = "";
  }
  updateSubmit();
}

function updateSubmit() {
  $("#submit-btn").disabled = !(state.file || $("#image-url").value.trim());
}

function initForm() {
  const dz = $("#dropzone"), fi = $("#file-input");
  fi.accept = catalog.accepted;
  dz.addEventListener("click", (e) => { if (!e.target.closest("#dz-clear")) fi.click(); });
  dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fi.click(); } });
  fi.addEventListener("change", () => fi.files[0] && setFile(fi.files[0]));
  $("#dz-clear").addEventListener("click", (e) => { e.stopPropagation(); setFile(null); });
  ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => e.dataTransfer.files[0] && setFile(e.dataTransfer.files[0]));
  $("#image-url").addEventListener("input", () => { if ($("#image-url").value.trim() && state.file) setFile(null); updateSubmit(); });
  $("#labor-region").innerHTML = options(catalog.regions, catalog.default_region);
  $("#declared-metal").innerHTML = `<option value="">Unknown – let AI infer</option>` + options(catalog.metals, "");

  $("#analyze-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("#submit-btn"), err = $("#form-error");
    err.hidden = true; btn.disabled = true; btn.textContent = "Submitting…";
    const fd = new FormData();
    if (state.file) fd.append("file", state.file);
    else fd.append("image_url", $("#image-url").value.trim());
    fd.append("labor_region", $("#labor-region").value);
    fd.append("gross_weight", $("#gross-weight").value);
    fd.append("stone_hints", $("#stone-hints").value);
    fd.append("metal", $("#declared-metal").value);
    fd.append("ring_size", $("#ring-size").value);
    try {
      const job = await api("/api/jobs", { method: "POST", body: fd });
      state.expanded.add(job.id);
      setFile(null); $("#image-url").value = "";
      await refreshJobs();
    } catch (ex) {
      err.textContent = ex.message; err.hidden = false;
    } finally {
      btn.textContent = "Generate BOM Quotation"; updateSubmit();
    }
  });
}

// ------------------------------------------------------------------ jobs list
async function refreshCredits() {
  try { $("#credits-n").textContent = (await api("/api/account")).credits; } catch { /* ignore */ }
}

async function refreshJobs() {
  const list = await api("/api/jobs");
  const container = $("#jobs");
  $("#empty-results").hidden = list.length > 0;
  const ids = new Set(list.map((j) => j.id));
  for (const el of [...container.children]) if (!ids.has(el.dataset.id)) el.remove();

  let prev = null;
  for (const job of list) {
    let el = container.querySelector(`[data-id="${job.id}"]`);
    if (!el) {
      el = document.createElement("div");
      el.className = "job"; el.dataset.id = job.id;
      el.innerHTML = `<div class="card job-head"></div><div class="job-body"></div>`;
      el.querySelector(".job-head").addEventListener("click", () => toggleJob(job.id));
      prev ? prev.after(el) : container.prepend(el);
    }
    prev = el;
    const before = state.seenStatus.get(job.id);
    state.jobs.set(job.id, job);
    state.seenStatus.set(job.id, job.status);
    renderJobHead(el, job);
    if (job.status === "completed" && before && before !== "completed") {
      state.expanded.add(job.id); refreshCredits();
    }
    await renderJobBody(el, job);
  }
  const active = list.some((j) => j.status === "queued" || j.status === "processing");
  clearTimeout(state.pollTimer);
  if (active) state.pollTimer = setTimeout(refreshJobs, 1500);
}

function renderJobHead(el, job) {
  const thumb = job.has_thumbnail ? `<img class="thumb" src="/api/jobs/${job.id}/thumbnail" alt="">`
    : `<div class="thumb">${job.is_pdf ? "PDF" : "IMG"}</div>`;
  let sub, badge;
  if (job.status === "completed") { sub = job.quote_title ? `Completed · ${esc(job.quote_title)} · ${money(job.quote_total)}` : "Completed"; badge = `<span class="badge done">Done</span>`; }
  else if (job.status === "failed") { sub = `<span>${esc(job.error || "Failed")}</span>`; badge = `<span class="badge failed">Failed</span>`; }
  else { sub = job.status === "queued" ? "Queued" : "Analysing piece with AI…"; badge = `<span class="badge running"><i class="spinner"></i>Running</span>`; }
  el.querySelector(".job-head").innerHTML = `${thumb}
    <div class="job-meta"><div class="job-name">${esc(job.filename)}</div>
    <div class="job-sub ${job.status === "failed" ? "err" : ""}">${sub}</div></div>
    ${badge}<span class="dur">${job.duration_s ? job.duration_s + "s" : ""}</span>
    <button class="icon-btn del" data-del title="Remove" aria-label="Remove">✕</button>`;
  el.querySelector("[data-del]").addEventListener("click", async (e) => {
    e.stopPropagation();
    if (!confirm("Remove this quotation?")) return;
    await api(`/api/jobs/${job.id}`, { method: "DELETE" });
    state.expanded.delete(job.id); refreshJobs();
  });
}

function toggleJob(id) {
  const job = state.jobs.get(id);
  if (!job || job.status !== "completed") return;
  state.expanded.has(id) ? state.expanded.delete(id) : state.expanded.add(id);
  renderJobBody(document.querySelector(`[data-id="${id}"]`), job);
}

async function renderJobBody(el, job) {
  const body = el.querySelector(".job-body");
  const open = state.expanded.has(job.id) && job.status === "completed" && job.quote_id;
  if (!open) { body.innerHTML = ""; body.dataset.quote = ""; return; }
  if (body.dataset.quote === job.quote_id) return; // already rendered; keep inputs intact
  body.dataset.quote = job.quote_id;
  const quote = await api(`/api/quotes/${job.quote_id}`);
  state.quotes.set(quote.id, quote);
  body.innerHTML = `<div class="job-toggle"><button class="btn ghost sm" data-collapse>⌃ Collapse</button></div><div class="card quote"></div>`;
  body.querySelector("[data-collapse]").addEventListener("click", () => toggleJob(job.id));
  renderQuote(body.querySelector(".quote"), quote);
}

// ------------------------------------------------------------------ quote card
const prov = (p, label) => p ? `<span class="prov prov-${esc(p)}" title="${esc(label ? label + ": " : "")}${esc(p)}">${esc(label ? label + " " : "")}${esc(p)}</span>` : "";
const fmtW = (n) => (Math.round(n * 100) / 100).toString();
const fmtC = (n) => (n >= 0.1 ? n.toFixed(2) : n.toFixed(3));
const range = (lo, hi, fmt, unit = "") => Math.abs(hi - lo) < 1e-9 ? "" : `${fmt(lo)}–${fmt(hi)}${unit}`;
const opName = (o) => o.replace(/_/g, " ");

function renderQuote(card, q) {
  const multi = q.metals.length > 1;
  const t = q.totals;
  const metalRows = q.metals.map((m, i) => `
    <div class="metal-row" data-metal="${i}">
      ${multi ? `<div class="comp-label">${esc(m.component || "Metal " + (i + 1))}</div>` : ""}
      <div><span class="caps">Type</span><select class="input sm" data-f="metal_type">${options(catalog.metals, m.metal_type)}</select>
        <div class="row-meta">${prov(m.metal_prov)}</div></div>
      <div><span class="caps">Net wt.</span><div class="unit-input"><input class="input sm" type="number" min="0" step="0.01" data-f="net_weight_g" value="${fmtW(m.net_weight_g)}"><span class="muted">g</span></div>
        <div class="row-meta">${range(m.weight_low, m.weight_high, fmtW, " g")} ${prov(m.weight_prov)}</div></div>
      <div><span class="caps">${multi ? "Cost" : "Gross wt."}</span><div class="val">${multi ? money(m.cost) : t.gross_weight_g.toFixed(2) + "g"}</div>
        <div class="row-meta">${multi ? range(m.cost_low, m.cost_high, money) : range(t.gross_weight_low, t.gross_weight_high, (x) => x.toFixed(2), " g")}</div></div>
      <div><span class="caps">Spot price</span><div class="val">${money(m.price_per_g)}/g</div><div class="row-meta"><span class="chip src">${esc(m.source)}</span></div></div>
      ${m.weight_trace && m.weight_trace.length && m.weight_prov !== "declared" ? `<div class="trace">Geometry: ${esc(m.weight_trace.join(" · "))}</div>` : ""}
      <div class="verify-line">${verifyBox(m)}</div>
    </div>`).join("");

  const stoneRows = q.stones.map((s) => {
    const fu = s.face_up_mm || {};
    const dim = fu.length && fu.length.high ? `${fu.length.low}–${fu.length.high} × ${(fu.width || fu.length).low}–${(fu.width || fu.length).high} mm` : "";
    const meta = [s.shape, s.setting && s.setting.replace(/_/g, " "), [s.color, s.clarity].filter(Boolean).join(" / "),
      s.treatment && s.treatment.toLowerCase() !== "none" ? s.treatment : "", s.component && q.components.length > 1 ? s.component : ""]
      .filter(Boolean).map((x) => `<span>${esc(x)}</span>`).join("<span>·</span>");
    return `<div class="stone-row stone-grid" data-stone="${esc(s.id)}">
      <div><select class="input" data-f="stone_type">${options(catalog.stones, s.stone_type)}</select>
        <div class="stone-meta"><b>${esc(s.label || "")}</b><span>·</span>${meta}</div>
        <div class="row-meta">${prov(s.species_prov, "species")} ${s.stone_type === "Diamond" || s.origin !== "natural" ? prov(s.origin_prov, s.origin.replace("_", " ")) : ""}</div></div>
      <div class="center"><input class="input center" type="number" min="0" step="1" data-f="qty" value="${s.qty}">
        <div class="row-meta center" title="${esc(s.count_rule || "")}">${s.count_visible && s.count_visible !== s.qty ? `${s.count_visible} seen · ` : ""}${prov(s.count_prov)}</div></div>
      <div class="center"><input class="input center" type="number" min="0" step="0.001" data-f="carat_each" value="${fmtC(s.carat_each)}">
        <div class="row-meta center">${range(s.carat_low, s.carat_high, fmtC)} ${prov(s.carat_prov)}</div></div>
      <div><select class="input" data-f="quality">${options(catalog.qualities, s.quality)}</select><div class="row-meta">${prov(s.quality_prov)}</div></div>
      <div class="stone-cost">${money(s.cost)}<span class="small">${range(s.cost_low, s.cost_high, money)}</span>
        <span class="small">${money(s.price_per_ct)}/ct · ${esc(s.source)}${s.priced_as && s.priced_as !== s.stone_type ? " · as " + esc(s.priced_as) : ""}</span></div>
      <button class="icon-btn del" data-del-stone title="Remove stone" aria-label="Remove stone">
        <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg></button>
      ${dim ? `<div class="trace span-all">Measured ${esc(dim)} ${prov(s.size_prov)} · depth ${s.depth_pct ? `${s.depth_pct.low}–${s.depth_pct.high}%` : "default"} assumed${s.count_rule ? ` · count: ${esc(s.count_rule)}` : ""}</div>` : ""}
      <div class="verify-line span-all">${verifyBox(s)}</div>
    </div>`;
  }).join("");

  const lb = q.labor;
  const breakdown = lb.breakdown.map((b) => `
    <div class="lb-comp"><span><b>${esc(b.component)}</b> · ${esc(b.source)}</span><span>${money(b.cost)}</span></div>
    ${(b.items || []).map((i) => `<div class="lb-item"><span>${esc(opName(i.op))}${i.variant ? " (" + esc(i.variant.replace(/_/g, " ")) + ")" : ""} · ${i.units} ${esc(i.unit)}</span><span>${money(i.cost)}</span></div>`).join("")}`).join("");

  const unresolved = q.unresolved || [];
  const blocking = unresolved.filter((u) => u.blocks_quote);
  const minor = unresolved.filter((u) => !u.blocks_quote);
  const assumptions = q.assumptions || [];
  const comps = (q.components || []).map((c) => `${c.name}: ${c.category.replace(/_/g, " ")} × ${c.quantity}, ${c.complexity}` +
    `${c.ring_size ? `, US ${c.ring_size} (${c.ring_size_prov})` : ""}, ${c.construction} construction (${c.construction_prov || "?"})` +
    `${c.texture_origin && c.texture_origin !== "none" ? `, texture ${c.texture_origin.replace(/_/g, " ")}` : ""}`);

  card.innerHTML = `
    <div class="q-head">
      <div>
        <div class="q-title"><h2>${esc(q.title)}</h2><span class="chip status-${esc(q.status)}">${q.status === "firm" ? "Firm quote" : q.status === "indicative" ? "Indicative" : esc(q.status)}</span></div>
        <div class="q-line"><span class="q-id mono">${esc(q.id)}</span><span>•</span><span>${fmtDate(q.created_at)}</span>${q.image_kind ? `<span>•</span><span>${esc(q.image_kind.replace(/_/g, " "))}</span>` : ""}</div>
        <div class="q-line"><span class="chip">Scenario v${q.scenario_version}</span><span class="chip">${q.snapshot_count} saved snapshot${q.snapshot_count === 1 ? "" : "s"}</span><span class="chip">${esc(q.region)} labor</span></div>
        <div class="q-actions">
          <button class="btn" data-act="snapshot">Save snapshot</button>
          <button class="btn" data-act="snapshots">${q.snapshot_count ? "View snapshots" : "No snapshots"}</button>
          <button class="btn" data-act="reset">Reset to AI estimate</button>
        </div>
      </div>
      <div class="q-total"><div class="caps">Total estimate</div><div class="amount">${money(t.total)}</div>
        ${t.total_high - t.total_low > 0.5 ? `<div class="q-range">Range ${money(t.total_low)} – ${money(t.total_high)}</div>` : ""}
        <div class="muted small">Metal ${money(t.metal)} · Stones ${money(t.stones)} · Labor ${money(t.labor)}</div></div>
    </div>
    ${q.description ? `<p class="q-desc">${esc(q.description)}</p>` : ""}
    <div class="snapshots-wrap" hidden style="padding:0 24px 18px"></div>
    ${q.notes && q.notes.length ? `<div style="padding:0 24px 16px">${q.notes.map((n) => `<div class="notice">${esc(n)}</div>`).join("")}</div>` : ""}
    ${blocking.length ? `<div class="blockers"><b>Open questions blocking a firm quote</b><ul>${blocking.map((u) =>
      `<li><span>${esc(u.field)}${u.swing ? ` <span class="muted">· moves cost ±${money(u.swing / 2)}</span>` : ""}</span><span class="muted">${esc(u.resolved_by)}</span></li>`).join("")}</ul>
      <p class="muted small">Edit or tick “Verified by me” on a row to declare its values.</p></div>` : ""}

    <div class="section">
      <div class="section-head"><h3>Metal Composition</h3><span class="amt">${money(t.metal)}</span></div>
      <div class="box">${metalRows}</div>
      <div class="weights"><span>Net metal <b>${t.net_weight_g.toFixed(2)} g</b></span><span>Stones <b>${t.stone_weight_g.toFixed(2)} g</b> (${t.total_carat} ct)</span><span>Gross <b>${t.gross_weight_g.toFixed(2)} g</b></span></div>
    </div>

    <div class="section">
      <div class="section-head"><h3>Gemstones</h3><span class="amt">${money(t.stones)}</span></div>
      <div class="box stones">
        <div class="stone-grid stone-head caps"><div>Stone</div><div class="center">Qty</div><div class="center">Ct each</div><div>Quality</div><div class="num">Cost</div><div></div></div>
        ${stoneRows || `<div class="empty-stones">No gemstones detected. Add one if the piece has stones.</div>`}
      </div>
      <button class="btn add-stone" data-act="add-stone">+ Add Stone</button>
      <p class="muted small" style="margin-top:8px">Carat is computed from measured millimetres, depth and the stone's density. Any value you edit becomes <span class="prov prov-declared">declared</span> and its range collapses.</p>
    </div>

    <div class="section">
      <div class="section-head"><h3>Labor &amp; Craftsmanship</h3><span class="amt">${money(lb.cost)}</span></div>
      <div class="labor-line"><span class="muted">Cost</span>
        <div class="money-input"><span class="muted">$</span><input class="input" type="number" min="0" step="1" data-labor value="${lb.cost}"></div>
        ${lb.override ? `<span class="chip">Manual override</span><button class="btn ghost sm" data-act="labor-reset">Use calculated ${money(lb.auto_cost)}</button>` : `<span class="chip src">Calculated</span>${lb.cost_high - lb.cost_low > 0.5 ? `<span class="muted small">${money(lb.cost_low)} – ${money(lb.cost_high)}</span>` : ""}`}
      </div>
      <div class="breakdown">${breakdown}</div>
    </div>

    ${assumptions.length ? `<div class="section">
      <div class="section-head"><h3>Assumptions</h3><span class="muted small">${assumptions.length} made by the AI</span></div>
      <div class="assumptions">${assumptions.map((a) => `
        <div class="as-row"><div><b>${esc(a.field)}</b>: ${esc(a.chose)} <span class="muted">— ${esc(a.reason)}</span></div>
        <div class="muted">instead of ${esc(a.alternative)} <span class="dir dir-${esc(a.cost_direction)}">${a.cost_direction === "up" ? "▲ would cost more" : a.cost_direction === "down" ? "▼ would cost less" : "≈ same cost"}</span></div></div>`).join("")}
      </div></div>` : ""}

    <div class="section">
      ${confidenceHtml(q)}
      <details class="notes"><summary>Analysis details</summary><ul>
        ${comps.map((c) => `<li>${esc(c)}</li>`).join("")}
        ${minor.map((u) => `<li>Minor open question: ${esc(u.field)} — ${esc(u.resolved_by)}</li>`).join("")}
        ${(q.warnings || []).map((n) => `<li>${esc(n)}</li>`).join("")}
      </ul></details>
    </div>`;
  bindQuote(card, q);
}

function verifyBox(row) {
  const edited = (row.user_fields || []).length;
  return `<label class="verify"><input type="checkbox" data-f="verified"${row.verified ? " checked" : ""}> Verified by me</label>
    ${edited ? `<span class="chip src">edited: ${esc(row.user_fields.map((f) => f.replace(/_/g, " ").replace("net weight g", "weight").replace("carat each", "carat")).join(", "))}</span>` : ""}`;
}

function confidenceHtml(q) {
  const d = q.confidence_detail;
  const pct = Math.round(q.confidence * 100);
  const factors = d.factors.map((f) => `
    <div class="cf-row"><span class="cf-name">${esc(f.name)}</span>
      <span class="bar sm"><i class="lvl-${f.score >= 0.8 ? "high" : f.score >= 0.6 ? "mid" : "low"}" style="width:${Math.round(f.score * 100)}%"></i></span>
      <span class="cf-pct">${Math.round(f.score * 100)}%</span>
      <span class="cf-note muted">${esc(f.note || "")}</span></div>`).join("");
  return `<div class="confidence"><span class="mono">Confidence:</span>
      <span class="bar"><i class="lvl-${d.level.toLowerCase()}" style="width:${pct}%"></i></span>
      <span class="mono">${pct}%</span><span class="chip lvl-chip-${d.level.toLowerCase()}">${esc(d.level)}</span></div>
    <details class="notes" open><summary>Why this score</summary>
      <div class="cf-grid">${factors}</div>
      ${d.notes.length ? `<ul>${d.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
      ${d.tips.length ? `<p class="cf-tips-title">How to raise it</p><ul class="cf-tips">${d.tips.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
    </details>`;
}

function collectEdits(card) {
  const metals = [...card.querySelectorAll("[data-metal]")].map((r) => ({
    metal_type: r.querySelector('[data-f="metal_type"]').value,
    net_weight_g: parseFloat(r.querySelector('[data-f="net_weight_g"]').value) || 0,
    verified: r.querySelector('[data-f="verified"]').checked,
  }));
  const stones = [...card.querySelectorAll("[data-stone]")].map((r) => ({
    id: r.dataset.stone,
    stone_type: r.querySelector('[data-f="stone_type"]').value,
    qty: parseInt(r.querySelector('[data-f="qty"]').value, 10) || 0,
    carat_each: parseFloat(r.querySelector('[data-f="carat_each"]').value) || 0,
    quality: r.querySelector('[data-f="quality"]').value,
    verified: r.querySelector('[data-f="verified"]').checked,
  }));
  const labor = parseFloat(card.querySelector("[data-labor]").value);
  return { metals, stones, labor_cost: isNaN(labor) ? null : labor };
}

async function quoteAction(card, q, path, payload) {
  card.classList.add("busy");
  try {
    const updated = await api(`/api/quotes/${q.id}/${path}`, { method: "POST", json: payload ?? {} });
    state.quotes.set(updated.id, updated);
    renderQuote(card, updated);
    refreshJobHeadFor(updated);
    return updated;
  } catch (ex) {
    toast(ex.message, true);
  } finally {
    card.classList.remove("busy");
  }
}

function refreshJobHeadFor(q) {
  const job = state.jobs.get(q.job_id);
  if (!job) return;
  job.quote_total = q.totals.total;
  renderJobHead(document.querySelector(`[data-id="${job.id}"]`), job);
}

function bindQuote(card, q) {
  card.querySelectorAll("[data-metal] [data-f], [data-stone] [data-f], [data-labor]").forEach((inp) =>
    inp.addEventListener("change", () => quoteAction(card, q, "recalculate", collectEdits(card))));
  card.querySelectorAll("[data-del-stone]").forEach((b) => b.addEventListener("click", () => {
    const edits = collectEdits(card);
    const id = b.closest("[data-stone]").dataset.stone;
    edits.stones = edits.stones.filter((s) => s.id !== id);
    quoteAction(card, q, "recalculate", edits);
  }));
  const act = (name, fn) => { const b = card.querySelector(`[data-act="${name}"]`); if (b) b.addEventListener("click", fn); };
  act("add-stone", () => {
    const edits = collectEdits(card);
    edits.stones.push({ stone_type: "Diamond", qty: 1, carat_each: 0.1, quality: "Standard" });
    quoteAction(card, q, "recalculate", edits);
  });
  act("labor-reset", () => quoteAction(card, q, "recalculate", { ...collectEdits(card), labor_cost: null, labor_reset: true }));
  act("reset", () => confirm("Discard edits and return to the original AI estimate?") && quoteAction(card, q, "reset"));
  act("snapshot", async () => {
    const label = prompt("Snapshot name", `Scenario v${q.scenario_version}`);
    if (label === null) return;
    const updated = await quoteAction(card, q, "snapshots", { label });
    if (updated) toast("Snapshot saved");
  });
  act("snapshots", () => toggleSnapshots(card, q));
}

async function toggleSnapshots(card, q) {
  const wrap = card.querySelector(".snapshots-wrap");
  if (!wrap.hidden) { wrap.hidden = true; return; }
  const snaps = await api(`/api/quotes/${q.id}/snapshots`);
  wrap.innerHTML = snaps.length ? `<div class="snapshots">${snaps.map((s) => `
    <div class="snap"><span><b>${esc(s.label)}</b> <span class="muted">· ${new Date(s.created_at * 1000).toLocaleString()} · ${money(s.total)}</span></span>
    <span><button class="btn sm" data-restore="${s.id}">Restore</button> <button class="btn ghost sm" data-delsnap="${s.id}">Delete</button></span></div>`).join("")}</div>`
    : `<p class="muted">No snapshots saved yet.</p>`;
  wrap.hidden = false;
  wrap.querySelectorAll("[data-restore]").forEach((b) => b.addEventListener("click", () => quoteAction(card, q, `snapshots/${b.dataset.restore}/restore`)));
  wrap.querySelectorAll("[data-delsnap]").forEach((b) => b.addEventListener("click", async () => {
    const updated = await api(`/api/quotes/${q.id}/snapshots/${b.dataset.delsnap}`, { method: "DELETE" });
    renderQuote(card, updated);
  }));
}

// ------------------------------------------------------------------ settings modal
function showSettingsError(msg) { const e = $("#settings-error"); e.textContent = msg; e.hidden = !msg; }

function renderInventory(rows) {
  $("#inv-rows").innerHTML = rows.length ? rows.map((r) => `
    <div class="tr inv-grid"><div>${esc(r.stone_type)}</div><div>${esc(r.color || "—")}</div><div>${esc(r.clarity || "—")}</div>
    <div class="num">${money(r.price_per_ct)}</div>
    <button class="icon-btn del" data-inv-del="${r.id}" aria-label="Delete">✕</button></div>`).join("")
    : `<div class="table-empty">No inventory entries yet. Add your first row below.</div>`;
  $("#inv-rows").querySelectorAll("[data-inv-del]").forEach((b) => b.addEventListener("click", async () =>
    renderInventory(await api(`/api/settings/inventory/${b.dataset.invDel}`, { method: "DELETE" }))));
}

function renderLabor(rows) {
  $("#labor-rows").innerHTML = rows.length ? rows.map((r) => `
    <div class="tr labor-grid" data-labor-row="${r.id}"><div>${esc(r.piece_type)}</div>
    ${["simple", "medium", "complex"].map((t) => `<input class="input" type="number" min="0" step="0.01" data-tier="${t}" value="${r[t]}">`).join("")}
    <button class="icon-btn del" data-labor-del="${r.id}" aria-label="Delete">✕</button></div>`).join("")
    : `<div class="table-empty">No labor rates yet. Add your first piece type below.</div>`;
  $("#labor-rows").querySelectorAll("[data-labor-row]").forEach((row) => {
    const r = rows.find((x) => String(x.id) === row.dataset.laborRow);
    row.querySelectorAll("[data-tier]").forEach((inp) => inp.addEventListener("change", async () => {
      const body = { piece_type: r.piece_type };
      row.querySelectorAll("[data-tier]").forEach((i) => (body[i.dataset.tier] = parseFloat(i.value) || 0));
      try { renderLabor(await api(`/api/settings/labor-rates/${r.id}`, { method: "PUT", json: body })); toast("Labor rate saved"); }
      catch (ex) { showSettingsError(ex.message); }
    }));
  });
  $("#labor-rows").querySelectorAll("[data-labor-del]").forEach((b) => b.addEventListener("click", async () =>
    renderLabor(await api(`/api/settings/labor-rates/${b.dataset.laborDel}`, { method: "DELETE" }))));
}

async function loadRates() {
  const rates = await api("/api/metal-rates");
  $("#metal-rates").innerHTML = rates.map((r) => `<div class="rate"><b>${esc(r.metal_type)}</b>${money(r.price_per_g)}/g · ${esc(r.source)}</div>`).join("");
}

async function openSettings() {
  showSettingsError("");
  $("#settings-modal").hidden = false;
  const [inv, labor, prefs] = await Promise.all([api("/api/settings/inventory"), api("/api/settings/labor-rates"), api("/api/settings/preferences")]);
  renderInventory(inv); renderLabor(labor);
  document.querySelectorAll("[data-pref]").forEach((t) => (t.checked = !!prefs[t.dataset.pref]));
}

function initSettings() {
  $("#stone-list").innerHTML = catalog.stones.map((s) => `<option value="${esc(s)}">`).join("");
  $("#open-settings").addEventListener("click", openSettings);
  $("#close-settings").addEventListener("click", () => ($("#settings-modal").hidden = true));
  $("#settings-modal").addEventListener("click", (e) => { if (e.target.id === "settings-modal") e.target.hidden = true; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#settings-modal").hidden = true; });
  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    document.querySelectorAll(".tab-panel").forEach((p) => (p.hidden = p.dataset.panel !== tab.dataset.tab));
    showSettingsError("");
    if (tab.dataset.tab === "prefs") loadRates();
  }));
  $("#inv-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target));
    f.price_per_ct = parseFloat(f.price_per_ct);
    try { renderInventory(await api("/api/settings/inventory", { method: "POST", json: f })); e.target.reset(); showSettingsError(""); }
    catch (ex) { showSettingsError(ex.message); }
  });
  $("#labor-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target));
    ["simple", "medium", "complex"].forEach((k) => (f[k] = parseFloat(f[k])));
    try { renderLabor(await api("/api/settings/labor-rates", { method: "POST", json: f })); e.target.reset(); showSettingsError(""); }
    catch (ex) { showSettingsError(ex.message); }
  });
  document.querySelectorAll("[data-pref]").forEach((t) => t.addEventListener("change", async () => {
    try { await api("/api/settings/preferences", { method: "PUT", json: { [t.dataset.pref]: t.checked } }); loadRates(); toast("Preference saved"); }
    catch (ex) { showSettingsError(ex.message); }
  }));
}

(async function init() {
  catalog = await api("/api/catalog");
  initForm();
  initSettings();
  refreshCredits();
  await refreshJobs();
  const first = [...state.jobs.values()].find((j) => j.status === "completed");
  if (first) { state.expanded.add(first.id); renderJobBody(document.querySelector(`[data-id="${first.id}"]`), first); }
})();
