/* getbased-dashboard frontend.
 *
 * Auth model: the bearer key is stored under `gbd.key` in localStorage.
 * The server validates against the same file rag + mcp use. First visit
 * shows the auth gate; the rest of the UI stays hidden until the key
 * passes `/api/auth/check`.
 *
 * Cross-tab signals exposed on `window.dashboard`:
 *   - setActiveLibrary(name): update the header chip from any tab
 *   - platform: resolved OS name ("darwin", "linux", "windows", "")
 */

const KEY_STORAGE = "gbd.key";
const THEME_STORAGE = "gbd.theme";

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

// Theme — restored on boot, toggled via header icon.
function _initTheme() {
  const stored = localStorage.getItem(THEME_STORAGE);
  if (stored === "light" || stored === "dark") {
    document.documentElement.setAttribute("data-theme", stored);
  }
}
_initTheme();

function _toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme") || "dark";
  const next = cur === "light" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem(THEME_STORAGE, next);
}

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
  // Auto-focus the key input so keyboard users don't need to tab to it.
  setTimeout(() => document.getElementById("api-key-input")?.focus(), 0);
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

// Active-library chip in the header — any tab can set it so the user
// always knows which library their searches / agent calls will hit.
function setActiveLibrary(name) {
  const el = document.getElementById("active-lib-chip");
  if (!el) return;
  if (!name) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.innerHTML = `<span class="active-lib-dot">●</span> Active library: <strong></strong>`;
  el.querySelector("strong").textContent = name;
}

async function bootstrap() {
  // Unauth health check — tells us where rag expects to find its key
  // and which OS the dashboard is running on (for platform-appropriate
  // MCP config paths).
  let health = {};
  try {
    health = await fetch("/api/health").then((r) => r.json());
  } catch {
    health = {};
  }
  window.dashboard = Object.assign(window.dashboard || {}, {
    platform: health.platform || "",
    setActiveLibrary,
  });

  const banner = document.getElementById("rag-banner");
  if (!health.has_api_key) {
    setStatus("no rag key on disk", "err");
    if (banner) banner.hidden = false;
  } else if (banner) {
    banner.hidden = true;
  }

  // Surface the on-disk key path on the auth gate so users who lost
  // their terminal output have a concrete file to `cat`. Unauthenticated
  // health endpoint already returns it; nothing secret leaked — this is
  // the filesystem location of a file that's already 0600 on disk.
  const keyPathEl = document.getElementById("auth-key-path");
  if (keyPathEl && health.api_key_file) {
    keyPathEl.textContent = health.api_key_file;
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

function _wireKeyVisibilityToggle() {
  const input = document.getElementById("api-key-input");
  const btn = document.getElementById("toggle-key-visibility");
  if (!input || !btn) return;
  btn.addEventListener("click", () => {
    const isPwd = input.type === "password";
    input.type = isPwd ? "text" : "password";
    btn.textContent = isPwd ? "hide" : "show";
    input.focus();
  });
}

function _wireAuthHelpCopy() {
  // The "I don't have my key" details block has copy buttons for the
  // shell commands. Clipboard is reliable on localhost (secure-context
  // carve-out), which is the only place this dashboard runs.
  document.querySelectorAll("[data-auth-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const v = btn.dataset.authCopy || "";
      try {
        await navigator.clipboard.writeText(v);
        const prev = btn.textContent;
        btn.textContent = "copied ✓";
        setTimeout(() => (btn.textContent = prev), 1200);
      } catch {
        // no-op — user can still select the <code> text manually
      }
    });
  });
}

function _wireKeyboardShortcuts() {
  document.addEventListener("keydown", (e) => {
    // Don't hijack while typing in inputs / textareas / contenteditable.
    const tag = (e.target && e.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    if (e.target && e.target.isContentEditable) return;
    // Gate's still up → shortcuts are noise until the user signs in.
    if (!document.getElementById("auth-gate").hidden) return;

    if (e.key === "1") activateTab("knowledge");
    else if (e.key === "2") activateTab("mcp");
    else if (e.key === "3") activateTab("activity");
    else if (e.key === "/") {
      // Focus the knowledge tab's search box if present; otherwise noop.
      const q = document.querySelector('#search-form input[name="query"]');
      if (q) { e.preventDefault(); q.focus(); }
    } else if (e.key === "?") {
      _showShortcutOverlay();
    }
  });
}

function _showShortcutOverlay() {
  // Small inline overlay; dismiss on click / Esc. Avoids a full modal
  // dependency graph here — the modals module handles the heavy
  // confirm/prompt/alert cases, this is just a reference card.
  let el = document.getElementById("shortcut-overlay");
  if (el) { el.remove(); return; }
  el = document.createElement("div");
  el.id = "shortcut-overlay";
  el.className = "shortcut-overlay";
  el.innerHTML = `
    <div class="shortcut-card">
      <h3>Keyboard shortcuts</h3>
      <dl>
        <dt>1 / 2 / 3</dt><dd>Switch tabs</dd>
        <dt>/</dt><dd>Focus search</dd>
        <dt>?</dt><dd>This cheatsheet</dd>
        <dt>Esc</dt><dd>Close dialog / cheatsheet</dd>
      </dl>
      <button type="button" class="confirm-btn confirm-btn-primary">Got it</button>
    </div>
  `;
  document.body.appendChild(el);
  const close = () => el.remove();
  el.addEventListener("click", (e) => { if (e.target === el) close(); });
  el.querySelector("button").addEventListener("click", close);
  document.addEventListener("keydown", function onEsc(e) {
    if (e.key === "Escape") {
      close();
      document.removeEventListener("keydown", onEsc);
    }
  });
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
  _wireKeyVisibilityToggle();
  _wireAuthHelpCopy();
  document.getElementById("theme-toggle")?.addEventListener("click", _toggleTheme);
  _wireKeyboardShortcuts();

  bootstrap();
});
