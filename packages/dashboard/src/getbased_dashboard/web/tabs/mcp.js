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
      <div class="kv-k" title="The bearer token rag generated on first start. Paste into the getbased PWA's External server field or into an AI client's MCP config.">LENS_API_KEY</div>
      <div class="kv-v">
        <code id="api-key-value">${'\u2022'.repeat(32)}</code>
        <button type="button" id="reveal-api-key" class="ghost" style="font-size:10px;padding:2px 8px">show</button>
        <button type="button" id="copy-api-key" class="ghost" style="font-size:10px;padding:2px 8px">copy</button>
      </div>
      <div class="kv-k">GETBASED_GATEWAY</div>
      <div class="kv-v"><code>${esc(e.getbased_gateway)}</code></div>
      <div class="kv-k" title="The MCP reads GETBASED_TOKEN from the env of whatever launches it (Claude Desktop, Hermes, Claude Code, etc). This row reflects the dashboard's own env — i.e. what a locally-spawned MCP would inherit. In production, the token goes in the client's config file, not here.">GETBASED_TOKEN</div>
      <div class="kv-v">${e.getbased_token_present ? '<span class="badge ok">set</span>' : '<span class="badge err" title="Empty in the dashboard\'s env. This is expected when running locally without the sync gateway — the token normally lives in your AI client\'s MCP config block, not here.">not set — configure via client env</span>'}</div>
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

async function _fetchApiKey() {
  const r = await j("/api/auth/api-key");
  return r.api_key || "";
}

function _maskKey(n) {
  return "\u2022".repeat(Math.max(8, n));
}

function wireHandlers(root) {
  root.querySelector("#refresh-env").addEventListener("click", () => loadEnv(root));
  root.querySelector("#client-picker").addEventListener("change", () => loadConfig(root));

  // API key show/copy — re-bound every render since loadEnv rewrites
  // the panel. `revealedAt` drives auto-remask; 15s feels right for
  // "glance, paste, hide" without being annoying if the user wanted
  // longer.
  const revealBtn = root.querySelector("#reveal-api-key");
  const copyKeyBtn = root.querySelector("#copy-api-key");
  const keyEl = root.querySelector("#api-key-value");
  let revealTimer = null;
  if (revealBtn && keyEl) {
    revealBtn.addEventListener("click", async () => {
      if (revealBtn.textContent === "hide") {
        keyEl.textContent = _maskKey(32);
        revealBtn.textContent = "show";
        if (revealTimer) { clearTimeout(revealTimer); revealTimer = null; }
        return;
      }
      try {
        const key = await _fetchApiKey();
        if (!key) { keyEl.textContent = "(empty — rag never started?)"; return; }
        keyEl.textContent = key;
        revealBtn.textContent = "hide";
        if (revealTimer) clearTimeout(revealTimer);
        revealTimer = setTimeout(() => {
          keyEl.textContent = _maskKey(32);
          revealBtn.textContent = "show";
          revealTimer = null;
        }, 15_000);
      } catch (err) {
        keyEl.textContent = `(error: ${err.message})`;
      }
    });
  }
  if (copyKeyBtn) {
    copyKeyBtn.addEventListener("click", async () => {
      try {
        const key = await _fetchApiKey();
        if (!key) return;
        await navigator.clipboard.writeText(key);
        const prev = copyKeyBtn.textContent;
        copyKeyBtn.textContent = "copied ✓";
        setTimeout(() => (copyKeyBtn.textContent = prev), 1200);
      } catch (err) {
        // Fall back to alert/modal — clipboard blocked in insecure
        // context (http non-localhost) or user denied permission.
        const { showAlert } = await import("../modals.js");
        await showAlert("Copy failed — reveal the key and select it manually.", { tone: "error" });
      }
    });
  }
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
