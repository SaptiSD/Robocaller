/* RoboCall AI dashboard - no build step, no dependencies. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// Populated from /api/settings; this is only the fallback if that call fails.
let TIMEZONES = ["America/New_York", "America/Chicago", "America/Denver",
                 "America/Los_Angeles"];

// Outcome slots use the reserved status palette; each is labelled, so hue never
// carries the meaning on its own.
const OUTCOMES = [
  { key: "completed",  label: "Delivered",         color: "var(--good)" },
  { key: "unanswered", label: "No answer / busy",  color: "var(--warning)" },
  { key: "failed",     label: "Failed",            color: "var(--critical)" },
  { key: "waiting",    label: "In queue",          color: "var(--queued)" },
  { key: "skipped",    label: "Screened out",      color: "var(--muted)" },
];

let SETTINGS = {};
let META = { voices: [], amd_modes: [] };
let currentView = "dashboard";
let currentCampaignId = null;
let pollTimer = null;

// --- plumbing ---------------------------------------------------------------

async function api(path, options = {}) {
  const res = await fetch("/api" + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) throw new Error((data && data.error) || res.statusText);
  return data;
}

function toast(message, bad = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = "on" + (bad ? " bad" : "");
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.className = ""), 3600);
}

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// `tz` renders the moment in the campaign's own scheduling timezone rather than
// the viewer's. Without it a card reads "Every Fri at 10:30 (New York) · next run
// 9:30 AM" to anyone sitting in another zone, which looks like a bug.
function fmtWhen(utcString, tz) {
  if (!utcString) return "—";
  const d = new Date(utcString.replace(" ", "T") + "Z");
  if (isNaN(d)) return utcString;
  const mins = Math.round((d - Date.now()) / 60000);
  const opts = { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" };
  if (tz) { opts.timeZone = tz; opts.timeZoneName = "short"; }
  let abs;
  try { abs = d.toLocaleString(undefined, opts); }
  catch { abs = d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }); }
  if (Math.abs(mins) < 1) return "just now";
  if (mins > 0 && mins < 60) return `in ${mins} min · ${abs}`;
  if (mins < 0 && mins > -60) return `${-mins} min ago`;
  return abs;
}

function fmtDay(utcString, tz) {
  if (!utcString) return "—";
  const d = new Date(utcString.replace(" ", "T") + "Z");
  if (isNaN(d)) return utcString;
  const opts = { month: "short", day: "numeric", year: "numeric" };
  if (tz) opts.timeZone = tz;
  try { return d.toLocaleDateString(undefined, opts); }
  catch { return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }); }
}

function fmtTime(utcString) {
  if (!utcString) return "";
  const d = new Date(utcString.replace(" ", "T") + "Z");
  return isNaN(d) ? "" : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

function statusPill(task) {
  const s = task.status || task.state;
  let cls = "muted", label = s || "—";
  if (s === "completed") { cls = "good"; label = "Delivered"; }
  else if (s === "busy") { cls = "warning"; label = "Busy"; }
  else if (s === "no-answer") { cls = "warning"; label = "No answer"; }
  else if (["failed", "canceled", "unknown"].includes(s)) { cls = "critical"; label = s === "unknown" ? "Lost track" : s[0].toUpperCase() + s.slice(1); }
  else if (["queued", "ringing", "initiated", "in-progress"].includes(s)) { cls = "queued"; label = s === "in-progress" ? "Talking" : s[0].toUpperCase() + s.slice(1); }
  else if (task.state === "deferred") { cls = "muted"; label = "Deferred"; }
  else if (task.state === "pending") { cls = "queued"; label = "Queued"; }
  else if (s === "opted-out") { cls = "critical"; label = "Opted out"; }
  const machine = String(task.answered_by || "").startsWith("machine");
  return `<span class="pill ${cls}"><span class="dot"></span>${esc(label)}</span>` +
         (machine ? ` <span class="pill muted">voicemail</span>` : "");
}

// --- routing ----------------------------------------------------------------

function show(view) {
  currentView = view;
  $$(".view").forEach((el) => el.classList.toggle("hidden", el.id !== "view-" + view));
  $$(".nav-item[data-view]").forEach((b) =>
    b.setAttribute("aria-current", b.dataset.view === view ? "page" : "false"));
  window.location.hash = view;

  const loader = { dashboard: loadDashboard, campaigns: loadCampaigns,
                   calls: loadCalls, dnc: loadDnc, docs: loadDocs,
                   settings: loadSettings }[view];
  // An unhandled rejection here would leave the page showing stale data with no
  // hint that anything went wrong.
  if (loader) loader().catch((err) => toast(err.message, true));
}

// --- dashboard --------------------------------------------------------------

function setupBanner(data) {
  const s = data.settings;
  if (!s.telnyx_ready) {
    return `<div class="banner bad"><span>&#9888;</span><span>
      <b>Telnyx isn't connected yet</b>, so no call can go out.
      Add your API key, TeXML application ID and a voice-capable From number on the
      <button class="link" data-view-jump="settings" style="padding:0">Settings page</button>.
    </span></div>`;
  }
  if (!s.business_name) {
    return `<div class="banner warn"><span>&#9888;</span><span>
      No <b>business name</b> is set, so scripts go out without the caller identification
      a pre-recorded marketing call is required to open with.
      <button class="link" data-view-jump="settings" style="padding:0">Add one</button>.
    </span></div>`;
  }
  if (data.engine.paused) {
    return `<div class="banner warn"><span>&#9208;</span><span>
      <b>Calling is paused.</b> Campaigns still queue up; nothing is dialled.</span></div>`;
  }
  return "";
}

function renderTiles(data) {
  const t = data.totals;
  const attempted = t.completed + t.unanswered + t.failed;
  const rate = attempted ? Math.round((t.completed / attempted) * 100) : null;
  const tiles = [
    { label: "Calls in the last 24 hours", value: data.calls_24h, hero: true,
      note: `${t.total} placed all time` },
    { label: "Delivered", value: rate === null ? "—" : rate + "%",
      note: rate === null ? "no calls attempted yet" : `${t.completed} of ${attempted} attempts` },
    { label: "Waiting to dial", value: t.waiting + t.in_flight,
      note: t.in_flight ? `${t.in_flight} on the line now` : "queued" },
    { label: "Active campaigns", value: data.campaigns.active,
      note: `${data.campaigns.total} total` },
    { label: "Do not call", value: data.suppressed, note: "numbers blocked" },
  ];
  $("#tiles").innerHTML = tiles.map((tile) => `
    <div class="tile">
      <div class="tile-label">${esc(tile.label)}</div>
      <div class="tile-value${tile.hero ? " hero" : ""}">${esc(tile.value)}</div>
      <div class="tile-note">${esc(tile.note)}</div>
    </div>`).join("");
}

function renderOutcomes(totals) {
  const values = {
    completed: totals.completed,
    unanswered: totals.unanswered,
    failed: totals.failed,
    waiting: totals.waiting + totals.in_flight,
    skipped: totals.skipped,
  };
  const total = Object.values(values).reduce((a, b) => a + b, 0);
  $("#outcome-total").textContent = total ? `${total} calls` : "";

  if (!total) {
    $("#outcome-chart").innerHTML =
      `<div class="outcome-empty">No calls yet &mdash; run a test call to see this fill in.</div>`;
    return;
  }

  const present = OUTCOMES.filter((o) => values[o.key] > 0);
  const bar = present.map((o) => {
    const pct = (values[o.key] / total) * 100;
    return `<div class="outcome-seg" style="flex:${pct};background:${o.color}"
      data-label="${esc(o.label)}" data-value="${values[o.key]}"
      data-pct="${pct.toFixed(1)}"></div>`;
  }).join("");

  // The legend carries the numbers, so identity never rests on colour alone.
  const legend = OUTCOMES.map((o) => `
    <div class="legend-item">
      <span class="legend-swatch" style="background:${o.color}"></span>
      <span class="legend-label">${esc(o.label)}</span>
      <span class="legend-value">${values[o.key]}</span>
      <span class="legend-pct">${total ? Math.round((values[o.key] / total) * 100) : 0}%</span>
    </div>`).join("");

  $("#outcome-chart").innerHTML =
    `<div class="outcome-bar">${bar}</div><div class="legend">${legend}</div>`;
}

function bindTooltip() {
  const tip = $("#tooltip");
  document.addEventListener("mousemove", (e) => {
    const seg = e.target.closest(".outcome-seg");
    if (!seg) { tip.className = ""; return; }
    tip.textContent = `${seg.dataset.label} — ${seg.dataset.value} calls (${seg.dataset.pct}%)`;
    tip.className = "on";
    tip.style.left = Math.min(e.clientX + 12, window.innerWidth - tip.offsetWidth - 8) + "px";
    tip.style.top = e.clientY - 34 + "px";
  });
}

async function loadDashboard() {
  let data;
  try { data = await api("/overview"); } catch (err) { return toast(err.message, true); }
  SETTINGS = data.settings;

  $("#setup-banner").innerHTML = setupBanner(data);
  renderTiles(data);
  renderOutcomes(data.totals);
  $("#nav-campaigns").textContent = data.campaigns.total || "";
  $("#nav-dnc").textContent = data.suppressed || "";

  const e = data.engine;
  $("#btn-pause").textContent = e.paused ? "Resume calling" : "Pause all calling";
  $("#engine-status").innerHTML = `
    <div style="display:flex;flex-direction:column;gap:6px;font-size:13px">
      <div>${e.running
        ? `<span class="pill good"><span class="dot"></span>Running</span>`
        : `<span class="pill critical"><span class="dot"></span>Stopped</span>`}
        <span class="hint" style="display:inline">last tick ${esc(fmtWhen(e.last_tick))}</span></div>
      <div class="hint">Mode: <b>${esc(data.settings.delivery_mode)}</b> &middot;
        ${data.settings.delivery_mode === "webhook"
          ? "Telnyx fetches scripts from this server; opt-out keypresses work."
          : "Scripts ride along with each call; nothing needs to be internet-reachable."}</div>
      <div class="hint">Calling window ${esc(data.settings.window_start)}&ndash;${esc(data.settings.window_end)}
        local to each recipient, ${esc(data.settings.calls_per_minute)} calls/minute.</div>
      ${e.last_error ? `<div class="hint" style="color:var(--critical)">${esc(e.last_error)}</div>` : ""}
    </div>`;

  $("#upcoming").innerHTML = data.upcoming.length
    ? `<div class="feed">` + data.upcoming.map((c) => `
        <div class="feed-row">
          <span class="feed-time">${esc(fmtWhen(c.next_run_at, c.timezone))}</span>
          <span class="feed-msg">${esc(c.name)} <span class="hint" style="display:inline">(${esc(c.frequency)})</span></span>
        </div>`).join("") + `</div>`
    : `<div class="hint">Nothing scheduled.</div>`;

  $("#feed").innerHTML = data.events.length
    ? data.events.map((ev) => `
        <div class="feed-row ${ev.level === "error" ? "error" : ""}">
          <span class="feed-time">${esc(fmtTime(ev.ts))}</span>
          <span class="feed-msg">${esc(ev.message)}</span>
        </div>`).join("")
    : `<div class="hint">Nothing yet.</div>`;

  if (!$("#test-phone").value && data.settings.test_number) {
    $("#test-phone").value = data.settings.test_number;
  }
}

// --- campaigns --------------------------------------------------------------

function campaignCard(c) {
  const stateClass = { active: "good", paused: "warning", finished: "muted", draft: "muted" }[c.state] || "muted";
  const done = c.calls_total ? Math.round((c.calls_completed / c.calls_total) * 100) : 0;
  let schedule = c.frequency === "once" ? "One time"
    : c.frequency === "hourly" ? "Every hour"
    : c.frequency === "daily" ? `Every day at ${c.call_time}`
    : `Every ${["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][c.weekday] || "Mon"} at ${c.call_time}`;
  // The window is the difference between a campaign that stops and one that
  // does not, so it belongs on the card rather than behind an Open click.
  if (c.starts_at) schedule += ` · from ${fmtDay(c.starts_at, c.timezone)}`;
  if (c.ends_at) schedule += ` · until ${fmtDay(c.ends_at, c.timezone)}`;
  else if (c.frequency !== "once" && c.state === "active") schedule += " · no end date";
  if (c.mode === "agent") schedule = `AI conversation · ${schedule}`;

  return `<div class="campaign">
    <div class="campaign-top">
      <span class="campaign-name">${esc(c.name)}</span>
      <span class="pill ${stateClass}"><span class="dot"></span>${esc(c.state)}</span>
      <div style="flex:1"></div>
      <div class="btn-row">
        <button class="small" data-open="${c.id}">Open</button>
        <button class="small" data-run="${c.id}">Run now</button>
        <button class="small" data-toggle="${c.id}" data-state="${esc(c.state)}">${c.state === "active" ? "Pause" : "Resume"}</button>
        <button class="small danger" data-delete="${c.id}">Delete</button>
      </div>
    </div>
    <div class="campaign-meta">
      ${schedule} &middot; ${esc(c.timezone)} &middot;
      ${c.contacts} contact${c.contacts === 1 ? "" : "s"}
      (${c.consented} consented) &middot;
      next run ${esc(fmtWhen(c.next_run_at, c.timezone))}
    </div>
    <div class="campaign-msg">${esc(c.message || "No message set.")}</div>
    ${c.calls_total ? `<div class="progress"><div style="width:${done}%"></div></div>
      <div class="campaign-meta">${c.calls_completed} delivered of ${c.calls_total} queued
        ${c.calls_waiting ? `&middot; ${c.calls_waiting} waiting` : ""}</div>` : ""}
  </div>`;
}

async function loadCampaigns() {
  const list = await api("/campaigns");
  $("#nav-campaigns").textContent = list.length || "";
  $("#campaign-list").innerHTML = list.length
    ? list.map(campaignCard).join("")
    : `<div class="card"><div class="empty">No campaigns yet.
        <button class="link" data-view-jump="new">Create the first one</button>.</div></div>`;
}

async function openCampaign(id) {
  const [list, contacts, calls] = await Promise.all([
    api("/campaigns"), api(`/campaigns/${id}/contacts`), api(`/calls?campaign_id=${id}&limit=100`),
  ]);
  const c = list.find((x) => x.id === id);
  if (!c) return show("campaigns");
  currentCampaignId = id;

  $("#detail-name").textContent = c.name;
  $("#detail-sub").textContent =
    `${c.contacts} contacts · ${c.calls_total} calls queued · next run ${fmtWhen(c.next_run_at, c.timezone)}`;

  $("#detail-body").innerHTML = `
    <div class="card">
      <div class="card-head"><h2>Script</h2><div class="spacer"></div>
        <span class="pill ${c.state === "active" ? "good" : "muted"}"><span class="dot"></span>${esc(c.state)}</span>
      </div>
      <div class="campaign-msg">${esc(c.message)}</div>
      <div class="btn-row" style="margin-top:12px">
        <button data-run="${c.id}">Run now</button>
        <button data-toggle="${c.id}" data-state="${esc(c.state)}">${c.state === "active" ? "Pause" : "Resume"}</button>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h2>Contacts</h2><div class="spacer"></div>
        <span class="hint">${c.consented} of ${c.contacts} consented</span></div>
      <div class="field">
        <textarea id="detail-add" class="mono" rows="2" placeholder="Add more numbers, one per line"></textarea>
        <label class="check" style="margin-top:8px">
          <input type="checkbox" id="detail-consent"> <span>These gave prior express written consent</span>
        </label>
      </div>
      <button class="small" id="detail-add-btn">Add contacts</button>
      <div class="table-wrap" style="margin-top:12px">
        <table><thead><tr><th>Number</th><th>Name</th><th>Timezone</th><th>Consent</th><th></th></tr></thead>
        <tbody>${contacts.map((ct) => `
          <tr>
            <td class="phone">${esc(ct.pretty)}${ct.suppressed ? ' <span class="pill critical"><span class="dot"></span>DNC</span>' : ""}</td>
            <td>${esc(ct.name || "—")}</td>
            <td class="hint">${esc(ct.timezone)}</td>
            <td>${ct.consent
              ? '<span class="pill good"><span class="dot"></span>yes</span>'
              : '<span class="pill warning"><span class="dot"></span>no</span>'}</td>
            <td><button class="link small" data-del-contact="${ct.id}" data-campaign="${c.id}">remove</button></td>
          </tr>`).join("") || `<tr><td colspan="5" class="empty">No contacts yet.</td></tr>`}
        </tbody></table>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h2>Calls</h2></div>
      <div class="table-wrap">${callsTable(calls, false)}</div>
    </div>`;

  show("detail");
}

// --- call log ---------------------------------------------------------------

// --- documents --------------------------------------------------------------

function fmtBytes(n) {
  if (!n) return "—";
  return n < 1024 * 1024 ? Math.round(n / 1024) + " KB"
                         : (n / 1024 / 1024).toFixed(1) + " MB";
}

function docsTable(rows) {
  if (!rows.length) {
    return `<div class="empty">No PDFs in the <span class="mono">docs</span> folder yet.
      Build them with <span class="mono">python docs/build_report.py</span>.</div>`;
  }
  return `<table>
    <thead><tr><th>Document</th><th>File</th><th>Size</th><th>Updated</th><th></th></tr></thead>
    <tbody>${rows.map((r) => `
      <tr>
        <td class="name"><b>${esc(r.title)}</b></td>
        <td class="hint" style="font-family:var(--mono);font-size:11px">${esc(r.name)}</td>
        <td class="num hint">${esc(fmtBytes(r.bytes))}</td>
        <td class="hint when">${esc(fmtWhen(r.modified))}</td>
        <td class="row-actions">
          <button data-doc="${esc(r.name)}" data-doc-title="${esc(r.title)}">Read</button>
        </td>
      </tr>`).join("")}
    </tbody></table>`;
}

async function loadDocs() {
  const rows = await api("/documents");
  $("#docs-table").innerHTML = docsTable(rows);
  $("#nav-docs").textContent = rows.length || "";
}

function openDoc(name, title) {
  // Cache-bust, so rebuilding a PDF and pressing Read again shows the new one
  // rather than whatever the browser's PDF viewer is still holding.
  $("#doc-frame").src = `/docs/${encodeURIComponent(name)}?t=${Date.now()}`;
  $("#doc-newtab").href = `/docs/${encodeURIComponent(name)}`;
  $("#doc-title").textContent = title || name;
  $("#doc-sub").textContent = name;
  $("#doc-viewer").classList.remove("hidden");
  $("#doc-viewer").scrollIntoView({ behavior: "smooth", block: "start" });
}

function closeDoc() {
  $("#doc-viewer").classList.add("hidden");
  $("#doc-frame").src = "about:blank";  // stop the PDF plugin rendering offscreen
}

function callsTable(rows, withCampaign = true) {
  if (!rows.length) return `<div class="empty">No calls yet.</div>`;
  return `<table>
    <thead><tr>
      <th>When</th>${withCampaign ? "<th>Campaign</th>" : ""}<th>Number</th>
      <th>Status</th><th>Length</th><th>Telnyx SID</th><th>Detail</th>
    </tr></thead>
    <tbody>${rows.map((r) => {
      // A missing name means either a test call (never had a campaign) or a
      // campaign deleted since - the id is what tells the two apart.
      const campaign = r.campaign_name || (r.campaign_id ? "Deleted campaign" : "Test call");
      return `
      <tr>
        <td class="hint when">${esc(fmtWhen(r.updated_at))}</td>
        ${withCampaign ? `<td class="name" title="${esc(campaign)}">${esc(campaign)}</td>` : ""}
        <td class="phone">${esc(r.pretty)}</td>
        <td>${statusPill(r)}</td>
        <td class="num">${r.duration ? r.duration + "s" : "—"}</td>
        <td class="hint" style="font-family:var(--mono);font-size:11px">${esc(r.sid || "—")}</td>
        <td class="hint">${esc(r.error || (r.state === "deferred" ? r.status : "") || "")}</td>
      </tr>`;
    }).join("")}
    </tbody></table>`;
}

async function loadCalls() {
  $("#calls-table").innerHTML = callsTable(await api("/calls?limit=300"));
}

// --- do not call ------------------------------------------------------------

async function loadDnc() {
  const rows = await api("/suppression");
  $("#nav-dnc").textContent = rows.length || "";
  $("#dnc-table").innerHTML = rows.length
    ? `<table><thead><tr><th>Number</th><th>Reason</th><th>Added</th><th></th></tr></thead>
       <tbody>${rows.map((r) => `
        <tr><td class="phone">${esc(r.pretty)}</td><td>${esc(r.reason)}</td>
        <td class="hint">${esc(fmtWhen(r.created_at))}</td>
        <td><button class="link small" data-unsuppress="${esc(r.phone)}">remove</button></td></tr>`).join("")}
       </tbody></table>`
    : `<div class="empty">The list is empty.</div>`;
}

// --- settings ---------------------------------------------------------------

async function loadSettings() {
  const data = await api("/settings");
  SETTINGS = data.settings;
  META = data;
  if (data.timezones && data.timezones.length) TIMEZONES = data.timezones;
  fillFormOptions();

  $("#s-api-key").placeholder = data.settings.telnyx_api_key_set
    ? "saved — leave blank to keep it" : "paste your Telnyx API key";
  $("#s-app-id").value = data.settings.telnyx_app_id || "";
  $("#s-from").value = data.settings.telnyx_from_number || "";
  $("#s-test").value = data.settings.test_number || "";
  $("#s-business").value = data.settings.business_name || "";
  $("#s-callback").value = data.settings.callback_number || "";
  $("#s-window-start").value = data.settings.window_start || "09:00";
  $("#s-window-end").value = data.settings.window_end || "20:00";
  $("#s-cpm").value = data.settings.calls_per_minute || "12";
  $("#s-ring").value = data.settings.ring_seconds || "30";
  $("#s-base-url").value = data.settings.public_base_url || "";
  $("#s-anthropic").placeholder = data.settings.anthropic_api_key_set
    ? "saved — leave blank to keep it" : "sk-ant-...";
  $("#s-perplexity").placeholder = data.settings.perplexity_api_key_set
    ? "saved — leave blank to keep it" : "pplx-...";
  $("#s-script-provider").value = data.settings.script_provider || "";
  renderAgent(data.agent);
  const store = data.database;
  $("#db-path").innerHTML = store.backend === "postgres"
    ? `Postgres &mdash; <code>${esc(store.host)}/${esc(store.dbname)}</code>,
       schema <code>${esc(store.schema)}</code><br>A hosted database: campaigns, contacts and
       call history survive restarts and redeploys.`
    : `SQLite file &mdash; <code>${esc(store.path)}</code><br>Deliberately outside this project
       folder &mdash; Dropbox overwrites a live SQLite file and destroys it. On a host like Render
       this file is wiped on every redeploy; set <code>DATABASE_URL</code> to use Postgres instead.`;

  $("#mode-status").innerHTML = data.settings.delivery_mode === "webhook"
    ? `<div class="banner ok"><span>&#10003;</span><span><b>Webhook mode.</b>
        Opt-out keypresses, machine-detection hang-ups and live status callbacks are active.</span></div>`
    : `<div class="banner"><span>&#9432;</span><span><b>Direct mode.</b>
        Everything works without exposing this machine. Recipients can only opt out by calling
        the number in the script.</span></div>`;
  fillVoices();
}

function fillVoices() {
  const select = $("#c-voice");
  if (!META.voices || select.options.length) return;
  select.innerHTML = META.voices
    .map((v) => `<option value="${esc(v.id)}">${esc(v.label)}</option>`).join("");
}

async function saveSettings() {
  const values = {
    telnyx_api_key: $("#s-api-key").value.trim(),
    telnyx_app_id: $("#s-app-id").value.trim(),
    telnyx_from_number: $("#s-from").value.trim(),
    test_number: $("#s-test").value.trim(),
    business_name: $("#s-business").value.trim(),
    callback_number: $("#s-callback").value.trim(),
    window_start: $("#s-window-start").value,
    window_end: $("#s-window-end").value,
    calls_per_minute: $("#s-cpm").value,
    ring_seconds: $("#s-ring").value,
    public_base_url: $("#s-base-url").value.trim(),
    anthropic_api_key: $("#s-anthropic").value.trim(),
    perplexity_api_key: $("#s-perplexity").value.trim(),
    script_provider: $("#s-script-provider").value,
  };
  try {
    await api("/settings", { method: "POST", body: { values } });
    $("#s-api-key").value = "";
    $("#s-anthropic").value = "";
    $("#s-perplexity").value = "";
    toast("Settings saved.");
    loadSettings();
  } catch (err) { toast(err.message, true); }
}

// --- new campaign -----------------------------------------------------------

function fillFormOptions() {
  const tzSelect = $("#c-timezone");
  const chosen = tzSelect.value;
  tzSelect.innerHTML = TIMEZONES
    .map((tz) => `<option value="${esc(tz)}">${esc(tz)}</option>`).join("");
  tzSelect.value = TIMEZONES.includes(chosen) ? chosen : "America/New_York";
  fillVoices();
}

let previewTimer = null;
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(async () => {
    const message = $("#c-message").value;
    if (!message.trim()) {
      $("#script-preview").textContent = "—";
      $("#script-meter").textContent = "";
      return;
    }
    try {
      const p = await api("/script/preview", { method: "POST", body: { message } });
      $("#script-preview").textContent = p.script;
      $("#script-meter").innerHTML =
        `About <b>${p.seconds} seconds</b> spoken · ${p.characters} characters` +
        (p.problems.length
          ? ` <span style="color:var(--critical)">· ${esc(p.problems.join(" "))}</span>` : "");
    } catch (err) { /* preview is best-effort */ }
  }, 400);
}

// Mirror of compliance.normalize, so the meter can tell you what the server will
// accept before you submit a list of ten thousand rows.
function normalizePhone(raw) {
  const text = (raw || "").trim();
  if (!text) return "";
  const digits = text.replace(/\D/g, "");
  if (!digits) return "";
  if (text.startsWith("+")) return digits.length >= 8 && digits.length <= 15 ? "+" + digits : "";
  if (digits.length === 10) return "+1" + digits;
  if (digits.length === 11 && digits.startsWith("1")) return "+" + digits;
  return "";
}

function countContacts() {
  const meter = $("#contacts-meter");
  const seen = new Set();
  let usable = 0, dupes = 0, bad = 0;

  for (let line of $("#c-contacts").value.split("\n")) {
    line = line.trim();
    if (!line || /^(phone|number|#)/i.test(line)) continue;
    // Same rule as the server: first field that parses as a number wins.
    const phone = line.split(",").map(normalizePhone).find(Boolean) || "";
    if (!phone) { bad++; continue; }
    if (seen.has(phone)) { dupes++; continue; }
    seen.add(phone);
    usable++;
  }

  if (!usable && !dupes && !bad) return (meter.textContent = "");
  const parts = [`<b>${usable}</b> number${usable === 1 ? "" : "s"} to call`];
  if (dupes) parts.push(`${dupes} duplicate${dupes === 1 ? "" : "s"} ignored`);
  if (bad) parts.push(`<span style="color:var(--critical)">${bad} line${bad === 1 ? "" : "s"} not a phone number</span>`);
  meter.innerHTML = parts.join(" · ");
}

// Read one or more CSV/text files in the browser and append their rows to the
// box. The server already parses "number", "number,name" and "name,number", so
// the file never has to leave the page as anything but pasted text.
async function importContactFiles(files) {
  const list = Array.from(files || []);
  if (!list.length) return;

  let text = "";
  for (const file of list) {
    try {
      text += (await file.text()).replace(/\r\n?/g, "\n").trim() + "\n";
    } catch (err) {
      return toast(`Could not read ${file.name}.`, true);
    }
  }

  const box = $("#c-contacts");
  const before = box.value.split("\n").filter((l) => l.trim()).length;
  box.value = (box.value.trim() ? box.value.trim() + "\n" : "") + text.trim();
  const added = box.value.split("\n").filter((l) => l.trim()).length - before;
  countContacts();
  toast(`Loaded ${added} row(s) from ${list.length} file${list.length === 1 ? "" : "s"}.`);
}

async function draftScript() {
  const brief = $("#c-brief").value.trim();
  const existing = $("#c-message").value.trim();
  if (!brief && !existing) return toast("Describe the call first.", true);

  const btn = $("#btn-draft");
  btn.disabled = true;
  const writer = SETTINGS.script_provider_active === "perplexity" ? "Perplexity" : "Claude";
  $("#draft-status").innerHTML = `<span class="spinner"></span> ${writer} is writing…`;
  try {
    const result = await api("/script/draft", {
      method: "POST",
      body: { brief, existing, business_name: SETTINGS.business_name || "", seconds: 25 },
    });
    $("#c-message").value = result.message;
    $("#draft-status").textContent = `Drafted · about ${result.seconds} seconds. Edit freely.`;
    schedulePreview();
  } catch (err) {
    $("#draft-status").innerHTML = `<span style="color:var(--critical)">${esc(err.message)}</span>`;
  } finally { btn.disabled = false; }
}

async function submitCampaign(event) {
  event.preventDefault();
  const body = {
    name: $("#c-name").value.trim(),
    message: $("#c-message").value.trim(),
    voice: $("#c-voice").value,
    mode: $("#c-agent").checked ? "agent" : "message",
    agent_info: $("#c-agent").checked ? $("#c-agent-info").value.trim() : "",
    amd: $("#c-amd").value,
    frequency: $("#c-frequency").value,
    call_time: $("#c-time").value || "10:00",
    weekday: Number($("#c-weekday").value || 0),
    timezone: $("#c-timezone").value,
    require_consent: $("#c-require-consent").checked,
    starts_at: $("#c-starts-at").value,
    ends_at: $("#c-ends-at").value,
    start_now: $("#c-start-now").checked,
    contacts: $("#c-contacts").value,
    contacts_consented: $("#c-consent-have").checked,
  };
  if (!body.message) return toast("The campaign needs a message.", true);

  try {
    const result = await api("/campaigns", { method: "POST", body });
    let note = `Campaign created with ${result.contacts_added} contact(s).`;
    if (result.invalid.length) note += ` ${result.invalid.length} line(s) weren't valid numbers.`;
    toast(note);
    $("#campaign-form").reset();
    refreshModeHint();
    $("#c-agent-info").dispatchEvent(new Event("input"));
    $("#script-preview").textContent = "—";
    $("#c-require-consent").checked = true;
    refreshWindowWarning();
    fillFormOptions();
    show("campaigns");
  } catch (err) { toast(err.message, true); }
}

// --- actions ----------------------------------------------------------------

async function handleClick(event) {
  const jump = event.target.closest("[data-view-jump]");
  if (jump) return show(jump.dataset.viewJump);

  const nav = event.target.closest(".nav-item[data-view]");
  if (nav) return show(nav.dataset.view);

  const doc = event.target.closest("[data-doc]");
  if (doc) return openDoc(doc.dataset.doc, doc.dataset.docTitle);

  const open = event.target.closest("[data-open]");
  if (open) return openCampaign(Number(open.dataset.open));

  const run = event.target.closest("[data-run]");
  if (run) {
    try {
      const r = await api(`/campaigns/${run.dataset.run}/run`, { method: "POST" });
      toast(`Queued ${r.queued} call(s)` + (r.skipped ? `, skipped ${r.skipped}.` : "."));
      currentView === "detail" ? openCampaign(Number(run.dataset.run)) : loadCampaigns();
    } catch (err) { toast(err.message, true); }
    return;
  }

  const toggle = event.target.closest("[data-toggle]");
  if (toggle) {
    const id = Number(toggle.dataset.toggle);
    await api(`/campaigns/${id}`, {
      method: "PATCH",
      body: { state: toggle.dataset.state === "active" ? "paused" : "active" },
    });
    currentView === "detail" ? openCampaign(id) : loadCampaigns();
    return;
  }

  const del = event.target.closest("[data-delete]");
  if (del) {
    if (!confirm("Delete this campaign and its recipient list? Queued calls are cancelled.")) return;
    await api(`/campaigns/${del.dataset.delete}`, { method: "DELETE" });
    toast("Campaign deleted.");
    loadCampaigns();
    return;
  }

  const delContact = event.target.closest("[data-del-contact]");
  if (delContact) {
    await api(`/campaigns/${delContact.dataset.campaign}/contacts/${delContact.dataset.delContact}`,
      { method: "DELETE" });
    openCampaign(Number(delContact.dataset.campaign));
    return;
  }

  const unsuppress = event.target.closest("[data-unsuppress]");
  if (unsuppress) {
    await api(`/suppression/${encodeURIComponent(unsuppress.dataset.unsuppress)}`, { method: "DELETE" });
    loadDnc();
    return;
  }

  if (event.target.closest("#detail-add-btn")) {
    const id = currentCampaignId;
    const r = await api(`/campaigns/${id}/contacts`, {
      method: "POST",
      body: { raw: $("#detail-add").value, consented: $("#detail-consent").checked },
    });
    toast(`Added ${r.added} contact(s).`);
    openCampaign(id);
  }
}

function renderAgent(info) {
  if (!info) return;
  const btn = $("#btn-agent-setup");
  btn.textContent = info.assistant_id ? "Update in Telnyx" : "Set up in Telnyx";
  btn.disabled = !info.can_setup;
  $("#agent-status").innerHTML = info.ready
    ? `<span style="color:var(--good)">&#10003; Ready.</span> Model <code>${esc(info.model)}</code>,
       voice <code>${esc(info.voice)}</code>, Telnyx assistant <code>${esc(info.assistant_id)}</code>.`
    : info.missing.length
      ? `Not ready yet. Still needed: ${info.missing.map(esc).join("; ")}.`
      : "Everything it needs is in place &mdash; press the button to create the agent in Telnyx.";
  refreshModeHint();
}

async function setupAgent() {
  const btn = $("#btn-agent-setup");
  btn.disabled = true;
  $("#agent-setup-status").innerHTML = `<span class="spinner"></span> setting up in Telnyx…`;
  try {
    META.agent = await api("/agent/setup", { method: "POST" });
    renderAgent(META.agent);
    $("#agent-setup-status").textContent = "Done.";
  } catch (err) {
    $("#agent-setup-status").innerHTML = `<span style="color:var(--critical)">${esc(err.message)}</span>`;
    btn.disabled = false;
  }
}

// An AI call has talking points rather than a script, and its own voice, so the
// script preview and voice picker only apply to recorded messages.
function refreshModeHint() {
  const agentMode = $("#c-agent").checked;
  $("#wrap-agent-info").hidden = !agentMode;
  $("#c-message-label").textContent = agentMode
    ? "Talking points for the AI agent" : "Message the voice will read";
  $("#c-message").placeholder = agentMode
    ? "Why you're calling - what the agent should tell each person. Put supporting facts in More info above."
    : "Write for the ear. Short sentences. Spell out numbers and dates.";
  $("#wrap-preview").style.display = agentMode ? "none" : "";
  $("#wrap-voice").style.display = agentMode ? "none" : "";
  const ready = META.agent && META.agent.ready;
  $("#c-mode-hint").innerHTML = !agentMode ? ""
    : ready
      ? "The agent says up front that it's an automated AI assistant, answers only from your talking points, and honours opt-outs. Each minute costs more than a recorded message."
      : `<span style="color:var(--critical)">The AI phone agent isn't set up yet &mdash; finish Settings &rsaquo; AI phone agent first.</span>`;
}

async function testCall() {
  const btn = $("#btn-test-call");
  btn.disabled = true;
  $("#test-status").innerHTML = `<span class="spinner"></span> dialling…`;
  try {
    const r = await api("/test-call", {
      method: "POST",
      body: { phone: $("#test-phone").value, message: $("#test-message").value,
              voice: "Polly.Joanna-Neural", mode: $("#test-mode").value },
    });
    const task = r.task || {};
    $("#test-status").innerHTML = task.sid
      ? `Call placed — your phone should ring. <span class="hint">SID ${esc(task.sid)}</span>`
      : task.error
        ? `<span style="color:var(--critical)">${esc(task.error)}</span>`
        : `Queued — ${esc(task.status || "waiting for the dispatcher")}.`;
    loadDashboard();
  } catch (err) {
    $("#test-status").innerHTML = `<span style="color:var(--critical)">${esc(err.message)}</span>`;
  } finally { btn.disabled = false; }
}

async function verifyTelnyx() {
  const btn = $("#btn-verify");
  btn.disabled = true;
  $("#verify-status").innerHTML = `<span class="spinner"></span> checking…`;
  try {
    const r = await api("/settings/verify", { method: "POST" });
    const fromNote = r.ok && r.from_detail
      ? `<div style="color:${r.from_ok ? "var(--ink-2)" : "var(--critical)"};margin-top:4px">
           ${r.from_ok ? "✓" : "✗"} ${esc(r.from_detail)}</div>`
      : "";
    $("#verify-status").innerHTML = (r.ok
      ? `<span style="color:var(--good)">✓ ${esc(r.detail)}</span>`
      : `<span style="color:var(--critical)">${esc(r.detail)}</span>`) + fromNote;
    const voiceNumbers = (r.numbers || []).filter((n) => n.voice);
    $("#from-options").innerHTML = voiceNumbers.length
      ? "Voice-capable numbers on this account: " +
        voiceNumbers.map((n) =>
          `<button type="button" class="link small" data-pick="${esc(n.phone_number)}">${esc(n.phone_number)}</button>`).join(" ")
      : r.ok ? "No voice-capable numbers on this account yet - buy one in the Telnyx portal." : "";
  } catch (err) {
    $("#verify-status").innerHTML = `<span style="color:var(--critical)">${esc(err.message)}</span>`;
  } finally { btn.disabled = false; }
}

// --- boot -------------------------------------------------------------------

function applyTheme() {
  const saved = localStorage.getItem("robocall-theme");
  if (saved) document.documentElement.dataset.theme = saved;
}

document.addEventListener("click", handleClick);
document.addEventListener("click", (e) => {
  const pick = e.target.closest("[data-pick]");
  if (pick) { $("#s-from").value = pick.dataset.pick; toast("From number set - save to keep it."); }
});

$("#theme-toggle").addEventListener("click", () => {
  const now = document.documentElement.dataset.theme;
  const next = now === "dark" ? "light" : now === "light" ? "" : "dark";
  if (next) { document.documentElement.dataset.theme = next; localStorage.setItem("robocall-theme", next); }
  else { delete document.documentElement.dataset.theme; localStorage.removeItem("robocall-theme"); }
});

$("#btn-test-call").addEventListener("click", testCall);
$("#btn-verify").addEventListener("click", verifyTelnyx);
$("#btn-save-settings").addEventListener("click", saveSettings);
$("#btn-draft").addEventListener("click", draftScript);
$("#btn-agent-setup").addEventListener("click", setupAgent);
$("#c-agent").addEventListener("change", refreshModeHint);
$("#c-agent-info").addEventListener("input", () => {
  const n = $("#c-agent-info").value.length;
  $("#c-agent-info-meter").textContent = n
    ? `${n.toLocaleString()} / 4,000 characters. Sent with every reply, so shorter is faster and cheaper.`
    : "The AI only states what's here or in the talking points. Anything else, it hands off to your callback number.";
});
$("#btn-refresh-calls").addEventListener("click", loadCalls);
$("#btn-refresh-docs").addEventListener("click", () => {
  loadDocs().catch((err) => toast(err.message, true));
});
$("#btn-close-doc").addEventListener("click", closeDoc);
$("#campaign-form").addEventListener("submit", submitCampaign);
$("#c-message").addEventListener("input", schedulePreview);
$("#c-contacts").addEventListener("input", countContacts);
$("#btn-import-csv").addEventListener("click", () => $("#c-contacts-file").click());
$("#c-contacts-file").addEventListener("change", (e) => {
  importContactFiles(e.target.files);
  e.target.value = "";  // so picking the same file twice fires again
});
$("#btn-clear-contacts").addEventListener("click", () => {
  $("#c-contacts").value = "";
  countContacts();
});
$("#c-contacts").addEventListener("dragover", (e) => e.preventDefault());
$("#c-contacts").addEventListener("drop", (e) => {
  if (!e.dataTransfer?.files?.length) return;
  e.preventDefault();
  importContactFiles(e.dataTransfer.files);
});
// A campaign with no end date keeps dialling and keeps billing, so say so
// rather than letting someone discover it from an invoice. Only recurring
// frequencies can run away; a one-off is bounded by definition.
function refreshWindowWarning() {
  const freq = $("#c-frequency").value;
  const recurring = freq !== "once";
  const open_ended = recurring && !$("#c-ends-at").value;
  $("#window-warning").classList.toggle("hidden", !open_ended);
  $("#window-warning-freq").textContent =
    { hourly: "hour", daily: "day", weekly: "week" }[freq] || "run";
}

$("#c-frequency").addEventListener("change", (e) => {
  $("#wrap-weekday").classList.toggle("hidden", e.target.value !== "weekly");
  $("#wrap-time").classList.toggle("hidden", e.target.value === "hourly");
  refreshWindowWarning();
});
$("#c-ends-at").addEventListener("input", refreshWindowWarning);

$("#btn-pause").addEventListener("click", async () => {
  const paused = $("#btn-pause").textContent.startsWith("Pause");
  await api("/settings", { method: "POST", body: { values: { dispatch_paused: paused ? "1" : "0" } } });
  toast(paused ? "Calling paused." : "Calling resumed.");
  loadDashboard();
});

$("#btn-add-dnc").addEventListener("click", async () => {
  const r = await api("/suppression", { method: "POST", body: { raw: $("#dnc-input").value } });
  $("#dnc-input").value = "";
  toast(`Added ${r.added} number(s).`);
  loadDnc();
});

applyTheme();
bindTooltip();
fillFormOptions();
loadSettings().then(() => show(window.location.hash.slice(1) || "dashboard"));

// show() writes the hash, so a view can be linked to and the back button works.
// Guarding on currentView stops show()'s own write from bouncing back through here.
window.addEventListener("hashchange", () => {
  const view = window.location.hash.slice(1) || "dashboard";
  if (view !== currentView) show(view);
});
pollTimer = setInterval(() => {
  if (currentView === "dashboard") loadDashboard();
  if (currentView === "calls") loadCalls();
}, 5000);
