# File:         staff_pages.py
# Purpose:      The Staff page (what a signed-in staff person sees) as one self-contained HTML document.
# Inputs:       The public dashboard's address (handed in by the server, checked here again); then the page asks /staff/state for everything it shows, after sign-in.
# Outputs:      A complete HTML document made of the shared page style plus this page's own markup and script.
# Dependencies: Python standard library and control_access.page_style (the look the sign-in page and the control panel already share).
"""The Staff page. It holds no data and no secret: everything it shows is fetched from /staff/state, which checks the staff cookie, and every press goes to /staff/act, which checks it again.

Text from people (a display name, a failure's studio, a note) is ONLY ever put into the page with textContent, never as markup, so nothing a person types can inject anything. The page
reaches the staff doors and nothing else: it has no way to ask for a super-user action.
"""
import re

from app import control_access

# The one kind of address the page ever puts in its back link: a plain https address with no quote, space or script. Anything else (or nothing) is this site's own front page.
_DASHBOARD_ADDRESS = re.compile(r"https://[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~/-]*)?")

_STAFF_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>ClarixTech Watchdog - staff</title>
<style>__STYLE__
  .alarm { border:2px solid #ff4d5e; background:#3b1a1a; border-radius:14px; padding:14px 18px; margin:0 0 14px; }
  .alarm .alarm-title { margin:0 0 6px; color:#ffffff; font-weight:700; font-size:1.1rem; }
  .alarm ul { margin:0 0 12px; padding-left:1.2rem; }
  .alarm li { color:#ffd6d6; font-weight:700; margin:0 0 4px; }
  .bad { color:var(--bad); font-weight:700; }
  @media (prefers-reduced-motion: no-preference) { .alarm { animation:alarmPulse 1s steps(2, end) infinite; } @keyframes alarmPulse { 50% { background:#5a1f26; } } }
</style>
</head>
<body>
<main>
  <div id="alarm" role="alert" class="alarm" hidden>
    <p class="alarm-title">Critical failure open</p>
    <ul id="alarm-list"></ul>
    <div class="row">
      <button id="alarm-form" type="button" class="on" hidden>Open the resolve form</button>
      <button id="alarm-sound" type="button" class="quiet" hidden>Tap anywhere on this page to turn the alarm sound on</button>
    </div>
  </div>
  <p id="stale" role="alert" class="bad" hidden>Live updates have stopped, so an alarm could be missed. Reload this page or go back to the dashboard.</p>
  <h1>Staff</h1>
  <p>Signed in as <strong id="who">...</strong>. <a id="to-dashboard" href="__DASHBOARD_URL__">Back to the dashboard</a>. This page returns to the dashboard by itself after two minutes without a touch.</p>
  <div class="card">
    <h2>My shift</h2>
    <p id="shift-text">Loading ...</p>
    <div class="row">
      <button id="start" type="button" class="on" hidden>Start shift</button>
      <button id="finish" type="button" class="off" hidden>Finish shift</button>
      <button id="out" type="button" class="quiet">Sign out</button>
    </div>
    <p>A shift is not ended by signing out. Only Finish shift ends it, and a shift can run up to 48 hours.</p>
    <p id="message" role="status" aria-live="polite"></p>
  </div>
  <details id="sec-roster" class="card">
    <summary id="sum-roster">On shift now</summary>
    <div id="roster" class="chips"></div>
  </details>
  <details id="sec-failures" class="card" open>
    <summary id="sum-failures">Open failures</summary>
    <p>Press I am on it when you have seen a failure and are working on it. You need to be on shift. It is recorded with the time and appears by name in the Monday report.</p>
    <div id="failures"></div>
  </details>
  <details id="sec-done" class="card">
    <summary id="sum-done">Finished in the last 24 hours</summary>
    <div id="done"></div>
  </details>
  <details class="card">
    <summary>Change my password</summary>
    <label for="pw-current">Current password</label>
    <input id="pw-current" type="password" autocomplete="current-password" spellcheck="false">
    <label for="pw-new">New password</label>
    <input id="pw-new" type="password" autocomplete="new-password" spellcheck="false">
    <button id="pw-go" type="button">Change password</button>
  </details>
</main>
<script>
  // ---- Rules that touch nothing on the page (a test cuts them out of this text and runs them on their own) ----
  function stopped(text) { return text.slice(-1) === "." ? text : text + "."; }                 // a display name can end in a full stop ("Ada O."): never write two
  function activeFailures(failures) { return (failures || []).filter(function (item) { return item.active === true; }); }
  // The siren is wanted for a person ON SHIFT, for a recent failure they have not yet said they are on, when they hold it or nobody does. Pressing "I am on it" silences it for them.
  function wantsSiren(me, failures) {
    if (!me || !me.on_shift) { return false; }
    return activeFailures(failures).some(function (item) { return !item.seen_by_me && (item.mine || !(item.people && item.people.length)); });
  }
  function alarmLines(me, failures) {
    var name = me && me.display ? me.display : "";
    return activeFailures(failures).map(function (item) {
      if (item.mine) {
        return item.seen_by_me ? name + ", " + item.display + " is assigned to you and you are on it. Fill in the resolve form when it is fixed."
                               : name + ", " + item.display + " is assigned to you. Press I am on it, then fill in the resolve form.";
      }
      return item.people && item.people.length ? stopped(item.display + " is assigned to " + item.people.join(", ")) : "Nobody has " + item.display + " yet.";
    });
  }
  // Long sections fold away (his standing rule): a native details/summary with the one-line status in the summary; the urgent one (open failures) is open.
  function rosterSummary(names) { return "On shift now: " + (names.length ? names.join(", ") : "nobody"); }
  function failuresSummary(count) { return "Open failures: " + (count ? count : "none"); }
  function doneSummary(count) { return "Finished in the last 24 hours: " + (count ? count : "none"); }
  // true = open it, false = fold it, null = leave it as the person left it. First look: open only if something is urgent. After that: only when there is MORE that is urgent than before.
  function urgentOpen(previous, current) {
    if (previous === null) { return current > 0; }
    return current > previous ? true : null;
  }
  function idleDue(now, last, limit) { return now - last >= limit; }          // a clock that went backwards gives a negative gap: never due
  function staleDue(now, lastGood, limit) { return now - lastGood >= limit; }
  function titleFor(base, alarming) { return (alarming ? "ALARM - " : "") + base; }
  // A note that is half written survives the return to the dashboard: it is kept in this tab only, as text, and a blocked or full store just means nothing is kept.
  function readDrafts(storage, key) {
    var drafts = {};
    try {
      var parsed = JSON.parse(storage.getItem(key));
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        Object.keys(parsed).forEach(function (fid) { if (/^[0-9A-Za-z._-]{1,80}$/.test(fid) && typeof parsed[fid] === "string" && parsed[fid] !== "") { drafts[fid] = parsed[fid].slice(0, 200); } });
      }
    } catch (e) { drafts = {}; }
    return drafts;
  }
  function writeDrafts(storage, key, drafts) {
    try {
      var keep = {};
      Object.keys(drafts).forEach(function (fid) { if (typeof drafts[fid] === "string" && drafts[fid] !== "") { keep[fid] = drafts[fid]; } });
      storage.setItem(key, JSON.stringify(keep));
    } catch (e) { return; }
  }
  // ---- End of the rules ----

  var DRAFT_KEY = "watchdog-staff-notes";
  var IDLE_MS = 120000;           // two minutes without a touch: back to the dashboard (his rule of 2026-11-08)
  var STALE_MS = 12000;           // three refreshes missed (they come every 4 seconds): say so out loud
  var ACTIVITY_EVENTS = ["pointerdown", "pointermove", "keydown", "touchstart", "wheel", "scroll", "input"];
  var BASE_TITLE = document.title;
  var store = null;
  try { store = window.sessionStorage; } catch (e) { store = null; }
  var notes = readDrafts(store, DRAFT_KEY);
  var lastActivity = performance.now();
  var lastGood = performance.now();
  var messageBox = document.getElementById("message");
  function $(id) { return document.getElementById(id); }
  function el(tag, text, className) { var node = document.createElement(tag); if (text !== undefined) { node.textContent = text; } if (className) { node.className = className; } return node; }
  function say(text, bad) { messageBox.textContent = text; messageBox.style.color = bad ? "var(--bad)" : "var(--good)"; }
  function clock(iso) { var moment = new Date(iso); return isNaN(moment.getTime()) ? "?" : moment.toLocaleString([], {weekday: "short", hour: "2-digit", minute: "2-digit", timeZone: "Africa/Lagos"}) + " WAT"; }
  function api(path, options) {
    return fetch(path, Object.assign({credentials: "same-origin", headers: {"Content-Type": "application/json"}}, options)).then(function (reply) {
      if (reply.status === 503) { say("Access is switched off. Please contact ClarixTech.", true); throw new Error("off"); }
      return reply.json().then(function (data) { return data; }, function () { return {}; }).then(function (data) {
        // A 401 means "you are signed out" ONLY when the server says so in its own words. A wrong CURRENT password when changing one is a 401 too, with a different message, and the person
        // is still signed in: it is shown here, not turned into a bounce to the sign-in page (his report of 2026-11-02, "change of password didn't work").
        if (reply.status === 401 && (data.error === "sign in first" || !data.error)) { location.href = "/control/login"; throw new Error("sign in"); }
        return {status: reply.status, data: data};
      });
    });
  }
  function renderRoster(items) {
    var box = $("roster");
    box.textContent = "";
    if (!items.length) { box.appendChild(el("p", "Nobody is on shift.")); return; }
    items.forEach(function (item) {
      var chip = el("span", item.display, "chip");
      chip.appendChild(el("small", clock(item.since)));
      if (item.long) { chip.appendChild(el("small", "over 48 hours", "tag")); }
      box.appendChild(chip);
    });
  }
  // A length of time written as the weekly report writes it: 45s, 4m 12s, 1h 05m.
  function span(seconds) {
    var whole = Math.round(seconds);
    if (whole < 60) { return whole + "s"; }
    if (whole < 3600) { return Math.floor(whole / 60) + "m " + ("0" + (whole % 60)).slice(-2) + "s"; }
    return Math.floor(whole / 3600) + "h " + ("0" + Math.floor((whole % 3600) / 60)).slice(-2) + "m";
  }
  // A press whose time could not be measured (the two servers' clocks disagreed) has after_s null: it is shown without a length of time, never as 0s.
  function seenText(item) { return item.display + " at " + clock(item.at) + (item.after_s !== null ? " (" + span(item.after_s) + " after the red)" : ""); }
  function renderFailures(items, onShift) {
    var list = $("failures");
    list.textContent = "";
    if (!items.length) { list.appendChild(el("p", "No open failures.")); return; }
    items.forEach(function (failure) {
      var box = el("div", undefined, "item"), seen = failure.seen || [];
      box.appendChild(el("p", failure.display + ": " + failure.cause + (failure.latest_cause !== failure.cause ? " (latest: " + failure.latest_cause + ")" : ""), "strong"));
      box.appendChild(el("p", "Red at " + clock(failure.first_at) + ", " + failure.alarms + (failure.alarms === 1 ? " alarm" : " alarms") + (failure.alarms > 1 ? ", the last at " + clock(failure.last_at) : "") + "."));
      box.appendChild(el("p", failure.people.length ? "With " + failure.people.join(", ") + "." : "Nobody has it yet."));
      box.appendChild(el("p", seen.length ? "On it: " + seen.map(function (item) { return seenText(item); }).join("; ") + "." : "Nobody has said they are on it yet."));
      if (onShift && !failure.seen_by_me) {
        var ack = el("button", "I am on it", "small");
        ack.type = "button";
        ack.addEventListener("click", function () { act({action: "acknowledge", fid: failure.fid}); });
        box.appendChild(ack);
      }
      if (failure.mine) {
        var input = el("input");
        input.type = "text"; input.maxLength = 200; input.placeholder = "A short note (optional)"; input.value = notes[failure.fid] || "";
        input.setAttribute("aria-label", "Note for " + failure.display);
        input.addEventListener("input", function () { saveNote(failure.fid, input.value); });
        var go = el("button", "Mark done", "small");
        go.type = "button";
        go.addEventListener("click", function () { act({action: "done", fid: failure.fid, note: input.value}, function () { delete notes[failure.fid]; writeDrafts(store, DRAFT_KEY, notes); }); });
        box.appendChild(input);
        box.appendChild(go);
      }
      list.appendChild(box);
    });
  }
  function renderDone(items) {
    var box = $("done");
    box.textContent = "";
    if (!items.length) { box.appendChild(el("p", "Nothing finished in the last day.")); return; }
    items.forEach(function (item) {
      box.appendChild(el("p", item.display + ", finished " + clock(item.done_at) + " by " + (item.done_by === "super" ? "the super user" : item.done_by) + (item.note ? ": " + item.note : ".")));
    });
  }
  function saveNote(fid, text) { notes[fid] = text; writeDrafts(store, DRAFT_KEY, notes); }
  // ---- The alarm: a red strip with the person's own name, a title that says ALARM, and a siren until they press "I am on it" ----
  var audioCtx = null, osc = null, gain = null, sirenUntil = 0, sirenWanted = false, urgentSeen = null;
  function unlockSound() {
    if (!audioCtx) {
      var Ctor = window.AudioContext || window.webkitAudioContext;
      if (!Ctor) { return; }
      try { audioCtx = new Ctor(); } catch (e) { audioCtx = null; return; }
    }
    if (audioCtx.state === "suspended") { audioCtx.resume(); }
    setSiren(sirenWanted);
  }
  document.addEventListener("pointerdown", unlockSound);
  document.addEventListener("keydown", unlockSound);
  // ONE oscillator for the life of the page, made the first time it is needed and never replaced; only its volume and pitch pattern change, so nothing can stack a second sound on top of it.
  function scheduleBeeps() {
    var now = audioCtx.currentTime;
    if (sirenUntil - now > 20) { return; }
    var from = Math.max(now, sirenUntil);
    for (var cycle = 0; cycle < 50; cycle++) {
      var start = from + cycle * 1.2;
      osc.frequency.setValueAtTime(880, start);
      osc.frequency.setValueAtTime(660, start + 0.3);
      gain.gain.setValueAtTime(0.5, start);
      gain.gain.setValueAtTime(0.0001, start + 0.55);
    }
    sirenUntil = from + 60;
  }
  function setSiren(wanted) {
    sirenWanted = wanted;
    var running = !!audioCtx && audioCtx.state === "running";
    $("alarm-sound").hidden = !(wanted && !running);          // wanted but locked by the browser: say so, never sit silent
    if (!audioCtx) { return; }
    if (wanted && running) {
      if (!osc) {
        osc = audioCtx.createOscillator();
        gain = audioCtx.createGain();
        osc.type = "square";
        gain.gain.value = 0.0001;
        osc.connect(gain);
        gain.connect(audioCtx.destination);
        osc.start();
      }
      scheduleBeeps();
    } else if (osc) {
      gain.gain.cancelScheduledValues(audioCtx.currentTime);
      gain.gain.setTargetAtTime(0.0001, audioCtx.currentTime, 0.02);
      sirenUntil = 0;
    }
  }
  function focusResolveForm() {
    var box = document.querySelector("#failures input[type=text]");
    if (box) { box.scrollIntoView(); box.focus(); }
  }
  function showAlarm(lines, anyMine) {
    var list = $("alarm-list");
    list.textContent = "";
    lines.forEach(function (line) { list.appendChild(el("li", line)); });
    $("alarm").hidden = lines.length === 0;
    $("alarm-form").hidden = !anyMine;
  }
  $("alarm-form").addEventListener("click", focusResolveForm);
  function render(view, force) {
    var me = view.me;
    $("who").textContent = me.display;
    var alarms = alarmLines(me, view.failures || []);
    showAlarm(alarms, activeFailures(view.failures).some(function (item) { return item.mine; }));
    setSiren(wantsSiren(me, view.failures || []));
    document.title = titleFor(BASE_TITLE, alarms.length > 0);
    $("sum-roster").textContent = rosterSummary((view.roster || []).map(function (item) { return item.display; }));
    $("sum-failures").textContent = failuresSummary((view.failures || []).length);
    $("sum-done").textContent = doneSummary((view.done || []).length);
    var urgent = activeFailures(view.failures).length, wanted = urgentOpen(urgentSeen, urgent);       // recent open failures are the urgent ones; older ones are only listed
    if (wanted !== null) { $("sec-failures").open = wanted; }
    urgentSeen = urgent;
    $("shift-text").textContent = me.on_shift ? "You are on shift since " + clock(me.since) + (me.long ? ". This shift is over 48 hours old: finish it if it is over." : ".") : "You are not on shift.";
    $("start").hidden = me.on_shift;
    $("finish").hidden = !me.on_shift;
    renderRoster(view.roster || []);
    var active = document.activeElement, typing = !!(active && active.tagName === "INPUT" && active.value !== "" && $("failures").contains(active));
    if (force || !typing) { renderFailures(view.failures || [], me.on_shift); }       // never redraw under a note that is being typed; always redraw after a press
    renderDone(view.done || []);
  }
  function load() {
    api("/staff/state", {method: "GET"}).then(function (result) { if (result.data.ok) { lastGood = performance.now(); render(result.data); } }).catch(function () { checkStale(); });
  }
  // A page that cannot refresh must say so: a silent failure here is exactly how an alarm gets missed.
  function checkStale() { $("stale").hidden = !staleDue(performance.now(), lastGood, STALE_MS); }
  function noteActivity() { lastActivity = performance.now(); }
  ACTIVITY_EVENTS.forEach(function (name) { document.addEventListener(name, noteActivity, {passive: true, capture: true}); });
  // Two minutes with nothing touched: back to the dashboard (the same address as the link at the top). A monotonic clock, so a PC clock being corrected can neither fire it early nor hold it back.
  function checkIdle() { if (idleDue(performance.now(), lastActivity, IDLE_MS)) { location.replace($("to-dashboard").href); } }
  function act(payload, after) {
    say("Working ...", false);
    api("/staff/act", {method: "POST", body: JSON.stringify(payload)}).then(function (result) {
      if (result.data.ok) { say("Done.", false); render(result.data, true); if (after) { after(); } }
      else { say(result.data.error || "That did not work.", true); }
    }).catch(function () { say("The server could not be reached.", true); });
  }
  $("start").addEventListener("click", function () { act({action: "start_shift"}); });
  $("finish").addEventListener("click", function () { act({action: "finish_shift"}); });
  $("pw-go").addEventListener("click", function () {
    act({action: "change_password", current: $("pw-current").value, "new": $("pw-new").value}, function () { $("pw-current").value = ""; $("pw-new").value = ""; });
  });
  $("out").addEventListener("click", function () { fetch("/staff/logout", {method: "POST", credentials: "same-origin"}).then(function () { location.href = "/control/login"; }); });
  load();
  setInterval(load, 4000);
  setInterval(checkIdle, 1000);
  setInterval(checkStale, 1000);
</script>
</body>
</html>
"""


def staff_page(dashboard_url="/") -> str:
    """The Staff page, ready to serve. `dashboard_url` is where 'Back to the dashboard' (and the return after two idle minutes) goes: the server hands in its setting, and only a plain https
    address is ever used, so nothing an attacker could shape lands in the page; anything else becomes this site's own front page."""
    ok = isinstance(dashboard_url, str) and len(dashboard_url) <= 200 and (dashboard_url == "/" or _DASHBOARD_ADDRESS.fullmatch(dashboard_url))
    return _STAFF_HTML.replace("__STYLE__", control_access.page_style()).replace("__DASHBOARD_URL__", dashboard_url if ok else "/")
