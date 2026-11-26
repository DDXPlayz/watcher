"use strict";

const $ = (id) => document.getElementById(id);
const IST = "Asia/Kolkata";
const REFRESH_MS = 5000;

// All page text goes through textContent: listing text comes from third-party sites.
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null) continue;
    if (key === "class") node.className = value;
    else node[key] = value;
  }
  node.append(...children.filter((c) => c !== null && c !== undefined));
  return node;
}

const timeFmt = new Intl.DateTimeFormat("en-IN", { timeZone: IST, hour: "numeric", minute: "2-digit", second: "2-digit", hour12: true });
const stampFmt = new Intl.DateTimeFormat("en-IN", { timeZone: IST, day: "numeric", month: "short", hour: "numeric", minute: "2-digit", second: "2-digit", hour12: true });
const clockFmt = new Intl.DateTimeFormat("en-IN", { timeZone: IST, weekday: "short", day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit", hour12: true });
const clockTime = (hhmm) => {
  const [h, m] = hhmm.split(":").map(Number);
  return `${h % 12 || 12}:${String(m).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`;
};

let skew = 0; // server clock minus browser clock
const now = () => Date.now() + skew;

function ago(iso) {
  if (!iso) return "Never";
  const s = Math.max(0, Math.round((now() - Date.parse(iso)) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
function until(iso) {
  if (!iso) return "Not scheduled";
  const s = Math.round((Date.parse(iso) - now()) / 1000);
  return s <= 0 ? "Now" : s < 120 ? `In ${s}s` : `In ${Math.round(s / 60)}m`;
}
function span(seconds) {
  const d = Math.floor(seconds / 86400), h = Math.floor((seconds % 86400) / 3600), m = Math.floor((seconds % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}
const ms = (v) => (v === null || v === undefined ? "–" : v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${v}ms`);
const pct = (v) => (v === null || v === undefined ? "–" : `${(v * 100).toFixed(1)}%`);

const HEALTH = {
  match_available: ["good", "Matching session available"],
  no_match: ["good", "Healthy, no matching session"],
  unverified: ["warn", "Possible match, not verified"],
  starting: ["info", "Waiting for first check"],
  stale: ["warn", "Data is stale"],
  unavailable: ["bad", "Temporarily unavailable"],
  rate_limited: ["warn", "Rate limited"],
  not_permitted: ["info", "Not checked: platform prohibits it"],
  awaiting_decision: ["info", "Not checked: waiting for your decision"],
  disabled: ["info", "Disabled"],
};
const STATE = {
  AVAILABLE: "Available", UNAVAILABLE: "No matching show", UNVERIFIED: "Unverified",
  PLATFORM_ERROR: "Could not check", NOT_YET_LISTED: "Not listed yet", SOLD_OUT: "Sold out",
};
const OVERALL = {
  healthy: ["good", "Monitoring"], degraded: ["warn", "Monitoring with problems"], down: ["bad", "Nothing can be checked"],
  starting: ["info", "Starting"], paused: ["warn", "Paused"], finished: ["info", "Finished"],
};

let data = null;
let lastLoad = 0;
let tab = "events";

async function call(path, options = {}) {
  const response = await fetch(path, options);
  if (response.status === 401) {
    location.replace("/login");
    throw new Error("signed out");
  }
  if (!response.ok) {
    let detail = `The watcher answered ${response.status}.`;
    try { detail = (await response.json()).detail || detail; } catch { /* not JSON */ }
    throw new Error(typeof detail === "string" ? detail : "That input is not valid.");
  }
  return response;
}

function tone(node, name) {
  node.classList.remove("tone-good", "tone-warn", "tone-bad", "tone-info");
  if (name) node.classList.add(`tone-${name}`);
}

function verdict() {
  const live = data.screenings.filter((s) => s.currently_available);
  const o = data.overall;
  const pvr = data.platforms.find((p) => p.id === "pvr");
  const names = (ids) => ids.map((id) => data.platforms.find((p) => p.id === id)?.name || id).join(" and ");
  const uncovered = o.not_automated.length ? ` ${names(o.not_automated)} ${o.not_automated.length > 1 ? "are" : "is"} not checked automatically.` : "";
  if (live.length) return ["good", "Tickets are on sale", `${live.length === 1 ? "One matching show is" : `${live.length} matching shows are`} bookable right now.`];
  if (!data.running) {
    return o.status === "finished"
      ? ["info", "The show window has passed", "Monitoring has stopped. Change the target to watch for something else."]
      : ["warn", "Monitoring is paused", "Nothing is being checked. Resume to start watching again."];
  }
  if (o.status === "down") return ["bad", "Nothing can be checked", "Every automated check is failing. This says nothing about whether tickets are on sale, so check the sites yourself."];
  if (o.status === "starting") return ["info", "Running first checks", "The first results usually take under a minute."];
  if (data.platforms.some((p) => p.health === "unverified")) return ["warn", "A possible match is not confirmed", `Something is listed that could not be fully verified, so no alert was sent.${uncovered}`];
  if (data.platforms.some((p) => p.health !== "stale" && p.state === "SOLD_OUT")) return ["warn", "Sold out for now", `You will be alerted if seats reopen.${uncovered}`];
  const note = pvr?.state === "UNAVAILABLE"
    ? "The cinema has shows listed for this date, but none is the one you want."
    : "The cinema has not listed this show yet.";
  return ["", o.not_automated.length ? `Not on sale at ${names(o.checking)} yet` : "Not on sale yet", `${note}${uncovered}`];
}

function renderAlarm() {
  const a = data.alarm;
  $("alarm").hidden = !a?.ringing;
  if (!a?.ringing) return;
  const shows = a.shows === 1 ? "A matching show is" : `${a.shows} matching shows are`;
  const sent = a.repeats ? ` ${a.repeats} repeat${a.repeats === 1 ? "" : "s"} sent so far.` : "";
  const every = a.every_seconds % 60 === 0 ? (a.every_seconds === 60 ? "minute" : `${a.every_seconds / 60} minutes`) : `${a.every_seconds} seconds`;
  $("alarm-note").textContent = `${shows} on sale. Your phone gets a reminder every ${every} until you press the button.${sent}`;
}

function renderTop() {
  const [oTone, oText] = OVERALL[data.overall.status] || ["info", data.overall.status];
  tone($("overall"), oTone);
  $("overall-text").textContent = oText;
  const [vTone, title, note] = verdict();
  tone($("verdict"), vTone);
  if ($("verdict-title").textContent !== title) $("verdict-title").textContent = title;
  let text = note;
  if (!data.overall.notifications_configured) text += " No notification provider is set up, so no alert can reach your phone.";
  $("verdict-note").textContent = text;
  document.title = vTone === "good" ? "TICKETS ON SALE: Doomsday Watcher" : "Doomsday Watcher";

  const t = data.target;
  const date = new Intl.DateTimeFormat("en-IN", { weekday: "long", day: "numeric", month: "long", year: "numeric", timeZone: "UTC" }).format(new Date(`${t.date}T00:00:00Z`));
  const rows = [["Film", t.movie], ["Cinema", `${t.cinema}, ${t.city}`], ["Date", date], ["Language", t.language], ["Format", t.format],
    ["Show time", `${clockTime(t.earliest_time)} to ${clockTime(t.latest_time)} IST`]];
  $("target").replaceChildren(...rows.map(([k, v]) => el("div", {}, el("dt", { textContent: k }), el("dd", { textContent: v }))));
}

function renderScreenings() {
  const list = data.screenings.filter((s) => s.in_target);
  $("screenings").hidden = !list.length;
  $("screenings").replaceChildren(...list.map((s) => {
    const status = s.currently_available ? "Bookable now"
      : s.stale ? `Last known: ${STATE[s.availability].toLowerCase()}. Not verified recently`
      : STATE[s.availability];
    let seats = "Seat availability unknown";
    if (s.seats_available !== null) {
      seats = `${s.seats_available} seats visible, ${ago(s.seats_at)}${s.seats_stale || s.stale ? " (stale)" : ""}`;
    } else if (s.seat_status) seats = `Listed as ${s.seat_status.toLowerCase()}${s.stale ? " (stale)" : ""}`;
    const cats = (s.seat_categories || []).filter((c) => c.available !== null)
      .map((c) => `${c.name} ${c.available}${c.price_text ? ` at ${c.price_text}` : ""}`).join(", ");
    return el("article", { class: `screening ${s.currently_available ? "live" : "old"}` },
      el("div", { class: "when", textContent: s.showtime }),
      el("div", { class: "what" },
        el("div", { textContent: `${s.platform_name}: ${status}` }),
        el("div", { textContent: `${s.cinema}, ${s.language}, ${s.format}` }),
        el("div", { class: "sub", textContent: seats }),
        cats ? el("div", { class: "sub", textContent: cats }) : null,
        el("div", { class: "sub", textContent: `Verified ${ago(s.last_verified)}` })),
      s.booking_url ? el("a", { class: "book", href: s.booking_url, target: "_blank", rel: "noopener noreferrer", textContent: s.currently_available ? "Book now" : "Open page" }) : null);
  }));
}

function renderStats() {
  const m = data.metrics;
  const stats = [
    ["Last successful check", ago(data.last_success)],
    ["Checks, last 7 days", m.total_checks.toLocaleString("en-IN")],
    ["Matches on sale", String(data.matches)],
    ["Matches ever seen", String(data.matches_ever)],
    ["Notifications delivered", String(m.notifications_delivered)],
    ["Uptime", span(data.overall.uptime_seconds)],
  ];
  $("stats").replaceChildren(...stats.map(([label, value]) =>
    el("div", {}, el("div", { class: "label", textContent: label }), el("div", { class: "value", textContent: value }))));
}

function renderPlatforms() {
  $("platforms").replaceChildren(...data.platforms.map((p) => {
    const [t, text] = HEALTH[p.health] || ["info", p.health];
    const automated = !["not_permitted", "awaiting_decision", "disabled"].includes(p.health);
    const rows = automated ? [
      ["Latest result", STATE[p.state] || p.state],
      ["Last good result", p.last_good_state ? STATE[p.last_good_state] : "None yet"],
      ["Last success", ago(p.last_success)],
      ["Last attempt", ago(p.last_attempt)],
      ["Checks every", p.interval_seconds ? `${p.interval_seconds}s` : "–"],
      ["Next check", data.running ? (p.checking ? "Running" : until(p.next_check_at)) : "Paused"],
      ["Failures in a row", String(p.failure_count)],
      ["Dates listed", (p.signals?.dates || []).join(", ") || "–"],
    ] : [["Latest result", "Not checked"]];
    const detail = p.error_detail || (p.signals?.venue_formats?.length ? `At the cinema on this date: ${p.signals.venue_formats.join("; ")}` : null);
    return el("article", { class: `platform tone-${t}${p.checking ? " checking" : ""}` },
      el("h2", { textContent: p.name }),
      el("div", { class: "health" }, el("span", { class: "dot" }), el("span", { class: "tone-text", textContent: text })),
      el("dl", {}, ...rows.flatMap(([k, v]) => [el("dt", { textContent: k }), el("dd", { textContent: v })])),
      detail ? el("p", { class: "detail", textContent: detail }) : null,
      el("a", { class: "open", href: p.url, target: "_blank", rel: "noopener noreferrer", textContent: `Open on ${p.name}` }));
  }));
}

function renderControls() {
  const run = $("btn-run");
  run.textContent = data.running ? "Pause monitoring" : data.last_success ? "Resume monitoring" : "Start monitoring";
  $("btn-logout").hidden = !data.auth_required;
  $("btn-check").disabled = !data.running;
  $("btn-test").disabled = !data.providers.length;
  $("btn-test").title = data.providers.length ? `Sends through ${data.providers[0]}` : "Set up a provider first";
}

const rowOf = (at, message, tag) => el("div", { class: "row" }, el("time", { textContent: stampFmt.format(new Date(at)) }), el("div", { class: "msg", textContent: message }), el("div", { class: "tag", textContent: tag }));
const nameOf = (id) => data?.platforms.find((p) => p.id === id)?.name || "";
const empty = (text) => el("p", { class: "empty", textContent: text });

async function renderPanel() {
  const panel = $("panel");
  const wanted = tab;
  let nodes = [];
  if (wanted === "events") {
    const rows = await (await call("/api/events?limit=80")).json();
    nodes = rows.length ? rows.map((e) => rowOf(e.at, e.message, e.type.replaceAll("_", " ").toLowerCase())) : [empty("Nothing has happened yet.")];
  } else if (wanted === "checks") {
    const rows = await (await call("/api/checks?limit=80")).json();
    nodes = rows.length ? rows.map((c) => rowOf(c.at, `${nameOf(c.platform)}: ${STATE[c.state] || c.state}${c.error_kind ? ` (${c.error_kind.replaceAll("_", " ").toLowerCase()})` : ""}`, ms(c.duration_ms))) : [empty("No checks have run yet.")];
  } else if (wanted === "notifications") {
    const rows = await (await call("/api/notifications")).json();
    nodes = rows.length ? rows.map((n) => rowOf(n.created_at, `${n.title}${n.last_error ? `: ${n.last_error}` : ""}`, `${n.status.toLowerCase()}${n.provider ? ` via ${n.provider}` : ""}${n.attempts > 1 ? `, ${n.attempts} attempts` : ""}`)) : [empty("No notifications have been sent.")];
  } else if (wanted === "performance") {
    const head = ["Platform", "Checks", "Success", "Uptime 24h", "Average", "Slowest", "Failed", "Recoveries"];
    const body = data.platforms.map((p) => {
      const m = p.metrics;
      return [p.name, m ? m.checks : "–", pct(m?.success_rate), pct(m?.uptime_24h), ms(m?.avg_ms), ms(m?.max_ms), m ? m.failed : "–", p.recoveries];
    });
    const m = data.metrics;
    nodes = [el("div", { class: "scroll" }, el("table", {},
      el("thead", {}, el("tr", {}, ...head.map((h) => el("th", { textContent: h })))),
      el("tbody", {}, ...body.map((r) => el("tr", {}, ...r.map((c) => el("td", { textContent: String(c) }))))))),
    empty(`Detection to delivery: average ${ms(m.detection_to_delivery_ms_avg)}, slowest ${ms(m.detection_to_delivery_ms_max)}. Provider send time: average ${ms(m.send_ms_avg)}. Waiting: ${m.notifications_pending}. Failed: ${m.notifications_failed}.`)];
  } else {
    const old = panel.querySelector("pre");
    const text = await (await call("/api/logs?lines=200")).text();
    const pre = el("pre", { textContent: text || "The log is empty." });
    nodes = [pre];
    if (wanted !== tab) return;
    const keep = old && old.scrollTop + old.clientHeight < old.scrollHeight - 20 ? old.scrollTop : null;
    panel.replaceChildren(...nodes);
    pre.scrollTop = keep ?? pre.scrollHeight;
    return;
  }
  if (wanted === tab) panel.replaceChildren(...nodes);
}

async function refresh() {
  try {
    data = await (await call("/api/status")).json();
    skew = Date.parse(data.now) - Date.now();
    lastLoad = Date.now();
    $("offline").hidden = true;
    renderAlarm(); renderTop(); renderScreenings(); renderStats(); renderPlatforms(); renderControls();
    await renderPanel();
  } catch (error) {
    if (error.message === "signed out") return;
    $("offline").hidden = false;
    $("offline").textContent = lastLoad
      ? `Lost contact with the watcher. Everything below is from ${timeFmt.format(new Date(lastLoad + skew))} and may be wrong.`
      : "The watcher is not answering.";
    tone($("overall"), "bad");
    $("overall-text").textContent = "No connection";
  }
}

let toastTimer;
function toast(text, bad = false) {
  $("toast").textContent = text;
  $("toast").classList.toggle("bad", bad);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("toast").textContent = ""; }, 8000);
}

async function act(button, work) {
  button.disabled = true;
  try { toast(await work()); } catch (error) { toast(error.message, true); }
  button.disabled = false;
  await refresh();
}

$("btn-run").addEventListener("click", (e) => act(e.target, async () => {
  const action = data?.running ? "pause" : "resume";
  await call(`/api/monitoring/${action}`, { method: "POST" });
  return action === "pause" ? "Monitoring paused." : "Monitoring started.";
}));
$("btn-check").addEventListener("click", (e) => act(e.target, async () => {
  const result = await (await call("/api/check", { method: "POST" })).json();
  const parts = [];
  if (result.started.length) parts.push(`Checking ${result.started.map(nameOf).join(" and ")}.`);
  const pvr = result.refused.pvr;
  if (pvr) parts.push(`PVR INOX not started: ${pvr}.`);
  return parts.join(" ") || "No platform can be checked right now.";
}));
$("btn-ack").addEventListener("click", (e) => act(e.target, async () => {
  const result = await (await call("/api/alarm/ack", { method: "POST" })).json();
  return result.acknowledged ? "Alerts stopped. You will be alerted again if a show sells out and comes back." : "Nothing was ringing.";
}));
$("btn-test").addEventListener("click", (e) => act(e.target, async () => {
  await call("/api/test-notification", { method: "POST" });
  tab = "notifications"; markTabs();
  return "Test queued. Its delivery status appears below.";
}));
$("btn-logout").addEventListener("click", async () => { await call("/api/logout", { method: "POST" }); location.replace("/login"); });

const dialog = $("settings");
$("btn-settings").addEventListener("click", () => {
  if (!data) return;
  const form = $("settings-form");
  form.reset();
  form.date.value = data.target.date;
  form.language.value = data.target.language;
  form.earliest_time.value = data.target.earliest_time.slice(0, 5);
  form.latest_time.value = data.target.latest_time.slice(0, 5);
  $("settings-error").textContent = "";
  dialog.showModal();
});
$("settings-cancel").addEventListener("click", () => dialog.close());
$("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await call("/api/target", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(Object.fromEntries(new FormData(event.target))) });
    dialog.close();
    toast("Target changed.");
    await refresh();
  } catch (error) { $("settings-error").textContent = error.message; }
});

function markTabs() {
  for (const b of document.querySelectorAll("[data-tab]")) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
}
for (const button of document.querySelectorAll("[data-tab]")) {
  button.addEventListener("click", () => { tab = button.dataset.tab; markTabs(); renderPanel().catch(() => {}); });
}

setInterval(() => {
  $("clock").textContent = `${clockFmt.format(new Date(now()))} IST`;
  if (data && $("offline").hidden) {
    renderStats();
    // Do not pull a link out from under someone who is tabbing to it.
    if (!$("platforms").contains(document.activeElement)) renderPlatforms();
  }
}, 1000);
setInterval(refresh, REFRESH_MS);
refresh();
