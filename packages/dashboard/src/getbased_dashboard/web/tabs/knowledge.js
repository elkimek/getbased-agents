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

async function j(path, opts = {}) {
  const r = await authed(path, opts);
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail || err.error || `HTTP ${r.status}`);
  }
  return r.json();
}

async function refresh() {
  [_libraries, _stats] = await Promise.all([
    j("/api/knowledge/libraries"),
    j("/api/knowledge/stats"),
  ]);
}

function html(strings, ...values) {
  const parts = [];
  strings.forEach((s, i) => {
    parts.push(s);
    if (i < values.length) {
      const v = values[i];
      parts.push(
        v == null
          ? ""
          : String(v).replace(/[&<>"']/g, (c) => ({
              "&": "&amp;",
              "<": "&lt;",
              ">": "&gt;",
              '"': "&quot;",
              "'": "&#39;",
            }[c]))
      );
    }
  });
  return parts.join("");
}

function renderLibraries(root) {
  const libs = _libraries.libraries || [];
  const active = _libraries.activeId;
  const rows = libs
    .map(
      (lib) => html`
        <li class="lib-row ${lib.id === active ? "is-active" : ""}">
          <div class="lib-name">${lib.name || "unnamed"}</div>
          <div class="lib-id">${lib.id}</div>
          <div class="lib-actions">
            ${lib.id === active
              ? '<span class="badge ok">active</span>'
              : `<button data-act="activate" data-id="${lib.id}">activate</button>`}
            <button data-act="rename" data-id="${lib.id}">rename</button>
            <button class="danger" data-act="delete" data-id="${lib.id}">delete</button>
          </div>
        </li>
      `
    )
    .join("");

  root.innerHTML = html`
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
      <ul class="src-list">
        ${(_stats.documents || [])
          .map(
            (d) => html`
              <li class="src-row">
                <div class="src-chunks">${d.chunks}</div>
                <div class="src-name">${d.source}</div>
                <button class="danger small" data-act="del-source" data-source="${d.source}">delete</button>
              </li>
            `
          )
          .join("") || '<li class="empty">No sources indexed yet.</li>'}
      </ul>
    </section>
  `;

  wireHandlers(root);
}

function wireHandlers(root) {
  // Library mutations
  root.querySelector("#create-lib").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = new FormData(e.target).get("name").toString().trim();
    if (!name) return;
    await safeMutate(() =>
      j("/api/knowledge/libraries", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      })
    );
    render(root);
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
        .map(
          (c, i) => html`
            <div class="result">
              <div class="result-head">
                <span class="result-num">[${i + 1}]</span>
                <span class="result-src">${c.source || "unknown"}</span>
                ${c.score != null ? `<span class="result-score">${c.score.toFixed(3)}</span>` : ""}
              </div>
              <div class="result-text">${c.text || ""}</div>
            </div>
          `
        )
        .join("");
    } catch (err) {
      results.innerHTML = `<p class="err">${err.message}</p>`;
    }
  });

  // Ingest — drag-drop + file input
  const dz = root.querySelector("#drop-zone");
  const input = root.querySelector("#file-input");
  const status = root.querySelector("#ingest-status");

  async function doIngest(fileList) {
    if (!fileList || !fileList.length) return;
    const fd = new FormData();
    for (const f of fileList) fd.append("files", f, f.name);
    status.textContent = `Ingesting ${fileList.length} file(s)…`;
    status.className = "ingest-status";
    try {
      const out = await authed("/api/knowledge/ingest", { method: "POST", body: fd });
      if (!out.ok) {
        const err = await out.json().catch(() => ({ detail: out.statusText }));
        throw new Error(err.detail || err.error || `HTTP ${out.status}`);
      }
      const result = await out.json();
      status.textContent = `Indexed ${result.chunks_indexed} chunk(s) from ${result.files_seen} file(s). Skipped: ${(result.skipped || []).length}`;
      status.className = "ingest-status ok";
      render(root);
    } catch (err) {
      status.textContent = `Failed: ${err.message}`;
      status.className = "ingest-status err";
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

async function safeMutate(fn) {
  try {
    await fn();
  } catch (err) {
    alert(`Failed: ${err.message}`);
    throw err;
  }
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
