/* MCP tab — env viewer, config generator, stdio tester.
 *
 * The MCP is a stdio child process, not a service; there's no
 * "connected" state to poll. Instead we let the user:
 *   1. See the resolved env defaults (the values a spawned MCP would read)
 *   2. Copy ready-to-paste config for their AI client
 *   3. Trigger a real spawn to verify the install works
 */

import { authed } from "../app.js";
import { showAlert } from "../modals.js";

function _errMessage(body, status, statusText) {
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

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

const CLIENTS = [
  { id: "claude-desktop", label: "Claude Desktop" },
  { id: "claude-code", label: "Claude Code" },
  { id: "cursor", label: "Cursor" },
  { id: "cline", label: "Cline" },
  { id: "hermes", label: "Hermes" },
];

export async function render(root) {
  root.innerHTML = `
    <section class="panel">
      <div class="panel-head">
        <h2>Environment</h2>
        <button id="refresh-env" class="ghost">refresh</button>
      </div>
      <div id="env-body" class="kv-grid"><p class="dim">Loading…</p></div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>Client config</h2>
        <div class="inline-form">
          <label>client
            <select id="client-picker">
              ${CLIENTS.map((c) => `<option value="${c.id}">${c.label}</option>`).join("")}
            </select>
          </label>
          <button id="copy-cfg">copy</button>
        </div>
      </div>
      <div id="cfg-filename" class="panel-sub"></div>
      <pre id="cfg-body" class="code-block">Loading…</pre>
      <p class="dim" style="margin-top: 8px; font-size: 12px;">
        The <code>GETBASED_TOKEN</code> placeholder needs your read-only token from
        <em>getbased → Settings → Data → Messenger Access</em>. Everything else is filled in.
      </p>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>Test MCP</h2>
        <button id="run-test">run test</button>
      </div>
      <div id="test-body"><p class="dim">Spawns the MCP subprocess, runs <code>tools/list</code>, reports what came back. Verifies your install end-to-end.</p></div>
    </section>
  `;

  await loadEnv(root);
  await loadConfig(root);
  wireHandlers(root);
}

async function loadEnv(root) {
  const body = root.querySelector("#env-body");
  try {
    const e = await j("/api/mcp/env");
    body.innerHTML = `
      <div class="kv-k">LENS_URL</div>
      <div class="kv-v"><code>${esc(e.lens_url)}</code></div>
      <div class="kv-k">LENS_API_KEY_FILE</div>
      <div class="kv-v"><code>${esc(e.lens_api_key_file)}</code> ${e.lens_api_key_present ? '<span class="badge ok">present</span>' : '<span class="badge err">missing</span>'}</div>
      <div class="kv-k">GETBASED_GATEWAY</div>
      <div class="kv-v"><code>${esc(e.getbased_gateway)}</code></div>
      <div class="kv-k">GETBASED_TOKEN</div>
      <div class="kv-v">${e.getbased_token_present ? '<span class="badge ok">set</span>' : '<span class="badge err">not set — configure via client env</span>'}</div>
      <div class="kv-k">MCP module</div>
      <div class="kv-v"><code>${esc(e.mcp_module_path)}</code></div>
    `;
  } catch (err) {
    body.innerHTML = `<p class="err">${esc(err.message)}</p>`;
  }
}

async function loadConfig(root) {
  const client = root.querySelector("#client-picker").value;
  const fnEl = root.querySelector("#cfg-filename");
  const pre = root.querySelector("#cfg-body");
  pre.textContent = "Loading…";
  fnEl.textContent = "";
  try {
    const cfg = await j(`/api/mcp/config?client=${encodeURIComponent(client)}`);
    fnEl.innerHTML = `Paste into: <code>${esc(cfg.filename)}</code>`;
    pre.textContent = cfg.content;
  } catch (err) {
    pre.textContent = "";
    fnEl.innerHTML = `<span class="err">${esc(err.message)}</span>`;
  }
}

async function runTest(root) {
  const out = root.querySelector("#test-body");
  out.innerHTML = '<p class="dim">Spawning MCP…</p>';
  try {
    const r = await j("/api/mcp/test", { method: "POST" });
    if (!r.ok) {
      out.innerHTML = `<p class="err">${esc(r.error || "Test failed")}</p>`;
      return;
    }
    const tools = (r.tools || []).map((t) => `<li><code>${esc(t)}</code></li>`).join("");
    const srv = r.server_info || {};
    out.innerHTML = `
      <div class="test-ok">
        <span class="badge ok">OK</span>
        <span>${r.elapsed_ms} ms · server ${esc(srv.name || "?")} ${esc(srv.version || "")}</span>
      </div>
      <p class="dim" style="margin: 10px 0 6px">Tools discovered (${(r.tools || []).length}):</p>
      <ul class="tool-list">${tools}</ul>
    `;
  } catch (err) {
    out.innerHTML = `<p class="err">${esc(err.message)}</p>`;
  }
}

function wireHandlers(root) {
  root.querySelector("#refresh-env").addEventListener("click", () => loadEnv(root));
  root.querySelector("#client-picker").addEventListener("change", () => loadConfig(root));
  // Guard against rapid double-clicks spawning two MCP subprocesses
  // concurrently — each spawn is ~500ms and pays a startup cost, so
  // letting users queue them up achieves nothing useful.
  const testBtn = root.querySelector("#run-test");
  testBtn.addEventListener("click", async () => {
    if (testBtn.disabled) return;
    testBtn.disabled = true;
    try {
      await runTest(root);
    } finally {
      testBtn.disabled = false;
    }
  });
  root.querySelector("#copy-cfg").addEventListener("click", async () => {
    const text = root.querySelector("#cfg-body").textContent;
    try {
      await navigator.clipboard.writeText(text);
      const btn = root.querySelector("#copy-cfg");
      const prev = btn.textContent;
      btn.textContent = "copied ✓";
      setTimeout(() => (btn.textContent = prev), 1200);
    } catch {
      await showAlert("Copy failed — select the text manually.", { tone: "error" });
    }
  });
}
