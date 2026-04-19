/* Custom confirm + prompt dialogs matching the getbased PWA.
 *
 * Why not window.confirm / window.prompt?
 *   - Native dialogs are ugly across themes (always light, native OS style)
 *   - They block the event loop synchronously
 *   - Some browsers (and Electron) disable them altogether in certain contexts
 *
 * Dialogs live at document.body level so they're independent of whichever
 * tab/panel triggered them. The overlay dims the rest of the UI; clicking
 * the backdrop triggers a nudge animation rather than closing, matching
 * the PWA's "accidental misclick" guard.
 */

function _esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function _ensureOverlay(id) {
  let overlay = document.getElementById(id);
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id = id;
    overlay.className = "confirm-overlay";
    document.body.appendChild(overlay);
  }
  return overlay;
}

function _backdropNudge(overlay) {
  overlay.onclick = (e) => {
    if (e.target !== overlay) return;
    const d = overlay.querySelector(".confirm-dialog");
    if (!d) return;
    d.classList.add("modal-nudge");
    d.addEventListener(
      "animationend",
      () => d.classList.remove("modal-nudge"),
      { once: true }
    );
  };
}

/**
 * showConfirm(message, { okLabel, cancelLabel, danger }) → Promise<boolean>
 * Resolves true on confirm, false on cancel / Esc.
 */
export function showConfirm(message, opts = {}) {
  const {
    okLabel = "Confirm",
    cancelLabel = "Cancel",
    danger = true,
  } = opts;
  return new Promise((resolve) => {
    const overlay = _ensureOverlay("confirm-dialog-overlay");
    const okCls = danger ? "confirm-btn-danger" : "confirm-btn-primary";
    overlay.innerHTML = `
      <div class="confirm-dialog" role="alertdialog" aria-modal="true" aria-label="Confirmation">
        <p class="confirm-message">${_esc(message)}</p>
        <div class="confirm-actions">
          <button class="confirm-btn confirm-btn-cancel" id="__confirm-cancel">${_esc(cancelLabel)}</button>
          <button class="confirm-btn ${okCls}" id="__confirm-ok">${_esc(okLabel)}</button>
        </div>
      </div>
    `;
    overlay.classList.add("show");

    const cleanup = (answer) => {
      overlay.classList.remove("show");
      document.removeEventListener("keydown", onKey);
      resolve(answer);
    };
    const onKey = (e) => {
      if (e.key === "Escape") cleanup(false);
      else if (e.key === "Enter") cleanup(true);
    };
    document.addEventListener("keydown", onKey);
    document.getElementById("__confirm-ok").onclick = () => cleanup(true);
    document.getElementById("__confirm-cancel").onclick = () => cleanup(false);
    _backdropNudge(overlay);
    // Default-focus the SAFE button (cancel) — matches the PWA pattern so
    // a user mashing Enter doesn't accidentally delete something.
    setTimeout(() => document.getElementById("__confirm-cancel")?.focus(), 0);
  });
}

/**
 * showPrompt(message, { defaultValue, okLabel, cancelLabel, placeholder })
 *   → Promise<string | null>
 * Resolves trimmed string on OK, or null on Cancel / Esc / empty input.
 */
export function showPrompt(message, opts = {}) {
  const {
    defaultValue = "",
    okLabel = "OK",
    cancelLabel = "Cancel",
    placeholder = "",
  } = opts;
  return new Promise((resolve) => {
    const overlay = _ensureOverlay("prompt-dialog-overlay");
    overlay.innerHTML = `
      <div class="confirm-dialog" role="dialog" aria-modal="true" aria-label="Prompt">
        <p class="confirm-message">${_esc(message)}</p>
        <input type="text" id="__prompt-input" class="prompt-input"
               value="${_esc(defaultValue)}"
               placeholder="${_esc(placeholder)}"
               autocomplete="off" />
        <div class="confirm-actions">
          <button class="confirm-btn confirm-btn-cancel" id="__prompt-cancel">${_esc(cancelLabel)}</button>
          <button class="confirm-btn confirm-btn-primary" id="__prompt-ok">${_esc(okLabel)}</button>
        </div>
      </div>
    `;
    overlay.classList.add("show");

    const input = document.getElementById("__prompt-input");
    const cleanup = (answer) => {
      overlay.classList.remove("show");
      document.removeEventListener("keydown", onKey);
      resolve(answer);
    };
    const submit = () => {
      const v = (input.value || "").trim();
      cleanup(v || null);
    };
    const onKey = (e) => {
      if (e.key === "Escape") cleanup(null);
      else if (e.key === "Enter") submit();
    };
    document.addEventListener("keydown", onKey);
    document.getElementById("__prompt-ok").onclick = submit;
    document.getElementById("__prompt-cancel").onclick = () => cleanup(null);
    _backdropNudge(overlay);
    setTimeout(() => {
      input.focus();
      input.select();
    }, 0);
  });
}

/**
 * showAlert(message) → Promise<void>
 * Single-button info modal. Replaces window.alert for failure surfaces.
 */
export function showAlert(message, { okLabel = "OK", tone = "info" } = {}) {
  return new Promise((resolve) => {
    const overlay = _ensureOverlay("alert-dialog-overlay");
    const okCls = tone === "error" ? "confirm-btn-danger" : "confirm-btn-primary";
    overlay.innerHTML = `
      <div class="confirm-dialog" role="alertdialog" aria-modal="true" aria-label="Alert">
        <p class="confirm-message">${_esc(message)}</p>
        <div class="confirm-actions">
          <button class="confirm-btn ${okCls}" id="__alert-ok">${_esc(okLabel)}</button>
        </div>
      </div>
    `;
    overlay.classList.add("show");
    const cleanup = () => {
      overlay.classList.remove("show");
      document.removeEventListener("keydown", onKey);
      resolve();
    };
    const onKey = (e) => {
      if (e.key === "Escape" || e.key === "Enter") cleanup();
    };
    document.addEventListener("keydown", onKey);
    document.getElementById("__alert-ok").onclick = cleanup;
    _backdropNudge(overlay);
    setTimeout(() => document.getElementById("__alert-ok")?.focus(), 0);
  });
}
