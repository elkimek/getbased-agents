/* Activity tab — per-tool usage + recent call feed.
 *
 * Auto-refreshes every 10s while the tab is visible, so you can watch
 * your agent's activity live without reloading. Polling stops when the
 * user switches to another tab (handled by the visibilitychange hook).
 */

import { authed } from "../app.js";

let _refreshTimer = null;

async function j(path, opts = {}) {
  const r = await authed(path, opts);
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail || err.error || `HTTP ${r.status}`);
  }
  return r.json();
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtTime(ts) {
  // Server emits seconds since epoch as a float
  return new Date(ts * 1000).toLocaleTimeString();
}

function fmtMs(ms) {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

function fmtRate(r) {
  return r == null ? "—" : `${(r * 100).toFixed(1)}%`;
}

async function load(root) {
  try {
    const data = await j("/api/activity?limit=200");
    const stats = data.stats;
    const records = (data.records || []).slice().reverse(); // newest first in UI

    const topStats = `
      <div class="stat-cards">
        <div class="stat-card"><div class="stat-v">${stats.total_calls}</div><div class="stat-l">total calls</div></div>
        <div class="stat-card"><div class="stat-v">${stats.total_errors}</div><div class="stat-l">errors</div></div>
        <div class="stat-card"><div class="stat-v">${fmtRate(stats.overall_error_rate)}</div><div class="stat-l">error rate</div></div>
        <div class="stat-card"><div class="stat-v">${stats.tools.length}</div><div class="stat-l">tools in use</div></div>
      </div>
    `;

    const perTool = stats.tools.length
      ? `
        <table class="tool-stats">
          <thead><tr>
            <th>Tool</th><th>Calls</th><th>Errors</th><th>Error rate</th><th>P50</th><th>P95</th>
          </tr></thead>
          <tbody>
            ${stats.tools
              .map(
                (t) => `<tr>
                  <td><code>${esc(t.tool)}</code></td>
                  <td>${t.calls}</td>
                  <td>${t.errors}</td>
                  <td>${fmtRate(t.error_rate)}</td>
                  <td>${fmtMs(t.p50_ms)}</td>
                  <td>${fmtMs(t.p95_ms)}</td>
                </tr>`
              )
              .join("")}
          </tbody>
        </table>
      `
      : '<p class="dim">No tool calls logged yet. Once your AI client invokes an MCP tool, records show up here.</p>';

    const feed = records.length
      ? `<ul class="activity-feed">
          ${records
            .map(
              (r) => `<li class="feed-row ${r.ok ? "" : "err"}">
                <span class="feed-t">${fmtTime(r.ts)}</span>
                <span class="feed-tool"><code>${esc(r.tool)}</code></span>
                <span class="feed-d">${fmtMs(r.duration_ms)}</span>
                <span class="feed-s">${r.ok ? "OK" : esc(r.error || "err")}</span>
              </li>`
            )
            .join("")}
        </ul>`
      : '<p class="dim">No recent activity.</p>';

    root.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>Usage</h2>
          <div class="panel-sub">Log: <code>${esc(data.log_path)}</code> · <button id="clear-log" class="ghost">clear</button></div>
        </div>
        ${topStats}
        ${perTool}
      </section>

      <section class="panel">
        <div class="panel-head"><h2>Recent calls</h2><div class="panel-sub">newest first · auto-refresh every 10s</div></div>
        ${feed}
      </section>
    `;

    root.querySelector("#clear-log").addEventListener("click", async () => {
      if (!confirm("Clear the activity log?")) return;
      try {
        await j("/api/activity", { method: "DELETE" });
        load(root);
      } catch (err) {
        alert(err.message);
      }
    });
  } catch (err) {
    root.innerHTML = `<p class="err">${esc(err.message)}</p>`;
  }
}

export async function render(root) {
  root.innerHTML = '<p class="dim">Loading…</p>';
  await load(root);

  // Start / restart the auto-refresh poll. Cleared on tab switch via
  // visibilitychange below.
  if (_refreshTimer) clearInterval(_refreshTimer);
  _refreshTimer = setInterval(() => {
    if (!document.hidden && !root.hidden) load(root);
  }, 10_000);
}
