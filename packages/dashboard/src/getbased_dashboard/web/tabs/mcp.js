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
  { id: "openclaw", label: "OpenClaw" },
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

function _copyBtn(value, label = "copy") {
  // Small inline copy button bound to a specific string. Kept lightweight
  // (no dataset indirection) so callers can add arbitrary copy-ables
  // without a new wiring hook per field.
  const v = String(value == null ? "" : value);
  return `<button type="button" class="ghost mini-copy" data-copy="${esc(v)}">${esc(label)}</button>`;
}

async function loadEnv(root) {
  const body = root.querySelector("#env-body");
  try {
    const e = await j("/api/mcp/env");
    body.innerHTML = `
      <div class="kv-k" title="The URL the MCP adapter talks to. Paste this into your AI client's MCP env block if you override the default.">LENS_URL</div>
      <div class="kv-v"><code>${esc(e.lens_url)}</code> ${_copyBtn(e.lens_url)}</div>
      <div class="kv-k" title="Path to the bearer-key file on disk. Same key the PWA's External server field wants.">LENS_API_KEY_FILE</div>
      <div class="kv-v"><code>${esc(e.lens_api_key_file)}</code> ${_copyBtn(e.lens_api_key_file)} ${e.lens_api_key_present ? '<span class="badge ok">present</span>' : '<span class="badge err">missing</span>'}</div>
      <div class="kv-k" title="The bearer token rag generated on first start. Paste into the getbased PWA's External server field or into an AI client's MCP config.">LENS_API_KEY</div>
      <div class="kv-v">
        <code id="api-key-value">${'\u2022'.repeat(32)}</code>
        <button type="button" id="reveal-api-key" class="ghost mini-copy">show</button>
        <button type="button" id="copy-api-key" class="ghost mini-copy">copy</button>
      </div>
      <div class="kv-k" title="One-click magic login URL — paste into another browser / second device and it auto-signs-in. Same pattern as Jupyter Lab / Open WebUI / code-server. The URL embeds the bearer key as a query param, so treat it like the key itself.">LOGIN URL</div>
      <div class="kv-v">
        <code id="login-url-value">${'\u2022'.repeat(40)}</code>
        <button type="button" id="reveal-login-url" class="ghost mini-copy">show</button>
        <button type="button" id="copy-login-url" class="ghost mini-copy">copy</button>
      </div>
      <div class="kv-k" title="Where the MCP will send agent-access requests when your AI client provides a GETBASED_TOKEN.">GETBASED_GATEWAY</div>
      <div class="kv-v"><code>${esc(e.getbased_gateway)}</code> ${_copyBtn(e.getbased_gateway)}</div>
      <div class="kv-k" title="The MCP reads GETBASED_TOKEN from the env of whatever launches it (Claude Desktop, Hermes, Claude Code, etc). This row reflects the dashboard's own env — i.e. what a locally-spawned MCP would inherit. Normally the token lives in your AI client's MCP config block, not here.">GETBASED_TOKEN</div>
      <div class="kv-v">${e.getbased_token_present ? '<span class="badge ok">set</span>' : '<span class="badge warn" title="Empty in the dashboard\'s env. This is expected when running locally without the sync gateway — the token normally lives in your AI client\'s MCP config block, not here.">configure in your client\'s MCP env</span>'}</div>
      <div class="kv-k" title="Filesystem path to the getbased_mcp Python module. Useful when debugging package-version mismatches.">MCP module</div>
      <div class="kv-v"><code>${esc(e.mcp_module_path)}</code></div>
    `;
    _wireMiniCopy(body);
  } catch (err) {
    body.innerHTML = `<p class="err">${esc(err.message)}</p>`;
  }
}

function _wireMiniCopy(root) {
  root.querySelectorAll(".mini-copy[data-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const v = btn.dataset.copy || "";
      try {
        await navigator.clipboard.writeText(v);
        const prev = btn.textContent;
        btn.textContent = "copied ✓";
        setTimeout(() => (btn.textContent = prev), 1200);
      } catch {
        // Silently no-op on insecure-context clipboard blocks — the
        // user can still select the <code> text manually.
      }
    });
  });
}

async function loadConfig(root) {
  const client = root.querySelector("#client-picker").value;
  const fnEl = root.querySelector("#cfg-filename");
  const pre = root.querySelector("#cfg-body");
  pre.textContent = "Loading…";
  fnEl.textContent = "";
  try {
    const cfg = await j(`/api/mcp/config?client=${encodeURIComponent(client)}`);
    fnEl.innerHTML = `
      <span class="cfg-filename-label">Paste into</span>
      <code>${esc(cfg.filename)}</code>
      <button type="button" class="ghost mini-copy" data-copy="${esc(cfg.filename)}">copy path</button>
    `;
    _wireMiniCopy(fnEl);
    pre.textContent = cfg.content;
  } catch (err) {
    pre.textContent = "";
    fnEl.innerHTML = `<span class="err">${esc(err.message)}</span>`;
  }
}

async function runTest(root) {
  const out = root.querySelector("#test-body");
  out.innerHTML = '<p class="dim"><span class="spinner" aria-hidden="true"></span> Spawning MCP subprocess…</p>';
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

  // Login URL row — builds `<current origin>/?key=<bearer>` and reuses
  // the masked/reveal/copy pattern the key row established. The URL is
  // as sensitive as the key itself (anyone with it is signed in), so
  // same 15s auto-remask window.
  const revealUrlBtn = root.querySelector("#reveal-login-url");
  const copyUrlBtn = root.querySelector("#copy-login-url");
  const urlEl = root.querySelector("#login-url-value");
  let urlRevealTimer = null;
  async function _buildLoginUrl() {
    const key = await _fetchApiKey();
    if (!key) return "";
    return `${window.location.origin}/?key=${encodeURIComponent(key)}`;
  }
  if (revealUrlBtn && urlEl) {
    revealUrlBtn.addEventListener("click", async () => {
      if (revealUrlBtn.textContent === "hide") {
        urlEl.textContent = _maskKey(40);
        revealUrlBtn.textContent = "show";
        if (urlRevealTimer) { clearTimeout(urlRevealTimer); urlRevealTimer = null; }
        return;
      }
      try {
        const url = await _buildLoginUrl();
        if (!url) { urlEl.textContent = "(no key — rag never started?)"; return; }
        urlEl.textContent = url;
        revealUrlBtn.textContent = "hide";
        if (urlRevealTimer) clearTimeout(urlRevealTimer);
        urlRevealTimer = setTimeout(() => {
          urlEl.textContent = _maskKey(40);
          revealUrlBtn.textContent = "show";
          urlRevealTimer = null;
        }, 15_000);
      } catch (err) {
        urlEl.textContent = `(error: ${err.message})`;
      }
    });
  }
  if (copyUrlBtn) {
    copyUrlBtn.addEventListener("click", async () => {
      try {
        const url = await _buildLoginUrl();
        if (!url) return;
        await navigator.clipboard.writeText(url);
        const prev = copyUrlBtn.textContent;
        copyUrlBtn.textContent = "copied ✓";
        setTimeout(() => (copyUrlBtn.textContent = prev), 1200);
      } catch {
        const { showAlert } = await import("../modals.js");
        await showAlert("Copy failed — reveal the URL and select it manually.", { tone: "error" });
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
