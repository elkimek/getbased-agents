/* Knowledge tab — library CRUD, ingest, search, stats.
 *
 * Consumes the /api/knowledge/* proxy routes. Every call goes through
 * `authed()` from app.js so the bearer header stays in one place. We
 * re-render the whole panel after any mutation — cheap for this data
 * scale (< 100 libraries, < 1000 sources per library) and keeps state
 * drift impossible.
 */

import { authed } from "../app.js";
import { showConfirm, showPrompt, showAlert } from "../modals.js";

let _libraries = { activeId: "", libraries: [] };
let _stats = { total_chunks: 0, documents: [] };
let _info = null;
let _models = { default: "", models: [] };

// (no inline persistent status — the pill is the single source of
// truth. It auto-dismisses 3s after completion, matching the PWA.
// A per-library persistent confirmation would lie once the user
// switches libraries.)

// Fixed bottom-right pill matching the getbased PWA. Singleton at the
// <body> level so it survives tab switches — user can start an ingest,
// move to the MCP or Activity tab, and still see the progress bar +
// chunks/sec rate without losing state. Mirrors the browser-local lens
// layout exactly (title row, progress bar, rate line, cancel button).
let _pill = null;        // DOM element or null
let _pillAbort = null;   // AbortController for the in-flight fetch
let _pillAutoDismiss = 0; // setTimeout id

function _ensurePill() {
  if (_pill && document.body.contains(_pill)) return _pill;
  const el = document.createElement("div");
  el.className = "ingest-pill";
  el.innerHTML = `
    <div class="pill-head">
      <span class="pill-title">Indexing knowledge base</span>
      <button class="pill-dismiss" type="button" title="Hide (ingest keeps running)" aria-label="Hide">×</button>
    </div>
    <div class="pill-status">Preparing…</div>
    <progress class="pill-progress" value="0" max="1"></progress>
    <div class="pill-sub">
      <span class="pill-count">0 / 0</span>
      <span class="pill-rate"></span>
    </div>
    <button class="pill-cancel" type="button">Cancel</button>
  `;
  document.body.appendChild(el);
  el.querySelector(".pill-dismiss").addEventListener("click", () => {
    // Hide the pill but DON'T abort — matches PWA's dismiss-vs-cancel
    // distinction. User is opting to get it out of their way, not stop
    // indexing work that's mostly done.
    el.remove();
    _pill = null;
  });
  el.querySelector(".pill-cancel").addEventListener("click", () => {
    if (_pillAbort) {
      _pillAbort.abort();
      const cancelBtn = el.querySelector(".pill-cancel");
      cancelBtn.textContent = "Cancelling…";
      cancelBtn.disabled = true;
    }
  });
  _pill = el;
  return el;
}

function _removePillSoon(delay = 3000) {
  // Auto-dismiss after completion/error, matching the PWA's 3s window.
  if (_pillAutoDismiss) clearTimeout(_pillAutoDismiss);
  _pillAutoDismiss = setTimeout(() => {
    if (_pill && document.body.contains(_pill)) _pill.remove();
    _pill = null;
    _pillAutoDismiss = 0;
  }, delay);
}

function _pillUpdate({ status, rate, index, total }) {
  if (!_pill) return;
  if (status != null) _pill.querySelector(".pill-status").textContent = status;
  if (total != null) {
    const bar = _pill.querySelector(".pill-progress");
    bar.max = Math.max(1, total);
    bar.value = Math.min(total, index || 0);
    _pill.querySelector(".pill-count").textContent = `${index || 0} / ${total}`;
  }
  _pill.querySelector(".pill-rate").textContent = rate != null ? rate : "";
}

function _pillComplete(message, kind) {
  if (!_pill) return;
  _pill.classList.toggle("err", kind === "err");
  _pill.classList.toggle("ok", kind === "ok");
  _pill.querySelector(".pill-status").textContent = message;
  _pill.querySelector(".pill-cancel").hidden = true;
  _removePillSoon();
}

async function runIngest(fd, onSuccess) {
  // Reset any previous pill auto-dismiss before kicking off a new run.
  if (_pillAutoDismiss) {
    clearTimeout(_pillAutoDismiss);
    _pillAutoDismiss = 0;
  }
  const pill = _ensurePill();
  pill.classList.remove("err", "ok");
  pill.querySelector(".pill-cancel").hidden = false;
  pill.querySelector(".pill-cancel").disabled = false;
  pill.querySelector(".pill-cancel").textContent = "Cancel";
  _pillUpdate({ status: "Preparing…", rate: "", index: 0, total: 1 });

  _pillAbort = new AbortController();
  const t0 = performance.now();

  let total = 0;
  let final = null;
  let errorMsg = null;
  let userCancelled = false;

  try {
    const resp = await authed("/api/knowledge/ingest", {
      method: "POST",
      body: fd,
      headers: { Accept: "application/x-ndjson" },
      signal: _pillAbort.signal,
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => null);
      throw new Error(_errMessage(body, resp.status, resp.statusText));
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, idx).trim();
        buffer = buffer.slice(idx + 1);
        if (!line) continue;
        let evt;
        try { evt = JSON.parse(line); } catch { continue; }
        if (evt.event === "start") {
          total = evt.total || 0;
          _pillUpdate({
            status: total ? `Preparing ${total} excerpts…` : "Preparing…",
            rate: "",
            index: 0,
            total: total || 1,
          });
        } else if (evt.event === "embed") {
          const index = evt.index || 0;
          const t = evt.total || total;
          const elapsedSec = (performance.now() - t0) / 1000;
          const rate = elapsedSec > 0 ? (index / elapsedSec).toFixed(1) : "0.0";
          _pillUpdate({
            status: evt.source || "",
            rate: `${rate}/s`,
            index,
            total: t,
          });
        } else if (evt.event === "result") {
          final = evt;
        } else if (evt.event === "error") {
          errorMsg = evt.message || "ingest failed";
        }
      }
    }
  } catch (err) {
    if (err.name === "AbortError") {
      userCancelled = true;
    } else {
      errorMsg = err.message;
    }
  } finally {
    _pillAbort = null;
  }

  const dur = ((performance.now() - t0) / 1000).toFixed(1);

  if (errorMsg) {
    _pillComplete(`Couldn't index: ${errorMsg}`, "err");
    return;
  }
  if (userCancelled || (final && final.cancelled)) {
    const got = final ? final.chunks_indexed : 0;
    const plan = final ? final.chunks_planned : total || 0;
    _pillComplete(`Cancelled — indexed ${got} of ${plan} excerpts in ${dur}s.`, "ok");
    if (onSuccess) onSuccess();
    return;
  }
  if (!final) {
    _pillComplete("Stream ended without result", "err");
    return;
  }

  const skipped = (final.skipped || []).length;
  const skippedSuffix = skipped ? ` (skipped ${skipped})` : "";
  const msg = `Indexed ${final.chunks_indexed} excerpt${final.chunks_indexed === 1 ? "" : "s"} from ${final.files_seen} file${final.files_seen === 1 ? "" : "s"} in ${dur}s${skippedSuffix}.`;
  _pillComplete(msg, "ok");
  if (onSuccess) onSuccess();
}

function _errMessage(body, status, statusText) {
  // Dashboard's exception_handler normalises to {error: "..."}. Fall back
  // to a stringified shape for old-path / upstream JSON that didn't go
  // through our handler. Crucially, never let an object slip through
  // unstringified — `new Error({foo: 1})` renders as "[object Object]".
  const raw = (body && (body.error ?? body.detail)) ?? `HTTP ${status}`;
  if (typeof raw === "string") return raw;
  try {
    return JSON.stringify(raw);
  } catch {
    return statusText || `HTTP ${status}`;
  }
}

async function j(path, opts = {}) {
  const r = await authed(path, opts);
  if (!r.ok) {
    const body = await r.json().catch(() => null);
    throw new Error(_errMessage(body, r.status, r.statusText));
  }
  return r.json();
}

async function refresh() {
  // `/info` + `/models` are best-effort — a slightly older rag that
  // pre-dates these endpoints just returns 404 and we render the panel
  // without the engine badge / model picker. Don't let a missing
  // endpoint break the whole tab.
  const [libs, stats, info, models] = await Promise.all([
    j("/api/knowledge/libraries"),
    j("/api/knowledge/stats"),
    j("/api/knowledge/info").catch(() => null),
    j("/api/knowledge/models").catch(() => ({ default: "", models: [] })),
  ]);
  _libraries = libs;
  _stats = stats;
  _info = info;
  _models = models;
}

function _modelLabel(modelId) {
  // Look up the human label from the /models list; fall back to the
  // id's basename if unknown. Keeps the chip tight ("BGE-M3") instead
  // of leaking the fully-qualified HF path into every library row.
  const entry = (_models.models || []).find((m) => m.id === modelId);
  if (entry && entry.label) return entry.label;
  return modelId && modelId.includes("/") ? modelId.split("/").pop() : modelId || "";
}

function _relTime(ms) {
  // Small "X ago" helper matching the terseness of the PWA. `0` = never.
  if (!ms || ms <= 0) return "never";
  const diff = Math.max(0, Date.now() - ms);
  const sec = Math.floor(diff / 1000);
  if (sec < 45) return "just now";
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  if (day < 30) return `${day}d ago`;
  const mo = Math.floor(day / 30);
  if (mo < 12) return `${mo}mo ago`;
  return `${Math.floor(mo / 12)}y ago`;
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function renderEngineBadge() {
  if (!_info) return "";
  const emb = _info.embedder || {};
  const engine = emb.engine || "unknown";
  const model = emb.model || "";
  const dim = emb.dimension;
  const loaded = emb.loaded;

  // Engine label — short, recognisable. Match the getbased PWA's
  // terseness: one row, not a card.
  let engineLabel;
  if (engine === "onnx") {
    const prov = (emb.provider || "").toLowerCase();
    const provShort = prov.includes("cuda")
      ? "CUDA"
      : prov.includes("rocm")
      ? "ROCm"
      : prov.includes("coreml")
      ? "CoreML"
      : prov.includes("openvino")
      ? "OpenVINO"
      : prov.includes("cpu")
      ? "CPU"
      : prov || "auto";
    engineLabel = `ONNX · ${provShort}`;
  } else if (engine === "pytorch") {
    const device = (emb.device || "cpu").toUpperCase();
    engineLabel = `PyTorch · ${device}`;
  } else if (engine === "qdrant-cloud") {
    engineLabel = `Qdrant Cloud${emb.host ? ` · ${emb.host}` : ""}`;
  } else {
    engineLabel = engine;
  }

  // Trim overly-long model ids — "sentence-transformers/all-MiniLM-L6-v2"
  // shows more usefully as "all-MiniLM-L6-v2". Keep the full value in
  // title so hover reveals it.
  const modelShort = model.includes("/") ? model.split("/").pop() : model;

  const rerankerCell = _info.reranker
    ? '<span class="engine-pill warn">reranker on</span>'
    : "";
  const floorCell =
    _info.similarity_floor != null
      ? `<span class="engine-cell">floor <strong>${_info.similarity_floor}</strong></span>`
      : "";
  const loadedCell = loaded
    ? '<span class="engine-pill ok">ready</span>'
    : '<span class="engine-pill dim">cold</span>';

  return `
    <div class="engine-strip" title="${esc(model)}">
      <span class="engine-cell"><span class="engine-k">engine</span> <strong>${esc(engineLabel)}</strong></span>
      <span class="engine-cell"><span class="engine-k">model</span> <strong>${esc(modelShort)}</strong></span>
      <span class="engine-cell"><span class="engine-k">dim</span> <strong>${dim != null ? dim : "—"}</strong></span>
      ${floorCell}
      ${rerankerCell}
      ${loadedCell}
    </div>
  `;
}

function renderLibraries(root) {
  const libs = _libraries.libraries || [];
  const active = _libraries.activeId;
  const rows = libs
    .map((lib) => {
      const isActive = lib.id === active;
      const activateOrBadge = isActive
        ? '<span class="badge ok">active</span>'
        : `<button data-act="activate" data-id="${esc(lib.id)}">activate</button>`;
      const modelChip = lib.embedding_model
        ? `<span class="model-chip" title="${esc(lib.embedding_model)}">${esc(_modelLabel(lib.embedding_model))}</span>`
        : "";
      const chunksLabel = lib.chunks != null
        ? `${lib.chunks.toLocaleString()} chunk${lib.chunks === 1 ? "" : "s"}`
        : "—";
      return `
        <li class="lib-row ${isActive ? "is-active" : ""}">
          <div class="lib-name">${esc(lib.name || "unnamed")}${modelChip}</div>
          <div class="lib-meta">
            <span class="lib-chunks">${esc(chunksLabel)}</span>
            <span class="lib-meta-sep">·</span>
            <span class="lib-lastingest" title="${lib.lastIngestAt ? new Date(lib.lastIngestAt).toLocaleString() : "never indexed"}">indexed ${_relTime(lib.lastIngestAt)}</span>
          </div>
          <div class="lib-actions">
            ${activateOrBadge}
            <button data-act="rename" data-id="${esc(lib.id)}">rename</button>
            <button class="danger" data-act="delete" data-id="${esc(lib.id)}">delete</button>
          </div>
        </li>
      `;
    })
    .join("");

  // Model picker for the create-library form. Only render options from
  // the curated /models list; server default is pre-selected.
  const modelOptions = (_models.models || [])
    .map((m) => {
      const selected = m.id === _models.default ? " selected" : "";
      const dim = m.dim ? ` · ${m.dim}d` : "";
      const sz = m.size_mb ? ` · ${m.size_mb >= 1024 ? (m.size_mb / 1024).toFixed(1) + "GB" : m.size_mb + "MB"}` : "";
      return `<option value="${esc(m.id)}"${selected}>${esc(m.label || m.id)}${dim}${sz}</option>`;
    })
    .join("");
  const modelPicker = modelOptions
    ? `<label class="model-picker">model <select name="embedding_model">${modelOptions}</select></label>`
    : "";

  const sourceRows = (_stats.documents || [])
    .map(
      (d) => `
        <li class="src-row">
          <div class="src-chunks">${d.chunks}</div>
          <div class="src-name">${esc(d.source)}</div>
          <button class="danger small" data-act="del-source" data-source="${esc(d.source)}">delete</button>
        </li>
      `
    )
    .join("");

  root.innerHTML = `
    ${renderEngineBadge()}
    <section class="panel">
      <div class="panel-head">
        <h2>Libraries</h2>
        <form id="create-lib" class="inline-form">
          <input name="name" placeholder="New library name" required />
          ${modelPicker}
          <button type="submit">Create</button>
        </form>
      </div>
      <p class="panel-sub" style="margin: -4px 0 12px">Each library is pinned to its model at creation — vectors are dim-locked and can't be switched later.</p>
      <ul class="lib-list">${rows || '<li class="empty">No libraries yet — create one above.</li>'}</ul>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>Ingest</h2>
        <div class="panel-sub">Drop files into the active library</div>
      </div>
      <div id="drop-zone" class="drop-zone">
        <p>Drag &amp; drop .md / .txt / .pdf / .docx / .zip here, or <button type="button" id="pick-files-btn" class="link">pick files</button></p>
        <input type="file" id="file-input" class="visually-hidden" multiple />
      </div>
    </section>

    <section class="panel">
      <div class="panel-head"><h2>Search</h2></div>
      <form id="search-form" class="inline-form">
        <input name="query" placeholder="Ask your knowledge base…" required />
        <label>top k <input type="number" name="top_k" min="1" max="20" value="5" /></label>
        <button type="submit">Search</button>
      </form>
      <div id="search-results" class="results"></div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>Sources (active library)</h2>
        ${(_stats.documents || []).length
          ? `<button id="clear-sources" class="ghost danger" type="button">Delete all</button>`
          : ""}
      </div>
      <div class="stat-total">Total chunks: ${_stats.total_chunks}</div>
      <ul class="src-list">${sourceRows || '<li class="empty">No sources indexed yet.</li>'}</ul>
    </section>
  `;

  wireHandlers(root);
}

function wireHandlers(root) {
  // Library mutations — submit-in-flight guard prevents rapid double-clicks
  // producing duplicate libraries (the server happily creates two with the
  // same name otherwise).
  const createForm = root.querySelector("#create-lib");
  const createBtn = createForm.querySelector("button[type=submit]");
  createForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (createBtn.disabled) return;
    const fd = new FormData(e.target);
    const name = fd.get("name").toString().trim();
    const embedding_model = (fd.get("embedding_model") || "").toString().trim();
    if (!name) return;
    createBtn.disabled = true;
    try {
      const body = { name };
      if (embedding_model) body.embedding_model = embedding_model;
      await j("/api/knowledge/libraries", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      await render(root);
    } catch (err) {
      await showAlert(`Failed: ${err.message}`, { tone: "error" });
    } finally {
      if (createBtn.isConnected) createBtn.disabled = false;
    }
  });

  root.querySelectorAll("[data-act]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const act = btn.dataset.act;
      const id = btn.dataset.id;
      const source = btn.dataset.source;
      // Resolve the current display name for the action's target so
      // prompt/confirm copy can reference it by name instead of id hash.
      const lib = (_libraries.libraries || []).find((l) => l.id === id);
      const libName = lib ? lib.name : id;
      try {
        if (act === "activate") {
          await j(`/api/knowledge/libraries/${encodeURIComponent(id)}/activate`, { method: "POST" });
        } else if (act === "rename") {
          const name = await showPrompt(`Rename "${libName}" to:`, {
            defaultValue: libName,
            okLabel: "Rename",
            placeholder: "Library name",
          });
          if (!name) return;
          await j(`/api/knowledge/libraries/${encodeURIComponent(id)}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name }),
          });
        } else if (act === "delete") {
          const ok = await showConfirm(
            `Delete "${libName}"? All indexed documents in it will be permanently removed.`,
            { okLabel: "Delete", danger: true }
          );
          if (!ok) return;
          await j(`/api/knowledge/libraries/${encodeURIComponent(id)}`, { method: "DELETE" });
        } else if (act === "del-source") {
          const ok = await showConfirm(`Drop all chunks for "${source}"?`, {
            okLabel: "Drop",
            danger: true,
          });
          if (!ok) return;
          await j(`/api/knowledge/sources/${encodeURIComponent(source)}`, { method: "DELETE" });
        }
        render(root);
      } catch (err) {
        await showAlert(`Failed: ${err.message}`, { tone: "error" });
      }
    });
  });

  // Delete-all sources — nukes the active library's chunks but keeps
  // the library itself. Rendered only when there's something to nuke;
  // see renderLibraries() for the conditional.
  const clearSourcesBtn = root.querySelector("#clear-sources");
  if (clearSourcesBtn) {
    clearSourcesBtn.addEventListener("click", async () => {
      const activeLib = (_libraries.libraries || []).find(
        (l) => l.id === _libraries.activeId
      );
      const libName = activeLib ? activeLib.name : "the active library";
      const n = _stats.total_chunks || 0;
      const ok = await showConfirm(
        `Drop all ${n.toLocaleString()} chunk${n === 1 ? "" : "s"} from "${libName}"? The library itself stays; only its indexed content is removed.`,
        { okLabel: "Delete all", danger: true }
      );
      if (!ok) return;
      try {
        await j("/api/knowledge/sources", { method: "DELETE" });
        render(root);
      } catch (err) {
        await showAlert(`Failed: ${err.message}`, { tone: "error" });
      }
    });
  }

  // Search
  root.querySelector("#search-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const query = fd.get("query").toString();
    const top_k = parseInt(fd.get("top_k"), 10) || 5;
    const results = root.querySelector("#search-results");
    results.innerHTML = '<p class="dim">Searching…</p>';
    try {
      const out = await j("/api/knowledge/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, top_k }),
      });
      const chunks = out.chunks || [];
      if (!chunks.length) {
        results.innerHTML = '<p class="dim">No results.</p>';
        return;
      }
      results.innerHTML = chunks
        .map((c, i) => {
          const scoreBadge = c.score != null
            ? `<span class="result-score">${c.score.toFixed(3)}</span>`
            : "";
          return `
            <div class="result">
              <div class="result-head">
                <span class="result-num">[${i + 1}]</span>
                <span class="result-src">${esc(c.source || "unknown")}</span>
                ${scoreBadge}
              </div>
              <div class="result-text">${esc(c.text || "")}</div>
            </div>
          `;
        })
        .join("");
    } catch (err) {
      results.innerHTML = `<p class="err">${esc(err.message)}</p>`;
    }
  });

  // Ingest — drag-drop + file input. Status is conveyed entirely
  // through the bottom-right pill; no persistent inline message in the
  // drop zone (that would lie after the user switches libraries).
  const dz = root.querySelector("#drop-zone");
  const input = root.querySelector("#file-input");

  async function doIngest(fileList) {
    if (!fileList || !fileList.length) return;
    const fd = new FormData();
    for (const f of fileList) fd.append("files", f, f.name);
    await runIngest(fd, () => render(root));
  }

  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      dz.classList.add("hover");
    })
  );
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      dz.classList.remove("hover");
    })
  );
  dz.addEventListener("drop", (e) => doIngest(e.dataTransfer.files));
  input.addEventListener("change", () => doIngest(input.files));

  // Click "pick files" → open the native picker. We avoid the
  // <label>-wraps-hidden-input pattern because modern Chrome will not
  // open the picker when the input is display:none (the `hidden` attr
  // sets that). Triggering .click() on an offscreen-but-rendered input
  // is the reliable cross-browser approach.
  root
    .querySelector("#pick-files-btn")
    .addEventListener("click", () => input.click());
}

export async function render(root) {
  root.innerHTML = '<p class="dim">Loading…</p>';
  try {
    await refresh();
    renderLibraries(root);
  } catch (err) {
    root.innerHTML = `<p class="err">Failed to load: ${err.message}</p>`;
  }
}
