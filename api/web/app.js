/* Voice Bridge dashboard - vanilla JS SPA with live SSE updates */
"use strict";

const state = { view: "overview", summary: null, devices: [], calls: [], tenant: null,
  help: [], helpSlug: null, selectedDevice: null };
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/* ------------------------------------------------------------------ utils */
const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
  .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const fmtDur = (s) => { s = Math.max(0, s | 0); const m = (s / 60) | 0;
  return `${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`; };
const ago = (iso) => { if (!iso) return "-"; const d = (Date.now() - new Date(iso)) / 1000;
  if (d < 5) return "just now"; if (d < 60) return `${d | 0}s ago`;
  if (d < 3600) return `${(d / 60) | 0}m ago`; return `${(d / 3600) | 0}h ago`; };
const badge = (v, cls) => `<span class="badge b-${esc(cls || v)}">${esc(v)}</span>`;
const bar = (pct, cls) => `<div class="meter ${cls || ""}"><i style="width:${Math.max(0, Math.min(100, pct))}%"></i></div>`;

function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 2600); }

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) { let m = res.statusText; try { m = (await res.json()).detail || m; } catch (e) {}
    throw new Error(m); }
  if (res.status === 204) return null;
  return res.json();
}
const post = (p, body) => api(p, { method: "POST", body: JSON.stringify(body || {}) });
const patch = (p, body) => api(p, { method: "PATCH", body: JSON.stringify(body || {}) });
const put = (p, body) => api(p, { method: "PUT", body: JSON.stringify(body || {}) });
const del = (p) => api(p, { method: "DELETE" });

/* Rewrite relative markdown links to the dashboard doc endpoint. */
function rewriteDocLinks(src, baseDir) {
  const resolve = (href) => {
    if (/^(https?:|#|\/)/.test(href)) return href;
    const out = [];
    for (const p of (baseDir + href).split("/")) {
      if (p === "" || p === ".") continue;
      if (p === "..") out.pop(); else out.push(p);
    }
    return "/v1/ui/docs/markdown?path=" + encodeURIComponent(out.join("/"));
  };
  return String(src).replace(/\]\(([^)\s]+)\)/g, (m, href) => `](${resolve(href)})`);
}

/* ------------------------------------------------------------- markdown */
function inlineMd(s) {
  s = esc(s);
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return s;
}
function renderTable(head, rows) {
  const cells = (l) => l.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
  const th = cells(head).map((c) => `<th>${inlineMd(c)}</th>`).join("");
  const tr = rows.map((r) => `<tr>${cells(r).map((c) => `<td>${inlineMd(c)}</td>`).join("")}</tr>`).join("");
  return `<table><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table>`;
}
function md(src) {
  const lines = String(src).split(/\r?\n/); let out = ""; let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith("```")) { const buf = []; i++;
      while (i < lines.length && !lines[i].startsWith("```")) buf.push(lines[i++]);
      i++; out += `<pre><code>${esc(buf.join("\n"))}</code></pre>`; continue; }
    let m = line.match(/^(#{1,6})\s+(.*)/);
    if (m) { const l = m[1].length; out += `<h${l}>${inlineMd(m[2])}</h${l}>`; i++; continue; }
    if (line.startsWith("|") && /^\|[\s:|-]+\|$/.test(lines[i + 1] || "")) {
      const head = line; const rows = []; i += 2;
      while (i < lines.length && lines[i].startsWith("|")) rows.push(lines[i++]);
      out += renderTable(head, rows); continue; }
    if (/^[-*]\s/.test(line)) { const buf = [];
      while (i < lines.length && /^[-*]\s/.test(lines[i])) buf.push(`<li>${inlineMd(lines[i++].slice(2))}</li>`);
      out += `<ul>${buf.join("")}</ul>`; continue; }
    if (/^\d+\.\s/.test(line)) { const buf = [];
      while (i < lines.length && /^\d+\.\s/.test(lines[i])) buf.push(`<li>${inlineMd(lines[i++].replace(/^\d+\.\s/, ""))}</li>`);
      out += `<ol>${buf.join("")}</ol>`; continue; }
    if (/^>\s?/.test(line)) { const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ""));
      out += `<blockquote>${inlineMd(buf.join(" "))}</blockquote>`; continue; }
    if (line.trim() === "") { i++; continue; }
    const buf = [line]; i++;
    while (i < lines.length && lines[i].trim() !== "" &&
      !/^(#{1,6}\s|```|\||[-*]\s|\d+\.\s|>\s?)/.test(lines[i])) buf.push(lines[i++]);
    out += `<p>${inlineMd(buf.join(" "))}</p>`;
  }
  return out;
}

/* ---------------------------------------------------------------- router */
function setView(v) {
  state.view = v;
  $$(".sidebar nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === v));
  $$(".view").forEach((s) => s.classList.toggle("active", s.id === "view-" + v));
  const titles = { overview: "Overview", devices: "Devices & Controls", wifi: "Wi-Fi & Provisioning",
    monitor: "Connection Monitor", calls: "Calls", numbers: "Numbers (DIDs)",
    settings: "Device ID Settings", help: "Help & Documentation", api: "API Reference" };
  $("#view-title").textContent = titles[v] || v;
  render(v);
}
$$(".sidebar nav a").forEach((a) => a.onclick = () => setView(a.dataset.view));

/* ------------------------------------------------------------ live stream */
function connectStream() {
  const es = new EventSource("/v1/ui/stream");
  const dot = $("#live-dot"), label = $("#live-label");
  es.addEventListener("tick", (e) => {
    const d = JSON.parse(e.data);
    state.summary = d.summary; state.devices = d.devices; state.calls = d.active_calls;
    dot.className = "dot ok"; label.textContent = "live";
    if (["overview", "monitor"].includes(state.view)) render(state.view);
  });
  es.onopen = () => { dot.className = "dot ok"; label.textContent = "live"; };
  es.onerror = () => { dot.className = "dot bad"; label.textContent = "reconnecting..."; };
}
setInterval(() => { if (state.view === "calls") render("calls"); }, 4000);
/* The demo simulator fabricates telemetry every 6 s. boot() starts it only when the server
   reports demo data (VB_DEMO_DATA=1): on a real console this must never run, or the panel
   would show invented figures next to real devices. */
function startDemoTick() {
  setInterval(() => post("/v1/ui/demo/tick").catch(() => {}), 6000); // gentle simulator
}

/* ----------------------------------------------------------------- views */
async function render(v) {
  if (v === "overview") return renderOverview();
  if (v === "devices") return renderDevices();
  if (v === "wifi") return renderWifi();
  if (v === "monitor") return renderMonitor();
  if (v === "calls") return renderCalls();
  if (v === "numbers") return renderNumbers();
  if (v === "settings") return renderSettings();
  if (v === "help") return renderHelp();
  if (v === "api") return renderApi();
}

async function renderOverview() {
  const s = state.summary;
  const el = $("#view-overview");
  if (!s) { el.innerHTML = `<p class="muted">Loading...</p>`; return; }
  const cards = [
    ["Devices online", `${s.devices_online}/${s.devices_total}`, "small"],
    ["Active calls", s.calls_active, ""],
    ["Avg MOS", s.avg_mos != null ? s.avg_mos : "n/a", ""],
    ["Total link drops", s.total_drops, ""],
    ["Degraded", s.devices_degraded, ""],
    ["Offline", s.devices_offline, ""],
  ];
  let html = `<div class="grid cards">${cards.map(([k, val]) =>
    `<div class="card"><div class="muted">${k}</div><div class="kpi ${val === "" ? "" : ""}">${val}</div></div>`).join("")}</div>`;
  html += `<div class="spacer"></div><h3>Active calls</h3>`;
  html += s.active_calls.length
    ? `<div class="grid cards">${s.active_calls.map(callMiniCard).join("")}</div>`
    : `<div class="card muted">No active calls.</div>`;
  html += `<div class="spacer"></div><h3>Device fleet</h3>` + devicesTable(state.devices, false);
  el.innerHTML = html;
}

function callMiniCard(c) {
  const active = c.active;
  return `<div class="card callcard">
    <div class="between"><span class="big">${esc(c.device_name || c.device_id)}</span>${badge(c.state)}</div>
    <div class="spacer"></div>
    <div class="row">${badge(c.source, c.source)} ${c.direction === "incoming" ? "incoming" : "outgoing"}</div>
    <div class="spacer"></div>
    <div><span class="muted">Number:</span> <strong>${esc(c.whatsapp_number || c.incoming_number || "unknown (anonymous)")}</strong></div>
    <div><span class="muted">Caller:</span> ${esc(c.caller_name || "-")}</div>
    <div><span class="muted">Route:</span> ${esc(c.route || "-")}</div>
    <div><span class="muted">Duration:</span> ${active ? fmtDur(c.duration_s) : fmtDur(c.duration_s) + " (ended)"}</div>
  </div>`;
}

function devicesTable(devices, withActions) {
  if (!devices || !devices.length) return `<div class="card muted">No devices.</div>`;
  const rows = devices.map((d) => `<tr data-dev="${esc(d.device_id || d.id)}">
    <td><strong>${esc(d.name || d.id)}</strong><div class="muted">${esc(d.serial || "")}</div></td>
    <td>${badge(d.status)}</td>
    <td>${esc((d.sound_balance && "") || d.project || "")}</td>
    <td>${esc(d.connected_ip || d.ip || "-")}</td>
    <td>${esc(d.ssid || "-")}</td>
    <td>${d.rssi != null ? d.rssi + " dBm" : "-"}</td>
    <td>${d.drops != null ? d.drops : "-"}</td>
    <td>${d.mos != null ? d.mos : (d.quality ? d.quality.mos : "-")}</td>
    <td>${esc(ago(d.last_seen))}</td>
    ${withActions ? `<td class="row"></td>` : ""}
  </tr>`).join("");
  return `<div class="tablewrap"><table>
    <thead><tr><th>Device</th><th>Status</th><th></th><th>Connected IP</th><th>SSID</th>
      <th>RSSI</th><th>Drops</th><th>MOS</th><th>Last seen</th>${withActions ? "<th></th>" : ""}</tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

async function renderDevices() {
  const el = $("#view-devices");
  const devices = await api("/v1/ui/devices");
  const rows = devices.map((d) => {
    // The socket hub is the live truth. If it says the device is connected, the
    // panel must not render a not-connected status carried over from the last hello
    // ("offline"/"provisioning"/"connecting"); "degraded" stays, since a degraded
    // device is connected but poor-quality.
    const shown = (d.live && ["offline", "provisioning", "connecting"].includes(d.status))
      ? "online" : d.status;
    return `<tr>
    <td><strong>${esc(d.name)}</strong><div class="muted">${esc(d.serial)} · ${esc(d.bluetooth_name)}</div></td>
    <td>${badge(shown)}${d.live ? ' <span class="muted">live</span>' : ""}</td>
    <td>${esc(d.project || "")}</td>
    <td>${esc(d.ip || "-")}</td>
    <td>${d.rssi != null ? d.rssi + " dBm" : "-"}</td>
    <td>${d.muted ? badge("muted", "offline") : badge("live", "online")}</td>
    <td>${d.aec_enabled ? badge("on", "online") : badge("off", "degraded")}</td>
    <td class="row">
      <button class="btn sec" data-act="reconnect" data-id="${d.id}">Reconnect</button>
      <button class="btn sec" data-act="reboot" data-id="${d.id}">Reboot</button>
      <button class="btn sec" data-act="edit" data-id="${d.id}">Manage</button>
      <button class="btn sec" data-act="rename" data-id="${d.id}">Rename</button>
      <button class="btn danger" data-act="delete" data-id="${d.id}">Delete</button>
    </td></tr>`;
  }).join("");
  el.innerHTML = `<div class="between"><span class="muted">Clocked controls per device. Changes apply live.</span>
      <button class="btn" id="new-device">+ Add device</button></div><div class="spacer"></div>
    <div class="tablewrap"><table><thead><tr><th>Device</th><th>Status</th><th>Project</th>
      <th>IP</th><th>RSSI</th><th>Mute</th><th>AEC</th><th>Controls</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
    <div id="device-detail"></div>`;
  $("#new-device").onclick = createDevice;
  $$("#view-devices [data-act]").forEach((b) => b.onclick = () => deviceAction(b.dataset.act, b.dataset.id));
}

async function createDevice() {
  const name = prompt("Device name:", "New Gbox"); if (!name) return;
  try { await post("/v1/ui/devices", { name }); toast("Device created"); renderDevices(); }
  catch (e) { toast("Error: " + e.message); }
}

async function deviceAction(act, id) {
  try {
    if (act === "reconnect") await post(`/v1/ui/devices/${id}/control`, { action: "reconnect" });
    if (act === "reboot") await post(`/v1/ui/devices/${id}/control`, { action: "reboot" });
    if (act === "edit") return renderDeviceDetail(id);
    if (act === "rename") {
      const name = prompt("New device name:", "");
      if (!name || !name.trim()) return;
      await patch(`/v1/ui/devices/${id}`, { name: name.trim() });
      toast("Renamed"); return renderDevices();
    }
    if (act === "delete") {
      const d = state.devices.find((x) => String(x.id) === String(id)) || {};
      if (!confirm(`Delete ${d.name || d.serial || id}? This cannot be undone.`)) return;
      await del(`/v1/ui/devices/${id}`);
      toast("Device deleted"); return renderDevices();
    }
    toast(act + " sent"); renderDevices();
  } catch (e) { toast("Error: " + e.message); }
}

async function renderDeviceDetail(id) {
  const [d, fw, caps] = await Promise.all([
    api(`/v1/ui/devices/${id}`),
    api(`/v1/ui/devices/${id}/forwarding`).catch(() => ({ destination: {}, source: "-" })),
    api("/v1/forwarding/capabilities").catch(() => ({ types: {} })),
  ]);
  const types = caps.types || {};
  const typeNames = Object.keys(types);
  const capRow = (t) => {
    const c = types[t] || {};
    return `<tr><td><code>${esc(t)}</code></td>
      <td><code>${esc(c.sample_uri || "-")}</code></td>
      <td>${(c.transports || []).map(esc).join(" ") || '<span class="muted">—</span>'}</td>
      <td class="muted">${(c.options || []).map(esc).join(", ") || "—"}</td></tr>`;
  };
  const capTable = typeNames.length ? `<table><thead><tr><th>Type</th><th>Example</th><th>Transports</th><th>Options</th></tr></thead>
    <tbody>${typeNames.map(capRow).join("")}</tbody></table>` : "";
  const sb = d.sound_balance || {};
  const fwd = fw.destination || {};
  const ov = fw.override || {};
  const q = d.quality || {};
  const el = $("#device-detail");
  el.innerHTML = `<div class="spacer"></div><div class="card">
    <div class="between"><h3>${esc(d.name)} <span class="muted">${esc(d.serial)}</span></h3>
      <button class="btn sec" id="close-detail">Close</button></div>
    <div class="grid cards">
      <div class="card"><h3>Connectivity</h3>
        <div>Status: ${badge(d.status)}</div>
        <div>Connected IP: <strong>${esc(d.ip || "-")}</strong></div>
        <div>SSID: ${esc(d.ssid || "-")} · RSSI ${d.rssi ?? "-"} dBm</div>
        <div>Drops: <strong>${d.drops}</strong> · Stability ${q.stability_pct ?? "-"}%</div>
      </div>
      <div class="card"><h3>Quality</h3>
        ${q.available ? `
          <div>MOS ${q.mos} · Jitter ${q.jitter_ms} ms</div>
          <div>Loss ${q.loss_pct}% · RTT ${q.latency_ms} ms</div>
          ${bar((q.mos || 0) / 5 * 100, (q.mos || 0) >= 4 ? "good" : (q.mos || 0) >= 3.5 ? "warn" : "bad")}`
        : `
          <div class="muted">Not measured yet.</div>
          <div class="muted">The board does not report MOS, jitter, loss or RTT. What it does
            report is real: RSSI ${d.rssi ?? "-"} dBm, uptime, free heap, and its frame and
            drop counters.</div>`}
      </div>
      <div class="card"><h3>Sound balance</h3>
        <div>Speaker <strong>${sb.speaker_volume}</strong> · Mic gain <strong>${sb.mic_gain}</strong></div>
        <div>${sb.balanced ? badge("balanced", "online") : badge("imbalance " + sb.imbalance, "degraded")}</div>
        <div class="muted">${esc(sb.recommendation || "")}</div>
        <div>Echo risk: ${badge(sb.echo_risk, sb.echo_risk === "high" ? "offline" : "online")} · ERLE ${sb.erle_db ?? "-"} dB</div>
      </div>
      <div class="card"><h3>Voice forwarding</h3>
        <div>Destination: <strong>${esc(fwd.uri || "-")}</strong> ${badge(fwd.type || "simulated", fwd.type && fwd.type !== "simulated" ? "online" : "degraded")}</div>
        <div class="muted">via ${esc(fw.source || "-")}${fwd.mode ? " · " + esc(fwd.mode) : ""}${fwd.transport ? " · " + esc(fwd.transport) : ""}</div>
        <div class="row" style="gap:6px;margin-top:8px;flex-wrap:wrap">
          <select id="fw-type">${(typeNames.length ? typeNames : ["simulated", "sip", "socket", "webhook", "recording"]).map((t) =>
            `<option value="${t}" ${((ov.type || fwd.type || "simulated") === t) ? "selected" : ""}>${t}</option>`).join("")}</select>
          <input id="fw-uri" style="flex:1;min-width:220px" placeholder="sip:1000@pbx:5060;transport=tls;register=1 · udp://vps:9999?mode=rtp · https://vps/audio?mode=chunked" value="${esc(ov.uri || "")}">
          <button class="btn" id="fw-save">Save</button>
          <button class="btn sec" id="fw-check" title="Parse the destination without saving it">Test</button>
          <button class="btn sec" id="fw-clear">Clear</button>
        </div>
        <div class="row" style="margin-top:6px">
          <input id="fw-opts" style="flex:1" placeholder='options JSON — {"srtp":1} · {"register":1,"password":"…"} · {"mode":"rtp"} · {"signing_secret":"…"}' value="${esc(ov.options && Object.keys(ov.options).length ? JSON.stringify(ov.options) : "")}">
        </div>
        <div class="muted" style="margin-top:10px">Destination types (from <code>/v1/forwarding/capabilities</code>):</div>
        ${capTable}
      </div>
    </div>
    <div class="spacer"></div>
    <div class="row">
      <label class="field">Speaker volume <input type="range" min="0" max="100" value="${d.volume}" id="vol"></label>
      <label class="field">Mic gain <input type="range" min="0" max="100" value="${d.mic_gain}" id="mic"></label>
      <div class="row" style="align-items:flex-end;gap:8px">
        <button class="btn sec" data-ctl="mute">Mute</button>
        <button class="btn sec" data-ctl="unmute">Unmute</button>
        <button class="btn sec" data-ctl="enable-aec">AEC on</button>
        <button class="btn sec" data-ctl="disable-aec">AEC off</button>
        <button class="btn warn" data-ctl="factory-reset">Factory reset Wi-Fi</button>
      </div>
    </div>
  </div>`;
  $("#close-detail").onclick = () => el.innerHTML = "";
  const send = async (action, value) => { try { await post(`/v1/ui/devices/${id}/control`, { action, value });
    toast(action); renderDeviceDetail(id); } catch (e) { toast(e.message); } };
  $("#vol").onchange = (e) => send("set-volume", +e.target.value);
  $("#mic").onchange = (e) => send("set-mic-gain", +e.target.value);
  $$("#device-detail [data-ctl]").forEach((b) => b.onclick = () => send(b.dataset.ctl));
  $("#fw-check").onclick = async () => {
    let options = {};
    const raw = $("#fw-opts").value.trim();
    if (raw) { try { options = JSON.parse(raw); } catch (e) { return toast("options must be JSON"); } }
    const uri = $("#fw-uri").value.trim();
    if (!uri) return toast("Enter a destination URI first");
    try {
      const r = await post("/v1/forwarding/validate",
        { dest_type: $("#fw-type").value, dest_uri: uri, options });
      if (!r || r.ok !== true) { return toast("invalid: " + ((r && r.error) || "rejected")); }
      const d = r.destination || {};
      toast("valid · " + (d.type || "?") + " -> " + (d.uri || uri));
    } catch (e) { toast(e.message); }
  };
  $("#fw-save").onclick = async () => {
    let options = {};
    const raw = $("#fw-opts").value.trim();
    if (raw) { try { options = JSON.parse(raw); } catch (e) { return toast("options must be JSON"); } }
    try { await put(`/v1/ui/devices/${id}/forwarding`,
        { dest_type: $("#fw-type").value, dest_uri: $("#fw-uri").value, options });
      toast("forwarding saved"); renderDeviceDetail(id);
    } catch (e) { toast(e.message); }
  };
  $("#fw-clear").onclick = async () => {
    try { await del(`/v1/ui/devices/${id}/forwarding`);
      toast("forwarding cleared"); renderDeviceDetail(id);
    } catch (e) { toast(e.message); }
  };
}

/* ----------------------------------------------------------------- Wi-Fi */
async function renderWifi() {
  const el = $("#view-wifi");
  const [devices, nets] = await Promise.all([api("/v1/ui/devices"), api("/v1/ui/networks")]);
  const devOpts = devices.map((d) => `<option value="${d.id}">${esc(d.name)} (${esc(d.serial)})</option>`).join("");
  const netRows = nets.map((n) => `<tr>
    <td>${esc(n.ssid)} ${n.hidden ? '<span class="pill">hidden</span>' : ""}</td>
    <td>${badge(n.band, "provisioning")}</td>
    <td>${n.is_open ? badge("open", "online") : badge(n.security, "cellular")}</td>
    <td>${n.is_requested ? badge("requested", "online") : badge("not saved", "degraded")}</td>
    <td class="row">
      <button class="btn" data-net="${esc(n.ssid)}" data-open="${n.is_open}">Connect device</button>
      ${n.is_open ? "" : `<button class="btn sec" data-req="${n.id}">Save password</button>`}
    </td></tr>`).join("");
  el.innerHTML = `
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(320px,1fr))">
      <div class="card"><h3>Provision a device</h3>
        <p class="muted">Same flow as the device's own portal. Select a device, then connect it to a network.</p>
        <label>Device</label><select id="prov-dev" style="width:100%">${devOpts}</select>
        <div class="spacer"></div>
        <div class="row"><button class="btn" id="scan">Scan networks</button>
          <button class="btn sec" id="prov-start">Show setup (GboxWifi / 192.168.4.1)</button></div>
        <div id="prov-out" class="muted"></div>
      </div>
      <div class="card"><h3>Manual setup</h3>
        <label>SSID</label><input id="set-ssid" class="field" style="width:100%" placeholder="Office-WiFi">
        <label>Password</label><input id="set-psk" class="field" style="width:100%" type="password" placeholder="(blank for open)">
        <div class="spacer"></div>
        <button class="btn" id="set-connect">Save &amp; connect device</button>
      </div>
    </div>
    <div class="spacer"></div>
    <div class="between"><h3>Available networks</h3></div>
    <div class="tablewrap"><table><thead><tr><th>SSID</th><th>Band</th><th>Security</th>
      <th>Profile</th><th>Action</th></tr></thead><tbody>${netRows}</tbody></table></div>
    <div class="spacer"></div>
    <div class="card"><h3>Saved Wi-Fi profiles</h3>
      <p class="muted">Devices auto-connect to saved profiles (priority order) or to open networks matching a profile.</p>
      <label>Device</label><select id="prof-dev" style="width:100%">${devOpts}</select>
      <div class="spacer"></div><div id="prof-list"></div>
    </div>`;
  const devSel = () => $("#prov-dev").value;
  $("#scan").onclick = async () => { const r = await post("/v1/ui/networks/scan"); toast(`Scanned: ${r.scanned} networks`); renderWifi(); };
  $("#prov-start").onclick = async () => { const r = await post(`/v1/ui/devices/${devSel()}/provision/start`);
    $("#prov-out").innerHTML = `Bluetooth: <strong>${esc(r.bluetooth_name)}</strong><br>AP: <strong>${esc(r.ap_ssid)}</strong><br>Portal: <strong>${esc(r.portal)}</strong><br>${esc(r.instruction)}`; };
  const connect = async (ssid, psk) => { try { const r = await post(`/v1/ui/devices/${devSel()}/wifi-setup`, { ssid, psk });
      toast(`Connected ${ssid}`); $("#prov-out").innerHTML = `Connected to <strong>${esc(ssid)}</strong> @ ${esc(r.device.ip)}`; renderWifi();
    } catch (e) { toast("Error: " + e.message); } };
  $("#set-connect").onclick = () => connect($("#set-ssid").value.trim(), $("#set-psk").value || null);
  $$("#view-wifi [data-net]").forEach((b) => b.onclick = () => {
    const ssid = b.dataset.net; const psk = b.dataset.open === "1" ? null : prompt(`Password for ${ssid}:`);
    connect(ssid, psk);
  });
  $$("#view-wifi [data-req]").forEach((b) => b.onclick = async () => {
    const psk = prompt("Enter password to store as a requested profile:"); if (psk == null) return;
    await post(`/v1/ui/networks/${b.dataset.req}/request`, { password: psk }); toast("Profile saved"); renderWifi();
  });
  const loadProfiles = async () => {
    const d = await api(`/v1/ui/devices/${$("#prof-dev").value}`);
    const list = d.wifi_profiles || [];
    $("#prof-list").innerHTML = list.length ? `<div class="tablewrap"><table><thead><tr>
        <th>SSID</th><th>Security</th><th>Priority</th><th>Auto</th><th></th></tr></thead><tbody>
        ${list.map((p) => `<tr><td>${esc(p.ssid)}</td><td>${esc(p.security)}</td><td>${p.priority}</td>
          <td>${p.auto_connect ? "yes" : "no"}</td>
          <td><button class="btn danger" data-del="${p.id}">Remove</button></td></tr>`).join("")}
      </tbody></table></div>` : `<div class="muted">No saved profiles.</div>`;
    $$("#prof-list [data-del]").forEach((b) => b.onclick = async () => {
      await del(`/v1/ui/devices/${$("#prof-dev").value}/wifi-profiles/${b.dataset.del}`);
      toast("Profile removed"); loadProfiles(); });
  };
  $("#prof-dev").onchange = loadProfiles;
  loadProfiles();
}

/* --------------------------------------------------------------- Monitor */
async function renderMonitor() {
  const el = $("#view-monitor");
  const data = await api("/v1/ui/monitor/overview");
  const cards = data.devices.map((d) => {
    const sb = d.sound_balance || {};
    const qual = d.quality || {};
    return `<div class="card">
      <div class="between"><strong>${esc(d.name)}</strong>${badge(d.status)}</div>
      <div class="muted">${esc(d.serial)} · ${esc(d.bluetooth_name || "")}</div>
      <div class="spacer"></div>
      <div class="row"><span class="pill">IP ${esc(d.connected_ip || "-")}</span>
        <span class="pill">SSID ${esc(d.ssid || "-")}</span>
        <span class="pill">RSSI ${d.rssi ?? "-"} dBm</span></div>
      <div class="spacer"></div>
      <div class="between"><span class="muted">Connectivity / drops</span><span><strong>${d.drops}</strong> drops · ${d.stability_pct ?? "-"}% stable</span></div>
      ${bar(d.stability_pct || 0, (d.stability_pct || 0) >= 95 ? "good" : (d.stability_pct || 0) >= 80 ? "warn" : "bad")}
      <div class="between"><span class="muted">Quality (MOS ${qual.mos ?? "-"})</span><span>jitter ${qual.jitter_ms ?? "-"} ms · loss ${qual.loss_pct ?? "-"}%</span></div>
      ${bar((qual.mos || 0) / 5 * 100, (qual.mos || 0) >= 4 ? "good" : (qual.mos || 0) >= 3.5 ? "warn" : "bad")}
      <div class="between"><span class="muted">Sound balance</span><span>spk ${sb.speaker_volume} / mic ${sb.mic_gain} ${sb.balanced ? "✔" : "⚠"}</span></div>
      ${bar(50 + (sb.imbalance || 0) / 2, sb.balanced ? "good" : "warn")}
      <div class="muted">${esc(sb.recommendation || "")}</div>
    </div>`;
  }).join("");
  const evRows = data.recent_events.map((e) => `<tr><td>${esc(ago(e.ts))}</td>
    <td>${badge(e.type, e.type === "disconnect" ? "offline" : "online")}</td>
    <td>${esc(e.detail || "")}</td></tr>`).join("");
  el.innerHTML = `<div class="between"><h3>Live device health</h3>
      <span class="muted">Updates every few seconds</span></div>
    <div class="grid cards">${cards}</div>
    <div class="spacer"></div><h3>Recent link events (drops / reconnects)</h3>
    <div class="tablewrap"><table><thead><tr><th>When</th><th>Type</th><th>Detail</th></tr></thead>
      <tbody>${evRows || '<tr><td colspan="3" class="muted">No events</td></tr>'}</tbody></table></div>`;
}

/* ----------------------------------------------------------------- Calls */
async function renderCalls() {
  const el = $("#view-calls");
  const [active, all] = await Promise.all([api("/v1/ui/calls?active=true"), api("/v1/ui/calls")]);
  const activeCards = active.length ? active.map((c) => `<div class="card callcard">
      <div class="between"><span class="big">${esc(c.device_name)}</span>${badge(c.state)}</div>
      <div class="spacer"></div>
      <div class="row">${badge(c.source, c.source)} <span class="muted">${esc(c.direction)}</span>
        <span class="pill">confidence ${c.confidence ?? "-"}</span></div>
      <div class="spacer"></div>
      <div><span class="muted">WhatsApp number:</span> <strong>${esc(c.whatsapp_number || "-")}</strong></div>
      <div><span class="muted">Incoming number (CLI):</span> ${esc(c.incoming_number || "unavailable")}</div>
      <div><span class="muted">Caller:</span> ${esc(c.caller_name || "-")}</div>
      <div><span class="muted">Route:</span> ${esc(c.route || "-")}</div>
      <div><span class="muted">Duration:</span> ${fmtDur(c.duration_s)}</div>
      <div><span class="muted">QoS:</span> MOS ${c.mos ?? "-"} · jitter ${c.jitter_ms ?? "-"} ms · loss ${c.loss_pct ?? "-"}%</div>
      <div><span class="muted">Echo:</span> AEC ${c.aec_enabled ? "on" : "off"} · ERLE ${c.erle_db ?? "-"} dB</div>
      <div class="spacer"></div>
      <div class="row">
        <button class="btn" data-call="${c.id}" data-act="answer">Answer</button>
        <button class="btn sec" data-call="${c.id}" data-act="hold">Hold</button>
        <button class="btn sec" data-call="${c.id}" data-act="resume">Resume</button>
        <button class="btn danger" data-call="${c.id}" data-act="hangup">Hang up</button>
        <button class="btn sec" data-call="${c.id}" data-act="route">Re-route</button>
      </div></div>`).join("") : `<div class="card muted">No active calls.</div>`;
  const rows = all.map((c) => `<tr><td>${esc(c.device_name)}</td>
    <td>${badge(c.source, c.source)}</td><td>${esc(c.whatsapp_number || c.incoming_number || "-")}</td>
    <td>${esc(c.caller_name || "-")}</td>${badge(c.state)}
    <td>${fmtDur(c.duration_s)}</td><td>${c.mos ?? "-"}</td><td>${esc(ago(c.started_at))}</td></tr>`).join("");
  el.innerHTML = `<h3>Active calls</h3><div class="grid cards">${activeCards}</div>
    <div class="spacer"></div><h3>Call history (CDR)</h3>
    <div class="tablewrap"><table><thead><tr><th>Device</th><th>Source</th><th>Number</th>
      <th>Caller</th><th>State</th><th>Duration</th><th>MOS</th><th>Started</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="8" class="muted">No calls</td></tr>'}</tbody></table></div>`;
  $$("#view-calls [data-call]").forEach((b) => b.onclick = async () => {
    const id = b.dataset.call, act = b.dataset.act;
    try {
      if (act === "route") { const r = prompt("New route (e.g. sip:desk@pbx.example):"); if (!r) return;
        await post(`/v1/ui/calls/${id}/route`, { route: r }); }
      else await post(`/v1/ui/calls/${id}/control`, { action: act });
      toast(`Call ${act}`); renderCalls();
    } catch (e) { toast(e.message); }
  });
}

/* --------------------------------------------------------------- Numbers */
async function renderNumbers() {
  const el = $("#view-numbers");
  const dids = await api("/v1/ui/dids");
  const devices = state.devices || [];
  const devId = (d) => d.device_id || d.id || d.serial;
  const devName = (id) => (devices.find((d) => devId(d) === id) || {}).name || id;
  const options = (selected) => `<option value="">— unassigned —</option>` +
    devices.map((d) => `<option value="${esc(devId(d))}" ${devId(d) === selected ? "selected" : ""}>${esc(d.name)}</option>`).join("");
  const rows = dids.map((d) => `<tr>
      <td><strong>${esc(d.number)}</strong> <span class="muted">${esc(d.region || "")}</span></td>
      <td>${esc(d.label || "-")}</td>
      <td>${d.device_id ? esc(devName(d.device_id)) : '<span class="muted">unassigned</span>'}</td>
      <td>${d.enabled ? badge("active", "online") : badge("disabled", "degraded")}</td>
      <td><select data-assign="${esc(d.id)}">${options(d.device_id)}</select>
          <button class="btn sec" data-del="${esc(d.id)}">Delete</button></td>
    </tr>`).join("");
  el.innerHTML = `
    <div class="card">
      <h3>Add a number</h3>
      <p class="muted">Numbers are stored in canonical E.164 (<code>+country…</code>). National formats
        like <code>(415) 555-0199</code> are normalised automatically.</p>
      <div class="row" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">
        <input id="did-num" placeholder="+1 555 010 0000" style="flex:1;min-width:200px">
        <input id="did-label" placeholder="label (optional)" style="flex:1;min-width:160px">
        <button class="btn" id="did-add">Add number</button>
      </div>
    </div>
    <div class="card">
      <h3>Inventory <span class="muted">(${dids.length})</span></h3>
      <table><thead><tr><th>Number</th><th>Label</th><th>Assigned device</th><th>Status</th><th></th></tr></thead>
        <tbody>${rows || '<tr><td colspan="5" class="muted">No numbers yet.</td></tr>'}</tbody></table>
    </div>`;
  $("#did-add").onclick = async () => {
    const number = $("#did-num").value.trim();
    if (!number) return toast("Enter a number");
    try {
      await post("/v1/ui/dids", { number, label: $("#did-label").value.trim() || null });
      toast("Number added"); renderNumbers();
    } catch (e) { toast(e.message); }
  };
  $$("#view-numbers [data-assign]").forEach((s) => s.onchange = async () => {
    try {
      await post(`/v1/ui/dids/${s.dataset.assign}/assign`, { device_id: s.value || null });
      toast(s.value ? "Assigned" : "Unassigned"); renderNumbers();
    } catch (e) { toast(e.message); }
  });
  $$("#view-numbers [data-del]").forEach((b) => b.onclick = async () => {
    try { await del(`/v1/ui/dids/${b.dataset.del}`); toast("Deleted"); renderNumbers(); }
    catch (e) { toast(e.message); }
  });
}

/* -------------------------------------------------------------- Settings */
async function renderSettings() {
  const el = $("#view-settings");
  const cfg = await api("/v1/ui/settings/device-id");
  el.innerHTML = `<div class="card" style="max-width:640px">
    <h3>Device ID scheme</h3>
    <p class="muted">Serial prefix used for newly added devices. The Bluetooth / SoftAP names derive
      from the serial suffix: <code>Gbox-XXXX</code> and <code>GboxWifi-XXXX</code>.</p>
    <label>Serial prefix</label>
    <input id="prefix" class="field" value="${esc(cfg.prefix)}" maxlength="8">
    <div class="spacer"></div>
    <div class="muted">Next serial example: <strong>${esc(cfg.next_example)}</strong> · Devices: ${cfg.devices}</div>
    <div class="spacer"></div><button class="btn" id="save-prefix">Save</button>
  </div>
  <div class="spacer"></div>
  <div class="card" style="max-width:640px"><h3>Wi-Fi setup model</h3>
    <ul class="muted">
      <li>Devices auto-connect to <strong>saved profiles</strong> (priority order).</li>
      <li>Open networks are joined automatically when a matching profile is allowed.</li>
      <li><strong>Requested</strong> networks are admin-supplied SSID + password pushed to devices.</li>
      <li>Per-device setup: Bluetooth <code>Gbox-XXXX</code> or SoftAP <code>GboxWifi-XXXX</code> at <code>192.168.4.1</code>.</li>
    </ul></div>`;
  $("#save-prefix").onclick = async () => {
    try { await api("/v1/ui/settings/device-id", { method: "PUT", body: JSON.stringify({ prefix: $("#prefix").value }) });
      toast("Saved"); renderSettings(); } catch (e) { toast(e.message); }
  };
}

/* ------------------------------------------------------------------ Help */
async function renderHelp() {
  const el = $("#view-help");
  if (!state.help.length) state.help = await api("/v1/ui/help");
  if (!state.tree) state.tree = await api("/v1/ui/docs/tree");
  if (!state.helpSlug && state.help.length) state.helpSlug = state.help[0].slug;
  const core = state.help.map((h) => `<a data-slug="${h.slug}" class="${h.slug === state.helpSlug ? "active" : ""}">${esc(h.title)}</a>`).join("");
  const guides = state.tree.filter((d) => d.path.startsWith("docs/clients/"));
  const guideList = guides.map((d) => `<a data-doc="${esc(d.path)}">${esc(d.title)}</a>`).join("");
  el.innerHTML = `<div class="help-layout"><div class="help-list card">
      <div class="muted help-group">Guides</div>${core || "<span class='muted'>No help topics.</span>"}
      <div class="muted help-group">Client Documentation</div>${guideList || "<span class='muted'>No client docs.</span>"}
    </div>
    <div class="card"><div class="md" id="help-body">Loading...</div></div></div>`;
  const clearActive = () => $$("#view-help .help-list a").forEach((a) => a.classList.remove("active"));
  const load = async (slug) => { state.helpSlug = slug; clearActive();
    $$(`#view-help .help-list a[data-slug="${slug}"]`).forEach((a) => a.classList.add("active"));
    const t = await api(`/v1/ui/help/${slug}`); $("#help-body").innerHTML = md(t.body); };
  const loadDoc = async (path) => { clearActive();
    $$(`#view-help .help-list a[data-doc="${path}"]`).forEach((a) => a.classList.add("active"));
    const res = await fetch(`/v1/ui/docs/markdown?path=${encodeURIComponent(path)}`);
    const base = path.slice(0, path.lastIndexOf("/") + 1);
    $("#help-body").innerHTML = md(rewriteDocLinks(await res.text(), base)); };
  $$("#view-help .help-list a[data-slug]").forEach((a) => a.onclick = () => load(a.dataset.slug));
  $$("#view-help .help-list a[data-doc]").forEach((a) => a.onclick = () => loadDoc(a.dataset.doc));
  if (state.helpSlug) load(state.helpSlug);
}

/* -------------------------------------------------------------- API view */
async function renderApi() {
  const el = $("#view-api");
  const tree = await api("/v1/ui/docs/tree");
  const groups = [
    ["Summary & tenant", ["/v1/ui/summary", "/v1/ui/tenant", "/v1/ui/health"]],
    ["Devices & controls", ["/v1/ui/devices", "/v1/ui/devices/{id}", "/v1/ui/devices/{id}/control",
      "/v1/ui/devices/{id}/status", "/v1/ui/devices/{id}/telemetry", "/v1/ui/devices/{id}/quality",
      "/v1/ui/devices/{id}/sound-balance"]],
    ["Wi-Fi & provisioning", ["/v1/ui/networks", "/v1/ui/networks/scan",
      "/v1/ui/devices/{id}/wifi-profiles", "/v1/ui/devices/{id}/wifi-setup",
      "/v1/ui/devices/{id}/provision/start", "/v1/ui/devices/{id}/claim"]],
    ["Monitoring", ["/v1/ui/monitor/overview", "/v1/ui/monitor/events", "/v1/ui/stream (SSE)"]],
    ["Calls", ["/v1/ui/calls", "/v1/ui/calls/{id}/control", "/v1/ui/calls/{id}/route"]],
    ["Settings", ["/v1/ui/settings/device-id"]],
  ];
  el.innerHTML = `<div class="card"><h3>Interactive API docs</h3>
      <p class="muted">Full OpenAPI (Swagger) UI at <a href="/docs" target="_blank">/docs</a>,
      schema at <a href="/openapi.json" target="_blank">/openapi.json</a>.</p></div>
    <div class="spacer"></div>
    <div class="grid cards">${groups.map(([g, eps]) => `<div class="card"><h3>${g}</h3>
      ${eps.map((e) => `<div class="muted"><code>${esc(e)}</code></div>`).join("")}</div>`).join("")}</div>
    <div class="spacer"></div><h3>Documentation files</h3>
    <div class="tablewrap"><table><thead><tr><th>Path</th></tr></thead><tbody>
      ${tree.map((d) => `<tr><td><a href="/v1/ui/docs/markdown?path=${encodeURIComponent(d.path)}" target="_blank">${esc(d.path)}</a></td></tr>`).join("")}
    </tbody></table></div>`;
}

/* ---------------------------------------------------------------- boot */
$("#reset-demo").onclick = async () => { await post("/v1/ui/demo/reset"); toast("Demo data reset"); render(state.view); };
$("#sign-out").onclick = async () => { try { await post("/v1/ui/logout"); } catch (e) {} location.reload(); };

/* The BFF sets an HttpOnly session cookie, so every /v1/ui call (and the SSE
   stream) is authenticated without the SPA handling tokens itself. */
function showLogin(message) {
  const box = document.createElement("div");
  box.id = "login-overlay";
  box.style.cssText = "position:fixed;inset:0;background:#0f172a;display:flex;" +
    "align-items:center;justify-content:center;z-index:50";
  box.innerHTML = `<form class="card" style="width:340px;padding:24px">
      <h3 style="margin-top:0">Voice Bridge sign in</h3>
      <p class="muted" style="margin-top:0">${esc(message || "Sign in to view the dashboard.")}</p>
      <label>Email</label>
      <input id="login-email" class="field" placeholder="you@gigies.lk" autocomplete="username">
      <div class="spacer"></div>
      <label>Password</label>
      <input id="login-password" class="field" type="password" placeholder="password" autocomplete="current-password">
      <div class="spacer"></div>
      <button class="btn" style="width:100%">Sign in</button>
    </form>`;
  document.body.appendChild(box);
  box.querySelector("form").onsubmit = async (e) => {
    e.preventDefault();
    const res = await fetch("/v1/ui/login", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: $("#login-email").value, password: $("#login-password").value }) });
    if (res.ok) { location.reload(); return; }
    let msg = "invalid credentials";
    try { msg = (await res.json()).detail || msg; } catch (err) {}
    box.querySelector(".muted").textContent = msg;
  };
}

(async function boot() {
  try {
    state.tenant = await api("/v1/ui/tenant");
    $("#tenant-line").textContent = `${state.tenant.name} · plan ${state.tenant.plan}`;
  } catch (e) { showLogin(); return; }
  let demo = false;
  try { demo = (await api("/v1/ui/summary")).demo === true; } catch (e) {}
  state.demo = demo;
  const resetBtn = $("#reset-demo");
  if (resetBtn) { resetBtn.style.display = demo ? "" : "none"; }
  if (demo) { startDemoTick(); }
  connectStream();
  setView("overview");
})();
