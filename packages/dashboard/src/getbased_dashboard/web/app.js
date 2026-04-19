/* getbased-dashboard frontend.
 *
 * Scope of this scaffold file: auth flow + tab switching + stubbed panels.
 * Per-tab logic (Knowledge, MCP, Activity) will land in separate modules
 * imported from here when their backends are wired up.
 *
 * Auth model: the bearer key is stored under `gbd.key` in localStorage.
 * The server validates against the same file rag + mcp use. First visit
 * shows the auth gate; the rest of the UI stays hidden until the key
 * passes `/api/auth/check`.
 */

const KEY_STORAGE = "gbd.key";

function storedKey() {
  return localStorage.getItem(KEY_STORAGE) || "";
}

function saveKey(k) {
  if (k) localStorage.setItem(KEY_STORAGE, k);
  else localStorage.removeItem(KEY_STORAGE);
}

// Auto-capture `?key=...` from the URL on first load. The CLI prints
// a one-click login URL with the bearer embedded so users don't have
// to copy-paste it from the terminal. After capture we drop the
// query string from the URL (history.replaceState) so a reload doesn't
// re-expose the key and the browser doesn't stash it in history with
// the secret visible. Jupyter / Open WebUI / code-server use the
// same pattern.
function _captureKeyFromUrl() {
  try {
    const url = new URL(window.location.href);
    const k = url.searchParams.get("key");
    if (!k) return false;
    saveKey(k);
    url.searchParams.delete("key");
    // Keep hash + path, drop the ?key=...
    const clean = url.pathname + (url.searchParams.toString() ? "?" + url.searchParams.toString() : "") + url.hash;
    window.history.replaceState(null, "", clean);
    return true;
  } catch {
    return false;
  }
}
_captureKeyFromUrl();

export async function authed(path, opts = {}) {
  const key = storedKey();
  const headers = Object.assign({}, opts.headers, {
    Authorization: `Bearer ${key}`,
  });
  return fetch(path, { ...opts, headers });
}

function setStatus(text, cls) {
  const el = document.getElementById("connection-status");
  el.textContent = text;
  el.className = "status " + (cls || "");
}

function showGate(errMsg = "") {
  document.getElementById("auth-gate").hidden = false;
  document.querySelectorAll(".tab-panel").forEach((p) => (p.hidden = true));
  document.getElementById("auth-error").textContent = errMsg;
}

function hideGate() {
  document.getElementById("auth-gate").hidden = true;
}

const TAB_MODULES = {
  knowledge: () => import("./tabs/knowledge.js"),
  mcp: () => import("./tabs/mcp.js"),
  activity: () => import("./tabs/activity.js"),
};

async function activateTab(name) {
  document.querySelectorAll(".tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.tab === name);
  });
  document.querySelectorAll(".tab-panel").forEach((p) => {
    p.hidden = p.id !== `tab-${name}`;
  });
  const panel = document.getElementById(`tab-${name}`);
  if (!panel) return;
  if (TAB_MODULES[name]) {
    const mod = await TAB_MODULES[name]();
    await mod.render(panel);
  } else if (!panel.dataset.rendered) {
    panel.innerHTML = `<p class="dim">
      <strong>${name}</strong> — landing in an upcoming commit.
    </p>`;
    panel.dataset.rendered = "1";
  }
}

async function bootstrap() {
  // Unauth health check — tells us where rag expects to find its key.
  const health = await fetch("/api/health").then((r) => r.json());
  if (!health.has_api_key) {
    setStatus("no rag key on disk", "err");
  }

  const key = storedKey();
  if (!key) {
    showGate("");
    setStatus("locked", "err");
    return;
  }

  const check = await authed("/api/auth/check");
  if (check.ok) {
    hideGate();
    setStatus("connected", "ok");
    activateTab("knowledge");
  } else {
    saveKey("");
    showGate("That key didn't match. Try again.");
    setStatus("invalid key", "err");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => activateTab(btn.dataset.tab));
  });
  document.getElementById("save-key").addEventListener("click", async () => {
    const input = document.getElementById("api-key-input");
    const v = input.value.trim();
    if (!v) return;
    saveKey(v);
    input.value = "";
    bootstrap();
  });
  document.getElementById("api-key-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") document.getElementById("save-key").click();
  });

  bootstrap();
});
