/* Knowledge tab — library CRUD, ingest, search, stats.
 *
 * Consumes the /api/knowledge/* proxy routes. Every call goes through
 * `authed()` from app.js so the bearer header stays in one place. We
 * re-render the whole panel after any mutation — cheap for this data
 * scale (< 100 libraries, < 1000 sources per library) and keeps state
 * drift impossible.
 */

import { authed } from "../app.js";

let _libraries = { activeId: "", libraries: [] };
let _stats = { total_chunks: 0, documents: [] };
let _info = null;
let _models = { default: "", models: [] };

// Ingest status survives across re-renders so the "Indexed N chunks"
// confirmation doesn't flash and disappear. Cleared when the tab is
// re-entered from scratch or when a new ingest starts.
let _lastIngest = null; // { text: string, cls: "ok" | "err" | "" }

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
      return `
        <li class="lib-row ${isActive ? "is-active" : ""}">
          <div class="lib-name">${esc(lib.name || "unnamed")}${modelChip}</div>
          <div class="lib-id">${esc(lib.id)}</div>
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
        <div id="ingest-progress" class="ingest-progress" hidden>
          <div class="progress-head">
            <span id="progress-label" class="progress-label">Starting…</span>
            <span id="progress-count" class="progress-count"></span>
          </div>
          <div class="progress-track"><div id="progress-fill" class="progress-fill"></div></div>
        </div>
        <div id="ingest-status" class="ingest-status"></div>
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
      <div class="panel-head"><h2>Sources (active library)</h2></div>
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
      alert(`Failed: ${err.message}`);
    } finally {
      // Button is replaced by render() on success — this just handles the
      // error path where the same DOM stays.
      if (createBtn.isConnected) createBtn.disabled = false;
    }
  });

  root.querySelectorAll("[data-act]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const act = btn.dataset.act;
      const id = btn.dataset.id;
      const source = btn.dataset.source;
      try {
        if (act === "activate") {
          await j(`/api/knowledge/libraries/${encodeURIComponent(id)}/activate`, { method: "POST" });
        } else if (act === "rename") {
          const name = prompt("New name:");
          if (!name) return;
          await j(`/api/knowledge/libraries/${encodeURIComponent(id)}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: name.trim() }),
          });
        } else if (act === "delete") {
          if (!confirm("Delete this library? All indexed documents in it will be removed.")) return;
          await j(`/api/knowledge/libraries/${encodeURIComponent(id)}`, { method: "DELETE" });
        } else if (act === "del-source") {
          if (!confirm(`Drop all chunks for "${source}"?`)) return;
          await j(`/api/knowledge/sources/${encodeURIComponent(source)}`, { method: "DELETE" });
        }
        render(root);
      } catch (err) {
        alert(`Failed: ${err.message}`);
      }
    });
  });

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

  // Ingest — drag-drop + file input
  const dz = root.querySelector("#drop-zone");
  const input = root.querySelector("#file-input");
  const status = root.querySelector("#ingest-status");

  // Re-apply any persistent status from a prior ingest so the "Indexed N
  // chunks" confirmation survives the re-render that refreshes the
  // sources list underneath it.
  if (_lastIngest) {
    status.textContent = _lastIngest.text;
    status.className = `ingest-status ${_lastIngest.cls}`;
  }

  function setStatus(text, cls) {
    _lastIngest = { text, cls };
    status.textContent = text;
    status.className = `ingest-status ${cls}`;
  }

  // Progress widget — hidden by default, shown during ingest.
  const progressEl = root.querySelector("#ingest-progress");
  const progressLabel = root.querySelector("#progress-label");
  const progressCount = root.querySelector("#progress-count");
  const progressFill = root.querySelector("#progress-fill");

  function showProgress() {
    progressEl.hidden = false;
    progressLabel.textContent = "Starting…";
    progressCount.textContent = "";
    progressFill.style.width = "0%";
  }
  function hideProgress() {
    progressEl.hidden = true;
  }
  function updateProgress(done, total, source) {
    const pct = total > 0 ? Math.round((done / total) * 100) : 0;
    progressFill.style.width = `${pct}%`;
    progressCount.textContent = `${done} / ${total}`;
    if (source) progressLabel.textContent = source;
  }

  async function doIngest(fileList) {
    if (!fileList || !fileList.length) return;
    const fd = new FormData();
    for (const f of fileList) fd.append("files", f, f.name);
    setStatus("", "");
    showProgress();

    try {
      // Stream NDJSON — rag + dashboard both support the `application/
      // x-ndjson` accept header, emitting {event:"start"}, {event:"file"}
      // per processed file, and a final {event:"result"|"error"}.
      const resp = await authed("/api/knowledge/ingest", {
        method: "POST",
        body: fd,
        headers: { Accept: "application/x-ndjson" },
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => null);
        throw new Error(_errMessage(body, resp.status, resp.statusText));
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let total = 0;
      let final = null;
      let errorMsg = null;

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
          try {
            evt = JSON.parse(line);
          } catch {
            continue; // skip malformed progress line
          }
          if (evt.event === "start") {
            total = evt.total || 0;
            updateProgress(0, total, "");
          } else if (evt.event === "file") {
            updateProgress(evt.index || 0, total || evt.total || 0, evt.source || "");
          } else if (evt.event === "result") {
            final = evt;
          } else if (evt.event === "error") {
            errorMsg = evt.message || "ingest failed";
          }
        }
      }

      hideProgress();
      if (errorMsg) throw new Error(errorMsg);
      if (!final) throw new Error("stream ended without result");

      setStatus(
        `Indexed ${final.chunks_indexed} chunk(s) from ${final.files_seen} file(s). Skipped: ${(final.skipped || []).length}`,
        "ok"
      );
      render(root); // sources list refresh — preserved status re-applies
    } catch (err) {
      hideProgress();
      setStatus(`Failed: ${err.message}`, "err");
    }
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
