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
  [_libraries, _stats] = await Promise.all([
    j("/api/knowledge/libraries"),
    j("/api/knowledge/stats"),
  ]);
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
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
      return `
        <li class="lib-row ${isActive ? "is-active" : ""}">
          <div class="lib-name">${esc(lib.name || "unnamed")}</div>
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
    <section class="panel">
      <div class="panel-head">
        <h2>Libraries</h2>
        <form id="create-lib" class="inline-form">
          <input name="name" placeholder="New library name" required />
          <button type="submit">Create</button>
        </form>
      </div>
      <ul class="lib-list">${rows || '<li class="empty">No libraries yet — create one above.</li>'}</ul>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>Ingest</h2>
        <div class="panel-sub">Drop files into the active library</div>
      </div>
      <div id="drop-zone" class="drop-zone">
        <p>Drag &amp; drop .md / .txt / .pdf / .docx / .zip here, or <label class="link">pick files<input type="file" id="file-input" multiple hidden></label></p>
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
    const name = new FormData(e.target).get("name").toString().trim();
    if (!name) return;
    createBtn.disabled = true;
    try {
      await j("/api/knowledge/libraries", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
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

  async function doIngest(fileList) {
    if (!fileList || !fileList.length) return;
    const fd = new FormData();
    for (const f of fileList) fd.append("files", f, f.name);
    setStatus(`Ingesting ${fileList.length} file(s)…`, "");
    try {
      const out = await authed("/api/knowledge/ingest", { method: "POST", body: fd });
      if (!out.ok) {
        const body = await out.json().catch(() => null);
        throw new Error(_errMessage(body, out.status, out.statusText));
      }
      const result = await out.json();
      setStatus(
        `Indexed ${result.chunks_indexed} chunk(s) from ${result.files_seen} file(s). Skipped: ${(result.skipped || []).length}`,
        "ok"
      );
      render(root);  // sources list refresh — preserved status re-applies
    } catch (err) {
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
