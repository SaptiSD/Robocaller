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

// What the engine's internal status codes mean, in words.
const STATUS_LABELS = {
  "no-consent": "No consent on record", "suppressed": "On do-not-call list",
  "campaign deleted": "Campaign deleted", "contact removed": "Contact removed",
  "expired": "Test call dropped", "unknown local time": "Unknown local time",
  "claimed": "Dialing", "machine-skipped": "Voicemail - hung up", "opted-out": "Opted out",
  "blocked-us": "Blocked 2 of your numbers",
};

// The SIP codes a call ends on when the recipient's phone or carrier turned it
// away. Rolling calls switch number for that person next time.
const BLOCK_LABELS = {
  "603": "Declined or blocked", "607": "Blocked - marked unwanted", "608": "Blocked by their carrier",
};

let SETTINGS = {};
let META = { voices: [], amd_modes: [] };
let CAMPAIGNS = new Map();     // id -> the last campaign row loaded, for confirmations
let currentView = "dashboard";
let currentCampaignId = null;
let editingId = null;          // the campaign the form is editing, or null for a new one
let settingsDirty = false;
let callsLimit = 100;

// --- plumbing ---------------------------------------------------------------

async function api(path, options = {}) {
  const res = await fetch("/api" + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; }
  catch {
    // A host waking from sleep or a proxy error answers with HTML. Say that,
    // not "Unexpected token <".
    throw new Error(res.ok ? "The server sent something unexpected - reload the page."
      : `The server isn't answering properly (HTTP ${res.status}). If it was asleep, give it 30 seconds and try again.`);
  }
  if (!res.ok) throw new Error((data && data.error) || res.statusText);
  return data;
}

// Run an action once at a time per button: a double-click must never create two
// campaigns or queue a list twice. Errors become a toast instead of vanishing.
async function busy(button, fn) {
  if (button && button.dataset.busy) return;
  if (button) { button.dataset.busy = "1"; button.disabled = true; }
  try { return await fn(); }
  catch (err) { toast(err.message, true); }
  finally { if (button) { delete button.dataset.busy; button.disabled = false; } }
}

// "Queued 0, skipped 1" left people guessing - say why, and what to do about it.
function runSummary(r) {
  const why = { "no-consent": "no consent on record", "suppressed": "on the do-not-call list",
                "already-queued": "already waiting to be called" };
  const reasons = Object.entries(r.skip_reasons || {}).map(([k, n]) => `${n} ${why[k] || k}`).join(", ");
  if (r.queued === 0 && r.skipped) {
    let msg = `Nothing was dialled: ${reasons || r.skipped + " skipped"}.`;
    if ((r.skip_reasons || {})["no-consent"]) {
      msg += " If they really gave written consent, mark them as consented under Contacts, then press Run now again.";
    }
    return msg;
  }
  return `Queued ${r.queued} call(s)` + (r.skipped ? `, skipped ${r.skipped} (${reasons}).` : ".")
    + (SETTINGS.dispatch_paused === "1" ? " Calling is paused, so they'll wait until you resume it." : "");
}

function toast(message, bad = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = "on" + (bad ? " bad" : "");
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.className = ""), Math.max(3600, message.length * 60));  // long enough to read
}

// Pages refresh every few seconds. Rewriting a section that hasn't changed would
// swallow a click that lands mid-refresh and throw keyboard focus back to the
// top, so a section is only replaced when its content actually differs.
function setHtml(el, html) {
  if (!el || el._html === html) return;
  el._html = html;
  el.innerHTML = html;
}

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const notNumbers = (n) => n === 1 ? "1 line wasn't a phone number" : `${n} lines weren't phone numbers`;
const plural = (n, word, many = word + "s") => `${n} ${n === 1 ? word : many}`;

// `tz` renders the moment in the campaign's own scheduling timezone rather than
// the viewer's. Without it a card reads "Every Fri at 10:30 (New York) · next run
// 9:30 AM" to anyone sitting in another zone, which looks like a bug.
function fmtWhen(utcString, tz) {
  if (!utcString) return "—";
  const d = new Date(utcString.replace(" ", "T") + "Z");
  if (isNaN(d)) return utcString;
  const mins = Math.round((d - Date.now()) / 60000);
  const opts = { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" };
  // Always name the zone: a tester abroad read a call placed at 11:51 CT as
  // "12:51 AM" - their own clock, with nothing to say so.
  opts.timeZoneName = "short";
  if (tz) opts.timeZone = tz;
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
  return isNaN(d) ? "" : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", timeZoneName: "short" });
}

// A stored UTC moment as the "YYYY-MM-DDTHH:MM" a datetime-local input wants,
// in the campaign's own timezone - the zone the form reads it back in.
function toLocalInput(utcString, tz) {
  if (!utcString) return "";
  const d = new Date(utcString.replace(" ", "T") + "Z");
  if (isNaN(d)) return "";
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
    timeZone: tz || "UTC", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(d).map((p) => [p.type, p.value]));
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

function statusPill(task) {
  const s = task.status || task.state;
  let cls = "muted", label = STATUS_LABELS[s] || s || "—";
  const waiting = ["pending", "deferred"].includes(task.state);
  if (task.state === "skipped") { cls = s === "blocked-us" ? "critical" : "muted"; }
  else if (task.state === "done" && BLOCK_LABELS[task.hangup_cause]) { cls = "critical"; label = BLOCK_LABELS[task.hangup_cause]; }
  else if (s === "opted-out") { cls = "warning"; label = "Opted out"; }
  else if (s === "machine-skipped") { cls = "muted"; }
  else if (s === "completed" && task.played === 0) { cls = "critical"; label = "Answered, message not played"; }
  else if (s === "completed") { cls = "good"; label = "Delivered"; }
  else if (s === "busy") { cls = "warning"; label = "Busy"; }
  else if (s === "no-answer") { cls = "warning"; label = "No answer"; }
  else if (["failed", "canceled", "unknown"].includes(s)) { cls = "critical"; label = s === "unknown" ? "Lost track" : s[0].toUpperCase() + s.slice(1); }
  else if (waiting && task.campaign_state === "paused" && !task.is_test) { cls = "muted"; label = "On hold - campaign paused"; }
  else if (["queued", "ringing", "initiated", "in-progress", "claimed"].includes(s)) { cls = "queued"; label = s === "in-progress" ? "Talking" : STATUS_LABELS[s] || s[0].toUpperCase() + s.slice(1); }
  else if (task.state === "deferred") { cls = "muted"; label = "Deferred"; }
  else if (task.state === "pending") { cls = "queued"; label = "Queued"; }
  const machine = String(task.answered_by || "").startsWith("machine") && s !== "machine-skipped";
  return `<span class="pill ${cls}"><span class="dot"></span>${esc(label)}</span>` +
         (machine ? ` <span class="pill muted">voicemail</span>` : "") +
         (task.is_test ? ` <span class="pill muted">test</span>` : "");
}

// --- routing ----------------------------------------------------------------
//
// The hash carries the view and, for a campaign, its id - "#campaign/7" - so a
// reload or a shared link lands on the same page instead of an empty shell.

let currentRouteId = null;

function parseHash() {
  const [view, id] = (window.location.hash.slice(1) || "dashboard").split("/");
  return { view, id: id ? Number(id) : null };
}

const routeHash = (view, id) => (id ? `${view}/${id}` : view);

function leaveSettingsOk() {
  if (currentView !== "settings" || !settingsDirty) return true;
  if (!confirm("You have unsaved changes on the Settings page. Leave without saving them?")) return false;
  settingsDirty = false;
  return true;
}

function show(view, id = null) {
  if (view !== currentView && !leaveSettingsOk()) {
    // Put the address bar back without adding a history entry.
    history.replaceState(null, "", "#" + routeHash(currentView, currentRouteId));
    return;
  }
  // "#detail" was the old campaign page address, which never carried an id.
  if (view === "detail" || (["campaign", "edit"].includes(view) && !id)) return show("campaigns");
  const target = { campaign: "detail", edit: "new" }[view] || view;
  if (!$("#view-" + target)) return show("dashboard");
  currentView = view;
  currentRouteId = id;
  $$(".view").forEach((el) => el.classList.toggle("hidden", el.id !== "view-" + target));
  const navView = { campaign: "campaigns", edit: "campaigns" }[view] || view;
  $$(".nav-item[data-view]").forEach((b) =>
    b.setAttribute("aria-current", b.dataset.view === navView ? "page" : "false"));
  // A real history entry, so Back works; the hashchange it fires is ignored below.
  if (window.location.hash.slice(1) !== routeHash(view, id)) window.location.hash = routeHash(view, id);

  const loader = {
    dashboard: loadDashboard, campaigns: loadCampaigns, dnc: loadDnc,
    docs: loadDocs, settings: loadSettings,
    calls: () => loadCallCampaigns().then(loadCalls),
    campaign: () => openCampaign(id), edit: () => openEditor(id), new: () => openEditor(null),
  }[view];
  // An unhandled rejection here would leave the page showing stale data with no
  // hint that anything went wrong.
  if (loader) Promise.resolve().then(loader).catch((err) => toast(err.message, true));
  window.scrollTo(0, 0);
}

// --- dashboard --------------------------------------------------------------

function setupBanners(data) {
  const s = data.settings;
  const out = [];
  // Every problem gets its own banner - one hiding another is how "calling is
  // paused" went unnoticed behind a missing business name.
  if (data.engine.paused) {
    out.push(`<div class="banner warn"><span>&#9208;</span><span>
      <b>Calling is paused.</b> Nothing is dialled and no scheduled runs are queued until you press
      Resume calling. A run that comes due meanwhile goes out when you resume if it's only a little
      late; one that's hours late is skipped, and Activity says so.</span></div>`);
  }
  if (!s.telnyx_ready) {
    out.push(`<div class="banner bad"><span>&#9888;</span><span>
      <b>Telnyx isn't connected yet</b>, so no call can go out.
      Add your API key, TeXML application ID and a voice-capable From number on the
      <button class="link" data-view-jump="settings" style="padding:0">Settings page</button>.
    </span></div>`);
  }
  if (!s.business_name) {
    out.push(`<div class="banner warn"><span>&#9888;</span><span>
      No <b>business name</b> is set, so a campaign without its own "Calling on behalf of" goes out
      without the caller identification a pre-recorded marketing call is required to open with.
      <button class="link" data-view-jump="settings" style="padding:0">Add one</button>.
    </span></div>`);
  }
  if (data.held) {
    out.push(`<div class="banner"><span>&#9432;</span><span>
      ${plural(data.held, "call")} ${data.held === 1 ? "is" : "are"} on hold because ${data.held === 1 ? "its campaign is" : "their campaigns are"} paused.
      ${data.held === 1 ? "It goes" : "They go"} out when the campaign is resumed.</span></div>`);
  }
  return out.join("");
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
      note: t.in_flight ? `${t.in_flight} on the line now` : data.held ? `${data.held} on hold` : "queued" },
    { label: "Active campaigns", value: data.campaigns.active,
      note: `${data.campaigns.total} total` },
    { label: "Do not call", value: data.suppressed, note: "numbers blocked" },
  ];
  setHtml($("#tiles"), tiles.map((tile) => `
    <div class="tile">
      <div class="tile-label">${esc(tile.label)}</div>
      <div class="tile-value${tile.hero ? " hero" : ""}">${esc(tile.value)}</div>
      <div class="tile-note">${esc(tile.note)}</div>
    </div>`).join(""));
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
    setHtml($("#outcome-chart"), `<div class="outcome-empty">No calls yet &mdash; run a test call to see this fill in.</div>`);
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

  setHtml($("#outcome-chart"), `<div class="outcome-bar">${bar}</div><div class="legend">${legend}</div>`);
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

function awakeFor(startedUtc) {
  if (!startedUtc) return "";
  const mins = Math.round((Date.now() - new Date(startedUtc.replace(" ", "T") + "Z")) / 60000);
  if (mins < 1) return "just started";
  if (mins < 120) return `up ${mins} min`;
  return `up ${Math.round(mins / 60)} h`;
}

// What wakes the server for scheduled runs. On a host that sleeps when idle this
// is the difference between a schedule that fires and one that only fires when
// someone happens to have this page open.
function wakeUpText(w) {
  if (!w || !w.needed) return "";
  if (!w.installed) {
    return `<div class="hint" style="color:var(--critical)">Nothing wakes this server for scheduled runs.
      On a host that sleeps when idle (Render's free plan does), they only go out while someone has
      this page open. Set up the wake-up job: <code>python wakeup.py install</code>.</div>`;
  }
  if (!w.active || (w.last_run_status && w.last_run_status !== "succeeded")) {
    return `<div class="hint" style="color:var(--critical)">The wake-up job is ${w.active ? "failing" : "switched off"}${
      w.last_run_message ? ` (${esc(w.last_run_message)})` : ""} - scheduled runs may find the server asleep.</div>`;
  }
  const call = w.last_call
    ? ` &middot; last woke it ${esc(fmtWhen(w.last_call))}${w.last_call_status && w.last_call_status !== 200
      ? ` (${esc(w.last_call_error || "HTTP " + w.last_call_status)})` : ""}` : "";
  return `<div class="hint">Woken before each run by the database: it calls this server every
    ${w.every_minutes} min from ${w.lead_minutes} min before a run until its calls are done${call}.</div>`;
}

// When no server was running - the record of whether the host slept through a run.
function downtimeText(rows) {
  if (!rows || !rows.length) return "";
  const last = rows[0];
  const mins = Math.round(last.seconds / 60);
  const length = mins < 120 ? `${mins} min` : `${Math.round(mins / 60)} h`;
  return `<div class="hint">Last down ${esc(fmtWhen(last.down_from))} until ${esc(fmtWhen(last.down_until))}
    (${length})${rows.length > 1 ? ` &middot; ${rows.length} times this week` : ""}.</div>`;
}

async function loadDashboard() {
  const data = await api("/overview");
  SETTINGS = data.settings;

  setHtml($("#setup-banner"), setupBanners(data));
  renderTiles(data);
  renderOutcomes(data.totals);
  $("#nav-campaigns").textContent = data.campaigns.total || "";
  $("#nav-dnc").textContent = data.suppressed || "";

  const e = data.engine;
  $("#btn-pause").textContent = e.paused ? "Resume calling" : "Pause all calling";
  $("#btn-pause").dataset.paused = e.paused ? "1" : "0";
  const pill = !e.running
    ? `<span class="pill critical"><span class="dot"></span>Stopped</span>`
    : e.paused
      ? `<span class="pill warning"><span class="dot"></span>Paused</span>`
      : `<span class="pill good"><span class="dot"></span>Running</span>`;
  setHtml($("#engine-status"), `
    <div style="display:flex;flex-direction:column;gap:6px;font-size:13px">
      <div>${pill}
        <span class="hint" style="display:inline">${esc(awakeFor(e.started_at))} &middot; last tick ${esc(fmtWhen(e.last_tick))}</span></div>
      <div class="hint">Mode: <b>${esc(data.settings.delivery_mode)}</b> &middot;
        ${data.settings.delivery_mode === "webhook"
          ? "Telnyx fetches scripts from this server; opt-out keypresses work."
          : "Scripts ride along with each call; nothing needs to be internet-reachable."}</div>
      <div class="hint">Calling window ${esc(data.settings.window_start)}&ndash;${esc(data.settings.window_end)}
        local to each recipient, ${esc(data.settings.calls_per_minute)} calls/minute.</div>
      ${wakeUpText(e.wake_up)}
      ${downtimeText(e.downtime)}
      ${e.last_error ? `<div class="hint" style="color:var(--critical)">${esc(e.last_error)}</div>` : ""}
    </div>`);

  setHtml($("#upcoming"), data.upcoming.length
    ? `<div class="feed">` + data.upcoming.map((c) => `
        <div class="feed-row">
          <span class="feed-time">${esc(fmtWhen(c.next_run_at, c.timezone))}</span>
          <span class="feed-msg"><button class="link" data-open="${c.id}" style="padding:0">${esc(c.name)}</button>
            <span class="hint" style="display:inline">(${esc(c.frequency)})</span></span>
        </div>`).join("") + `</div>`
    : `<div class="hint">Nothing scheduled.</div>`);

  setHtml($("#feed"), data.events.length
    ? data.events.map((ev) => `
        <div class="feed-row ${ev.level === "error" ? "error" : ev.level === "warn" ? "warn" : ""}">
          <span class="feed-time">${esc(fmtTime(ev.ts))}</span>
          <span class="feed-msg">${esc(ev.message)}</span>
        </div>`).join("")
    : `<div class="hint">Nothing yet.</div>`);

  if (!$("#test-phone").value && data.settings.test_number) {
    $("#test-phone").value = data.settings.test_number;
  }
}

// --- campaigns --------------------------------------------------------------

function scheduleText(c) {
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
  return schedule;
}

function nextRunText(c) {
  if (c.state === "finished") return "finished";
  if (c.state === "paused") return "paused";
  return c.next_run_at ? `next run ${fmtWhen(c.next_run_at, c.timezone)}` : "nothing scheduled";
}

// Screened-out rows were never going to be dialled, so they are counted
// separately instead of dragging "delivered" down.
function statsText(c) {
  const parts = [`${c.calls_completed} delivered of ${c.calls_finished} called`];
  if (c.calls_in_flight) parts.push(`${c.calls_in_flight} on the line`);
  if (c.calls_waiting) parts.push(c.state === "paused" ? `${c.calls_waiting} on hold while paused` : `${c.calls_waiting} waiting`);
  if (c.calls_skipped) parts.push(`${c.calls_skipped} screened out`);
  return parts.join(" · ");
}

function campaignWarning(c) {
  if (c.state === "finished") return "";
  if (!c.contacts) return "No contacts yet - add some before it runs.";
  if (c.require_consent && !c.consented) return "None of its contacts is marked as consented, so nobody will be called.";
  return "";
}

// Runs that came due while nothing was running. Without this a schedule that
// stopped firing looks exactly like one that simply has nothing to report.
function missedText(c) {
  if (!c.missed_runs) return "";
  return `${plural(c.missed_runs, "scheduled run")} missed - the server was asleep or calling was `
    + "paused when it came due. Activity on the dashboard has the dates; Run now sends one by hand.";
}

function stateActions(c, small = true) {
  const cls = small ? "small" : "";
  const toggle = c.state === "finished" ? ""
    : `<button class="${cls}" data-toggle="${c.id}" data-state="${esc(c.state)}">${c.state === "active" ? "Pause" : "Resume"}</button>`;
  return `<button class="${cls}" data-run="${c.id}" ${c.state === "paused" ? 'disabled title="Resume the campaign first"' : ""}>${c.state === "finished" ? "Run again" : "Run now"}</button>
    ${toggle}
    <button class="${cls}" data-edit="${c.id}">Edit</button>`;
}

function campaignCard(c) {
  const stateClass = { active: "good", paused: "warning", finished: "muted", draft: "muted" }[c.state] || "muted";
  const placed = c.calls_finished + c.calls_waiting + c.calls_in_flight;
  const done = placed ? Math.round((c.calls_completed / placed) * 100) : 0;
  const warning = campaignWarning(c);
  return `<div class="campaign">
    <div class="campaign-top">
      <button class="link campaign-name" data-open="${c.id}">${esc(c.name)}</button>
      <span class="pill ${stateClass}"><span class="dot"></span>${esc(c.state)}</span>
      <div style="flex:1"></div>
      <div class="btn-row">
        <button class="small" data-open="${c.id}">Open</button>
        ${stateActions(c)}
      </div>
    </div>
    <div class="campaign-meta">
      ${esc(scheduleText(c))} &middot; ${esc(c.timezone)} &middot;
      ${plural(c.contacts, "contact")} (${c.consented} consented) &middot;
      ${esc(nextRunText(c))}${c.business_name ? ` &middot; from ${esc(c.business_name)}` : ""}
      ${c.rolling ? ` &middot; rolling calls from ${plural(c.caller_list.length, "number")}` : ""}
    </div>
    ${warning ? `<div class="campaign-meta" style="color:var(--warning-ink)">&#9888; ${esc(warning)}</div>` : ""}
    ${missedText(c) ? `<div class="campaign-meta" style="color:var(--warning-ink)">&#9888; ${esc(missedText(c))}</div>` : ""}
    <div class="campaign-msg">${esc(c.message || "No message set.")}</div>
    ${c.calls_total ? `<div class="progress"><div style="width:${done}%"></div></div>
      <div class="campaign-meta">${esc(statsText(c))}</div>` : ""}
  </div>`;
}

async function loadCampaigns() {
  const list = await api("/campaigns");
  CAMPAIGNS = new Map(list.map((c) => [c.id, c]));
  $("#nav-campaigns").textContent = list.length || "";
  setHtml($("#campaign-list"), list.length
    ? list.map(campaignCard).join("")
    : `<div class="card"><div class="empty">No campaigns yet.
        <button class="link" data-view-jump="new">Create the first one</button>.</div></div>`);
}

// The detail page is drawn once as a skeleton, then its live parts are
// refreshed in place every few seconds - without touching what's being typed.
function renderCampaignSkeleton(id) {
  $("#detail-body").innerHTML = `
    <div class="card">
      <div class="card-head"><h2>Script</h2><div class="spacer"></div><span id="detail-state"></span></div>
      <div class="campaign-msg" id="detail-message"></div>
      <div class="hint" id="detail-identity" style="margin-top:8px"></div>
      <div class="btn-row" style="margin-top:12px">
        <span class="btn-row" id="detail-actions"></span>
        <div style="flex:1"></div>
        <button class="small danger" data-delete="${id}">Delete campaign</button>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h2>Try it on your phone</h2></div>
      <p class="hint" style="margin-bottom:10px">Rings one number with exactly this campaign's call &mdash;
        its script, voice, caller identity and call type &mdash; without touching the list.
        Telnyx bills it like any call.</p>
      <div class="btn-row">
        <input type="text" id="detail-test-phone" placeholder="+1 617 555 0142" style="max-width:220px">
        <button class="primary" id="detail-test-btn">Call me with this campaign</button>
        <span class="hint" id="detail-test-status"></span>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h2>Contacts</h2><div class="spacer"></div>
        <span class="hint" id="detail-consent-count"></span></div>
      <div class="field">
        <textarea id="detail-add" class="mono" rows="2" placeholder="Add more numbers, one per line - number, name"></textarea>
        <label class="check" style="margin-top:8px">
          <input type="checkbox" id="detail-consent"> <span>These gave prior express written consent</span>
        </label>
      </div>
      <div class="btn-row">
        <button class="small" id="detail-add-btn">Add contacts</button>
        <button class="small" id="detail-consent-all">Mark all as consented&hellip;</button>
        <span class="hint" id="detail-add-status"></span>
      </div>
      <div class="table-wrap" style="margin-top:12px" id="detail-contacts"></div>
    </div>

    <div class="card">
      <div class="card-head"><h2>Calls</h2><div class="spacer"></div>
        <button class="small" id="detail-refresh">Refresh</button></div>
      <div class="table-wrap" id="detail-calls"></div>
    </div>`;
  $("#detail-test-phone").value = SETTINGS.test_number || "";
}

async function openCampaign(id) {
  if (!id) return show("campaigns");
  if (currentCampaignId !== id || !$("#detail-calls")) {
    currentCampaignId = id;
    $("#detail-name").textContent = "Campaign";
    $("#detail-sub").textContent = "";
    renderCampaignSkeleton(id);
  }
  await refreshCampaign(id);
}

async function refreshCampaign(id) {
  let c, contacts, calls;
  try {
    [c, contacts, calls] = await Promise.all([
      api(`/campaigns/${id}`), api(`/campaigns/${id}/contacts`), api(`/calls?campaign_id=${id}&limit=100`),
    ]);
  } catch (err) {
    if (/No such campaign/.test(err.message)) { toast("That campaign no longer exists.", true); return show("campaigns"); }
    throw err;
  }
  if (currentView !== "campaign" || currentCampaignId !== id) return;
  CAMPAIGNS.set(c.id, c);
  // The soonest call still waiting - often deferred to the start of someone's calling window.
  const nextCall = calls.filter((r) => ["pending", "deferred"].includes(r.state) && !r.is_test)
    .map((r) => r.scheduled_for).sort()[0];

  $("#detail-name").textContent = c.name;
  $("#detail-sub").textContent = `${plural(c.contacts, "contact")} · ${scheduleText(c)} · ${nextRunText(c)}`
    + (nextCall && c.state !== "paused" ? ` · next call ${fmtWhen(nextCall)}` : "");
  const stateClass = { active: "good", paused: "warning" }[c.state] || "muted";
  setHtml($("#detail-state"), `<span class="pill ${stateClass}"><span class="dot"></span>${esc(c.state)}</span>`);
  $("#detail-message").textContent = c.message;
  const business = c.business_name || SETTINGS.business_name;
  $("#detail-identity").innerHTML =
    `${c.mode === "agent" ? "AI conversation" : "Recorded message"} &middot; calling on behalf of
     <b>${esc(business || "(no business name set)")}</b>${c.business_name ? "" : " (from Settings)"} &middot;
     callback ${esc(c.callback_number || SETTINGS.callback_number || "the caller-ID number")}
     <br>${c.rolling
       ? `<b>Rolling calls</b> &mdash; each call from one of: ${c.caller_list.map((n) =>
           `${esc(n.pretty)}${n.label ? ` (${esc(n.label)})` : ""}`).join(", ")}`
       : `Calls from ${esc(SETTINGS.telnyx_from_number || "the primary number")} (rolling calls off)`}
     ${c.calls_total ? `<br>${esc(statsText(c))}` : ""}
     ${campaignWarning(c) ? `<br><span style="color:var(--warning-ink)">&#9888; ${esc(campaignWarning(c))}</span>` : ""}
     ${missedText(c) ? `<br><span style="color:var(--warning-ink)">&#9888; ${esc(missedText(c))}</span>` : ""}`;
  setHtml($("#detail-actions"), stateActions(c, false));

  $("#detail-consent-count").textContent = `${c.consented} of ${c.contacts} consented`;
  $("#detail-consent-all").classList.toggle("hidden", c.consented >= c.contacts);
  setHtml($("#detail-contacts"), `
    <table><thead><tr><th>Number</th><th>Name</th><th>Timezone</th><th>Consent</th><th></th></tr></thead>
    <tbody>${contacts.map((ct) => `
      <tr>
        <td class="phone">${esc(ct.pretty)}${ct.suppressed ? ' <span class="pill critical"><span class="dot"></span>DNC</span>' : ""}</td>
        <td>${esc(ct.name || "—")}</td>
        <td class="hint">${esc(ct.timezone)}</td>
        <td><button class="link pill-button" data-consent="${ct.id}" data-value="${ct.consent ? 0 : 1}"
              title="${ct.consent ? "Mark as not consented" : "Mark as consented"}">${ct.consent
          ? '<span class="pill good"><span class="dot"></span>yes</span>'
          : '<span class="pill warning"><span class="dot"></span>no</span>'}</button></td>
        <td><button class="link small" data-del-contact="${ct.id}" data-pretty="${esc(ct.pretty)}">remove</button></td>
      </tr>`).join("") || `<tr><td colspan="5" class="empty">No contacts yet.</td></tr>`}
    </tbody></table>`);
  setHtml($("#detail-calls"), callsTable(calls, false, true));
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

// A scheduled time as the recipient will experience it, e.g. "9:01 AM PDT".
function fmtTheirTime(utcString, tz) {
  if (!utcString || !tz) return "";
  const d = new Date(utcString.replace(" ", "T") + "Z");
  try {
    return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", timeZone: tz, timeZoneName: "short" });
  } catch { return ""; }
}

function scheduleCell(r) {
  const waiting = ["pending", "deferred"].includes(r.state);
  const theirs = waiting ? fmtTheirTime(r.scheduled_for, r.recipient_tz) : "";
  return `<td class="hint when">${waiting ? `<b style="color:var(--ink)">${esc(fmtWhen(r.scheduled_for))}</b>` : esc(fmtWhen(r.scheduled_for))}`
    + (theirs ? `<br>${esc(theirs)} their time` : "") + `</td>`;
}

function callsTable(rows, withCampaign = true, withSchedule = false) {
  if (!rows.length) return `<div class="empty">No calls yet.</div>`;
  return `<table>
    <thead><tr>
      <th>When</th>${withSchedule ? "<th>Scheduled for</th>" : ""}${withCampaign ? "<th>Campaign</th>" : ""}<th>Number</th>
      <th>From</th><th>Status</th><th>Length</th><th>Telnyx SID</th><th>Detail</th>
    </tr></thead>
    <tbody>${rows.map((r) => {
      // A missing name means either a test call (never had a campaign) or a
      // campaign deleted since - the id is what tells the two apart.
      const campaign = r.campaign_name || (r.campaign_id ? "Deleted campaign" : "Test call");
      const detail = r.error || (["deferred", "pending"].includes(r.state) ? r.status : "") || "";
      return `
      <tr>
        <td class="hint when">${esc(fmtWhen(r.updated_at))}</td>
        ${withSchedule ? scheduleCell(r) : ""}
        ${withCampaign ? `<td class="name" title="${esc(campaign)}">${r.campaign_name && r.campaign_id
          ? `<button class="link" data-open="${r.campaign_id}" style="padding:0">${esc(campaign)}</button>` : esc(campaign)}</td>` : ""}
        <td class="phone">${esc(r.pretty)}</td>
        <td class="phone hint">${esc(r.from_pretty || "—")}</td>
        <td>${statusPill(r)}</td>
        <td class="num">${r.duration ? r.duration + "s" : "—"}</td>
        <td class="hint" style="font-family:var(--mono);font-size:11px">${esc(r.sid || "—")}</td>
        <td class="hint">${esc(detail)}</td>
      </tr>`;
    }).join("")}
    </tbody></table>`;
}

function callFilters() {
  const params = new URLSearchParams();
  if ($("#calls-campaign").value) params.set("campaign_id", $("#calls-campaign").value);
  if ($("#calls-outcome").value) params.set("outcome", $("#calls-outcome").value);
  if ($("#calls-q").value.trim()) params.set("q", $("#calls-q").value.trim());
  return params;
}

async function loadCallCampaigns() {
  const select = $("#calls-campaign");
  const chosen = select.value;
  const list = await api("/campaigns");
  select.innerHTML = `<option value="">All campaigns</option>` +
    list.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
  select.value = list.some((c) => String(c.id) === chosen) ? chosen : "";
}

async function loadCalls() {
  const params = callFilters();
  $("#calls-export").href = "/api/calls.csv" + (params.toString() ? "?" + params : "");
  params.set("limit", String(callsLimit));
  const filtered = [...params.keys()].length > 1;  // anything beyond the limit
  const rows = await api("/calls?" + params);
  setHtml($("#calls-table"), rows.length || !filtered
    ? callsTable(rows) : `<div class="empty">No calls match these filters.</div>`);
  $("#calls-more-row").classList.toggle("hidden", rows.length < callsLimit);
}

// --- do not call ------------------------------------------------------------

async function loadDnc() {
  const rows = await api("/suppression");
  $("#nav-dnc").textContent = rows.length || "";
  setHtml($("#dnc-table"), rows.length
    ? `<table><thead><tr><th>Number</th><th>Reason</th><th>Added</th><th></th></tr></thead>
       <tbody>${rows.map((r) => `
        <tr><td class="phone">${esc(r.pretty)}</td><td>${esc(r.reason)}</td>
        <td class="hint">${esc(fmtWhen(r.created_at))}</td>
        <td><button class="link small" data-unsuppress="${esc(r.phone)}" data-pretty="${esc(r.pretty)}"
              data-reason="${esc(r.reason)}">remove</button></td></tr>`).join("")}
       </tbody></table>`
    : `<div class="empty">The list is empty.</div>`);
}

// --- settings ---------------------------------------------------------------

const SETTING_FIELDS = {
  telnyx_app_id: "#s-app-id", telnyx_from_number: "#s-from", test_number: "#s-test",
  business_name: "#s-business", callback_number: "#s-callback", window_start: "#s-window-start",
  window_end: "#s-window-end", calls_per_minute: "#s-cpm", ring_seconds: "#s-ring",
  public_base_url: "#s-base-url", script_provider: "#s-script-provider",
};

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
  renderCallerRows(data.settings.caller_numbers);
  $("#s-test").value = data.settings.test_number || "";
  $("#s-business").value = data.settings.business_name || "";
  $("#s-callback").value = data.settings.callback_number || "";
  $("#s-window-start").value = data.settings.window_start || "09:00";
  $("#s-window-end").value = data.settings.window_end || "20:00";
  $("#s-cpm").value = data.settings.calls_per_minute || "12";
  $("#s-ring").value = data.settings.ring_seconds || "50";
  $("#s-base-url").value = data.settings.public_base_url || "";
  $("#s-anthropic").placeholder = data.settings.anthropic_api_key_set
    ? "saved — leave blank to keep it" : "sk-ant-...";
  $("#s-perplexity").placeholder = data.settings.perplexity_api_key_set
    ? "saved — leave blank to keep it" : "pplx-...";
  $("#s-script-provider").value = data.settings.script_provider || "";
  // A value coming from the server's environment comes back after being cleared
  // here - say so next to it, rather than letting Save look broken.
  $$(".env-note").forEach((el) => el.remove());
  for (const [key, envVar] of Object.entries(data.settings.from_environment || {})) {
    const input = SETTING_FIELDS[key] && $(SETTING_FIELDS[key]);
    if (!input) continue;
    input.insertAdjacentHTML("afterend", `<div class="hint env-note">From the server's environment
      (<code>${esc(envVar)}</code>). Clearing it here won't remove it &mdash; change it where the server runs.</div>`);
  }
  renderAgent(data.agent);
  const store = data.database;
  $("#settings-sub").textContent = store.backend === "postgres"
    ? `Saved in the hosted Postgres database. Each API key is only ever sent to the service it belongs to.`
    : `Saved in a SQLite file on this machine. Each API key is only ever sent to the service it belongs to.`;
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
  settingsDirty = false;
  loadInbound().catch(() => {});
}

// More numbers to call from, for rolling calls. Four rows are always offered,
// so there's room to add numbers without hunting for a button.
const CALLER_SLOTS = 4;

function callerRow(n = {}) {
  return `<div class="caller-row">
    <input type="text" class="caller-number" placeholder="+1 512 555 0100" aria-label="Caller number" value="${esc(n.number || "")}">
    <input type="text" class="caller-label" placeholder="Label, e.g. Austin" maxlength="40" aria-label="Label for this number" value="${esc(n.label || "")}">
    <span class="hint caller-stats"></span>
    <button type="button" class="link small" data-remove-caller>remove</button>
  </div>`;
}

function renderCallerRows(list) {
  const rows = [...(list || [])];
  while (rows.length < CALLER_SLOTS) rows.push({});
  $("#caller-rows").innerHTML = rows.map(callerRow).join("");
  loadCallerStats().catch(() => {});
}

function callerRowsValue() {
  return $$("#caller-rows .caller-row")
    .map((row) => ({ number: $(".caller-number", row).value.trim(), label: $(".caller-label", row).value.trim() }))
    .filter((n) => n.number);
}

function addCallerRow(number = "") {
  const empty = $$("#caller-rows .caller-number").find((input) => !input.value.trim());
  if (!empty) $("#caller-rows").insertAdjacentHTML("beforeend", callerRow());
  const input = empty || $$("#caller-rows .caller-number").pop();
  input.value = number;
  if (!number) input.focus();
  settingsDirty = true;
}

// How each number has been doing: a number many people block is worth swapping out.
async function loadCallerStats() {
  const stats = new Map((await api("/caller-numbers")).map((s) => [s.number, s]));
  const text = (s) => !s ? "" : !s.calls ? "No calls in 30 days"
    : `${plural(s.calls, "call")} in 30 days` + (s.blocked ? ` · <span style="color:var(--critical)">blocked ${s.blocked}×</span>` : "");
  for (const row of $$("#caller-rows .caller-row")) {
    $(".caller-stats", row).innerHTML = text(stats.get(normalizePhone($(".caller-number", row).value)));
  }
  $("#from-stats").innerHTML = text(stats.get(normalizePhone($("#s-from").value)));
}

function fillVoices() {
  for (const select of [$("#c-voice"), $("#test-voice")]) {
    if (!META.voices || !META.voices.length || select.options.length) continue;
    select.innerHTML = META.voices
      .map((v) => `<option value="${esc(v.id)}">${esc(v.label)}</option>`).join("");
  }
}

function settingsValues() {
  return {
    telnyx_api_key: $("#s-api-key").value.trim(),
    telnyx_app_id: $("#s-app-id").value.trim(),
    telnyx_from_number: $("#s-from").value.trim(),
    caller_numbers: JSON.stringify(callerRowsValue()),
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
}

async function saveSettings({ quiet = false } = {}) {
  await api("/settings", { method: "POST", body: { values: settingsValues() } });
  $("#s-api-key").value = "";
  $("#s-anthropic").value = "";
  $("#s-perplexity").value = "";
  settingsDirty = false;
  if (!quiet) toast("Settings saved.");
  await loadSettings();
}

// --- new / edit campaign ----------------------------------------------------

function fillFormOptions() {
  const tzSelect = $("#c-timezone");
  const chosen = tzSelect.value;
  tzSelect.innerHTML = TIMEZONES
    .map((tz) => `<option value="${esc(tz)}">${esc(tz)}</option>`).join("");
  tzSelect.value = TIMEZONES.includes(chosen) ? chosen : "America/New_York";
  fillVoices();
}

// Which schedule fields apply depends on the frequency; called after every
// change and every reset, so a reset form never shows a weekday picker for "Once".
function refreshScheduleFields() {
  const freq = $("#c-frequency").value;
  $("#wrap-weekday").classList.toggle("hidden", freq !== "weekly");
  $("#wrap-time").classList.toggle("hidden", freq === "hourly");
  refreshWindowWarning();
}

// Rolling calls: the box starts with every number in Settings, primary first.
function allCallerLines() {
  const lines = SETTINGS.telnyx_from_number ? [`${SETTINGS.telnyx_from_number}, Primary`] : [];
  for (const n of SETTINGS.caller_numbers || []) lines.push(n.label ? `${n.number}, ${n.label}` : n.number);
  return lines.join("\n");
}

function refreshRolling() {
  const on = $("#c-rolling").checked;
  $("#wrap-rolling").hidden = !on;
  if (on && !$("#c-caller-numbers").value.trim()) $("#c-caller-numbers").value = allCallerLines();
  countCallers();
}

// Same rules as the server: one number per line, and only numbers in Settings.
function countCallers() {
  const meter = $("#caller-numbers-meter");
  if (!$("#c-rolling").checked) return (meter.textContent = "");
  const known = new Set([SETTINGS.telnyx_from_number, ...(SETTINGS.caller_numbers || []).map((n) => n.number)]
    .map(normalizePhone).filter(Boolean));
  const seen = new Set();
  let unknown = 0, bad = 0;
  for (let line of $("#c-caller-numbers").value.split("\n")) {
    line = line.trim();
    if (!line || line.startsWith("#")) continue;
    const number = normalizePhone(line.split(",")[0]);
    if (!number) { bad++; continue; }
    if (seen.has(number)) continue;
    seen.add(number);
    if (!known.has(number)) unknown++;
  }
  const parts = [`<b>${plural(seen.size, "number")}</b> in the rotation`];
  if (seen.size === 1) parts.push("add more under Settings &rsaquo; Call from this number to rotate");
  if (unknown) parts.push(`<span style="color:var(--critical)">${unknown} not in Settings &mdash; calls can only come from numbers there</span>`);
  if (bad) parts.push(`<span style="color:var(--critical)">${notNumbers(bad)}</span>`);
  meter.innerHTML = parts.join(" · ");
}

function identityHints() {
  $("#c-business").placeholder = SETTINGS.business_name ? `${SETTINGS.business_name} (from Settings)` : "No default set in Settings";
  $("#c-callback").placeholder = SETTINGS.callback_number ? `${SETTINGS.callback_number} (from Settings)` : "The caller-ID number";
}

function resetCampaignForm() {
  $("#campaign-form").reset();
  $("#c-require-consent").checked = true;
  $("#script-preview").textContent = "—";
  $("#script-meter").textContent = "";
  $("#draft-status").textContent = "";
  $("#contacts-meter").textContent = "";
  fillFormOptions();
  refreshScheduleFields();
  refreshModeHint();
  refreshConsentWarning();
  refreshRolling();
  $("#c-agent-info").dispatchEvent(new Event("input"));
}

async function openEditor(id) {
  if (!META.voices.length) await loadSettings();
  editingId = id;
  resetCampaignForm();
  identityHints();
  const editing = Boolean(id);
  $("#form-title").textContent = editing ? "Edit campaign" : "New campaign";
  $("#form-sub").textContent = editing
    ? "Changes apply from the next call. Contacts are managed on the campaign's page."
    : "A furniture store announcing a Memorial Day sale, for example.";
  $("#btn-submit-campaign").textContent = editing ? "Save changes" : "Create campaign";
  $("#card-contacts").classList.toggle("hidden", editing);
  $("#wrap-start-now").classList.toggle("hidden", editing);
  if (!editing) return;

  const c = await api(`/campaigns/${id}`);
  $("#c-name").value = c.name;
  $("#c-agent").checked = c.mode === "agent";
  $("#c-agent-info").value = c.agent_info || "";
  $("#c-message").value = c.message;
  $("#c-business").value = c.business_name || "";
  $("#c-callback").value = c.callback_number || "";
  $("#c-voice").value = c.voice;
  $("#c-amd").value = c.amd;
  $("#c-frequency").value = c.frequency;
  $("#c-time").value = c.call_time;
  $("#c-weekday").value = String(c.weekday);
  $("#c-timezone").value = c.timezone;
  $("#c-starts-at").value = toLocalInput(c.starts_at, c.timezone);
  $("#c-ends-at").value = toLocalInput(c.ends_at, c.timezone);
  $("#c-require-consent").checked = Boolean(c.require_consent);
  $("#c-rolling").checked = Boolean(c.rolling);
  $("#c-caller-numbers").value = c.rolling ? c.caller_numbers || "" : "";
  refreshRolling();
  refreshScheduleFields();
  refreshModeHint();
  $("#c-agent-info").dispatchEvent(new Event("input"));
  schedulePreview();
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
      const p = await api("/script/preview", { method: "POST", body: {
        message, business_name: $("#c-business").value, callback_number: $("#c-callback").value } });
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
  let digits = text.replace(/\D/g, "");
  if (!digits) return "";
  const plus = text.startsWith("+");
  if (plus && !digits.startsWith("1")) return digits.length >= 8 && digits.length <= 15 ? "+" + digits : "";
  if (digits.length === 10 && !plus) digits = "1" + digits;
  if (digits.length !== 11 || !digits.startsWith("1")) return "";
  // Area code and exchange never start with 0 or 1; N11 codes are services.
  const area = digits.slice(1, 4), exchange = digits.slice(4, 7);
  if (!/[2-9]/.test(area[0]) || !/[2-9]/.test(exchange[0]) || area.slice(1) === "11") return "";
  return "+" + digits;
}

function contactStats() {
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
  return { usable, dupes, bad };
}

function countContacts() {
  const meter = $("#contacts-meter");
  const { usable, dupes, bad } = contactStats();
  refreshConsentWarning();
  if (!usable && !dupes && !bad) return (meter.textContent = "");
  const parts = [`<b>${usable}</b> number${usable === 1 ? "" : "s"} to call`];
  if (dupes) parts.push(`${dupes} duplicate${dupes === 1 ? "" : "s"} ignored`);
  if (bad) parts.push(`<span style="color:var(--critical)">${bad} line${bad === 1 ? "" : "s"} not a phone number</span>`);
  meter.innerHTML = parts.join(" · ");
}

// Said before submitting, not after: with the boxes left as they are, every
// number is screened out and a "start now" campaign finishes having called nobody.
function refreshConsentWarning() {
  const { usable } = contactStats();
  const trapped = usable > 0 && $("#c-require-consent").checked && !$("#c-consent-have").checked;
  $("#consent-warning").classList.toggle("hidden", !trapped);
  if (trapped) {
    $("#consent-warning-text").innerHTML = `<b>None of these ${plural(usable, "number")} will be called</b> unless
      you tick the consent box above. Tick it only if they really gave written consent &mdash; or untick
      "Refuse to dial" if you're only calling your own test numbers.`;
  }
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
  const writer = SETTINGS.script_provider_active === "perplexity" ? "Perplexity"
    : SETTINGS.script_provider_active === "anthropic" ? "Claude" : "The AI";
  $("#draft-status").innerHTML = `<span class="spinner"></span> ${writer} is writing…`;
  try {
    const result = await api("/script/draft", {
      method: "POST",
      body: { brief, existing, business_name: $("#c-business").value.trim() || SETTINGS.business_name || "", seconds: 25 },
    });
    $("#c-message").value = result.message;
    $("#draft-status").textContent = `Drafted · about ${result.seconds} seconds. Edit freely.`;
    schedulePreview();
  } catch (err) {
    $("#draft-status").innerHTML = `<span style="color:var(--critical)">${esc(err.message)}</span>`;
  } finally { btn.disabled = false; }
}

function campaignBody() {
  return {
    name: $("#c-name").value.trim(),
    message: $("#c-message").value.trim(),
    voice: $("#c-voice").value,
    mode: $("#c-agent").checked ? "agent" : "message",
    agent_info: $("#c-agent").checked ? $("#c-agent-info").value.trim() : "",
    business_name: $("#c-business").value.trim(),
    callback_number: $("#c-callback").value.trim(),
    amd: $("#c-amd").value,
    frequency: $("#c-frequency").value,
    call_time: $("#c-time").value || "10:00",
    weekday: Number($("#c-weekday").value || 0),
    timezone: $("#c-timezone").value,
    require_consent: $("#c-require-consent").checked,
    rolling: $("#c-rolling").checked,
    caller_numbers: $("#c-rolling").checked ? $("#c-caller-numbers").value : "",
    starts_at: $("#c-starts-at").value,
    ends_at: $("#c-ends-at").value,
  };
}

async function submitCampaign(event) {
  event.preventDefault();
  const body = campaignBody();
  if (!body.message) return toast("The campaign needs a message.", true);
  const button = $("#btn-submit-campaign");

  if (editingId) {
    return busy(button, async () => {
      const id = editingId;
      const result = await api(`/campaigns/${id}`, { method: "PATCH", body });
      toast(result.note || "Changes saved.");
      editingId = null;
      show("campaign", id);
    });
  }

  const { usable } = contactStats();
  Object.assign(body, {
    start_now: $("#c-start-now").checked,
    contacts: $("#c-contacts").value,
    contacts_consented: $("#c-consent-have").checked,
  });
  if (usable && body.require_consent && !body.contacts_consented && !confirm(
    `None of the ${plural(usable, "number")} is marked as having given consent, so nobody will be called.\n\nCreate the campaign anyway? You can mark consent later on its page.`)) return;

  await busy(button, async () => {
    const result = await api("/campaigns", { method: "POST", body });
    let note = `Campaign created with ${result.contacts_added} contact(s).`;
    if (result.invalid.length) note += ` ${result.invalid.length} line(s) weren't valid numbers: ${result.invalid.slice(0, 3).join("; ")}${result.invalid.length > 3 ? "…" : ""}.`;
    if (result.unconsented) note += " None are marked as consented, so they won't be called until you mark them.";
    else if (!result.contacts_added) note += " Add contacts on its page before it runs.";
    toast(note, Boolean(result.unconsented || !result.contacts_added));
    resetCampaignForm();
    show("campaign", result.id);
  });
}

// --- actions ----------------------------------------------------------------

function confirmRun(c) {
  if (!c) return true;
  const eligible = c.require_consent ? c.consented : c.contacts;
  const screened = c.contacts - eligible;
  const schedule = c.frequency === "once"
    ? (c.state === "active" && c.next_run_at ? `\n\nThis takes the place of its scheduled run (${fmtWhen(c.next_run_at, c.timezone)}).` : "")
    : "\n\nIts regular schedule carries on as well.";
  return confirm(`Call ${plural(eligible, "person", "people")} on "${c.name}" now?`
    + (screened ? `\n\n${screened} without consent will be skipped.` : "")
    + "\n\nCalls go out at the paced rate, inside each person's calling window." + schedule);
}

async function handleClick(event) {
  const jump = event.target.closest("[data-view-jump]");
  if (jump) return show(jump.dataset.viewJump);

  const nav = event.target.closest(".nav-item[data-view]");
  if (nav) return show(nav.dataset.view);

  const doc = event.target.closest("[data-doc]");
  if (doc) return openDoc(doc.dataset.doc, doc.dataset.docTitle);

  const open = event.target.closest("[data-open]");
  if (open) return show("campaign", Number(open.dataset.open));

  const edit = event.target.closest("[data-edit]");
  if (edit) return show("edit", Number(edit.dataset.edit));

  const run = event.target.closest("[data-run]");
  if (run) {
    const id = Number(run.dataset.run);
    if (!confirmRun(CAMPAIGNS.get(id))) return;
    return busy(run, async () => {
      const r = await api(`/campaigns/${id}/run`, { method: "POST" });
      toast(runSummary(r) + (r.note ? " " + r.note : ""), r.queued === 0 && r.skipped > 0);
      currentView === "campaign" ? refreshCampaign(id) : loadCampaigns();
    });
  }

  const toggle = event.target.closest("[data-toggle]");
  if (toggle) {
    const id = Number(toggle.dataset.toggle);
    const pausing = toggle.dataset.state === "active";
    return busy(toggle, async () => {
      const r = await api(`/campaigns/${id}`, { method: "PATCH", body: { state: pausing ? "paused" : "active" } });
      toast(r.note || (pausing ? "Paused - its queued calls are on hold too." : "Resumed."));
      currentView === "campaign" ? refreshCampaign(id) : loadCampaigns();
    });
  }

  const del = event.target.closest("[data-delete]");
  if (del) {
    if (!confirm("Delete this campaign and its recipient list? Queued calls are cancelled. This can't be undone.")) return;
    return busy(del, async () => {
      await api(`/campaigns/${del.dataset.delete}`, { method: "DELETE" });
      toast("Campaign deleted.");
      show("campaigns");
    });
  }

  const consent = event.target.closest("[data-consent]");
  if (consent) {
    const giving = consent.dataset.value === "1";
    if (giving && !confirm("Mark this person as having given prior express written consent to automated calls?\n\nOnly do this if you have their consent on record.")) return;
    return busy(consent, async () => {
      await api(`/campaigns/${currentCampaignId}/contacts/${consent.dataset.consent}`,
        { method: "PATCH", body: { consent: giving } });
      refreshCampaign(currentCampaignId);
    });
  }

  const delContact = event.target.closest("[data-del-contact]");
  if (delContact) {
    if (!confirm(`Remove ${delContact.dataset.pretty} from this campaign? A call already queued for them is cancelled.`)) return;
    return busy(delContact, async () => {
      const r = await api(`/campaigns/${currentCampaignId}/contacts/${delContact.dataset.delContact}`,
        { method: "DELETE" });
      if (r.cancelled) toast(`Removed, and their queued call was cancelled.`);
      refreshCampaign(currentCampaignId);
    });
  }

  const unsuppress = event.target.closest("[data-unsuppress]");
  if (unsuppress) {
    // An opt-out is a legal record. Taking one off the list should be deliberate.
    if (!confirm(`Remove ${unsuppress.dataset.pretty} from the do-not-call list?\n\nIt was added because: ${unsuppress.dataset.reason}.\nOnly do this if they asked to be called again. The removal is recorded under Activity.`)) return;
    return busy(unsuppress, async () => {
      await api(`/suppression/${encodeURIComponent(unsuppress.dataset.unsuppress)}`, { method: "DELETE" });
      toast("Removed from the do-not-call list.");
      loadDnc();
    });
  }

  if (event.target.closest("#detail-add-btn")) {
    const id = currentCampaignId;
    return busy($("#detail-add-btn"), async () => {
      const r = await api(`/campaigns/${id}/contacts`, {
        method: "POST",
        body: { raw: $("#detail-add").value, consented: $("#detail-consent").checked },
      });
      $("#detail-add").value = r.invalid.join("\n");
      $("#detail-add-status").innerHTML = (r.added ? `Added or updated ${plural(r.added, "contact")}.` : "")
        + (r.invalid.length ? ` <span style="color:var(--critical)">${notNumbers(r.invalid.length)} - left in the box to fix.</span>` : "");
      refreshCampaign(id);
    });
  }

  if (event.target.closest("#detail-consent-all")) {
    const c = CAMPAIGNS.get(currentCampaignId);
    const missing = c ? c.contacts - c.consented : 0;
    if (!confirm(`Mark all ${plural(missing, "remaining contact")} as having given prior express written consent?\n\nOnly do this if you have consent on record for every one of them.`)) return;
    return busy($("#detail-consent-all"), async () => {
      const r = await api(`/campaigns/${currentCampaignId}/contacts/consent-all`, { method: "POST" });
      toast(`${plural(r.changed, "contact")} marked as consented.`);
      refreshCampaign(currentCampaignId);
    });
  }

  if (event.target.closest("#detail-test-btn")) {
    return busy($("#detail-test-btn"), () =>
      runTestCall(`/campaigns/${currentCampaignId}/test-call`, { phone: $("#detail-test-phone").value },
                  $("#detail-test-status")).then(() => refreshCampaign(currentCampaignId)));
  }

  if (event.target.closest("#detail-refresh")) {
    return busy($("#detail-refresh"), () => refreshCampaign(currentCampaignId));
  }
}

function renderAgent(info) {
  if (!info) return;
  const btn = $("#btn-agent-setup");
  btn.textContent = info.assistant_id ? "Update in Telnyx" : "Set up in Telnyx";
  btn.disabled = !info.can_setup;
  $("#s-agent-llm").value = info.route || "perplexity";
  const where = info.route === "telnyx" ? "Telnyx" : "Perplexity";
  $("#agent-status").innerHTML = info.ready
    ? `<span style="color:var(--good)">&#10003; Ready.</span> Model <code>${esc(info.model)}</code> via ${where},
       voice <code>${esc(info.voice)}</code>, Telnyx assistant <code>${esc(info.assistant_id)}</code>.`
    : info.needs_update && !info.missing.length
      ? `<span style="color:var(--critical)">Switched to ${where} &mdash; press Update in Telnyx to apply it.
         AI campaign calls wait until then.</span>`
    : info.missing.length
      ? `Not ready yet. Still needed: ${info.missing.map(esc).join("; ")}.`
      : "Everything it needs is in place &mdash; press the button to create the agent in Telnyx.";
  refreshModeHint();
}

async function changeAgentLlm() {
  try {
    await api("/settings", { method: "POST", body: { values: { agent_llm: $("#s-agent-llm").value } } });
    META.agent = (await api("/settings")).agent;
    renderAgent(META.agent);
    toast("Saved - press Update in Telnyx to apply it.");
  } catch (err) { toast(err.message, true); }
}

async function setupAgent() {
  const btn = $("#btn-agent-setup");
  btn.disabled = true;
  $("#agent-setup-status").innerHTML = `<span class="spinner"></span> setting up in Telnyx…`;
  try {
    // Setup reads the saved settings, so anything typed but unsaved goes first.
    if (settingsDirty) await saveSettings({ quiet: true });
    META.agent = await api("/agent/setup", { method: "POST" });
    renderAgent(META.agent);
    $("#agent-setup-status").textContent = "Done.";
  } catch (err) {
    $("#agent-setup-status").innerHTML = `<span style="color:var(--critical)">${esc(err.message)}</span>`;
    btn.disabled = false;
  }
}

async function loadInbound() {
  const info = await api("/inbound");
  const btn = $("#btn-inbound-setup");
  btn.disabled = !info.can_setup;
  btn.textContent = info.ready ? "Set up again" : "Set up callback line";
  $("#inbound-status").innerHTML = info.ready
    ? `<span style="color:var(--good)">&#10003; Set up.</span> Calls to ${esc(info.number)} hear: press 9 to be
       removed from the list, or leave a message.`
    : info.missing && info.missing.length
      ? `Not set up. Still needed: ${info.missing.map(esc).join("; ")}.`
      : `<span style="color:var(--critical)">Not set up</span> &mdash; calls to ${esc(info.number)} currently go to
         <code>${esc(info.url || "nowhere")}</code>, so nobody who calls back can opt out or reach you.`;
}

async function setupInbound() {
  const btn = $("#btn-inbound-setup");
  btn.disabled = true;
  $("#inbound-setup-status").innerHTML = `<span class="spinner"></span> updating Telnyx…`;
  try {
    if (settingsDirty) await saveSettings({ quiet: true });
    await api("/inbound/setup", { method: "POST" });
    $("#inbound-setup-status").textContent = "Done.";
    await loadInbound();
  } catch (err) {
    $("#inbound-setup-status").innerHTML = `<span style="color:var(--critical)">${esc(err.message)}</span>`;
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
  $("#wrap-amd").style.display = agentMode ? "none" : "";  // the agent handles voicemail itself
  const ready = META.agent && META.agent.ready;
  $("#c-mode-hint").innerHTML = !agentMode ? ""
    : ready
      ? "The agent says up front that it's an automated AI assistant, answers only from your talking points, and honours opt-outs. Each minute costs more than a recorded message."
      : `<span style="color:var(--critical)">The AI phone agent isn't set up yet &mdash; finish Settings &rsaquo; AI phone agent first.</span>`;
  $("#wrap-test-voice").style.display = $("#test-mode").value === "agent" ? "none" : "";
}

// Shared by "Call me now" and a campaign's "Call me with this campaign": place
// it, then say plainly what happened - including when it didn't ring.
async function runTestCall(path, body, statusEl) {
  statusEl.innerHTML = `<span class="spinner"></span> dialling…`;
  try {
    const r = await api(path, { method: "POST", body });
    const task = r.task || {};
    statusEl.innerHTML = task.sid
      ? `<span style="color:var(--good)">Call placed — your phone should ring.</span> <span class="hint">SID ${esc(task.sid)}</span>`
      : task.error
        ? `<span style="color:var(--critical)">${esc(task.error)}</span>`
        : task.state === "skipped"
          ? `<span style="color:var(--critical)">Not called: ${esc(STATUS_LABELS[task.status] || task.status)}.</span>`
          : `Queued — ${esc(task.status || "waiting for the dispatcher")}.`;
  } catch (err) {
    statusEl.innerHTML = `<span style="color:var(--critical)">${esc(err.message)}</span>`;
  }
}

async function testCall() {
  await busy($("#btn-test-call"), async () => {
    await runTestCall("/test-call", {
      phone: $("#test-phone").value, message: $("#test-message").value,
      voice: $("#test-voice").value || "Polly.Joanna-Neural", mode: $("#test-mode").value,
    }, $("#test-status"));
    loadDashboard().catch(() => {});
  });
}

async function verifyTelnyx() {
  const btn = $("#btn-verify");
  btn.disabled = true;
  $("#verify-status").innerHTML = `<span class="spinner"></span> checking…`;
  try {
    // What is typed now, saved or not - that's what the person is looking at.
    const v = settingsValues();
    const r = await api("/settings/verify", { method: "POST", body: { values: {
      telnyx_api_key: v.telnyx_api_key, telnyx_app_id: v.telnyx_app_id,
      telnyx_from_number: v.telnyx_from_number, public_base_url: v.public_base_url,
      caller_numbers: v.caller_numbers } } });
    // The primary first, then each rolling number - a bought number left
    // unassigned would otherwise only show up as failed calls.
    const fromNote = r.ok && r.from_detail
      ? `<div style="color:${r.from_ok ? "var(--ink-2)" : "var(--critical)"};margin-top:4px">
           ${r.from_ok ? "✓" : "✗"} ${esc(r.from_detail)}</div>`
        + (r.callers || []).slice(1).map((c) => `<div style="color:${c.ok ? "var(--ink-2)" : "var(--critical)"};margin-top:4px">
           ${c.ok ? "✓" : "✗"} Rolling number ${esc(c.pretty)}: ${esc(c.detail)}.</div>`).join("")
      : "";
    const limits = r.limits || {};
    const limitNotes = [];
    if (limits.channel_limit) limitNotes.push(`At most <b>${limits.channel_limit}</b> call${limits.channel_limit === 1 ? "" : "s"} at once on this account` + (limits.channel_limit < 4 ? " &mdash; raise the channel limit in Telnyx to call faster." : "."));
    if (limits.daily_spend_limit) limitNotes.push(`Daily spend cap of <b>$${esc(limits.daily_spend_limit)}</b> &mdash; Telnyx refuses calls for the rest of the day once it's reached.`);
    if (r.ok && "inbound_url" in limits) limitNotes.push(r.inbound_ready ? "Callback line: set up." : "Callback line: <b>not set up</b> &mdash; see Callback line below.");
    $("#verify-status").innerHTML = (r.ok
      ? `<span style="color:var(--good)">✓ ${esc(r.detail)}</span>`
      : `<span style="color:var(--critical)">${esc(r.detail)}</span>`) + fromNote
      + limitNotes.map((n) => `<div style="margin-top:4px">${n}</div>`).join("")
      + (r.unsaved && r.unsaved.length ? `<div style="margin-top:4px;color:var(--warning-ink)">Checked what's typed, which isn't saved yet &mdash; press Save settings to keep it.</div>` : "");
    const voiceNumbers = (r.numbers || []).filter((n) => n.voice);
    const inUse = new Set([v.telnyx_from_number, ...callerRowsValue().map((n) => n.number)].map(normalizePhone));
    const spare = voiceNumbers.filter((n) => !inUse.has(n.phone_number));
    $("#from-options").innerHTML = voiceNumbers.length
      ? "Voice-capable numbers on this account: " +
        voiceNumbers.map((n) =>
          `<button type="button" class="link small" data-pick="${esc(n.phone_number)}">${esc(n.phone_number)}</button>`).join(" ")
        + (spare.length ? `<br>Not used yet: ${spare.map((n) =>
          `<button type="button" class="link small" data-add-caller="${esc(n.phone_number)}">+ add ${esc(n.phone_number)} to rolling</button>`).join(" ")}` : "")
      : r.ok ? "No voice-capable numbers on this account yet - buy one in the Telnyx portal." : "";
  } catch (err) {
    $("#verify-status").innerHTML = `<span style="color:var(--critical)">${esc(err.message)}</span>`;
  } finally { btn.disabled = false; }
}

// --- boot -------------------------------------------------------------------

const THEMES = ["", "dark", "light"];  // "" follows the system setting

function applyTheme() {
  let saved = "";
  try { saved = localStorage.getItem("robocall-theme") || ""; } catch { /* storage blocked */ }
  if (saved) document.documentElement.dataset.theme = saved;
  else delete document.documentElement.dataset.theme;
  $("#theme-label").textContent = `Theme: ${saved || "system"}`;
}

document.addEventListener("click", (event) => {
  handleClick(event).catch?.((err) => toast(err.message, true));
});
document.addEventListener("click", (e) => {
  const pick = e.target.closest("[data-pick]");
  if (pick) { $("#s-from").value = pick.dataset.pick; settingsDirty = true; toast("From number set - save to keep it."); }
  const add = e.target.closest("[data-add-caller]");
  if (add) { addCallerRow(add.dataset.addCaller); add.remove(); toast("Added to rolling numbers - save to keep it."); }
  const remove = e.target.closest("[data-remove-caller]");
  if (remove) {
    remove.closest(".caller-row").remove();
    if ($$("#caller-rows .caller-row").length < CALLER_SLOTS) $("#caller-rows").insertAdjacentHTML("beforeend", callerRow());
    settingsDirty = true;
  }
});
$("#btn-add-caller").addEventListener("click", () => addCallerRow());

$("#theme-toggle").addEventListener("click", () => {
  const now = document.documentElement.dataset.theme || "";
  const next = THEMES[(THEMES.indexOf(now) + 1) % THEMES.length];
  try { next ? localStorage.setItem("robocall-theme", next) : localStorage.removeItem("robocall-theme"); }
  catch { /* storage blocked - the change still applies for this visit */ }
  if (next) document.documentElement.dataset.theme = next;
  else delete document.documentElement.dataset.theme;
  $("#theme-label").textContent = `Theme: ${next || "system"}`;
});

$("#btn-test-call").addEventListener("click", testCall);
$("#test-mode").addEventListener("change", refreshModeHint);
$("#btn-verify").addEventListener("click", verifyTelnyx);
$("#btn-save-settings").addEventListener("click", () => busy($("#btn-save-settings"), () => saveSettings()));
$("#btn-draft").addEventListener("click", draftScript);
$("#btn-agent-setup").addEventListener("click", setupAgent);
$("#btn-inbound-setup").addEventListener("click", setupInbound);
$("#s-agent-llm").addEventListener("change", changeAgentLlm);
$("#view-settings").addEventListener("input", (e) => {
  if (e.target.id !== "s-agent-llm") settingsDirty = true;
});
$("#c-agent").addEventListener("change", refreshModeHint);
$("#c-agent-info").addEventListener("input", () => {
  const n = $("#c-agent-info").value.length;
  $("#c-agent-info-meter").textContent = n
    ? `${n.toLocaleString()} / 4,000 characters. Sent with every reply, so shorter is faster and cheaper.`
    : "The AI only states what's here or in the talking points. Anything else, it hands off to your callback number.";
});
$("#btn-refresh-calls").addEventListener("click", () => busy($("#btn-refresh-calls"), loadCalls));
$("#btn-calls-more").addEventListener("click", () => { callsLimit += 100; busy($("#btn-calls-more"), loadCalls); });
let callsSearchTimer = null;
for (const id of ["#calls-campaign", "#calls-outcome"]) {
  $(id).addEventListener("change", () => { callsLimit = 100; loadCalls().catch((err) => toast(err.message, true)); });
}
$("#calls-q").addEventListener("input", () => {
  clearTimeout(callsSearchTimer);
  callsSearchTimer = setTimeout(() => { callsLimit = 100; loadCalls().catch((err) => toast(err.message, true)); }, 300);
});
$("#btn-refresh-docs").addEventListener("click", () => {
  loadDocs().catch((err) => toast(err.message, true));
});
$("#btn-close-doc").addEventListener("click", closeDoc);
$("#campaign-form").addEventListener("submit", submitCampaign);
$("#btn-cancel-campaign").addEventListener("click", () => {
  const id = editingId;
  editingId = null;
  id ? show("campaign", id) : show("campaigns");
});
$("#c-message").addEventListener("input", schedulePreview);
$("#c-business").addEventListener("input", schedulePreview);
$("#c-callback").addEventListener("input", schedulePreview);
$("#c-contacts").addEventListener("input", countContacts);
$("#c-consent-have").addEventListener("change", refreshConsentWarning);
$("#c-require-consent").addEventListener("change", refreshConsentWarning);
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

$("#c-frequency").addEventListener("change", refreshScheduleFields);
$("#c-rolling").addEventListener("change", refreshRolling);
$("#c-caller-numbers").addEventListener("input", countCallers);
$("#btn-refill-callers").addEventListener("click", () => {
  $("#c-caller-numbers").value = allCallerLines();
  countCallers();
});
$("#c-ends-at").addEventListener("input", refreshWindowWarning);

$("#btn-pause").addEventListener("click", () => {
  const pausing = $("#btn-pause").dataset.paused !== "1";
  if (pausing && !confirm("Pause all calling? Nothing is dialled until you resume. Runs that come due meanwhile go out on resume if only a little late, and are skipped if hours late.")) return;
  busy($("#btn-pause"), async () => {
    await api("/settings", { method: "POST", body: { values: { dispatch_paused: pausing ? "1" : "0" } } });
    toast(pausing ? "Calling paused." : "Calling resumed.");
    await loadDashboard();
  });
});

$("#btn-add-dnc").addEventListener("click", () => busy($("#btn-add-dnc"), async () => {
  const r = await api("/suppression", { method: "POST", body: { raw: $("#dnc-input").value } });
  // Keep anything that couldn't be read in the box, so it can be fixed.
  $("#dnc-input").value = r.invalid.join("\n");
  $("#dnc-status").innerHTML = `Added ${plural(r.added, "number")}.`
    + (r.invalid.length ? ` <span style="color:var(--critical)">${notNumbers(r.invalid.length)} - left in the box to fix.</span>` : "");
  loadDnc();
}));

window.addEventListener("beforeunload", (e) => {
  if (currentView === "settings" && settingsDirty) { e.preventDefault(); e.returnValue = ""; }
});

applyTheme();
bindTooltip();
fillFormOptions();
loadSettings()
  .catch((err) => toast(err.message, true))
  .then(() => { const { view, id } = parseHash(); show(view, id); });

// show() writes the hash, so a view can be linked to and the back button works.
// Guarding on the current view stops show()'s own write from bouncing back here.
window.addEventListener("hashchange", () => {
  const { view, id } = parseHash();
  if (view !== currentView || id !== currentRouteId) show(view, id);
});

// Keep whatever is on screen current. Pages with a form being typed into are
// left alone; the campaign page refreshes only its live parts.
setInterval(() => {
  if (document.hidden) return;
  const refresh = { dashboard: loadDashboard, calls: loadCalls, campaigns: loadCampaigns,
                    campaign: () => refreshCampaign(currentCampaignId) }[currentView];
  if (refresh) Promise.resolve().then(refresh).catch(() => {});
}, 5000);
