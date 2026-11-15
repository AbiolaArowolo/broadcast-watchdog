"""Read-only live status pages for the Clarix Broadcast Watchdog.

The listener serves two vanilla HTML views:

* / shows six independent playout endpoint tiles.
* /topology shows the same six endpoints as live nodes feeding Core.

Both pages consume the pushed /stream Server-Sent Events feed. The frontend
keeps alarm, mute, selected-node, and expanded-panel state outside the DOM so a
fresh 5-second snapshot can rebuild the visual surface without wiping operator
interaction. Detector data is always written with textContent.
"""
from __future__ import annotations

import hashlib
import json

from app import matrix_pages


# The one Bulldog mark, in a 120x120 box. Straight segments and plain lines only, on purpose:
# the page, the loading screen, the offline screen and ops/make_icons.py (which rasterises the
# PWA icons from this very markup with Pillow) all share it, so they cannot drift apart.
BULLDOG_VIEWBOX = 120
BULLDOG_BODY = (
    '<g class="bd-lines" stroke="#0ea5e9" stroke-width="5" fill="none" stroke-linejoin="round" stroke-linecap="round">'
    '<path d="M25,40 L95,40 L105,65 L95,100 L75,110 L45,110 L25,100 L15,65 Z"/>'
    '<path d="M25,40 L10,30 L15,60"/><path d="M95,40 L110,30 L105,60"/>'
    '<path d="M35,65 L85,65 L75,90 L45,90 Z"/>'
    '<path d="M52,73 L68,73 L60,82 Z" fill="#0ea5e9"/>'
    '<path d="M40,98 L60,92 L80,98"/>'
    '<line x1="60" y1="40" x2="60" y2="55" stroke-width="4"/>'
    '<g class="cyber-eye">'
    '<line x1="38" y1="62" x2="50" y2="66" stroke="#22c55e" stroke-width="6"/>'
    '<line x1="82" y1="62" x2="70" y2="66" stroke="#22c55e" stroke-width="6"/>'
    '</g></g>'
)


def _bulldog_svg(css_class: str) -> str:
    return (
        f'<svg class="{css_class}" xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {BULLDOG_VIEWBOX} {BULLDOG_VIEWBOX}" aria-hidden="true" focusable="false">{BULLDOG_BODY}</svg>'
    )


def _render_html(token: str, page_mode: str) -> str:
    """`token` is the server's own SHARED_TOKEN, embedded for same-origin SSE."""
    return (
        _TEMPLATE
        .replace("__TOKEN_JSON__", json.dumps(token))
        .replace("__PAGE_MODE_JSON__", json.dumps(page_mode))
        .replace("__PAGE_VERSION_JSON__", json.dumps(PAGE_VERSION))
        .replace("__BULLDOG_HEADER__", _bulldog_svg("logo-svg"))
        .replace("__BULLDOG_LOADER__", _bulldog_svg("loader-dog"))
        .replace("__ICON_VERSION__", ICON_VERSION)
    )


# Where the sign-in link goes (the toolbar, in the spot the Test siren and Reconnect stream buttons had). The listener swaps it for the real link, or for nothing when the control phase is off.
STAFF_LINK_MARKER = "<!--staff-link-->"
# Where the unavailable screen is put (hidden) for a dashboard that is already open; the listener swaps it for the real layer, or for nothing when the control phase is off.
OFF_SCREEN_MARKER = "<!--off-screen-->"


def render_dashboard_html(token: str) -> str:
    return _render_html(token, "dashboard")


def render_topology_html(token: str) -> str:
    return _render_html(token, "topology")


# Change this whenever the icons change. A phone keeps its own copy of an installed app's icon and
# launch splash and only refreshes it when the manifest changes; new icon URLs are what make
# Chrome notice and update the installed app (the server ignores the query string).
ICON_VERSION = "bulldog-20261011"

MANIFEST_JSON = json.dumps({
    "name": "Clarix Broadcast Watchdog",
    "short_name": "Watchdog",
    "description": "Live status for the Station playout machines",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#050911",
    "theme_color": "#050911",
    "icons": [
        {"src": f"/icon-192.png?v={ICON_VERSION}", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": f"/icon-512.png?v={ICON_VERSION}", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": f"/icon-512-maskable.png?v={ICON_VERSION}", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
})


# The service worker keeps TWO kinds of thing, both static: the /offline.html placeholder, and the page SHELLS of / and /topology (the HTML and
# script of the app itself). It never stores status data, the stream or any API answer: every number and every name arrives over the live
# stream after the page opens, so a saved shell can never show a stale healthy state. Why the shells are kept: a phone on a weak or ship
# connection, or a brief outage, used to meet "Cannot reach the Watchdog server" for a server that was fine, because a page load that took
# more than 6 seconds was abandoned. Now the app opens at once from its saved shell and says "Live feed lost" (and why) inside itself, then
# fills in by itself when the connection returns. Only a first-ever visit with no saved shell can still land on the offline placeholder.
# Only page navigations are intercepted; every other request goes straight to the network.
SERVICE_WORKER_JS = """\
var CACHE = 'watchdog-shell-v3';
var SHELL_PATHS = ['/', '/topology'];
self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(CACHE)
      .then(function (cache) {
        // the placeholder must be there or the install fails; the shells are best effort (a failure here never blocks the install)
        var shells = Promise.all(SHELL_PATHS.map(function (path) {
          return fetch(path, { cache: 'reload' }).then(function (response) { return response.status === 200 ? cache.put(path, response) : null; }).catch(function () { return null; });
        }));
        return cache.add(new Request('/offline.html', { cache: 'reload' })).then(function () { return shells; });
      })
      .then(function () { return self.skipWaiting(); })
  );
});
self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys()
      .then(function (names) {
        return Promise.all(names.filter(function (name) { return name !== CACHE; }).map(function (name) { return caches.delete(name); }));
      })
      .then(function () { return self.clients.claim(); })
  );
});
function offlineResponse() {
  return caches.match('/offline.html').then(function (cached) {
    return cached || new Response('Clarix Watchdog cannot be reached.', { status: 503, headers: { 'Content-Type': 'text/plain' } });
  });
}
// How long a page load may take before the saved copy is used instead. With a saved shell the wait is short (the app can open at once and
// connect in its own time); with none, a first visit is given longer, because the offline placeholder is the only alternative.
var SHELL_TIMEOUT_MS = 4000;
var FIRST_VISIT_TIMEOUT_MS = 8000;
function withTimeout(promise, ms) {
  return new Promise(function (resolve, reject) {
    var timer = setTimeout(function () { reject(new Error('timeout')); }, ms);
    promise.then(
      function (value) { clearTimeout(timer); resolve(value); },
      function (error) { clearTimeout(timer); reject(error); }
    );
  });
}
self.addEventListener('fetch', function (event) {
  if (event.request.mode !== 'navigate') { return; }
  var path = new URL(event.request.url).pathname;
  var isShell = SHELL_PATHS.indexOf(path) !== -1;
  event.respondWith(
    (isShell ? caches.match(path) : Promise.resolve(null)).then(function (saved) {
      var fallback = function () { return saved || offlineResponse(); };
      if (self.navigator && self.navigator.onLine === false) { return fallback(); }   // a phone that says it is offline: no waiting at all
      var network = fetch(event.request);
      if (isShell) {
        // keep the saved shell current: a slow answer that arrives after the timeout still refreshes it for next time
        network.then(function (response) {
          if (response.status === 200) { var copy = response.clone(); caches.open(CACHE).then(function (cache) { return cache.put(path, copy); }); }
        }, function () {});
      }
      return withTimeout(network, saved ? SHELL_TIMEOUT_MS : FIRST_VISIT_TIMEOUT_MS)
        .then(function (response) { return response.status >= 500 ? fallback() : response; })
        .catch(fallback);
    })
  );
});
"""


# Shown by the service worker when the server cannot be reached. It carries no status data and no
# access key, and it keeps retrying by itself, returning to the real dashboard the moment the server
# answers. It makes exactly two kinds of outside request: the retry of this site's own front page, and a
# small connectivity check (two well-known public addresses, no cookies, no referrer, nothing sent) whose
# only purpose is to tell "this device has no internet" from "the Watchdog server is not answering".
# Without that check a phone on Wi-Fi with no uplink reports that the server is down, which is wrong
# and sends the operator looking at the wrong end.
OFFLINE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Clarix Watchdog - reconnecting</title>
<meta name="theme-color" content="#05070c">
<style>
:root {
  --bg:#05070c; --ink:#eef3fa; --ink-2:#c3cedd; --muted:#8190a6; --faint:#4a566a; --hair:rgba(255,255,255,.10);
  --ice:#6cc8f2; --ice-rgb:108,200,242; --orange:#ff9a2e; --orange-rgb:255,154,46; --green:#3ddc97; --green-rgb:61,220,151;
}
* { box-sizing:border-box; }
html, body { min-height:100%; margin:0; }
body {
  background:
    radial-gradient(900px 520px at 50% -8%, rgba(var(--ice-rgb),.13), transparent 62%),
    radial-gradient(700px 420px at 100% 112%, rgba(var(--orange-rgb),.09), transparent 60%),
    var(--bg);
  color:var(--ink);
  font-family:Inter,"SF Pro Display",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;
  display:grid;
  place-items:center;
  min-height:100vh;
  padding:28px 18px;
  text-align:center;
}
main { width:min(440px, 100%); }
.dog-wrap { position:relative; width:128px; height:128px; margin:0 auto 20px; display:grid; place-items:center; }
.ring { position:absolute; inset:0; border-radius:50%; border:1px solid rgba(var(--orange-rgb),.5); }
.ring.two { inset:-15px; border-color:rgba(var(--orange-rgb),.2); }
.dog { width:88px; height:88px; filter:drop-shadow(0 0 12px rgba(var(--orange-rgb),.45)); }
.dog .bd-lines { stroke:var(--orange); }
.dog .bd-lines path[fill="#0ea5e9"] { fill:var(--orange); }
h1 { margin:0; font-size:25px; font-weight:650; letter-spacing:.01em; }
.state { margin:16px 0 0; font-size:17px; font-weight:700; color:var(--orange); }
.why { margin:9px auto 0; max-width:36ch; color:var(--ink-2); font-size:14px; line-height:1.55; }
.chain { display:grid; grid-template-columns:repeat(3, 1fr); width:min(390px, 100%); margin:26px auto 0; padding:0; list-style:none; }
.node { position:relative; padding-top:24px; }
.node::before { content:""; position:absolute; left:-50%; right:50%; top:6px; height:2px; background:var(--hair); }
.node:first-child::before { display:none; }
.node[data-state="fail"]::before { background:linear-gradient(90deg, var(--hair), rgba(var(--orange-rgb),.7)); }
.dot { position:absolute; left:50%; top:0; width:14px; height:14px; margin-left:-7px; border-radius:50%; background:var(--faint); box-shadow:0 0 0 4px var(--bg), 0 0 0 5px var(--hair); }
.node[data-state="ok"] .dot { background:var(--green); box-shadow:0 0 0 4px var(--bg), 0 0 0 5px rgba(var(--green-rgb),.5), 0 0 16px rgba(var(--green-rgb),.55); }
.node[data-state="fail"] .dot { background:var(--orange); box-shadow:0 0 0 4px var(--bg), 0 0 0 5px rgba(var(--orange-rgb),.55), 0 0 16px rgba(var(--orange-rgb),.55); }
.node-name { font-size:10px; font-weight:700; letter-spacing:.1em; text-transform:uppercase; color:var(--muted); white-space:nowrap; }
@media (max-width:360px) { .node-name { white-space:normal; } }
.node-state { margin-top:3px; font-size:12.5px; color:var(--ink-2); }
.bar { height:3px; border-radius:2px; background:rgba(255,255,255,.07); margin:26px auto 11px; overflow:hidden; width:min(260px, 80%); }
.bar span { display:block; height:100%; width:40%; background:linear-gradient(90deg, transparent, var(--orange), transparent); }
.try { margin:0; color:var(--muted); font-size:12.5px; min-height:18px; font-variant-numeric:tabular-nums; }
button {
  margin-top:18px; min-height:46px; padding:10px 26px; border-radius:12px; cursor:pointer;
  border:1px solid rgba(var(--orange-rgb),.55); background:rgba(var(--orange-rgb),.12); color:var(--ink); font:inherit; font-weight:650;
  transition:background .16s ease, border-color .16s ease;
}
button:hover { background:rgba(var(--orange-rgb),.2); border-color:rgba(var(--orange-rgb),.8); }
button:focus-visible, a:focus-visible { outline:3px solid var(--ice); outline-offset:3px; }
.brand { margin:34px auto 0; padding-top:18px; border-top:1px solid var(--hair); display:grid; gap:5px; justify-items:center; }
.brand-mark { font-size:12px; font-weight:750; letter-spacing:.34em; text-indent:.34em; color:var(--orange); }
.brand-tag { color:var(--muted); font-size:12px; line-height:1.5; max-width:38ch; }
.brand-link { color:var(--ice); font-size:12.5px; text-decoration:none; letter-spacing:.04em; padding:6px 4px; }
.brand-link:hover { text-decoration:underline; }
.cyber-eye { animation:bdScan 3s ease-in-out infinite, bdBlink 6s infinite; }
@keyframes bdScan { 0%,100% { transform:translateX(-2px); } 50% { transform:translateX(2px); } }
@keyframes bdBlink { 0%,96%,98%,100% { opacity:1; } 97%,99% { opacity:0; } }
@media (prefers-reduced-motion:no-preference) {
  .ring { animation:ringPulse 2.6s ease-out infinite; }
  .ring.two { animation-delay:1.3s; }
  .bar span { animation:slide 1.6s ease-in-out infinite; }
  .dog { animation:breathe 3.4s ease-in-out infinite; }
  .node[data-state="unknown"] .dot { animation:edge 1.4s ease-in-out infinite; }
}
@media (prefers-reduced-motion:reduce) { .cyber-eye { animation:none; } }
@keyframes ringPulse { 0% { transform:scale(.84); opacity:.9; } 100% { transform:scale(1.18); opacity:0; } }
@keyframes slide { 0% { transform:translateX(-120%); } 100% { transform:translateX(320%); } }
@keyframes breathe { 0%,100% { transform:scale(1); } 50% { transform:scale(1.04); } }
@keyframes edge { 50% { opacity:.35; } }
</style>
</head>
<body>
<main>
  <div class="dog-wrap"><span class="ring"></span><span class="ring two"></span>__BULLDOG_OFFLINE__</div>
  <h1>Clarix Watchdog</h1>
  <p class="state" id="state" role="status" aria-live="polite">Checking the connection</p>
  <p class="why" id="why">Working out whether the problem is this device's internet or the Watchdog server.</p>
  <ol class="chain" aria-label="Connection check">
    <li class="node" id="node-device" data-state="unknown"><span class="dot"></span><div class="node-name">This device</div><div class="node-state" id="state-device">Checking</div></li>
    <li class="node" id="node-internet" data-state="unknown"><span class="dot"></span><div class="node-name">Internet</div><div class="node-state" id="state-internet">Checking</div></li>
    <li class="node" id="node-server" data-state="unknown"><span class="dot"></span><div class="node-name">Watchdog server</div><div class="node-state" id="state-server">Checking</div></li>
  </ol>
  <div class="bar" aria-hidden="true"><span></span></div>
  <p class="try" id="try">Reconnecting...</p>
  <button type="button" id="retry">Try now</button>
  <footer class="brand">
    <span class="brand-mark">CLARIXTECH GROUP</span>
    <span class="brand-tag">Resilient systems for broadcast, automation, and field infrastructure.</span>
    <a class="brand-link" href="https://corp.example" target="_blank" rel="noopener noreferrer">corp.example</a>
  </footer>
</main>
<script>
(function () {
  "use strict";
  // Two unrelated, always-on public addresses: the page counts the internet as reachable if EITHER answers.
  // The answer is never read (no-cors), nothing is sent with the request, and it is only used to word the message.
  var PROBES = ["https://www.gstatic.com/generate_204", "https://www.cloudflare.com/cdn-cgi/trace"];
  var PROBE_TIMEOUT_MS = 4000;
  var SERVER_TIMEOUT_MS = 8000;
  var attempts = 0;
  var since = Date.now();
  var delays = [5000, 5000, 10000, 10000, 15000, 30000];
  var timer = null;
  var internet = null;          // null: not known yet; true: the public internet answers; false: it does not
  var stateEl = document.getElementById("state");
  var whyEl = document.getElementById("why");
  var tryEl = document.getElementById("try");

  function offlineFor() {
    var s = Math.round((Date.now() - since) / 1000);
    return s < 60 ? s + "s" : Math.floor(s / 60) + "m " + (s % 60) + "s";
  }

  function withTimeout(url, options, ms) {
    var controller = window.AbortController ? new AbortController() : null;
    var stop = setTimeout(function () { if (controller) { controller.abort(); } }, ms);
    if (controller) { options.signal = controller.signal; }
    return fetch(url, options).then(
      function (response) { clearTimeout(stop); return response; },
      function (error) { clearTimeout(stop); throw error; }
    );
  }

  function probeOne(url) {
    var options = { mode: "no-cors", cache: "no-store", credentials: "omit", referrerPolicy: "no-referrer" };
    return withTimeout(url + "?_=" + Date.now(), options, PROBE_TIMEOUT_MS).then(function () { return true; }, function () { return false; });
  }

  function probeInternet() {
    return new Promise(function (resolve) {
      var pending = PROBES.length;
      PROBES.forEach(function (url) {
        probeOne(url).then(function (reachable) {
          if (reachable) { resolve(true); return; }
          pending -= 1;
          if (pending === 0) { resolve(false); }
        });
      });
    });
  }

  function setNode(name, state, text) {
    document.getElementById("node-" + name).setAttribute("data-state", state);
    document.getElementById("state-" + name).textContent = text;
  }

  function describe() {
    var deviceOnline = navigator.onLine !== false;
    setNode("device", deviceOnline ? "ok" : "fail", deviceOnline ? "Online" : "Offline");
    if (!deviceOnline) {
      setNode("internet", "fail", "No access");
      setNode("server", "unknown", "Unknown");
      stateEl.textContent = "This device is offline";
      whyEl.textContent = "Turn on Wi-Fi or mobile data. The dashboard reloads by itself as soon as the connection is back.";
    } else if (internet === false) {
      setNode("internet", "fail", "No access");
      setNode("server", "unknown", "Unknown");
      stateEl.textContent = "No internet connection";
      whyEl.textContent = "This device has a network link, but the internet is not answering, for example Wi-Fi with no uplink. The Watchdog server cannot be checked until it is back. This page returns to the dashboard by itself.";
    } else if (internet === true) {
      setNode("internet", "ok", "Reachable");
      setNode("server", "fail", "Not answering");
      stateEl.textContent = "Cannot reach the Watchdog server";
      whyEl.textContent = "Your internet works, but the Watchdog server did not answer. It is retrying automatically.";
    } else {
      setNode("internet", "unknown", "Checking");
      setNode("server", "unknown", "Checking");
      stateEl.textContent = "Checking the connection";
      whyEl.textContent = "Working out whether the problem is this device's internet or the Watchdog server.";
    }
    tryEl.textContent = "Reconnecting - attempt " + (attempts + 1) + " - waiting " + offlineFor();
  }

  function check() {
    clearTimeout(timer);
    attempts += 1;
    tryEl.textContent = "Trying now - attempt " + attempts + " - waiting " + offlineFor();
    var probing = probeInternet();
    withTimeout("/", { cache: "no-store" }, SERVER_TIMEOUT_MS)
      .then(function (response) {
        if (response.ok) { window.location.replace("/"); return; }
        throw new Error("status " + response.status);
      })
      .catch(function () {
        return probing.then(function (reachable) {
          internet = reachable;
          describe();
          timer = setTimeout(check, delays[Math.min(attempts, delays.length - 1)]);
        });
      });
  }

  document.getElementById("retry").addEventListener("click", check);
  window.addEventListener("online", check);
  window.addEventListener("offline", describe);
  setInterval(describe, 1000);
  describe();
  probeInternet().then(function (reachable) { internet = reachable; describe(); });
  timer = setTimeout(check, 2500);
})();
</script>
</body>
</html>
""".replace("__BULLDOG_OFFLINE__", _bulldog_svg("dog"))


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Clarix Broadcast Watchdog</title>
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icon-192.png?v=__ICON_VERSION__" type="image/png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png?v=__ICON_VERSION__">
<meta name="theme-color" content="#050911">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Watchdog">
<style>
:root {
  --bg:#05070c;
  --bg-2:#080c14;
  --surface:#0a0f18;
  --surface-2:#0f1621;
  --surface-3:#151e2c;
  --line:#26354a;
  --line-soft:#182233;
  --hair:rgba(255,255,255,.085);
  --hair-2:rgba(255,255,255,.15);
  --text:#eef3fa;
  --text-2:#c3cedd;
  --muted:#8190a6;
  --faint:#566377;
  --blue:#6cc8f2;
  --blue-rgb:108,200,242;
  --green:#3ddc97;
  --green-rgb:61,220,151;
  --yellow:#f5c542;
  --yellow-rgb:245,197,66;
  --orange:#ff9a2e;
  --orange-rgb:255,154,46;
  --red:#ff4d5e;
  --red-rgb:255,77,94;
  --violet:#b79cff;      /* the Site C feed card only: no other part of the page uses it, so the card stands apart from the status colours */
  --violet-rgb:183,156,255;
  --radius:14px;
  --side:340px;   /* the right-hand column: the Core panel above and the incident feed below share it */
  --gap:16px;     /* the gap between cards: counters above and playout tiles below share it, so their columns line up */
  --radius-sm:10px;
  --shadow:0 18px 48px rgba(0,0,0,.42);
  --font:Inter,"SF Pro Display",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
  --mono:ui-monospace,"SF Mono","JetBrains Mono",Consolas,"Liberation Mono",monospace;
}
* { box-sizing:border-box; }
html, body { min-height:100%; }
body {
  margin:0;
  color:var(--text);
  font-family:var(--font);
  font-variant-numeric:tabular-nums;
  -webkit-font-smoothing:antialiased;
  text-rendering:optimizeLegibility;
  background:
    radial-gradient(1100px 620px at 10% -12%, rgba(var(--blue-rgb),.11), transparent 62%),
    radial-gradient(900px 520px at 100% -4%, rgba(var(--orange-rgb),.065), transparent 64%),
    linear-gradient(rgba(255,255,255,.02) 1px, transparent 1px) 0 0 / 56px 56px,
    linear-gradient(90deg, rgba(255,255,255,.02) 1px, transparent 1px) 0 0 / 56px 56px,
    var(--bg);
}
a { color:inherit; text-decoration:none; }
button {
  appearance:none;
  border:1px solid var(--hair-2);
  border-radius:var(--radius-sm);
  background:rgba(255,255,255,.035);
  color:var(--text-2);
  cursor:pointer;
  font:inherit;
  font-size:13px;
  font-weight:600;
  min-height:44px;
  padding:9px 15px;
  transition:background .18s ease, border-color .18s ease, color .18s ease;
}
button:hover { border-color:rgba(var(--blue-rgb),.55); color:var(--text); background:rgba(var(--blue-rgb),.08); }
button:focus-visible, a:focus-visible { outline:3px solid var(--blue); outline-offset:3px; }
button[aria-pressed="true"] { border-color:rgba(var(--orange-rgb),.55); color:var(--text); background:rgba(var(--orange-rgb),.12); }
.eyebrow { display:block; font-size:10.5px; font-weight:650; letter-spacing:.16em; text-transform:uppercase; color:var(--muted); }
.shell { width:min(1440px, 100%); margin:0 auto; padding:22px clamp(14px, 2.2vw, 32px) 30px; }
.topbar {
  display:grid;
  grid-template-columns:minmax(220px, 1fr) auto minmax(220px, 1fr);
  gap:16px;
  align-items:center;
  margin-bottom:22px;
}
.brandline { display:flex; align-items:center; gap:14px; min-width:0; text-decoration:none; }
.logo-svg {
  width:42px;
  height:42px;
  flex:none;
  filter:drop-shadow(0 0 8px rgba(14,165,233,.55));
}
.brand-text { min-width:0; }
.brand-sub { display:block; margin-top:5px; font-size:10px; font-weight:700; letter-spacing:.24em; text-transform:uppercase; color:var(--orange); }
.cyber-eye { animation:bdScan 3s ease-in-out infinite, bdBlink 6s infinite; }
@keyframes bdScan { 0%,100% { transform:translateX(-2px); } 50% { transform:translateX(2px); } }
@keyframes bdBlink { 0%,96%,98%,100% { opacity:1; } 97%,99% { opacity:0; } }
.preloader {
  position:fixed;
  inset:0;
  z-index:9999;
  display:grid;
  place-items:center;
  padding:24px 18px;
  text-align:center;
  background:radial-gradient(circle at 50% 32%, rgba(var(--blue-rgb),.14), transparent 46%), var(--bg);
  transition:opacity .45s ease, visibility .45s ease;
}
.preloader.done { opacity:0; visibility:hidden; pointer-events:none; }
.preloader-inner { width:min(380px, 100%); }
.loader-wrap { position:relative; width:150px; height:150px; margin:0 auto 18px; display:grid; place-items:center; }
.loader-ring { position:absolute; inset:0; border-radius:50%; border:1px solid rgba(var(--blue-rgb),.5); }
.loader-ring.two { inset:-16px; border-color:rgba(var(--blue-rgb),.22); }
.loader-dog { width:104px; height:104px; filter:drop-shadow(0 0 12px rgba(14,165,233,.7)); }
.loader-title { margin:0; font-size:27px; font-weight:650; letter-spacing:.01em; }
.loader-brand { margin:8px 0 0; color:var(--orange); font-size:10.5px; font-weight:700; letter-spacing:.28em; text-transform:uppercase; }
.loader-step { margin:14px 0 0; color:var(--text-2); font-size:14px; min-height:20px; }
.loader-bar { height:3px; border-radius:2px; background:rgba(255,255,255,.08); margin:18px auto 0; overflow:hidden; width:min(260px, 80%); }
.loader-bar span { display:block; height:100%; width:40%; background:linear-gradient(90deg, transparent, var(--blue), transparent); }
.loader-skip { margin-top:20px; display:none; }
.preloader.slow .loader-skip { display:inline-block; }
@media (prefers-reduced-motion:no-preference) {
  .loader-ring { animation:loaderPulse 2.4s ease-out infinite; }
  .loader-ring.two { animation-delay:1.2s; }
  .loader-dog { animation:loaderBreathe 3.2s ease-in-out infinite; }
  .loader-bar span { animation:loaderSlide 1.5s ease-in-out infinite; }
}
@keyframes loaderPulse { 0% { transform:scale(.82); opacity:.9; } 100% { transform:scale(1.18); opacity:0; } }
@keyframes loaderBreathe { 0%,100% { transform:scale(1); } 50% { transform:scale(1.04); } }
@keyframes loaderSlide { 0% { transform:translateX(-120%); } 100% { transform:translateX(320%); } }
h1 { margin:0; font-size:clamp(20px, 2.4vw, 26px); font-weight:650; line-height:1.1; letter-spacing:.01em; }
.nav {
  display:flex;
  gap:2px;
  padding:3px;
  border:1px solid var(--hair);
  border-radius:999px;
  background:rgba(255,255,255,.03);
}
.nav a {
  display:inline-flex;
  align-items:center;
  min-height:44px;
  border-radius:999px;
  color:var(--muted);
  font-size:13px;
  font-weight:600;
  padding:0 20px;
  transition:color .18s ease, background .18s ease;
}
.nav a:hover { color:var(--text); }
.nav a.active { color:var(--text); background:rgba(255,255,255,.08); box-shadow:inset 0 1px 0 rgba(255,255,255,.08); }
.topbar-right { display:flex; align-items:center; justify-content:flex-end; gap:18px; min-width:0; }
.clocks { display:flex; flex-wrap:wrap; justify-content:flex-end; align-items:flex-end; gap:6px 22px; min-width:0; }
.clock-chip { display:grid; gap:3px; text-align:right; }
.clock { color:var(--text); font-size:15px; font-weight:600; letter-spacing:.03em; white-space:nowrap; }
.clock-chip.is-stale .clock { color:var(--muted); }
.clock-skew { flex-basis:100%; text-align:right; color:var(--yellow); font-size:11.5px; line-height:1.4; }
.standby-strip { margin:0 0 var(--gap); padding:9px 16px; border-radius:var(--radius-sm); font-size:13px; font-weight:600; letter-spacing:.01em; text-align:center; line-height:1.4; }
.standby-strip[hidden] { display:none; }
.standby-up { background:rgba(var(--blue-rgb),.14); border:1px solid rgba(var(--blue-rgb),.45); color:var(--blue); }
.standby-down { background:rgba(var(--red-rgb),.18); border:1px solid rgba(var(--red-rgb),.65); color:#ffd9dd; }
/* site_c-feed */
.vz { margin:0 0 var(--gap); border:1px solid rgba(var(--violet-rgb),.5); border-left:4px solid var(--violet); border-radius:var(--radius); background:linear-gradient(180deg, rgba(var(--violet-rgb),.12), rgba(var(--violet-rgb),.03) 160px), var(--surface); }
.vz[hidden] { display:none; }
.vz-summary { display:flex; align-items:center; flex-wrap:wrap; gap:14px; min-height:44px; padding:10px 18px; cursor:pointer; list-style:none; }
.vz-summary::-webkit-details-marker { display:none; }
.vz-summary::before { content:""; flex:none; width:10px; height:10px; border-radius:50%; background:var(--muted); }
.vz-summary:focus-visible { outline:2px solid var(--violet); outline-offset:2px; }
.vz-summary .eyebrow { color:var(--violet); font-weight:700; }
.vz-headline { font-size:14px; font-weight:600; color:var(--text); overflow-wrap:anywhere; }
.vz.vz-ok > .vz-summary::before { background:var(--green); }
.vz.vz-warning > .vz-summary::before { background:var(--yellow); }
.vz.vz-critical > .vz-summary::before { background:var(--red); }
.vz.vz-critical { border-color:rgba(var(--red-rgb),.65); border-left-color:var(--red); }
.vz-body { display:grid; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr)); gap:var(--gap); padding:4px 18px 18px; }
.vz-tile { min-width:0; padding:14px 16px; border:1px solid rgba(var(--violet-rgb),.3); border-radius:var(--radius-sm); background:rgba(var(--violet-rgb),.07); }
.vz-tile.vz-warning { border-color:rgba(var(--yellow-rgb),.55); }
.vz-tile.vz-critical { border-color:rgba(var(--red-rgb),.65); }
.vz-name { display:flex; justify-content:space-between; align-items:baseline; gap:10px; }
.vz-name .eyebrow { color:var(--violet); font-weight:700; }
.vz-word { font-size:12px; font-weight:700; letter-spacing:.1em; color:var(--text-2); }
.vz-tile.vz-ok .vz-word { color:var(--green); }
.vz-tile.vz-warning .vz-word { color:var(--yellow); }
.vz-tile.vz-critical .vz-word { color:var(--red); }
.vz-addr { margin-top:4px; font-family:var(--mono); font-size:14px; font-weight:600; color:var(--text); overflow-wrap:anywhere; }
.vz-rate { margin-top:8px; font-size:30px; font-weight:300; letter-spacing:-.02em; color:var(--text); }
.vz-rate i { margin-left:6px; font-style:normal; font-size:14px; color:var(--muted); }
.vz-age { margin-top:2px; color:var(--muted); font-size:12px; }
.vz-note { margin-top:6px; color:var(--muted); font-size:12px; line-height:1.5; overflow-wrap:anywhere; }
.vz-chart { display:block; width:100%; height:96px; margin-top:10px; border-radius:6px; background:rgba(var(--violet-rgb),.06); }
.vz-chart-note { margin-top:4px; color:var(--muted); font-size:11.5px; line-height:1.5; }
.vz-spark { display:block; width:100%; height:48px; margin-top:8px; }
.vz-line { fill:none; stroke:var(--violet); stroke-width:1.8; vector-effect:non-scaling-stroke; }
.vz-floor { fill:none; stroke:var(--red); stroke-width:1; stroke-dasharray:4 4; opacity:.6; vector-effect:non-scaling-stroke; }
.vz-usual { fill:none; stroke:var(--violet); stroke-width:1; stroke-dasharray:2 5; opacity:.45; vector-effect:non-scaling-stroke; }
.vz-facts { display:grid; grid-template-columns:auto 1fr; gap:4px 14px; margin:10px 0 0; font-size:12px; line-height:1.5; }
.vz-facts dt { color:var(--muted); }
.vz-facts dd { margin:0; color:var(--text); text-align:right; overflow-wrap:anywhere; }
.vz-day { margin-top:8px; }
.vz-day > summary { display:flex; align-items:center; min-height:32px; cursor:pointer; color:var(--text-2); font-size:12px; }
.vz-dot { fill:none; stroke:var(--violet); stroke-width:4; stroke-linecap:round; vector-effect:non-scaling-stroke; }
.vz-tools { display:flex; align-items:center; flex-wrap:wrap; gap:8px; padding:0 18px 4px; }
.vz-tools-label { color:var(--muted); font-size:12px; margin-right:4px; }
.vz-win { min-height:44px; min-width:44px; padding:0 16px; border:1px solid rgba(var(--violet-rgb),.45); border-radius:999px; background:transparent; color:var(--text-2); font:inherit; font-size:12px; font-weight:600; cursor:pointer; }
.vz-win[aria-pressed="true"] { background:rgba(var(--violet-rgb),.22); border-color:var(--violet); color:var(--text); }
.vz-win:focus-visible { outline:2px solid var(--violet); outline-offset:2px; }
/* end site_c-feed */
__MATRIX_TILES_STYLE__
.incident-zone { float:right; color:var(--muted); font-weight:600; letter-spacing:.1em; }
.live-indicator {
  display:flex;
  align-items:center;
  justify-content:flex-end;
  gap:8px;
  color:var(--muted);
  font-size:11px;
  font-weight:700;
  letter-spacing:.16em;
  text-transform:uppercase;
  white-space:nowrap;
}
.live-dot {
  width:8px;
  height:8px;
  border-radius:50%;
  background:var(--yellow);
  box-shadow:0 0 0 1px rgba(var(--yellow-rgb),.3);
}
.live-dot.connected { background:var(--green); box-shadow:0 0 0 1px rgba(var(--green-rgb),.35), 0 0 16px rgba(var(--green-rgb),.5); }
.live-dot.down { background:var(--orange); box-shadow:0 0 0 1px rgba(var(--orange-rgb),.4), 0 0 16px rgba(var(--orange-rgb),.4); }
@media (prefers-reduced-motion:no-preference) {
  .live-dot.connected { animation:livePulse 2.3s ease-in-out infinite; }
}
@keyframes livePulse { 0%,100% { opacity:1; } 50% { opacity:.42; } }
.toolbar {
  display:flex;
  flex-wrap:wrap;
  gap:8px;
  align-items:center;
  margin-bottom:18px;
}
.toolbar button { background:transparent; font-size:12.5px; padding:8px 14px; }
.toolbar a.staff-link {
  display:inline-flex; align-items:center; box-sizing:border-box; min-height:44px; padding:8px 14px;
  border:1px solid var(--hair-2); border-radius:var(--radius-sm); background:transparent; color:var(--text-2);
  font-size:12.5px; font-weight:600; transition:background .18s ease, border-color .18s ease, color .18s ease;
}
.toolbar a.staff-link:hover { border-color:rgba(var(--blue-rgb),.55); color:var(--text); background:rgba(var(--blue-rgb),.08); }
.toolbar .shift-chip {
  display:inline-flex; align-items:center; min-height:44px; padding:8px 14px; box-sizing:border-box;
  border:1px solid var(--hair-2); border-radius:var(--radius-sm); color:var(--text-2); font-size:12.5px; font-weight:600;
}
.toolbar .shift-chip[hidden] { display:none; }
.toolbar .spacer { flex:1; }
/* Says out loud when the siren cannot be heard: sound off, silenced, or locked by the browser. */
.siren-strip {
  display:block;
  width:100%;
  margin:-4px 0 18px;
  padding:12px 16px;
  min-height:44px;
  border-radius:var(--radius-sm);
  border:1px solid rgba(var(--orange-rgb),.55);
  background:rgba(var(--orange-rgb),.14);
  color:#ffd9a8;
  font:inherit;
  font-size:14px;
  font-weight:700;
  line-height:1.35;
  text-align:left;
  cursor:pointer;
}
.siren-strip[hidden] { display:none; }
.siren-strip.alarm { border-color:#ff4d5e; background:rgba(255,77,94,.24); color:#fff; }
@media (prefers-reduced-motion:no-preference) {
  .siren-strip.alarm { animation:sirenStrip 1s steps(2,end) infinite; }
}
@keyframes sirenStrip { 50% { background:rgba(255,77,94,.55); } }
/* Open critical failures with the display names that hold them (the staff section). Hidden unless a failure is open. */
.staff-alert {
  display:block; margin:0 0 18px; padding:12px 16px; border-radius:var(--radius-sm);
  border:1px solid #ff4d5e; background:rgba(255,77,94,.18); color:#ffe3e6; font-size:14px; font-weight:600; line-height:1.4;
}
.staff-alert[hidden] { display:none; }
.staff-alert .staff-alert-title { font-weight:800; color:#fff; margin-bottom:4px; }
.staff-alert a { display:inline-block; margin-top:6px; color:#fff; font-weight:700; text-decoration:underline; }
.pill {
  border:1px solid var(--hair);
  border-radius:999px;
  color:var(--muted);
  font-size:12px;
  font-weight:600;
  padding:8px 13px;
  background:rgba(255,255,255,.03);
}
/* One surface for every panel: a hairline edge, a faint top light and a soft drop, so the page reads as glass over the dark. */
.banner, .counter, .vitals, .endpoint-tile, .incident-feed, .topology-wrap {
  border:1px solid var(--hair);
  border-radius:var(--radius);
  background:linear-gradient(180deg, rgba(255,255,255,.045), rgba(255,255,255,.012)), rgba(9,14,23,.88);
  box-shadow:var(--shadow), inset 0 1px 0 rgba(255,255,255,.06);
}
.overview {
  display:grid;
  grid-template-columns:minmax(0, 1fr) var(--side);
  gap:var(--gap);
  align-items:stretch;
  margin-bottom:16px;
}
.overview-main { display:grid; grid-template-rows:auto minmax(0, 1fr); gap:var(--gap); min-width:0; }
.banner {
  --tone:var(--green-rgb);
  display:grid;
  grid-template-columns:auto minmax(0, 1fr) auto;
  gap:18px;
  align-items:center;
  padding:20px 24px;
  position:relative;
  overflow:hidden;
}
.banner::before { content:""; position:absolute; inset:0; background:radial-gradient(560px 170px at 0% 0%, rgba(var(--tone),.15), transparent 70%); pointer-events:none; }
.banner > * { position:relative; }
.banner.status-warning { --tone:var(--yellow-rgb); }
.banner.status-attention { --tone:var(--orange-rgb); }
.banner.status-critical { --tone:var(--red-rgb); border-color:rgba(var(--red-rgb),.55); }
.banner.status-blind { --tone:150,160,176; }
.banner-orb {
  width:48px;
  height:48px;
  border-radius:50%;
  border:1px solid rgba(var(--tone),.6);
  display:grid;
  place-items:center;
  box-shadow:0 0 30px rgba(var(--tone),.3), inset 0 0 16px rgba(var(--tone),.2);
}
.banner-orb::after { content:""; width:14px; height:14px; border-radius:50%; background:rgb(var(--tone)); box-shadow:0 0 16px rgba(var(--tone),.95); }
@media (prefers-reduced-motion:no-preference) {
  .banner.status-critical .banner-orb { animation:orbAlarm 1.2s ease-in-out infinite; }
}
@keyframes orbAlarm { 50% { box-shadow:0 0 52px rgba(var(--red-rgb),.8), inset 0 0 22px rgba(var(--red-rgb),.4); } }
.banner-title { font-size:clamp(18px, 2.2vw, 24px); font-weight:650; color:var(--text); line-height:1.2; }
.banner.status-critical .banner-title { color:#ffd9dd; }
.banner-sub { color:var(--text-2); font-size:13px; line-height:1.5; margin-top:5px; }
.banner-time { color:var(--muted); font-size:12px; white-space:nowrap; text-align:right; }
.counters { display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); grid-auto-rows:1fr; gap:var(--gap); }
.counter { display:flex; flex-direction:column; padding:16px 18px 15px; min-width:0; }
.counter-value { display:flex; align-items:baseline; gap:7px; margin-top:auto; padding-top:11px; font-size:clamp(28px, 3.1vw, 38px); line-height:1; letter-spacing:-.02em; color:var(--text); }
.counter-value b { font-weight:300; }
.counter-value i { font-style:normal; font-size:15px; font-weight:500; letter-spacing:0; color:var(--muted); }
.counter-note { margin-top:10px; color:var(--muted); font-size:12px; line-height:1.4; min-height:17px; overflow-wrap:anywhere; }
.counter.tone-good .counter-value b { color:var(--green); }
.counter.tone-watch .counter-value b { color:var(--orange); }
.counter.tone-alarm .counter-value b { color:var(--red); text-shadow:0 0 24px rgba(var(--red-rgb),.5); }
.vitals { padding:18px 20px 16px; display:grid; gap:15px; align-content:start; min-width:0; }
.vitals-left, .vitals-right { display:grid; gap:15px; align-content:start; min-width:0; }
.vitals-head { display:flex; justify-content:space-between; align-items:baseline; gap:10px; }
.vitals-up { color:var(--muted); font-size:12px; white-space:nowrap; }
.vitals-head .eyebrow { white-space:nowrap; letter-spacing:.11em; font-size:10px; }
.vitals-main { display:grid; grid-template-columns:auto minmax(0, 1fr); gap:20px; align-items:center; }
.gauge { position:relative; width:120px; height:120px; flex:none; }
.gauge svg { width:100%; height:100%; transform:rotate(-90deg); display:block; }
.gauge-track { fill:none; stroke:rgba(255,255,255,.07); stroke-width:9; }
.gauge-arc {
  fill:none; stroke:var(--green); stroke-width:9; stroke-linecap:round;
  stroke-dasharray:289.03; stroke-dashoffset:289.03;
  transition:stroke-dashoffset .9s cubic-bezier(.22,.8,.3,1), stroke .4s ease;
  filter:drop-shadow(0 0 6px rgba(var(--green-rgb),.55));
}
.gauge.warn .gauge-arc { stroke:var(--yellow); filter:drop-shadow(0 0 6px rgba(var(--yellow-rgb),.55)); }
.gauge.hot .gauge-arc { stroke:var(--red); filter:drop-shadow(0 0 7px rgba(var(--red-rgb),.65)); }
.gauge-readout { position:absolute; inset:0; display:grid; place-content:center; text-align:center; }
.gauge-readout b { font-size:32px; font-weight:300; letter-spacing:-.02em; line-height:1; }
.gauge-readout span { margin-top:7px; }
.vitals-side { display:grid; gap:14px; min-width:0; }
.vstat b { display:block; margin-top:5px; font-size:21px; font-weight:400; letter-spacing:-.01em; }
.vstat i { display:block; margin-top:2px; font-style:normal; color:var(--muted); font-size:11.5px; }
.meter { display:grid; grid-template-columns:92px minmax(0, 1fr) 48px; gap:10px; align-items:center; }
.meter .eyebrow { letter-spacing:.12em; white-space:nowrap; }
.meter-track { height:6px; border-radius:3px; background:rgba(255,255,255,.07); overflow:hidden; }
.meter-track span { display:block; height:100%; width:0; border-radius:3px; background:linear-gradient(90deg, rgba(var(--blue-rgb),.5), rgb(var(--blue-rgb))); transition:width .9s cubic-bezier(.22,.8,.3,1); }
.meter.warn .meter-track span { background:linear-gradient(90deg, rgba(var(--yellow-rgb),.5), rgb(var(--yellow-rgb))); }
.meter.hot .meter-track span { background:linear-gradient(90deg, rgba(var(--red-rgb),.5), rgb(var(--red-rgb))); }
.meter b { font-size:12.5px; font-weight:600; color:var(--text-2); text-align:right; }
.spark-wrap { padding-top:4px; }
.spark { width:100%; height:48px; display:block; }
.spark-area { fill:url(#sparkFill); }
.spark-line { fill:none; stroke:rgb(var(--blue-rgb)); stroke-width:1.6; stroke-linejoin:round; stroke-linecap:round; }
.spark-caption { margin-top:6px; }
.vitals-note { color:var(--faint); font-size:11.5px; line-height:1.45; }
.mcr-layout {
  display:grid;
  grid-template-columns:minmax(0, 1fr) var(--side);
  gap:var(--gap);
  align-items:start;
}
.endpoint-grid {
  display:grid;
  grid-template-columns:repeat(3, minmax(0, 1fr));
  gap:var(--gap);
  align-items:stretch;
}
/* While a tile's details are open the row's other tiles keep their own height instead of stretching to match it. */
.endpoint-grid:has(.endpoint-tile.is-open) { align-items:start; }
.endpoint-tile {
  --tone:var(--green-rgb);
  position:relative;
  min-width:0;
  overflow:hidden;
  display:flex;
  flex-direction:column;
  transition:border-color .2s ease, box-shadow .2s ease;
}
.endpoint-tile::before { content:""; position:absolute; left:0; right:0; top:0; height:2px; background:linear-gradient(90deg, transparent, rgba(var(--tone),.9), transparent); }
.endpoint-tile:hover { border-color:rgba(var(--tone),.42); }
.endpoint-face {
  display:flex;
  flex-direction:column;
  flex:1 1 auto;
  width:100%;
  min-height:0;
  padding:0;
  text-align:left;
  border:0;
  border-radius:0;
  background:transparent;
  color:inherit;
}
.endpoint-face:hover { background:transparent; }
.tile-top {
  display:flex;
  justify-content:space-between;
  align-items:flex-start;
  gap:10px;
  padding:17px 18px 13px;
}
.endpoint-name { font-size:16px; font-weight:650; letter-spacing:.03em; }
.endpoint-meta { color:var(--muted); font-size:10.5px; font-weight:650; letter-spacing:.14em; margin-top:5px; text-transform:uppercase; }
.badge {
  --badge-tone:var(--green-rgb);
  display:inline-flex;
  align-items:center;
  border:1px solid rgba(var(--badge-tone),.45);
  border-radius:999px;
  background:rgba(var(--badge-tone),.08);
  color:rgb(var(--badge-tone));
  font-size:10px;
  font-weight:700;
  letter-spacing:.13em;
  padding:5px 10px;
  text-transform:uppercase;
  white-space:nowrap;
}
.badge-warn { --badge-tone:var(--yellow-rgb); }
.badge-attention { --badge-tone:var(--orange-rgb); }
.badge-critical { --badge-tone:var(--red-rgb); }
.screen {
  margin:0 14px;
  min-height:118px;
  border:1px solid var(--hair);
  border-radius:12px;
  background:linear-gradient(180deg, rgba(var(--tone),.08), rgba(0,0,0,.2)), rgba(5,9,15,.7);
  padding:14px 16px 13px;
  position:relative;
  overflow:hidden;
}
.now-label { display:flex; align-items:center; gap:8px; color:var(--blue); font-size:10px; font-weight:700; letter-spacing:.18em; text-transform:uppercase; }
.air-dot { width:7px; height:7px; border-radius:50%; background:rgb(var(--tone)); box-shadow:0 0 10px rgba(var(--tone),.9); flex:none; }
@media (prefers-reduced-motion:no-preference) {
  .air-dot { animation:livePulse 2.3s ease-in-out infinite; }
}
.screen.stale .now-label { color:var(--muted); }
.screen.stale .air-dot { background:var(--faint); box-shadow:none; animation:none; }
.screen.off-air .now-label { color:var(--red); }
.screen.off-air .air-dot { background:var(--red); box-shadow:0 0 12px rgba(var(--red-rgb),.95); animation:none; }
.screen.off-air .now-title { color:var(--text-2); }
.now-title {
  color:var(--text);
  font-size:17px;
  font-weight:650;
  line-height:1.25;
  margin-top:9px;
  overflow-wrap:anywhere;
  display:-webkit-box;
  -webkit-line-clamp:3;
  -webkit-box-orient:vertical;
  overflow:hidden;
}
.now-title.missing { color:var(--muted); font-weight:500; }
.now-progress { display:flex; align-items:center; gap:10px; margin-top:12px; }
/* The progress bar is orange to yellow whatever the tile's status colour, so it stands out from the green/red status around it. */
.now-bar { position:relative; flex:1; height:7px; border-radius:4px; background:rgba(255,255,255,.14); overflow:hidden; }
.now-bar i { position:absolute; left:0; top:0; bottom:0; width:0; border-radius:4px; background:linear-gradient(90deg, #ff8a00, #ffc400); box-shadow:0 0 12px rgba(255,170,0,.65); transition:width 1s linear; }
.now-time { color:var(--text-2); font-size:11.5px; white-space:nowrap; }
/* The bar is always drawn. Live: filled and moving. Unknown / none: an empty, hatched track that says why. Stale or feed lost: dimmed where it stood. Off air: empty, red words. */
.now-progress.is-unknown .now-bar, .now-progress.is-none .now-bar { background:repeating-linear-gradient(90deg, rgba(255,255,255,.14) 0 6px, rgba(255,255,255,.05) 6px 12px); }
.now-progress.is-unknown .now-time, .now-progress.is-none .now-time, .now-progress.is-stale .now-time { color:var(--muted); }
.now-progress.is-stale .now-bar i { opacity:.45; filter:saturate(.3); box-shadow:none; }
.now-progress.is-off .now-bar i { width:0 !important; }
.now-progress.is-paused .now-bar i { background:repeating-linear-gradient(135deg, #ffd23f 0 7px, #c99600 7px 14px); transition:none; }
.now-progress.is-estimated .now-bar i { background:repeating-linear-gradient(135deg, #ff8a00 0 7px, #ffc400 7px 14px); }
.now-progress.is-paused .now-time { color:#ffd23f; font-weight:700; }
.now-progress.is-live .now-time { color:#ffd9a0; font-weight:600; }
.now-progress.is-off .now-time { color:var(--red); font-weight:700; }
.now-sub { color:var(--text-2); font-size:12px; line-height:1.35; margin-top:8px; overflow-wrap:anywhere; }
.now-age { margin-top:7px; font-size:11.5px; color:var(--muted); }
.now-age.stale { color:var(--yellow); }
.now-next { margin-top:6px; font-size:11.5px; color:var(--muted); overflow-wrap:anywhere; }
.tile-telemetry {
  display:grid;
  gap:8px;
  align-content:start;
  flex:1 1 auto;
  padding:14px 18px 16px;
}
.mini-row {
  display:grid;
  grid-template-columns:82px 1fr;
  gap:8px;
  align-items:baseline;
}
.mini-label { color:var(--muted); font-size:10px; font-weight:650; letter-spacing:.12em; text-transform:uppercase; }
.mini-value { color:var(--text-2); font-size:12.5px; line-height:1.35; overflow-wrap:anywhere; }
.tile-footer {
  border-top:1px solid var(--hair);
  color:var(--muted);
  display:flex;
  justify-content:space-between;
  gap:8px;
  font-size:12px;
  padding:12px 18px;
}
.details-panel {
  border-top:1px solid var(--hair);
  background:rgba(0,0,0,.22);
  padding:16px 18px;
}
/* A tile is only about a third of the page wide, so its two detail groups stack. Side by side they
   squeezed the value column to nothing and long text wrapped one letter per line. The wide
   selected-endpoint panel on the topology page keeps two columns. */
.details-grid { display:grid; grid-template-columns:minmax(0, 1fr); gap:16px; }
.topology-detail .details-grid { grid-template-columns:minmax(0, 1.15fr) minmax(0, .85fr); }
.detail-group { min-width:0; }
.detail-heading { color:var(--blue); font-size:10.5px; font-weight:700; letter-spacing:.16em; margin-bottom:10px; text-transform:uppercase; }
.diag-table { display:grid; gap:8px; min-width:0; }
.diag-row { display:grid; grid-template-columns:minmax(84px, 118px) minmax(0, 1fr); gap:10px; align-items:baseline; min-width:0; }
.diag-label { color:var(--muted); font-size:10px; font-weight:650; letter-spacing:.1em; text-transform:uppercase; min-width:0; }
.diag-value { color:var(--text-2); font-size:12.5px; line-height:1.42; min-width:0; white-space:normal; word-wrap:break-word; overflow-wrap:break-word; word-break:normal; }
.event-list { display:grid; gap:8px; }
.event-item {
  border:1px solid var(--hair);
  border-radius:var(--radius-sm);
  background:rgba(255,255,255,.03);
  padding:10px 12px;
}
.event-name { color:var(--text); font-size:12.5px; font-weight:650; }
.event-copy { color:var(--text-2); font-size:12px; line-height:1.4; margin-top:3px; overflow-wrap:anywhere; }
.incident-feed { min-height:360px; overflow:hidden; }
.incident-heading {
  border-bottom:1px solid var(--hair);
  color:var(--blue);
  font-size:10.5px;
  font-weight:700;
  letter-spacing:.18em;
  padding:16px 18px;
  text-transform:uppercase;
}
.incident-list {
  display:grid;
  gap:10px;
  max-height:calc(100vh - 250px);
  overflow:auto;
  padding:16px 18px;
}
.incident-empty { color:var(--muted); font-size:12.5px; line-height:1.5; }
.incident-entry {
  border-left:2px solid var(--blue);
  border-radius:0 var(--radius-sm) var(--radius-sm) 0;
  background:rgba(255,255,255,.03);
  padding:10px 12px;
}
.incident-entry.status-warning { border-left-color:var(--yellow); }
.incident-entry.status-attention { border-left-color:var(--orange); }
.incident-entry.status-critical { border-left-color:var(--red); }
.incident-time { color:var(--muted); display:block; font-size:10.5px; font-weight:650; letter-spacing:.1em; margin-bottom:4px; text-transform:uppercase; }
.incident-text { color:var(--text-2); font-size:12.5px; line-height:1.45; overflow-wrap:anywhere; }
.endpoint-tile.status-warning { --tone:var(--yellow-rgb); }
.endpoint-tile.status-attention, .endpoint-tile.status-offline { --tone:var(--orange-rgb); }
.endpoint-tile.status-critical { --tone:var(--red-rgb); border-color:rgba(var(--red-rgb),.65); box-shadow:0 0 0 1px rgba(var(--red-rgb),.16), 0 0 38px rgba(var(--red-rgb),.18), var(--shadow); }
@media (prefers-reduced-motion:no-preference) {
  .endpoint-tile.status-critical { animation:criticalPulse 1.4s ease-in-out infinite; }
}
@keyframes criticalPulse { 0%,100% { box-shadow:0 0 0 1px rgba(var(--red-rgb),.18), 0 0 24px rgba(var(--red-rgb),.14), var(--shadow); } 50% { box-shadow:0 0 0 1px rgba(var(--red-rgb),.34), 0 0 44px rgba(var(--red-rgb),.3), var(--shadow); } }
/* No live data (the stream is down or has stalled): everything that shows state is dimmed and the banner says so, so a
   frozen picture of a healthy past can never be mistaken for the present. */
body.is-blind .endpoint-tile, body.is-blind .counter, body.is-blind .vitals, body.is-blind .holo-stage, body.is-blind .topology-stack, body.is-blind .incident-feed {
  opacity:.5;
  filter:saturate(.35);
  transition:opacity .4s ease, filter .4s ease;
}
@keyframes dashFlow { to { stroke-dashoffset:-180; } }
/* PHONE LAYOUT. The map is wide; shrunk to a phone's width its text would be tiny. So on narrow screens
   (by width, in either orientation) the map is replaced by a
   stack built for thumbs: Core on top, a spine down the middle, and the six playouts as readable
   tappable cards in rows of two, each joined to the spine by a link coloured with its own status. */
.topology-stack { display:none; position:relative; padding:14px 12px 18px; --spine:rgba(var(--blue-rgb),.5); }
.stack-hub {
  position:relative;
  width:min(280px, 88%);
  margin:0 auto;
  border:1px solid rgba(var(--green-rgb),.48);
  border-radius:var(--radius);
  background:rgba(11,20,38,.96);
  box-shadow:0 0 36px rgba(var(--green-rgb),.12);
  padding:12px 14px;
  text-align:center;
  z-index:2;
}
.stack-hub::after { content:""; position:absolute; left:50%; top:100%; width:2px; height:16px; margin-left:-1px; background:var(--spine); }
.stack-body { position:relative; padding-top:16px; }
.stack-row { position:relative; display:grid; grid-template-columns:minmax(0, 1fr) minmax(0, 1fr); column-gap:26px; margin-top:14px; }
.stack-row:first-child { margin-top:0; }
/* The spine is drawn one row at a time: each segment covers its row plus the gap above it, so
   they join end to end, and the last one stops at the middle of the last row's link. */
.stack-row::before { content:""; position:absolute; left:50%; top:-14px; bottom:0; width:2px; margin-left:-1px; background:var(--spine); }
.stack-row:first-child::before { top:-16px; }
.stack-row:last-child::before { bottom:50%; }
.stack-node {
  --link:rgba(var(--blue-rgb),.55);
  position:relative;
  min-width:0;
  border:1px solid rgba(var(--blue-rgb),.26);
  border-radius:var(--radius);
  background:rgba(11,20,38,.97);
  box-shadow:var(--shadow);
}
.stack-node.status-healthy { --link:rgba(var(--green-rgb),.75); }
.stack-node.status-warning { --link:rgba(var(--yellow-rgb),.85); }
.stack-node.status-attention, .stack-node.status-offline { --link:rgba(var(--orange-rgb),.85); }
.stack-node.status-critical { --link:rgba(var(--red-rgb),.95); }
.stack-node::after { content:""; position:absolute; top:50%; height:2px; width:13px; margin-top:-1px; background:var(--link); }
.stack-row > .stack-node:first-child::after { right:-13px; }
.stack-row > .stack-node:last-child::after { left:-13px; }
.stack-node button {
  display:block;
  width:100%;
  min-height:44px;
  border:0;
  border-radius:var(--radius);
  background:transparent;
  color:inherit;
  padding:10px 10px 11px;
  text-align:left;
}
.stack-node button:hover { transform:none; }
.stack-node button[aria-pressed="true"] { background:rgba(var(--blue-rgb),.1); }
/* Badge above the name, not beside it: beside it the name had no room and broke mid-word. The name
   wraps only at its hyphen (MAIN- / CARIBBEAN) and only breaks inside a word if a single word is wider than the card. */
.stack-title { display:flex; flex-direction:column-reverse; align-items:flex-start; gap:6px; }
.stack-name { font-size:12.5px; font-weight:900; letter-spacing:.02em; text-transform:uppercase; overflow-wrap:break-word; word-break:normal; max-width:100%; }
.stack-sub { color:var(--muted); font-size:10px; font-weight:800; letter-spacing:.1em; margin-top:2px; text-transform:uppercase; }
.stack-line { color:var(--text-2); font-size:11.5px; line-height:1.35; margin-top:8px; overflow-wrap:break-word; }
.stack-line b { color:var(--muted); display:block; font-size:9.5px; font-weight:900; letter-spacing:.1em; text-transform:uppercase; }
.stack-node .badge { font-size:9px; padding:4px 6px; }
.stack-overall-critical { --spine:rgba(var(--red-rgb),.8); }
.stack-overall-warning { --spine:rgba(var(--yellow-rgb),.75); }
.stack-overall-attention { --spine:rgba(var(--orange-rgb),.75); }
.stack-overall-healthy { --spine:rgba(var(--green-rgb),.6); }
@media (prefers-reduced-motion:no-preference) {
  .stack-node.status-critical { animation:criticalPulse 1.4s ease-in-out infinite; }
}
/* ===== Topology: a command-deck display. The map is a real grid, so a card can grow for longer text without ever touching
   another; the connector lines are drawn from the cards' MEASURED positions (see layoutHoloLines). ===== */
.core-title { color:var(--green); font-size:18px; font-weight:900; letter-spacing:.08em; text-transform:uppercase; }
.core-copy { color:var(--text-2); font-size:12px; line-height:1.4; margin-top:8px; }
.holo-stage {
  position:relative;
  isolation:isolate;
  overflow:hidden;
  padding:40px clamp(14px, 3vw, 46px) 46px;
  background:
    radial-gradient(1100px 480px at 50% 50%, rgba(var(--green-rgb),.10), transparent 64%),
    radial-gradient(800px 380px at 12% 0%, rgba(var(--blue-rgb),.13), transparent 62%),
    radial-gradient(800px 380px at 88% 100%, rgba(var(--blue-rgb),.11), transparent 62%),
    #030812;
}
/* the floor: a perspective grid drifting toward the viewer */
.holo-stage::before {
  content:"";
  position:absolute;
  left:-30%; right:-30%; bottom:-6%; height:78%;
  z-index:-3;
  background:
    linear-gradient(rgba(var(--blue-rgb),.20) 1px, transparent 1px) 0 0 / 64px 64px,
    linear-gradient(90deg, rgba(var(--blue-rgb),.20) 1px, transparent 1px) 0 0 / 64px 64px;
  transform:perspective(520px) rotateX(64deg);
  transform-origin:50% 100%;
  -webkit-mask-image:linear-gradient(to top, #000 0%, transparent 72%);
  mask-image:linear-gradient(to top, #000 0%, transparent 72%);
}
.holo-stars {
  position:absolute; inset:0; z-index:-3; opacity:.8;
  background-image:
    radial-gradient(1px 1px at 7% 16%, rgba(190,226,255,.7), transparent),
    radial-gradient(1px 1px at 19% 63%, rgba(190,226,255,.5), transparent),
    radial-gradient(1.5px 1.5px at 31% 9%, rgba(190,226,255,.6), transparent),
    radial-gradient(1px 1px at 44% 83%, rgba(190,226,255,.45), transparent),
    radial-gradient(1px 1px at 58% 22%, rgba(190,226,255,.55), transparent),
    radial-gradient(1.5px 1.5px at 71% 71%, rgba(190,226,255,.6), transparent),
    radial-gradient(1px 1px at 83% 12%, rgba(190,226,255,.5), transparent),
    radial-gradient(1px 1px at 93% 48%, rgba(190,226,255,.55), transparent),
    radial-gradient(1px 1px at 12% 90%, rgba(190,226,255,.4), transparent),
    radial-gradient(1px 1px at 66% 92%, rgba(190,226,255,.45), transparent);
}
.holo-scan {
  position:absolute; left:0; right:0; top:-140px; height:140px; z-index:-2; pointer-events:none;
  background:linear-gradient(to bottom, transparent, rgba(var(--blue-rgb),.10), rgba(var(--blue-rgb),.03), transparent);
}
/* the radar behind Core: range rings and a rotating sweep. It sits BELOW the cards, so it never draws over their text. */
.holo-radar {
  position:absolute; left:50%; top:50%; width:620px; height:620px; margin:-310px 0 0 -310px; z-index:-1; pointer-events:none; border-radius:50%;
  background:repeating-radial-gradient(circle at center, transparent 0 58px, rgba(var(--green-rgb),.13) 59px 60px);
  -webkit-mask-image:radial-gradient(circle, #000 0 69%, transparent 71%);
  mask-image:radial-gradient(circle, #000 0 69%, transparent 71%);
}
.holo-radar::after {
  content:""; position:absolute; inset:0; border-radius:50%;
  background:conic-gradient(from 0deg, rgba(var(--green-rgb),0) 0deg 296deg, rgba(var(--green-rgb),.20) 350deg, rgba(var(--green-rgb),.52) 360deg);
}
.holo-lines { position:absolute; inset:0; width:100%; height:100%; z-index:1; pointer-events:none; overflow:visible; }
.holo-base { fill:none; stroke-width:1.2; stroke:rgba(var(--blue-rgb),.30); }
.holo-flow { fill:none; stroke-width:2.4; stroke-linecap:round; stroke-dasharray:3 12; stroke:rgba(var(--blue-rgb),.9); filter:drop-shadow(0 0 4px rgba(var(--blue-rgb),.9)); }
.holo-port { fill:#030812; stroke-width:2; stroke:rgba(var(--blue-rgb),.95); }
.holo-packet { fill:#fff; filter:drop-shadow(0 0 5px rgba(var(--blue-rgb),1)); }
.holo-link.status-healthy .holo-base { stroke:rgba(var(--green-rgb),.34); }
.holo-link.status-healthy .holo-flow { stroke:rgba(var(--green-rgb),.95); filter:drop-shadow(0 0 4px rgba(var(--green-rgb),.9)); }
.holo-link.status-healthy .holo-port { stroke:rgba(var(--green-rgb),.95); }
.holo-link.status-warning .holo-base { stroke:rgba(var(--yellow-rgb),.4); }
.holo-link.status-warning .holo-flow { stroke:rgba(var(--yellow-rgb),.95); filter:drop-shadow(0 0 4px rgba(var(--yellow-rgb),.9)); }
.holo-link.status-warning .holo-port { stroke:rgba(var(--yellow-rgb),.95); }
.holo-link.status-attention .holo-base, .holo-link.status-offline .holo-base { stroke:rgba(var(--orange-rgb),.4); }
.holo-link.status-attention .holo-flow, .holo-link.status-offline .holo-flow { stroke:rgba(var(--orange-rgb),.95); filter:drop-shadow(0 0 4px rgba(var(--orange-rgb),.9)); }
.holo-link.status-attention .holo-port, .holo-link.status-offline .holo-port { stroke:rgba(var(--orange-rgb),.95); }
.holo-link.status-critical .holo-base { stroke:rgba(var(--red-rgb),.5); }
.holo-link.status-critical .holo-flow { stroke:rgba(var(--red-rgb),1); stroke-width:3; filter:drop-shadow(0 0 7px rgba(var(--red-rgb),1)); }
.holo-link.status-critical .holo-port { stroke:rgba(var(--red-rgb),1); }
.holo-link.status-critical .holo-packet { filter:drop-shadow(0 0 7px rgba(var(--red-rgb),1)); }
.holo-grid {
  position:relative; z-index:2; display:grid; align-items:stretch;
  grid-template-columns:minmax(0, 1fr) minmax(230px, 310px) minmax(0, 1fr);
  column-gap:clamp(34px, 7vw, 112px);
}
.holo-col { display:flex; flex-direction:column; justify-content:space-between; gap:28px; min-width:0; }
.holo-node {
  --accent:var(--blue-rgb);
  position:relative; min-width:0;
  border:1px solid rgba(var(--accent), .42); border-radius:3px;
  background:linear-gradient(165deg, rgba(14,30,56,.93) 0%, rgba(5,13,27,.95) 100%);
  box-shadow:0 0 0 1px rgba(0,0,0,.5), 0 0 28px rgba(var(--accent), .14), inset 0 0 32px rgba(var(--accent), .055);
}
/* HUD corner brackets */
.holo-node::before, .holo-node::after, .holo-core-card::before, .holo-core-card::after {
  content:""; position:absolute; width:16px; height:16px; pointer-events:none; border:0 solid rgba(var(--accent, var(--green-rgb)), .98);
  filter:drop-shadow(0 0 4px rgba(var(--accent, var(--green-rgb)), .9));
}
.holo-node::before, .holo-core-card::before { top:-2px; left:-2px; border-width:2px 0 0 2px; }
.holo-node::after, .holo-core-card::after { bottom:-2px; right:-2px; border-width:0 2px 2px 0; }
.holo-node.status-healthy { --accent:var(--green-rgb); }
.holo-node.status-warning { --accent:var(--yellow-rgb); }
.holo-node.status-attention, .holo-node.status-offline { --accent:var(--orange-rgb); }
.holo-node.status-critical { --accent:var(--red-rgb); }
.holo-node button {
  display:block; width:100%; border:0; border-radius:0; background:transparent; color:inherit; min-height:0;
  padding:15px 16px 14px; text-align:left;
}
.holo-node button:hover { transform:none; background:rgba(var(--accent), .07); }
.holo-node button[aria-pressed="true"] { background:rgba(var(--accent), .11); box-shadow:inset 3px 0 0 rgba(var(--accent), .95); }
.holo-tag, .holo-row span:first-child, .holo-core-tag, .holo-stat span {
  font-family:ui-monospace, "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-weight:800; letter-spacing:.18em; text-transform:uppercase;
}
.holo-tag { display:block; font-size:9.5px; line-height:1; color:rgba(var(--accent), .92); margin-bottom:10px; }
.holo-head { display:flex; align-items:flex-start; justify-content:space-between; gap:10px; }
.holo-name { font-size:15.5px; font-weight:900; letter-spacing:.07em; text-transform:uppercase; overflow-wrap:break-word; word-break:normal; text-shadow:0 0 14px rgba(var(--accent), .35); }
.holo-sub { color:var(--muted); font-size:10.5px; font-weight:800; letter-spacing:.14em; margin-top:3px; text-transform:uppercase; }
.holo-now {
  margin-top:6px; color:var(--text-2); font-size:12.5px; line-height:1.4;
  display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden; overflow-wrap:anywhere;
}
/* The same On air / Off air / Last reported line as the dashboard tile (.now-label), decided by the same function (airStatus). */
.holo-air {
  display:flex; align-items:center; gap:8px; margin-top:11px; padding-top:10px; border-top:1px dashed rgba(var(--accent), .30);
  font-size:10px; font-weight:700; letter-spacing:.18em; text-transform:uppercase; color:var(--blue);
}
.holo-air .air-dot { width:7px; height:7px; border-radius:50%; flex:none; background:rgb(var(--accent)); box-shadow:0 0 10px rgba(var(--accent), .9); }
.holo-air.is-stale { color:var(--muted); }
.holo-air.is-stale .air-dot { background:var(--faint); box-shadow:none; }
.holo-air.is-off { color:var(--red); }
.holo-air.is-off .air-dot { background:var(--red); box-shadow:0 0 12px rgba(var(--red-rgb), .95); }
@media (prefers-reduced-motion:no-preference) { .holo-air.is-live .air-dot { animation:livePulse 2.3s ease-in-out infinite; } }
.holo-progress { --tone:var(--accent); margin-top:9px; }
.stack-node { --accent:var(--blue-rgb); }
.stack-node.status-healthy { --accent:var(--green-rgb); }
.stack-node.status-warning { --accent:var(--yellow-rgb); }
.stack-node.status-attention, .stack-node.status-offline { --accent:var(--orange-rgb); }
.stack-node.status-critical { --accent:var(--red-rgb); }
.holo-air.stack-air { margin-top:9px; padding-top:0; border-top:0; font-size:9.5px; letter-spacing:.14em; }
.stack-node .holo-progress { margin-top:7px; gap:6px; flex-wrap:wrap; }
.stack-node .holo-progress .now-bar { flex:1 1 100%; }
.stack-node .holo-progress .now-time { font-size:10.5px; white-space:normal; }
.holo-rows { display:grid; gap:6px; margin-top:10px; }
.holo-row { display:flex; justify-content:space-between; align-items:baseline; gap:14px; font-size:11.5px; color:var(--muted); }
.holo-row span:first-child { font-size:9.5px; color:rgba(var(--accent), .8); flex:none; }
.holo-row span:last-child { color:var(--text-2); text-align:right; overflow-wrap:anywhere; min-width:0; }
.holo-core { position:relative; align-self:center; z-index:3; }
.holo-core-card {
  --accent:var(--green-rgb);
  position:relative; text-align:center; padding:24px 18px 20px;
  border:1px solid rgba(var(--green-rgb), .6); border-radius:4px;
  background:linear-gradient(180deg, rgba(8,30,28,.93), rgba(3,12,18,.96));
  box-shadow:0 0 60px rgba(var(--green-rgb), .20), inset 0 0 40px rgba(var(--green-rgb), .09);
}
.holo-core-tag { display:block; font-size:9.5px; color:rgba(var(--green-rgb), .85); margin-bottom:8px; }
.holo-core-title { color:var(--green); font-size:26px; font-weight:900; letter-spacing:.34em; text-indent:.34em; text-transform:uppercase; text-shadow:0 0 22px rgba(var(--green-rgb), .8); }
.holo-core-copy { color:var(--text-2); font-size:12px; line-height:1.45; margin-top:10px; }
.holo-stats { display:grid; grid-template-columns:repeat(3, 1fr); gap:8px; margin-top:16px; padding-top:14px; border-top:1px dashed rgba(var(--green-rgb), .3); }
.holo-stat b { display:block; font-size:24px; font-weight:900; line-height:1; color:#fff; text-shadow:0 0 14px rgba(var(--green-rgb), .7); }
.holo-stat span { display:block; font-size:8.5px; margin-top:6px; color:var(--muted); letter-spacing:.14em; }
.holo-stat.is-alarm b { color:var(--red); text-shadow:0 0 14px rgba(var(--red-rgb), .85); }
.holo-stat.is-watch b { color:var(--orange); text-shadow:0 0 14px rgba(var(--orange-rgb), .8); }
@media (prefers-reduced-motion:no-preference) {
  .holo-stage::before { animation:holoFloor 5s linear infinite; }
  .holo-scan { animation:holoScan 9s linear infinite; }
  .holo-radar::after { animation:holoSpin 8s linear infinite; }
  .holo-flow { animation:dashFlow 5s linear infinite; }
  .holo-link.status-critical .holo-flow { animation-duration:1.6s; }
  .holo-node.status-critical::before, .holo-node.status-critical::after { animation:holoFlicker 1.1s steps(2, end) infinite; }
}
@media (prefers-reduced-motion:reduce) { .holo-packet { display:none; } }
@keyframes holoFloor { to { background-position:0 64px, 64px 0; } }
@keyframes holoScan { to { top:calc(100% + 140px); } }
@keyframes holoSpin { to { transform:rotate(360deg); } }
@keyframes holoFlicker { 50% { opacity:.25; } }
.topology-detail { margin-top:16px; }
.topology-wrap { overflow:hidden; }
/* The topology page keeps only the status banner above the map; the counters and server panel belong to the Monitor page. */
.overview.compact { grid-template-columns:minmax(0, 1fr); }
.overview.compact .counters, .overview.compact .vitals { display:none; }
.spark-line { vector-effect:non-scaling-stroke; }
.update-chip { display:block; width:100%; margin:-4px 0 16px; text-align:left; border-color:rgba(var(--blue-rgb),.55); background:rgba(var(--blue-rgb),.1); color:var(--text); }
/* a file name that has not been refreshed lately is shown quieter: it may no longer be what is on air */
.holo-now.is-stale { opacity:.6; }
/* Core's CPU inside the topology's Core card */
.holo-cpu {
  display:grid; grid-template-columns:auto minmax(0, 1fr) auto; gap:10px; align-items:center; margin-top:14px;
  font-family:var(--mono); font-size:10px; font-weight:800; letter-spacing:.16em; text-transform:uppercase; color:var(--muted);
}
.holo-cpu-track { height:5px; border-radius:3px; background:rgba(255,255,255,.08); overflow:hidden; }
.holo-cpu-track i { display:block; height:100%; width:0; background:linear-gradient(90deg, rgba(var(--green-rgb),.4), rgb(var(--green-rgb))); transition:width .9s cubic-bezier(.22,.8,.3,1); }
.holo-cpu b { color:var(--text-2); font-weight:800; min-width:3.4em; text-align:right; letter-spacing:.06em; }
.message { color:var(--muted); font-size:12.5px; line-height:1.4; min-height:18px; }
.foot { color:var(--muted); font-size:12.5px; line-height:1.45; }
.site-foot {
  display:flex; flex-wrap:wrap; align-items:center; gap:10px 24px;
  margin-top:34px; padding-top:20px; border-top:1px solid var(--hair);
  color:var(--muted); font-size:12px; line-height:1.5;
}
.foot-mark { color:var(--orange); font-size:11px; font-weight:750; letter-spacing:.3em; }
.foot-link { color:var(--blue); padding:10px 2px; }
.foot-link:hover { text-decoration:underline; }
.foot-spacer { flex:1; }
[hidden] { display:none !important; }
@media (max-width:992px) {
  .overview { grid-template-columns:minmax(0, 1fr); }
  .vitals { grid-template-columns:minmax(0, 1fr) minmax(0, 1fr); column-gap:34px; align-items:center; }
  .shell { padding-inline:12px; }
  .topbar { grid-template-columns:1fr; gap:12px; }
  .nav { width:100%; }
  .nav a { flex:1; justify-content:center; }
  .topbar-right { justify-content:flex-start; }
  .clocks { justify-content:flex-start; }
  .clock-chip, .clock-skew { text-align:left; }
  .banner { grid-template-columns:auto minmax(0, 1fr); padding:18px; }
  .banner-time { grid-column:1 / -1; text-align:left; }
  .mcr-layout { grid-template-columns:1fr; }
  .endpoint-grid { grid-template-columns:repeat(2, minmax(0, 1fr)); }
  .incident-list { max-height:320px; }
  .details-grid { grid-template-columns:1fr; }
  .topology-detail .details-grid { grid-template-columns:minmax(0, 1fr); }
  .diag-row { grid-template-columns:1fr; gap:2px; }
}
@media (max-width:700px) {
  .endpoint-grid { grid-template-columns:1fr; }
  .counters { grid-template-columns:repeat(2, minmax(0, 1fr)); }
  .vitals { grid-template-columns:minmax(0, 1fr); }
  .counter { padding:14px 15px 13px; }
}
/* Phones and tablets (anything under 980 px wide, either way up): the three-column map would squeeze its cards until names broke
   mid-word, so it is swapped for the stacked layout (see .topology-stack). */
@media (max-width:980px) {
  .holo-stage { display:none; }
  .topology-stack { display:block; }
  .topology-detail { margin-top:12px; }
}
</style>
</head>
<body>
<!--off-screen-->
<div id="preloader" class="preloader" role="status" aria-live="polite">
  <div class="preloader-inner">
    <div class="loader-wrap"><span class="loader-ring"></span><span class="loader-ring two"></span>__BULLDOG_LOADER__</div>
    <p class="loader-title">Clarix Watchdog</p>
    <p class="loader-brand">by ClarixTech Group</p>
    <p id="loader-step" class="loader-step">Starting up...</p>
    <div class="loader-bar" aria-hidden="true"><span></span></div>
    <button type="button" id="loader-skip" class="loader-skip">Show dashboard anyway</button>
  </div>
</div>
<noscript><p style="padding:24px;color:#fa8c16;font-family:sans-serif">Clarix Watchdog needs JavaScript to show live status.</p></noscript>
<div class="shell">
  <header class="topbar">
    <a class="brandline" href="/" aria-label="Clarix Watchdog home">
      __BULLDOG_HEADER__
      <div class="brand-text">
        <h1 id="page-title">Clarix Watchdog</h1>
        <span class="brand-sub">by ClarixTech Group</span>
      </div>
    </a>
    <nav class="nav" aria-label="Dashboard views">
      <a id="nav-dashboard" href="/">Monitor</a>
      <a id="nav-topology" href="/topology">Topology</a>
    </nav>
    <div class="topbar-right">
      <div class="clocks" aria-label="Clocks">
        <div class="clock-chip" id="chip-core" title="The time on the Core server, which stamps every alarm and event"><span class="eyebrow" id="clock-core-label">New York</span><span class="clock" id="clock-core">--:--:--</span></div>
        <div class="clock-chip" id="chip-lagos" title="Lagos time: West Africa Time (WAT), UTC+1, no daylight saving. Taken from Core's clock, so a wrong device clock cannot move it"><span class="eyebrow" id="clock-lagos-label">Lagos</span><span class="clock" id="clock-lagos">--:--:--</span></div>
        <div class="clock-chip" title="The time on the device you are looking at"><span class="eyebrow">This device</span><span class="clock" id="clock-device">--:--:--</span></div>
        <div class="clock-skew" id="clock-skew" hidden></div>
      </div>
      <span class="live-indicator"><span id="live-dot" class="live-dot"></span><span id="live-text">Connecting</span></span>
    </div>
  </header>
  <div id="standby-strip" class="standby-strip" role="status" aria-live="polite" hidden></div>
  <div id="staff-alert" class="staff-alert" role="alert" hidden></div>

  <div class="toolbar">
    <button type="button" id="sound-toggle" aria-pressed="false">Alarm sound: off</button>
    <button type="button" id="mute-toggle" aria-pressed="false">Silence siren</button>
    <button type="button" id="awake-toggle" aria-pressed="true">Keep screen on: on</button>
    <!--staff-link-->
    <span id="shift-chip" class="shift-chip" role="status" aria-live="polite" hidden></span>
    <button type="button" id="install-app" hidden>Install app</button>
    <span class="spacer"></span>
    <span id="stream-pill" class="pill">Waiting for SSE</span>
  </div>
  <div aria-live="assertive"><button type="button" id="siren-strip" class="siren-strip" hidden></button></div>
  <p id="ios-install-hint" class="foot" hidden>On iPhone/iPad: tap Share, then Add to Home Screen.</p>
  <button type="button" id="update-chip" class="update-chip" hidden>A newer version of this page is available. Tap to update.</button>

  <section id="overview" class="overview" aria-label="Overview">
    <div class="overview-main">
      <div id="banner" class="banner" role="status" aria-live="assertive">
        <div class="banner-orb" aria-hidden="true"></div>
        <div>
          <div class="banner-title">Waiting for live telemetry</div>
          <div class="banner-sub">The next pushed snapshot will populate the six playout endpoints.</div>
        </div>
        <div class="banner-time">SSE</div>
      </div>
      <section id="counters" class="counters" aria-label="Counters">
        <div class="counter" id="counter-healthy"><span class="eyebrow">Playouts healthy</span><div class="counter-value"><b>0</b><i>/ 6</i></div><div class="counter-note">Waiting for data</div></div>
        <div class="counter" id="counter-since"><span class="eyebrow">Since last critical alarm</span><div class="counter-value"><b>-</b></div><div class="counter-note">Waiting for data</div></div>
        <div class="counter" id="counter-red24"><span class="eyebrow">Critical alarms, 24 h</span><div class="counter-value"><b>0</b></div><div class="counter-note">Emails are sent on critical only</div></div>
      </section>
    </div>
    <aside id="vitals" class="vitals" aria-label="Core server load">
      <div class="vitals-left">
        <div class="vitals-head"><span class="eyebrow">Core server, all workloads</span><span class="vitals-up" id="v-up"></span></div>
        <div class="vitals-main">
          <div class="gauge" id="v-gauge" role="img" aria-label="CPU load">
            <svg viewBox="0 0 120 120" aria-hidden="true" focusable="false"><circle class="gauge-track" cx="60" cy="60" r="46"/><circle class="gauge-arc" id="v-arc" cx="60" cy="60" r="46"/></svg>
            <div class="gauge-readout"><b id="v-cpu">--</b><span class="eyebrow">CPU</span></div>
          </div>
          <div class="vitals-side">
            <div class="vstat"><span class="eyebrow">Load average</span><b id="v-load">--</b><i id="v-load-note">1 / 5 / 15 min</i></div>
            <div class="vstat"><span class="eyebrow">Cores</span><b id="v-cores">--</b></div>
          </div>
        </div>
      </div>
      <div class="vitals-right">
        <div class="meter" id="m-mem"><span class="eyebrow">Memory</span><div class="meter-track"><span></span></div><b>--</b></div>
        <div class="meter" id="m-disk"><span class="eyebrow">Disk</span><div class="meter-track"><span></span></div><b>--</b></div>
        <div class="meter" id="m-load"><span class="eyebrow">Load / core</span><div class="meter-track"><span></span></div><b>--</b></div>
        <div class="spark-wrap">
          <svg class="spark" id="v-spark" viewBox="0 0 240 58" preserveAspectRatio="none" aria-hidden="true" focusable="false">
            <defs><linearGradient id="sparkFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#6cc8f2" stop-opacity=".32"/><stop offset="1" stop-color="#6cc8f2" stop-opacity="0"/></linearGradient></defs>
            <path class="spark-area" id="v-spark-area" d=""/>
            <path class="spark-line" id="v-spark-line" d=""/>
          </svg>
          <span class="eyebrow spark-caption" id="v-spark-caption">CPU, recent minutes</span>
        </div>
        <p class="vitals-note" id="v-note" hidden></p>
      </div>
    </aside>
  </section>
__MATRIX_TILES_HTML__
  <details id="vz-panel" class="vz vz-nodata" hidden>
    <summary class="vz-summary"><span class="eyebrow">Site C ZIXI Feed</span><span id="vz-headline" class="vz-headline" role="status">Not reporting yet</span></summary>
    <div id="vz-tools" class="vz-tools"></div>
    <div id="vz-body" class="vz-body"></div>
  </details>

  <main id="dashboard-view">
    <div class="mcr-layout">
      <section id="endpoint-grid" class="endpoint-grid" aria-label="Independent playout endpoints"></section>
      <aside class="incident-feed" aria-label="Incident feed">
        <div class="incident-heading">Incident Feed <span class="incident-zone" id="incident-zone">New York time</span></div>
        <div id="incident-list" class="incident-list"></div>
      </aside>
    </div>
  </main>

  <main id="topology-view" hidden>
    <section id="topology-root" class="topology-wrap" aria-label="Core telemetry topology"></section>
    <section id="topology-detail" class="topology-detail" aria-label="Selected endpoint diagnostics"></section>
  </main>

  <p id="message" class="message" role="status" aria-live="polite"></p>

  <footer class="site-foot">
    <span class="foot-mark">CLARIXTECH GROUP</span>
    <span>Resilient systems for broadcast, automation, and field infrastructure.</span>
    <span class="foot-spacer"></span>
    <a class="foot-link" href="https://corp.example" target="_blank" rel="noopener noreferrer">corp.example</a>
  </footer>
</div>

<script>
(function () {
  "use strict";
  var token = __TOKEN_JSON__;
  var pageMode = __PAGE_MODE_JSON__;
  var pageVersion = __PAGE_VERSION_JSON__;   // which build of this page this tab loaded; every frame says which build the listener serves now

  var ALARM_PEAK = 0.85;          // loud: close to full scale, with the limiter below as a safety ceiling
  var SIREN_CYCLE_S = 1.12;
  var SIREN_HORIZON_S = 60;       // scheduled this far ahead, so a throttled background tab keeps sounding
  var SVG_NS = "http://www.w3.org/2000/svg";
  var ENDPOINTS = [
    { id:"SITE-A", sourceId:"SITE-A", label:"SITE-A", location:"Region A", feedLabel:"Output decode", kind:"source", method:"Decoded multicast output: freeze, black, silence, and loudness checks.", x:9, y:12 },
    { id:"SITE-B", sourceId:"SITE-B", label:"SITE-B", location:"Region B", feedLabel:"Playlist engine", kind:"source", method:"Insta playlist timing proxy. It cannot prove the picture is moving without decode access.", x:69, y:12 },
    { id:"MAIN-USA", sourceId:"MAIN", label:"MAIN-USA", location:"New York", feedLabel:"USA", kind:"feed", feedKey:"usa", method:"Insta engine activity: the player's CPU work, checked every 10s. Shows a stall or hang; it does not decode the picture.", x:4, y:41 },
    { id:"MAIN-Caribbean", sourceId:"MAIN", label:"MAIN-Caribbean", location:"New York", feedLabel:"Caribbean", kind:"feed", feedKey:"caribbean", method:"Insta engine activity: the player's CPU work, checked every 10s. Shows a stall or hang; it does not decode the picture.", x:10, y:69 },
    { id:"BACKUP-USA", sourceId:"BACKUP-SERVER", label:"BACKUP-USA", location:"New York", feedLabel:"USA", kind:"feed", feedKey:"usa", method:"AdMax engine activity (Network feed): the player's CPU work, checked every 10s. Shows a stall or hang; it does not decode the picture.", x:73, y:41 },
    { id:"BACKUP-Caribbean", sourceId:"BACKUP-SERVER", label:"BACKUP-Caribbean", location:"New York", feedLabel:"Caribbean", kind:"feed", feedKey:"caribbean", method:"AdMax engine activity (Box Office feed): the player's CPU work, checked every 10s. Shows a stall or hang; it does not decode the picture.", x:67, y:69 }
  ];
  var FEED_NAMES = { usa:"USA", caribbean:"Caribbean" };
  var CRITICAL_TYPES = ["freeze", "black", "silence", "off_air", "off-air", "offair"];
  var pageOpenedAt = Date.now();
  var STALE_NOW_PLAYING_S = 45;   // a file name older than this "may have changed": items can be shorter than the report interval
  var BLIND_AFTER_MS = 15000;     // no pushed frame for this long: the page says it is blind instead of showing a frozen past
  var STALL_RECONNECT_MS = 15000; // no pushed frame for this long (three missed pushes): the stream is opened again by the page itself
  var RECONNECT_SPACING_MS = 20000;   // ... but never more often than this, so a slow link (a ship's) is given time to finish connecting
  var ACCESS_POLL_MS = 5000;          // while the unavailable screen is up: how often to ask whether access is on again
  var GAUGE_CIRCUMFERENCE = 289.03;   // 2 x pi x 46, the radius of the CPU ring
  // The same two connectivity checks as the offline screen: used ONLY to word the "live feed lost" message (this device's
  // internet, or Core?). The answer is never read, nothing is sent with the request.
  var PROBES = ["https://www.gstatic.com/generate_204", "https://www.cloudflare.com/cdn-cgi/trace"];
  var PROBE_TIMEOUT_MS = 4000;
  var REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)");

  var dashboardView = document.getElementById("dashboard-view");
  var topologyView = document.getElementById("topology-view");
  var endpointGrid = document.getElementById("endpoint-grid");
  var incidentList = document.getElementById("incident-list");
  var topologyRoot = document.getElementById("topology-root");
  var topologyDetail = document.getElementById("topology-detail");
  var banner = document.getElementById("banner");
  var message = document.getElementById("message");
  var liveDot = document.getElementById("live-dot");
  var liveText = document.getElementById("live-text");
  var streamPill = document.getElementById("stream-pill");
  var soundToggle = document.getElementById("sound-toggle");
  var muteToggle = document.getElementById("mute-toggle");
  var sirenStrip = document.getElementById("siren-strip");
  var installButton = document.getElementById("install-app");
  var awakeToggle = document.getElementById("awake-toggle");
  var updateChip = document.getElementById("update-chip");
  var offScreen = document.getElementById("off-screen");     // null where the control phase is off
  var overview = document.getElementById("overview");
  var clockCore = document.getElementById("clock-core");
  var clockCoreLabel = document.getElementById("clock-core-label");
  var chipCore = document.getElementById("chip-core");
  var clockDevice = document.getElementById("clock-device");
  var clockLagos = document.getElementById("clock-lagos");
  var clockLagosLabel = document.getElementById("clock-lagos-label");
  var chipLagos = document.getElementById("chip-lagos");
  var clockSkew = document.getElementById("clock-skew");
  var incidentZone = document.getElementById("incident-zone");
  var SKEW_NOTICE_MS = 5000;     // a device clock further than this from Core's is called out

  var uiState = {
    expanded: {},
    selectedEndpointId: "SITE-A",
    soundOn: true,
    muted: false,
    audioUnlocked: false,
    offShown: false,        // the unavailable screen is up: access was switched off from the control panel
    offPollAt: 0,
    accessCheckAt: 0,
    criticalActive: false,
    lastData: { sources: [], recent_events: [] },
    states: null,
    tileSignature: "",
    frameAt: 0,         // when the newest frame arrived, by THIS device's clock: ages are counted forward from it, never compared with a server time
    lastFrameAt: 0,
    streamDown: false,
    blind: false,
    internet: null,     // null: unknown; true/false: the public internet answers / does not (only checked while blind)
    internetCheckedAt: 0,
    since: null,        // { seconds, at }: time since the last critical alarm as of the newest frame
    serverClock: null,  // { ms, at, tz }: Core's own clock as of the newest frame, and when (on THIS device's timer) that frame arrived
    updateOffered: false,
    lastConnectAt: 0,   // when the stream was last (re)opened, by THIS device's timer: rate-limits the self-healing reconnects
    lastRenewAt: 0      // when this page last asked for a new view key (see renewViewKey)
  };
  var audioCtx = null;
  var limiter = null;
  // The alarm is ONE oscillator for the life of the page. It is created once, never replaced, and
  // only its volume and pitch pattern change, so no number of critical tiles, pushed frames or
  // mute/unmute clicks can ever stack a second sound on top of it.
  var alarm = { osc:null, gain:null, on:false, scheduledUntil:0 };
  var eventSource = null;
  var firstFrameSeen = false;
  // The siren is ON unless this device was told otherwise (remembered). Browsers still require one
  // tap on the page before Web Audio may play, so a fresh page load shows a strip asking for that
  // tap (see renderSirenStrip) instead of sitting silent. Silencing is deliberately NOT remembered:
  // it applies to the current alarm only and re-arms by itself once the board is clear, so one tap
  // during a test can never leave every future siren muted.
  var savedSound = null;
  try { savedSound = window.localStorage.getItem("watchdog-sound"); } catch (e) { savedSound = null; }
  uiState.soundOn = savedSound !== "off";

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js?v=20261013-shell").catch(function () {});
  }

  var deferredInstallPrompt = null;
  window.addEventListener("beforeinstallprompt", function (event) {
    event.preventDefault();
    deferredInstallPrompt = event;
    installButton.hidden = false;
  });
  installButton.addEventListener("click", function () {
    if (!deferredInstallPrompt) { return; }
    installButton.hidden = true;
    deferredInstallPrompt.prompt();
    deferredInstallPrompt = null;
  });
  window.addEventListener("appinstalled", function () { installButton.hidden = true; });
  var isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent) && !window.MSStream;
  var isStandalone = window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
  if (isIOS && !isStandalone) { document.getElementById("ios-install-hint").hidden = false; }

  function el(tag, text, className) {
    var node = document.createElement(tag);
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    if (className) { node.className = className; }
    return node;
  }

  function svg(tag) {
    return document.createElementNS(SVG_NS, tag);
  }

  function parseIso(value) {
    return Date.parse(String(value || "").replace(/(\.\d{3})\d+/, "$1"));
  }

  function ageText(value) {
    var t = parseIso(value);
    if (isNaN(t)) { return "never"; }
    var s = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (s < 90) { return s + "s ago"; }
    if (s < 7200) { return Math.round(s / 60) + "m ago"; }
    return Math.round(s / 3600) + "h ago";
  }

  // Core's time and this device's time are shown side by side because they differ whenever the viewer is in another zone or on a
  // wrong clock, and an alarm time read against the wrong one is a mistake waiting to happen. Core's clock is the SERVER's, kept
  // running on this device's timer from the moment each frame arrived, so a wrong device clock never moves it.
  function formatInZone(ms, zone, withDate) {
    var options = { hour:"2-digit", minute:"2-digit", second:"2-digit", timeZoneName:"short" };
    if (withDate) { options.month = "short"; options.day = "numeric"; options.year = "numeric"; }
    if (zone) { options.timeZone = zone; }
    try {
      return new Intl.DateTimeFormat(undefined, options).format(new Date(ms));
    } catch (error) {
      options.timeZone = "UTC";   // a zone name this browser does not know: fall back to UTC rather than show nothing
      return new Intl.DateTimeFormat(undefined, options).format(new Date(ms));
    }
  }

  function coreZone() { return uiState.serverClock ? uiState.serverClock.tz : "UTC"; }

  // The heading above Core's time names the place, not the machine: "New York" for America/New_York (the city part of the zone name).
  // It follows the zone the server reports, so it stays right if WATCHDOG_DISPLAY_TIMEZONE is ever changed.
  function zoneCity(zone) {
    var parts = String(zone || "").split("/");
    var city = parts[parts.length - 1].replace(/_/g, " ").trim();
    return city || "Core";
  }

  // Lagos time for the team there: West Africa Time (WAT) is UTC+1 all year, with no daylight saving. The zone name is written out ("WAT")
  // because browsers print Africa/Lagos as "GMT+1". It is read off Core's clock, never the device's, like the Core chip.
  var LAGOS_ZONE = "Africa/Lagos";
  function lagosText(ms) {
    var options = { hour:"2-digit", minute:"2-digit", second:"2-digit", timeZone:LAGOS_ZONE };
    try {
      return new Intl.DateTimeFormat(undefined, options).format(new Date(ms)) + " WAT";
    } catch (error) {
      options.timeZone = "Etc/GMT-1";   // the same UTC+1 under another name, if a browser does not know Africa/Lagos
      return new Intl.DateTimeFormat(undefined, options).format(new Date(ms)) + " WAT";
    }
  }

  // A stamp from the listener (always UTC) shown in Core's zone, with the zone's name so it cannot be mistaken for local time.
  function coreTimestamp(value) {
    var ms = parseIso(value);
    return isNaN(ms) ? "never" : formatInZone(ms, coreZone(), true);
  }

  // Core's clock now: the server time of the newest frame plus the time since that frame arrived. While the feed is lost it stops at
  // the last frame, because an extrapolated time would look live.
  function coreNowMs() {
    var clock = uiState.serverClock;
    if (!clock) { return null; }
    var until = uiState.blind && uiState.lastFrameAt ? uiState.lastFrameAt : Date.now();
    return clock.ms + (until - clock.at);
  }

  function tickClocks() {
    var device = Date.now();
    clockDevice.textContent = formatInZone(device, undefined, false);
    var core = coreNowMs();
    if (core === null) { clockCore.textContent = "--:--:--"; clockLagos.textContent = "--:--:--"; clockSkew.hidden = true; return; }
    clockCore.textContent = formatInZone(core, coreZone(), false);
    clockCoreLabel.textContent = zoneCity(coreZone()) + (uiState.blind ? ", last known" : "");
    incidentZone.textContent = zoneCity(coreZone()) + " time";
    chipCore.className = "clock-chip" + (uiState.blind ? " is-stale" : "");
    clockLagos.textContent = lagosText(core);
    clockLagosLabel.textContent = uiState.blind ? "Lagos, last known" : "Lagos";
    chipLagos.className = "clock-chip" + (uiState.blind ? " is-stale" : "");
    var skew = device - core;
    if (!uiState.blind && Math.abs(skew) >= SKEW_NOTICE_MS) {
      clockSkew.textContent = "This device's clock is " + durationText(Math.abs(skew) / 1000) + (skew > 0 ? " ahead of" : " behind") + " Core's.";
      clockSkew.hidden = false;
    } else {
      clockSkew.hidden = true;
    }
  }

  function fullTimestamp(value) {
    var t = parseIso(value);
    if (isNaN(t)) { return "never"; }
    return new Date(t).toLocaleString(undefined, { dateStyle:"medium", timeStyle:"medium" });
  }

  function parseJson(value, fallback) {
    if (value && typeof value === "object") { return value; }
    if (typeof value !== "string" || !value) { return fallback || {}; }
    try {
      var parsed = JSON.parse(value);
      return parsed && typeof parsed === "object" ? parsed : (fallback || {});
    } catch (e) {
      return fallback || {};
    }
  }

  function titleCase(value) {
    return String(value || "")
      .replace(/[_-]+/g, " ")
      .replace(/\b\w/g, function (ch) { return ch.toUpperCase(); });
  }

  function firstText() {
    for (var i = 0; i < arguments.length; i += 1) {
      var value = arguments[i];
      if (typeof value === "string" && value.trim()) { return value.trim(); }
      if (typeof value === "number") { return String(value); }
    }
    return "";
  }

  function sourceMap(data) {
    var byId = {};
    (data.sources || []).forEach(function (source) {
      if (source && source.source_id) { byId[source.source_id] = source; }
    });
    return byId;
  }

  function sourcePayload(source) {
    return parseJson(source ? source.last_payload_json : "", {});
  }

  function sourceEvents(endpoint, data) {
    return (data.recent_events || []).filter(function (event) {
      if (!event || event.source_id !== endpoint.sourceId) { return false; }
      var detail = parseJson(event.detail, {});
      if (endpoint.feedKey && detail.feed && String(detail.feed).toLowerCase() !== endpoint.feedKey) { return false; }
      return true;
    });
  }

  function latestEventFor(endpoint, source, data) {
    var events = sourceEvents(endpoint, data);
    if (events.length > 0) { return events[0]; }
    return source && source.latest_event ? source.latest_event : null;
  }

  function findInstance(endpoint, payload) {
    var instances = Array.isArray(payload.instances) ? payload.instances : [];
    if (endpoint.kind === "feed") {
      for (var i = 0; i < instances.length; i += 1) {
        if (String(instances[i].feed || "").toLowerCase() === endpoint.feedKey) { return instances[i]; }
      }
      return null;
    }
    return null;
  }

  function eventPhase(event) {
    var detail = parseJson(event ? event.detail : "", {});
    return String(detail.phase || "").toLowerCase();
  }

  function eventIsActive(event) {
    var phase = eventPhase(event);
    return phase !== "end" && phase !== "resolved" && phase !== "complete";
  }

  function eventIsCritical(event) {
    if (!event || !eventIsActive(event)) { return false; }
    var severity = String(event.severity || "").toLowerCase();
    var type = String(event.event_type || "").toLowerCase();
    if (severity === "critical" || severity === "major") { return true; }
    return CRITICAL_TYPES.some(function (name) { return type.indexOf(name) !== -1; }) &&
      (severity === "warning" || severity === "critical" || severity === "major");
  }

  function eventIsWarning(event) {
    if (!event || !eventIsActive(event)) { return false; }
    var severity = String(event.severity || "").toLowerCase();
    return severity === "warning" || severity === "minor";
  }

  function instanceProblem(instance) {
    if (!instance) { return false; }
    if (instance.launcher_present === false || instance.engine_present === false) { return true; }
    if (instance.running === false || instance.present === false) { return true; }
    return false;
  }

  function numericValue(value) {
    if (typeof value === "number" && isFinite(value)) { return value; }
    if (typeof value === "string" && value.trim()) {
      var parsed = Number(value);
      return isFinite(parsed) ? parsed : null;
    }
    return null;
  }

  function advancementStatus(fields) {
    if (!fields) { return null; }
    if (fields.advance_alarm_confirmed !== true) { return null; }
    var age = null;
    ["advance_age_seconds", "not_advancing_seconds", "stale_seconds", "playlist_age_seconds", "log_age_seconds"].some(function (key) {
      age = numericValue(fields[key]);
      return age !== null;
    });
    var state = String(fields.advance_status || fields.playout_motion || fields.motion_state || "").toLowerCase();
    var staleFlag = fields.advancing === false || ["stale", "paused", "frozen", "not advancing", "not_advancing", "halted"].indexOf(state) !== -1;
    if (age === null && !staleFlag) { return null; }
    if (age === null) { age = 60; }
    if (age >= 60) {
      return { level:"critical", label:"Paused/Frozen", detail:"Playout motion has not advanced for " + Math.round(age) + "s.", badgeClass:"badge-critical" };
    }
    if (age >= 45) {
      return { level:"warning", label:"Paused/Frozen", detail:"Playout motion has not advanced for " + Math.round(age) + "s.", badgeClass:"badge-warn" };
    }
    return null;
  }

  function statusFor(endpoint, source, payload, instance, latest) {
    if (!source || source.state === "never_reported") {
      return { level:"offline", label:"Waiting", detail:"No heartbeat has reached Core yet.", badgeClass:"badge-attention" };
    }
    var motionStatus = advancementStatus(instance || payload);
    if (motionStatus && motionStatus.level === "critical") {
      return motionStatus;
    }
    if (eventIsCritical(latest)) {
      return { level:"critical", label:titleCase(latest.event_type), detail:"Active critical event from detector.", badgeClass:"badge-critical" };
    }
    if (source.state !== "healthy") {
      return { level:"attention", label:"Telemetry offline", detail:"No heartbeat from source. This is connectivity-unconfirmed, not proof of failed playout.", badgeClass:"badge-attention" };
    }
    if (endpoint.kind === "feed" && !instance) {
      return { level:"critical", label:"Player Down", detail:"Expected endpoint is missing from the heartbeat payload.", badgeClass:"badge-critical" };
    }
    if (instanceProblem(instance)) {
      return { level:"critical", label:"Player Down", detail:"One reported playout process is down.", badgeClass:"badge-critical" };
    }
    if (motionStatus && motionStatus.level === "warning") {
      return motionStatus;
    }
    if (eventIsWarning(latest)) {
      return { level:"warning", label:titleCase(latest.event_type), detail:"Recent active warning from detector.", badgeClass:"badge-warn" };
    }
    return { level:"healthy", label:"Healthy", detail:"Latest telemetry is within the reported signal model.", badgeClass:"" };
  }

  function statusClass(status) {
    if (status.level === "critical") { return "status-critical"; }
    if (status.level === "warning") { return "status-warning"; }
    if (status.level === "attention") { return "status-attention"; }
    if (status.level === "offline") { return "status-offline"; }
    return "status-healthy";
  }

  function badgeText(status) {
    if (status.level === "critical") { return "Critical"; }
    if (status.level === "warning") { return "Warn"; }
    if (status.level === "attention") { return "Attention"; }
    if (status.level === "offline") { return "Offline"; }
    return "Healthy";
  }

  function nowPlayingFromObject(value) {
    if (!value || typeof value !== "object") { return null; }
    var title = firstText(value.filename, value.current_item, value.title, value.name, value.file);
    if (!title) { return null; }
    var durationLooksValid = typeof value.duration === "string" && value.duration && value.duration.charAt(0) !== "-";
    var bits = [value.result, durationLooksValid ? (value.duration + " long") : "", value.status].filter(Boolean).join(" / ");
    return { title:title, detail:bits || "Reported by heartbeat", missing:false };
  }

  function nowPlayingInfo(endpoint, payload, instance) {
    var fromInstance = nowPlayingFromObject(instance && (instance.now_playing || instance.current_item));
    if (fromInstance) { return fromInstance; }
    var fromPayload = nowPlayingFromObject(payload.now_playing);
    if (fromPayload) {
      if (endpoint.sourceId === "MAIN") { fromPayload.detail = fromPayload.detail + " / shared MAIN log"; }
      return fromPayload;
    }
    var title = firstText(payload.current_item, payload.now_playing, payload.current_program, payload.program, payload.title, payload.filename);
    if (title) { return { title:title, detail:"Reported by heartbeat", missing:false }; }
    return { title:"Not reported by sender", detail:"No normalized now-playing field in this heartbeat.", missing:true };
  }

  function pidText(instance, payload) {
    var parts = [];
    if (instance) {
      if (instance.launcher_pid) { parts.push("launcher " + instance.launcher_pid); }
      if (instance.engine_pid) { parts.push("engine " + instance.engine_pid); }
      if (instance.pid_value) { parts.push(String(instance.pid_value)); }
      if (instance.pid) { parts.push(String(instance.pid)); }
      if (instance.process_id) { parts.push(String(instance.process_id)); }
    }
    if (parts.length === 0) {
      ["pid", "detector_pid", "engine_pid", "process_id"].forEach(function (key) {
        if (payload[key]) { parts.push(String(payload[key])); }
      });
    }
    return parts.length ? parts.join(", ") : "not reported";
  }

  function engineText(instance, source, payload) {
    if (instance) {
      if (instance.engine_present === false) { return "engine down"; }
      if (instance.launcher_present === false) { return "launcher down"; }
      if (instance.process_name) { return "running: " + instance.process_name; }
      if (instance.engine_present === true || instance.launcher_present === true) { return "process pair running"; }
      return "reported";
    }
    return firstText(payload.engine_state, payload.state, payload.status) || (source && source.state === "healthy" ? "reporting" : "not reporting");
  }

  function eventSummary(event) {
    if (!event) { return "No event recorded yet."; }
    var detail = parseJson(event.detail, {});
    var basis = firstText(detail.basis, detail.reason, detail.summary, detail.message);
    var phase = eventPhase(event);
    var pieces = [titleCase(event.event_type), String(event.severity || "info")];
    if (phase) { pieces.push("phase " + phase); }
    pieces.push(ageText(event.timestamp_utc));
    return pieces.join(" / ") + (basis ? ": " + basis : "");
  }

  function eventIsFailureLike(event) {
    if (!event) { return false; }
    if (!eventIsActive(event)) { return false; }
    var severity = String(event.severity || "").toLowerCase();
    var type = String(event.event_type || "").toLowerCase();
    return severity === "warning" || severity === "major" || severity === "critical" ||
      type.indexOf("lost") !== -1 || type.indexOf("missing") !== -1 || type.indexOf("freeze") !== -1 ||
      type.indexOf("black") !== -1 || type.indexOf("silence") !== -1 ||
      type.indexOf("paused") !== -1 || type.indexOf("frozen") !== -1 || type.indexOf("stale") !== -1;
  }

  function endpointState(endpoint, data, bySource) {
    var source = bySource[endpoint.sourceId];
    var payload = sourcePayload(source);
    var instance = findInstance(endpoint, payload);
    var latest = latestEventFor(endpoint, source, data);
    var status = statusFor(endpoint, source, payload, instance, latest);
    var nowPlaying = nowPlayingInfo(endpoint, payload, instance);
    var events = sourceEvents(endpoint, data);
    return {
      endpoint:endpoint,
      source:source,
      payload:payload,
      instance:instance,
      latest:latest,
      status:status,
      statusClass:statusClass(status),
      nowPlaying:nowPlaying,
      events:events,
      failures:events.filter(eventIsFailureLike).slice(0, 4),
      telemetry:[
        { label:"Source", value:endpoint.sourceId },
        { label:"Last heard", value:source && source.last_seen_utc ? ageText(source.last_seen_utc) : "never" },
        { label:"Engine", value:engineText(instance, source, payload) },
        { label:"PID", value:pidText(instance, payload) }
      ]
    };
  }

  function badgeClassForLevel(level) {
    if (level === "critical") { return "badge-critical"; }
    if (level === "warning") { return "badge-warn"; }
    if (level === "attention" || level === "offline") { return "badge-attention"; }
    return "";
  }

  function endpointStateFromBackend(raw) {
    var endpoint = ENDPOINTS.find(function (item) { return item.id === raw.endpoint_id; }) || {
      id: raw.endpoint_id,
      sourceId: raw.source_id,
      label: raw.endpoint_id,
      location: raw.source_id,
      feedLabel: raw.feed_label || "Playout",
      method: "Reported by Core endpoint state.",
      x: 50,
      y: 50
    };
    var level = raw.status_level || "attention";
    var status = {
      level: level,
      label: raw.status_label || titleCase(level),
      root_cause: raw.root_cause || "Status Unknown",
      detail: raw.status_detail || "No status detail reported.",
      badgeClass: badgeClassForLevel(level)
    };
    var nowPlaying = raw.now_playing || { title:"Not reported by sender", detail:"No normalized now-playing field in this heartbeat." };
    return {
      endpoint: Object.assign({}, endpoint, {
        label: raw.endpoint_id || endpoint.label,
        sourceId: raw.source_id || endpoint.sourceId,
        feedLabel: raw.feed_label || endpoint.feedLabel
      }),
      source: { source_id: raw.source_id, last_seen_utc: raw.last_seen_utc, expected_interval_seconds: raw.expected_interval_seconds, state: level },
      payload: {},
      instance: null,
      latest: null,
      status: status,
      statusClass: statusClass(status),
      nowPlaying: {
        title: nowPlaying.title || "Not reported by sender",
        detail: nowPlaying.detail || "",
        missing: (nowPlaying.title || "").indexOf("Not reported") === 0,
        age_s: numericValue(nowPlaying.age_s),
        elapsed_s: numericValue(nowPlaying.elapsed_s),
        duration_s: numericValue(nowPlaying.duration_s),
        paused: nowPlaying.paused === true,
        estimated: nowPlaying.estimated === true,
        timing_suspect: nowPlaying.timing_suspect === true,
        next: typeof nowPlaying.next === "string" ? nowPlaying.next : ""
      },
      events: raw.recent_events || [],
      failures: (raw.recent_events || []).filter(eventIsFailureLike).slice(0, 4),
      diagnostics: raw.diagnostics || [],
      telemetry: [
        { label:"Source", value:raw.source_id || "not reported" },
        { label:"Last heard", value:raw.last_seen_utc ? ageText(raw.last_seen_utc) : "never" },
        { label:"Motion", value:raw.motion_text || "not reported" },
        { label:"Engine", value:raw.engine_state || "not reported" },
        { label:"PID", value:raw.pid_text || "not reported" }
      ]
    };
  }

  function allEndpointStates(data) {
    if (Array.isArray(data.endpoints) && data.endpoints.length) {
      var byEndpoint = {};
      data.endpoints.forEach(function (endpoint) { byEndpoint[endpoint.endpoint_id] = endpoint; });
      return ENDPOINTS.map(function (definition) {
        return endpointStateFromBackend(byEndpoint[definition.id] || {
          endpoint_id: definition.id,
          source_id: definition.sourceId,
          feed_label: definition.feedLabel,
          status_level: "critical",
          status_label: "Player Down",
          root_cause: "Endpoint Missing",
          status_detail: "Core did not include this expected endpoint in the stream.",
          now_playing: { title:"Not reported by sender", detail:"Endpoint snapshot missing." },
          diagnostics: [{ label:"Root cause", value:"Endpoint Missing" }]
        });
      });
    }
    var bySource = sourceMap(data);
    return ENDPOINTS.map(function (endpoint) { return endpointState(endpoint, data, bySource); });
  }

  function detailRows(state) {
    if (Array.isArray(state.diagnostics) && state.diagnostics.length) {
      return [
        { label:"Endpoint", value:state.endpoint.label + " / " + state.endpoint.feedLabel },
        { label:"Monitoring model", value:state.endpoint.method },
        { label:"Status basis", value:state.status.detail }
      ].concat(state.diagnostics.map(function (row) {
        // the listener sends this one as a raw ISO time; show it the way the rest of the page shows times
        return row.label === "Last heard" && !isNaN(parseIso(row.value)) ? { label:row.label, value:coreTimestamp(row.value) } : row;
      }));
    }
    var source = state.source;
    var instance = state.instance;
    var payload = state.payload;
    var rows = [
      { label:"Endpoint", value:state.endpoint.label + " / " + state.endpoint.feedLabel },
      { label:"Monitoring model", value:state.endpoint.method },
      { label:"Status basis", value:state.status.detail },
      { label:"Last heard full time", value:source ? fullTimestamp(source.last_seen_utc) : "never" }
    ];
    if (source && source.expected_interval_seconds) {
      rows.push({ label:"Expected interval", value:source.expected_interval_seconds + "s" });
    }
    rows.push({ label:"Now playing", value:state.nowPlaying.title + (state.nowPlaying.detail ? " / " + state.nowPlaying.detail : "") });
    rows.push({ label:"PID", value:pidText(instance, payload) });
    rows.push({ label:"Engine state", value:engineText(instance, source, payload) });
    var motionFields = instance || payload;
    var motionAge = motionFields ? numericValue(motionFields.advance_age_seconds) : null;
    if (motionAge !== null) {
      rows.push({ label:"Advancement age", value:Math.round(motionAge) + "s" });
    }
    if (motionFields && motionFields.advance_source) {
      rows.push({ label:"Motion source", value:motionFields.advance_source });
    }
    if (instance && instance.start_time) {
      rows.push({ label:"Process start", value:fullTimestamp(instance.start_time) });
    }
    if (source && source.instance_alert) {
      rows.push({ label:"Instance alert state", value:source.instance_alert });
    }
    if (state.latest) {
      rows.push({ label:"Latest event", value:eventSummary(state.latest) });
    }
    if (typeof payload.extra_instances_ignored === "number" && payload.extra_instances_ignored > 0) {
      rows.push({ label:"Extra instances", value:payload.extra_instances_ignored + " additional MAIN process pair(s) ignored by design." });
    }
    return rows;
  }

  // The diagnostics rows and the event cards are updated IN PLACE: a value's text is touched only when it actually changed, so
  // the panel is never rebuilt under the operator's cursor while they are selecting or copying something in it.
  function syncRows(table, rows) {
    var kept = table._rows || [];
    var sameShape = kept.length === rows.length && kept.every(function (item, index) { return item.label === rows[index].label; });
    if (!sameShape) {
      table.textContent = "";
      kept = rows.map(function (row) {
        var wrapper = el("div", undefined, "diag-row");
        wrapper.appendChild(el("div", row.label, "diag-label"));
        var valueNode = el("div", "", "diag-value");
        wrapper.appendChild(valueNode);
        table.appendChild(wrapper);
        return { label: row.label, valueNode: valueNode, value: null };
      });
      table._rows = kept;
    }
    rows.forEach(function (row, index) {
      var value = row.value || "not reported";
      if (kept[index].value !== value) { kept[index].valueNode.textContent = value; kept[index].value = value; }
    });
  }

  function syncEvents(list, state) {
    var events = state.failures.length ? state.failures : state.events.slice(0, 3);
    if (!events.length && state.latest) { events = [state.latest]; }
    var empty = events.length === 0;
    var wanted = empty
      ? [{ name: "", copy: "No recent failures reported for this endpoint." }]
      : events.map(function (event) { return { name: titleCase(event.event_type) + " / " + String(event.severity || "info"), copy: eventSummary(event) }; });
    if (!list._items || list._items.length !== wanted.length || list._empty !== empty) {
      list.textContent = "";
      list._empty = empty;
      list._items = wanted.map(function () {
        if (empty) {
          var note = el("div", "", "foot");
          list.appendChild(note);
          return { nameNode: null, copyNode: note, name: null, copy: null };
        }
        var item = el("div", undefined, "event-item");
        var nameNode = el("div", "", "event-name");
        var copyNode = el("div", "", "event-copy");
        item.appendChild(nameNode);
        item.appendChild(copyNode);
        list.appendChild(item);
        return { nameNode: nameNode, copyNode: copyNode, name: null, copy: null };
      });
    }
    wanted.forEach(function (entry, index) {
      var item = list._items[index];
      if (item.nameNode && item.name !== entry.name) { item.nameNode.textContent = entry.name; item.name = entry.name; }
      if (item.copy !== entry.copy) { item.copyNode.textContent = entry.copy; item.copy = entry.copy; }
    });
  }

  // A tile's details: built once, then kept up to date by syncRows and syncEvents.
  function createDetailsGrid() {
    var grid = el("div", undefined, "details-grid");
    var diagGroup = el("div", undefined, "detail-group");
    diagGroup.appendChild(el("div", "Diagnostics", "detail-heading"));
    var table = el("div", undefined, "diag-table");
    diagGroup.appendChild(table);
    var eventGroup = el("div", undefined, "detail-group");
    eventGroup.appendChild(el("div", "Recent failures and events", "detail-heading"));
    var list = el("div", undefined, "event-list");
    eventGroup.appendChild(list);
    grid.appendChild(diagGroup);
    grid.appendChild(eventGroup);
    return { grid: grid, table: table, list: list };
  }

  function durationText(totalSeconds) {
    var s = Math.max(0, Math.round(totalSeconds));
    if (s < 60) { return s + "s"; }
    if (s < 3600) { return Math.floor(s / 60) + "m " + (s % 60) + "s"; }
    var h = Math.floor(s / 3600);
    if (h < 48) { return h + "h " + Math.floor((s % 3600) / 60) + "m"; }
    return Math.floor(h / 24) + "d " + (h % 24) + "h";
  }

  function positionText(totalSeconds) {   // a position inside an item, m:ss or h:mm:ss
    var s = Math.max(0, Math.round(totalSeconds));
    var h = Math.floor(s / 3600);
    var m = Math.floor((s % 3600) / 60);
    var sec = s % 60;
    return (h ? h + ":" + (m < 10 ? "0" : "") + m : String(m)) + ":" + (sec < 10 ? "0" : "") + sec;
  }

  // ---- Playout tiles ---------------------------------------------------------------------------------------------
  // Every tile keeps its elements and only the text and classes that changed are touched. Rebuilding all six on every
  // pushed frame (the old way) threw away scroll position, text selection and every running animation, five times a minute.
  var tiles = {};        // endpoint id -> tile parts
  var topoTile = null;   // the one "selected endpoint" tile on the topology page

  function createTile(endpointId, forceOpen) {
    var article = el("article", undefined, "endpoint-tile");
    var detailsId = "details-" + endpointId + (forceOpen ? "-selected" : "");
    var face = document.createElement("button");
    face.type = "button";
    face.className = "endpoint-face";
    face.setAttribute("aria-controls", detailsId);

    var top = el("div", undefined, "tile-top");
    var titleWrap = el("div");
    var name = el("div", "", "endpoint-name");
    var meta = el("div", "", "endpoint-meta");
    titleWrap.appendChild(name);
    titleWrap.appendChild(meta);
    var badge = el("div", "", "badge");
    top.appendChild(titleWrap);
    top.appendChild(badge);
    face.appendChild(top);

    var screen = el("div", undefined, "screen");
    var label = el("div", undefined, "now-label");
    label.appendChild(el("span", undefined, "air-dot"));
    var labelText = el("span", "On air");
    label.appendChild(labelText);
    var title = el("div", "", "now-title");
    var progress = el("div", undefined, "now-progress");
    var bar = el("span", undefined, "now-bar");
    bar.setAttribute("aria-hidden", "true");
    var barFill = document.createElement("i");
    bar.appendChild(barFill);
    var time = el("span", "", "now-time");
    progress.appendChild(bar);
    progress.appendChild(time);
    var sub = el("div", "", "now-sub");
    var age = el("div", "", "now-age");
    var next = el("div", "", "now-next");
    [label, title, progress, sub, age, next].forEach(function (node) { screen.appendChild(node); });
    face.appendChild(screen);

    var tele = el("div", undefined, "tile-telemetry");
    var rows = {};
    ["Last heard", "Motion", "Engine", "PID"].forEach(function (labelName) {
      var mini = el("div", undefined, "mini-row");
      mini.appendChild(el("div", labelName, "mini-label"));
      rows[labelName] = el("div", "", "mini-value");
      mini.appendChild(rows[labelName]);
      tele.appendChild(mini);
    });
    face.appendChild(tele);

    var foot = el("div", undefined, "tile-footer");
    var footStatus = el("span");
    var footAction = el("span");
    foot.appendChild(footStatus);
    foot.appendChild(footAction);
    face.appendChild(foot);
    article.appendChild(face);

    var details = el("div", undefined, "details-panel");
    details.id = detailsId;
    article.appendChild(details);

    var part = {
      id: endpointId, forceOpen: !!forceOpen, article: article, face: face, name: name, meta: meta, badge: badge, screen: screen,
      labelText: labelText, title: title, progress: progress, barFill: barFill, time: time, sub: sub, age: age, next: next,
      rows: rows, footStatus: footStatus, footAction: footAction, details: details, detailsParts: null, state: null
    };
    if (!forceOpen) {
      face.addEventListener("click", function () {
        uiState.expanded[endpointId] = !uiState.expanded[endpointId];
        uiState.selectedEndpointId = endpointId;
        if (part.state) { updateTile(part, part.state); }
      });
    }
    return part;
  }

  function updateTile(part, state) {
    part.state = state;
    var endpoint = state.endpoint;
    var isOpen = part.forceOpen || !!uiState.expanded[endpoint.id];
    var byLabel = {};
    state.telemetry.forEach(function (row) { byLabel[row.label] = row.value; });
    part.article.className = "endpoint-tile " + state.statusClass + (part.forceOpen ? " selected" : (isOpen ? " is-open" : ""));
    part.name.textContent = endpoint.label;
    part.meta.textContent = endpoint.location + " / " + endpoint.feedLabel;
    part.badge.className = "badge " + (state.status.badgeClass || "");
    part.badge.textContent = badgeText(state.status);
    part.face.setAttribute("aria-expanded", String(isOpen));
    part.title.textContent = state.nowPlaying.title;
    part.title.title = state.nowPlaying.title;
    part.title.className = "now-title" + (state.nowPlaying.missing ? " missing" : "");
    // "Reported by heartbeat" only repeats what the age line below says, so it is not shown.
    var detailText = state.nowPlaying.detail === "Reported by heartbeat" ? "" : (state.nowPlaying.detail || "");
    part.sub.textContent = detailText;
    part.sub.hidden = detailText === "";
    part.next.textContent = state.nowPlaying.next ? "Next: " + state.nowPlaying.next : "";
    part.next.hidden = !state.nowPlaying.next;
    part.rows["Last heard"].textContent = byLabel["Last heard"] || "never";
    part.rows.Motion.textContent = byLabel.Motion || "not reported";
    part.rows.Engine.textContent = byLabel.Engine || "not reported";
    part.rows.PID.textContent = byLabel.PID || "not reported";
    part.footStatus.textContent = state.status.label;
    part.footAction.textContent = part.forceOpen ? "Selected endpoint" : (isOpen ? "Hide details" : "Open details");
    part.details.hidden = !isOpen;
    if (isOpen) {
      if (!part.detailsParts) {
        part.detailsParts = createDetailsGrid();
        part.details.appendChild(part.detailsParts.grid);
      }
      syncRows(part.detailsParts.table, detailRows(state));
      syncEvents(part.detailsParts.list, state);
    }
    refreshNowPlaying(part);
  }

  // The age and the position in the item run on this device's own clock from the moment the frame arrived, so they keep
  // counting between the 5-second frames and a wrong clock on the phone cannot distort them.
  // A site that reports every 5 minutes by design (SITE-B) is not "stale" 60 seconds after its report: the name is only called old once a whole
  // reporting interval plus 45 seconds have passed (never less than STALE_NOW_PLAYING_S). That is just before the listener itself calls the
  // heartbeat lost (interval + 60 s), so the "last reported" state is still reachable for every site, SITE-B included.
  function staleAfterSeconds(state) {
    var interval = state && state.source ? numericValue(state.source.expected_interval_seconds) : null;
    return Math.max(STALE_NOW_PLAYING_S, (interval || 0) + 45);
  }

  function nowPlayingTiming(nowPlaying, state) {
    var sinceFrame = uiState.frameAt ? (Date.now() - uiState.frameAt) / 1000 : 0;
    var age = typeof nowPlaying.age_s === "number" ? nowPlaying.age_s + sinceFrame : null;
    // While the item is paused the position stands still: it is the position at the heartbeat, with nothing added for the time since.
    var paused = !!nowPlaying.paused;
    var elapsed = typeof nowPlaying.elapsed_s === "number" ? nowPlaying.elapsed_s + (paused ? 0 : sinceFrame) : null;
    var duration = typeof nowPlaying.duration_s === "number" && nowPlaying.duration_s > 0 ? nowPlaying.duration_s : null;
    var finished = !paused && elapsed !== null && duration !== null && elapsed > duration + 5;
    var stale = !nowPlaying.missing && ((age !== null && age > staleAfterSeconds(state)) || finished);
    return { age: age, elapsed: elapsed, duration: duration, finished: finished, stale: stale, paused: paused };
  }

  // What a player says about itself, decided in ONE place so the dashboard tile, the topology map card and the phone layout can never disagree:
  // a critical player is Off air, a name that has not been refreshed lately is Last reported, anything else is On air.
  function airStatus(state, timing) {
    if (state.status.level === "critical") { return { text: "Off air", mode: "off" }; }
    if (timing.stale) { return { text: "Last reported", mode: "stale" }; }
    return { text: "On air", mode: "live" };
  }

  // The progress bar is ALWAYS drawn, for every player, on every view. A player that reports when its file started and how long it is gets a live
  // bar; a player that has not reported them (yet) still shows an empty bar that says so, so a missing bar can never be mistaken for a page that
  // stopped. A critical player reads "Off air" and a lost feed dims the bar where it stood. The bar is decorative (the words beside it carry the meaning).
  function paintProgress(parts, state, timing) {
    var nowPlaying = state.nowPlaying;
    var mode = "unknown";
    var text = "Length not reported";
    var width = 0;
    var haveTiming = !nowPlaying.missing && timing.elapsed !== null && timing.duration !== null;
    var estimated = false;
    if (state.status.level === "critical") {
      mode = "off"; text = "Off air";
    } else if (uiState.blind) {
      mode = "stale"; text = "Live feed lost";
      if (haveTiming && typeof nowPlaying.elapsed_s === "number") { width = 100 * Math.min(nowPlaying.elapsed_s, timing.duration) / timing.duration; }
    } else if (nowPlaying.missing) {
      mode = "none"; text = "No file reported";
    } else if (haveTiming) {
      var shown = Math.min(timing.elapsed, timing.duration);
      width = 100 * shown / timing.duration;
      text = positionText(shown) + " / " + positionText(timing.duration);
      mode = timing.stale ? "stale" : (timing.paused ? "paused" : "live");
      if (mode === "paused") { text = "Paused " + text; }
      // Worked out from Insta's block schedule, not read from the player: shown with a "~" so nobody takes it for a measured position.
      if (nowPlaying.estimated) { text = "~" + text + " est."; estimated = true; }
    } else if (nowPlaying.timing_suspect) {
      text = "Start time looks wrong";   // the sender's start cannot be true (hours early or in the future): not drawn, and said so
    }
    parts.wrap.className = parts.base + " is-" + mode + (estimated ? " is-estimated" : "");
    parts.time.title = estimated ? "Estimated from Insta's block schedule for this channel: it can differ from the player by a few minutes" : "";
    parts.fill.style.width = width.toFixed(1) + "%";
    parts.time.textContent = text;
  }

  function refreshNowPlaying(part) {
    if (!part || !part.state) { return; }
    var nowPlaying = part.state.nowPlaying;
    var timing = nowPlayingTiming(nowPlaying, part.state);
    var air = airStatus(part.state, timing);
    var offAir = air.mode === "off";
    part.screen.className = "screen" + (offAir ? " off-air" : (air.mode === "stale" ? " stale" : ""));
    part.labelText.textContent = air.text;
    var ageText = "";
    if (!nowPlaying.missing && timing.age !== null) {
      ageText = "Reported " + durationText(timing.age) + " ago";
      var interval = part.state.source ? numericValue(part.state.source.expected_interval_seconds) : null;
      if (timing.finished) { ageText += " - the item has finished, the next one is not reported yet"; }
      else if (timing.stale) { ageText += " - may have changed"; }
      else if (interval !== null && interval >= 60) { ageText += " - this site reports every " + durationText(interval); }
    }
    part.age.textContent = ageText;
    part.age.className = "now-age" + (timing.stale && !offAir ? " stale" : "");
    part.age.hidden = ageText === "";
    paintProgress({ wrap: part.progress, base: "now-progress", fill: part.barFill, time: part.time }, part.state, timing);
  }

  function renderDashboard(states) {
    var signature = states.map(function (state) { return state.endpoint.id; }).join("|");
    if (uiState.tileSignature !== signature) {
      endpointGrid.textContent = "";
      tiles = {};
      states.forEach(function (state) {
        var part = createTile(state.endpoint.id, false);
        tiles[state.endpoint.id] = part;
        endpointGrid.appendChild(part.article);
      });
      uiState.tileSignature = signature;
    }
    states.forEach(function (state) { updateTile(tiles[state.endpoint.id], state); });
  }

  function incidentTextFromEvent(event) {
    // The listener's own critical-alarm records carry the playout name and root cause; say exactly that.
    var detail = parseJson(event.detail, {});
    if (detail.endpoint && detail.root_cause) {
      return detail.endpoint + ": " + detail.root_cause + (detail.message ? " - " + detail.message : "");
    }
    var source = event.source_id || "Core";
    var name = event.event_type ? titleCase(event.event_type) : "Event";
    var message = event.message || eventSummary(event);
    return source + ": " + name + " - " + message;
  }

  function incidentLevel(event) {
    var severity = String(event.severity || "").toLowerCase();
    if (severity === "critical" || severity === "major") { return "critical"; }
    if (severity === "warning" || severity === "minor") { return "warning"; }
    return "attention";
  }

  function renderIncidentFeed(states, data) {
    if (!incidentList) { return; }
    incidentList.textContent = "";
    // The Site C ZIXI Feed's critical streams are listed from the live state, first; its old event rows are left out so one alarm is not listed twice.
    var vzIncidents = vzAlarmLines(data.site_c_feed).map(function (line) { return { level: "critical", time: line.since, text: line.text }; });
    var incidents = (data.recent_events || []).filter(function (event) { return event.source_id !== "SITE-C" && eventIsFailureLike(event); }).slice(0, 10).map(function (event) {
      return { level:incidentLevel(event), time:event.timestamp_utc, text:incidentTextFromEvent(event) };
    });
    if (!incidents.length) {
      states.filter(function (state) { return state.status.level !== "healthy"; }).forEach(function (state) {
        incidents.push({
          level:state.status.level,
          time:state.source && state.source.last_seen_utc,
          text:state.endpoint.label + ": " + state.status.root_cause + " - " + state.status.detail
        });
      });
    }
    incidents = vzIncidents.concat(incidents).slice(0, 10);
    if (!incidents.length) {
      incidentList.appendChild(el("div", "No active incidents in the current stream.", "incident-empty"));
      return;
    }
    incidents.forEach(function (incident) {
      var item = el("div", undefined, "incident-entry " + statusClass({ level:incident.level }));
      item.appendChild(el("span", incident.time ? coreTimestamp(incident.time) : "live state", "incident-time"));
      item.appendChild(el("div", incident.text, "incident-text"));
      incidentList.appendChild(item);
    });
  }

  // ---- Topology: a command-deck display -------------------------------------------------------------
  // The six playouts sit in two columns beside Core in a real CSS grid, so a card can grow taller for
  // longer text without ever touching another. The connector lines are drawn from where the cards
  // ACTUALLY are (measured), and redrawn whenever the page, a card or its text changes size.
  var holo = { stage:null, svg:null, core:null, stats:null, cpu:null, nodes:{}, columns:{ left:[], right:[] }, signature:"", observer:null, states:[] };

  function holoStat(label) {
    var box = el("div", undefined, "holo-stat");
    var number = el("b", "0");
    box.appendChild(number);
    box.appendChild(el("span", label));
    return { box:box, number:number };
  }

  function buildHolo(states) {
    if (holo.observer) { holo.observer.disconnect(); }
    var stage = el("div", undefined, "holo-stage");
    stage.appendChild(el("div", undefined, "holo-stars"));
    stage.appendChild(el("div", undefined, "holo-radar"));
    stage.appendChild(el("div", undefined, "holo-scan"));
    var lines = svg("svg");
    lines.setAttribute("class", "holo-lines");
    lines.setAttribute("aria-hidden", "true");
    stage.appendChild(lines);

    var grid = el("div", undefined, "holo-grid");
    var leftColumn = el("div", undefined, "holo-col holo-left");
    var rightColumn = el("div", undefined, "holo-col holo-right");
    var coreWrap = el("div", undefined, "holo-core");
    var coreCard = el("div", undefined, "holo-core-card");
    coreCard.appendChild(el("span", "Core // New York", "holo-core-tag"));
    coreCard.appendChild(el("div", "Core", "holo-core-title"));
    var statsRow = el("div", undefined, "holo-stats");
    var stats = { endpoints:holoStat("Endpoints"), critical:holoStat("Critical"), watch:holoStat("Watch") };
    statsRow.appendChild(stats.endpoints.box);
    statsRow.appendChild(stats.critical.box);
    statsRow.appendChild(stats.watch.box);
    coreCard.appendChild(statsRow);
    var cpuRow = el("div", undefined, "holo-cpu");
    cpuRow.appendChild(el("span", "CPU"));
    var cpuTrack = el("div", undefined, "holo-cpu-track");
    var cpuFill = document.createElement("i");
    cpuTrack.appendChild(cpuFill);
    var cpuValue = el("b", "--");
    cpuRow.appendChild(cpuTrack);
    cpuRow.appendChild(cpuValue);
    coreCard.appendChild(cpuRow);
    coreWrap.appendChild(coreCard);

    holo.nodes = {};
    holo.columns = { left:[], right:[] };
    states.slice().sort(function (a, b) { return a.endpoint.y - b.endpoint.y; }).forEach(function (state, index) {
      var endpoint = state.endpoint;
      var side = endpoint.x < 50 ? "left" : "right";
      var node = el("div", undefined, "holo-node");
      var button = document.createElement("button");
      button.type = "button";
      var tag = el("span", "Node 0" + (index + 1) + " // " + endpoint.location, "holo-tag");
      var head = el("div", undefined, "holo-head");
      var names = el("div");
      var name = el("div", endpoint.label, "holo-name");
      names.appendChild(name);
      names.appendChild(el("div", endpoint.feedLabel, "holo-sub"));
      var badge = el("div", "", "badge");
      head.appendChild(names);
      head.appendChild(badge);
      var mirror = mirrorParts("");
      var now = el("div", "", "holo-now");
      var rows = el("div", undefined, "holo-rows");
      var rowValues = {};
      ["Last", "Motion"].forEach(function (label) {
        var line = el("div", undefined, "holo-row");
        line.appendChild(el("span", label));
        rowValues[label] = el("span", "");
        line.appendChild(rowValues[label]);
        rows.appendChild(line);
      });
      button.appendChild(tag);
      button.appendChild(head);
      button.appendChild(mirror.air);
      button.appendChild(now);
      button.appendChild(mirror.progress.wrap);
      button.appendChild(rows);
      button.addEventListener("click", function () { selectEndpoint(endpoint.id); });
      node.appendChild(button);
      (side === "left" ? leftColumn : rightColumn).appendChild(node);
      holo.columns[side].push(endpoint.id);

      // the link to Core: a faint base line, a bright flowing line, a port at each end and a data packet
      var link = svg("g");
      link.setAttribute("class", "holo-link");
      var base = svg("path"); base.setAttribute("class", "holo-base");
      var flow = svg("path"); flow.setAttribute("class", "holo-flow");
      flow.setAttribute("id", "holo-flow-" + index);
      var portFrom = svg("circle"); portFrom.setAttribute("class", "holo-port"); portFrom.setAttribute("r", "4.5");
      var portTo = svg("circle"); portTo.setAttribute("class", "holo-port"); portTo.setAttribute("r", "4.5");
      var packet = svg("circle"); packet.setAttribute("class", "holo-packet"); packet.setAttribute("r", "3");
      [base, flow, portFrom, portTo, packet].forEach(function (part) { link.appendChild(part); });
      lines.appendChild(link);
      holo.nodes[endpoint.id] = { el:node, button:button, tag:tag, name:name, badge:badge, now:now, mirror:mirror, rows:rowValues, side:side, index:index,
        link:link, base:base, flow:flow, portFrom:portFrom, portTo:portTo, packet:packet, path:"" };
    });

    grid.appendChild(leftColumn);
    grid.appendChild(coreWrap);
    grid.appendChild(rightColumn);
    stage.appendChild(grid);
    holo.stage = stage;
    holo.svg = lines;
    holo.core = coreCard;
    holo.stats = stats;
    holo.cpu = { fill:cpuFill, value:cpuValue };
    if (window.ResizeObserver) {
      holo.observer = new ResizeObserver(function () { layoutHoloLines(); });
      holo.observer.observe(stage);
      Object.keys(holo.nodes).forEach(function (id) { holo.observer.observe(holo.nodes[id].el); });
    }
    if (document.fonts && document.fonts.ready) { document.fonts.ready.then(function () { layoutHoloLines(); }); }
  }

  // Cheap and idempotent: only text, classes and attributes change, so the animations keep running between frames.
  function updateHolo(states) {
    holo.states = states;
    states.forEach(function (state) {
      var part = holo.nodes[state.endpoint.id];
      if (!part) { return; }
      var telemetryByLabel = {};
      state.telemetry.forEach(function (row) { telemetryByLabel[row.label] = row.value; });
      part.el.className = "holo-node " + state.statusClass;
      part.link.setAttribute("class", "holo-link " + state.statusClass);
      part.badge.className = "badge " + (state.status.badgeClass || "");
      part.badge.textContent = badgeText(state.status);
      part.button.setAttribute("aria-pressed", String(uiState.selectedEndpointId === state.endpoint.id));
      part.button.setAttribute("aria-label", state.endpoint.label + ", " + badgeText(state.status) + ". Open details.");
      part.now.textContent = state.nowPlaying.title;
      part.now.title = state.nowPlaying.title;
      var mirrored = paintMirror(part.mirror, state);
      part.now.className = "holo-now" + (mirrored.air.mode === "off" ? " is-off-air" : (mirrored.air.mode === "stale" ? " is-stale" : ""));
      part.rows.Last.textContent = telemetryByLabel["Last heard"] || (state.source ? ageText(state.source.last_seen_utc) : "never");
      part.rows.Motion.textContent = telemetryByLabel.Motion || "not reported";
    });
    var critical = states.filter(function (s) { return s.status.level === "critical"; }).length;
    var watch = states.filter(function (s) { return s.status.level === "warning" || s.status.level === "attention" || s.status.level === "offline"; }).length;
    holo.stats.endpoints.number.textContent = String(states.length);
    holo.stats.critical.number.textContent = String(critical);
    holo.stats.critical.box.className = "holo-stat" + (critical ? " is-alarm" : "");
    holo.stats.watch.number.textContent = String(watch);
    holo.stats.watch.box.className = "holo-stat" + (watch ? " is-watch" : "");
    var system = uiState.lastData.system;
    if (holo.cpu && system && typeof system.cpu_percent === "number") {
      holo.cpu.fill.style.width = Math.max(0, Math.min(100, system.cpu_percent)).toFixed(0) + "%";
      holo.cpu.value.textContent = Math.round(system.cpu_percent) + "%";
    }
  }

  // Lines run from the inner edge of each card, at its own middle, to the matching side of the Core card, fanned out evenly.
  function layoutHoloLines() {
    if (!holo.stage || !holo.svg || !holo.core) { return; }
    var stageRect = holo.stage.getBoundingClientRect();
    if (!stageRect.width || !holo.core.getBoundingClientRect().width) { return; }   // hidden (phone layout)
    holo.svg.setAttribute("viewBox", "0 0 " + Math.round(stageRect.width) + " " + Math.round(stageRect.height));
    var coreRect = holo.core.getBoundingClientRect();
    ["left", "right"].forEach(function (side) {
      var ids = holo.columns[side];
      ids.forEach(function (id, position) {
        var part = holo.nodes[id];
        var rect = part.el.getBoundingClientRect();
        var x1 = (side === "left" ? rect.right : rect.left) - stageRect.left;
        var y1 = rect.top + rect.height / 2 - stageRect.top;
        var x2 = (side === "left" ? coreRect.left : coreRect.right) - stageRect.left;
        var y2 = coreRect.top - stageRect.top + coreRect.height * (position + 1) / (ids.length + 1);
        var mid = (x1 + x2) / 2;
        var d = "M " + x1.toFixed(1) + " " + y1.toFixed(1) + " C " + mid.toFixed(1) + " " + y1.toFixed(1) + ", " + mid.toFixed(1) + " " + y2.toFixed(1) + ", " + x2.toFixed(1) + " " + y2.toFixed(1);
        part.portFrom.setAttribute("cx", x1.toFixed(1)); part.portFrom.setAttribute("cy", y1.toFixed(1));
        part.portTo.setAttribute("cx", x2.toFixed(1)); part.portTo.setAttribute("cy", y2.toFixed(1));
        if (part.path === d) { return; }
        part.path = d;
        part.base.setAttribute("d", d);
        part.flow.setAttribute("d", d);
        // the data packet rides the line from the playout to Core; recreated only when the line itself moved
        while (part.packet.firstChild) { part.packet.removeChild(part.packet.firstChild); }
        var motion = svg("animateMotion");
        motion.setAttribute("dur", "3.6s");
        motion.setAttribute("begin", (part.index * 0.55).toFixed(2) + "s");
        motion.setAttribute("repeatCount", "indefinite");
        motion.setAttribute("path", d);
        part.packet.appendChild(motion);
      });
    });
  }

  // kept under its old name: the resize handler and the page switch call this
  function fitTopologyPlane() { layoutHoloLines(); }

  function overallLevel(states) {
    if (states.some(function (s) { return s.status.level === "critical"; })) { return "critical"; }
    if (states.some(function (s) { return s.status.level === "warning"; })) { return "warning"; }
    if (states.some(function (s) { return s.status.level === "attention" || s.status.level === "offline"; })) { return "attention"; }
    return "healthy";
  }

  // One tap target for both layouts: pick the endpoint, open its details, and (on a phone, where the
  // details sit below the stack) bring them into view.
  function selectEndpoint(endpointId) {
    uiState.selectedEndpointId = endpointId;
    uiState.userSelected = true;
    uiState.scrollToDetail = true;
    renderAll(uiState.lastData);
  }

  // Until the operator taps something, the details panel follows the playout that most needs
  // attention (critical, then yellow, then the rest), so a problem is on screen without a hunt.
  function mostUrgentEndpointId(states) {
    var rank = { critical:0, warning:1, attention:2, offline:2, healthy:3 };
    var best = states[0];
    states.forEach(function (state) {
      if ((rank[state.status.level] === undefined ? 3 : rank[state.status.level]) < (rank[best.status.level] === undefined ? 3 : rank[best.status.level])) { best = state; }
    });
    return best ? best.endpoint.id : null;
  }

  // The topology map mirrors the dashboard: every playout card carries the same On air / Off air / Last reported line and the same progress bar,
  // decided by the same two functions (airStatus, paintProgress). One builder makes the pair for both the map card and the phone layout.
  function mirrorParts(extraClass) {
    var air = el("div", undefined, "holo-air" + (extraClass ? " " + extraClass : ""));
    air.appendChild(el("span", undefined, "air-dot"));
    var airText = el("span", "On air");
    air.appendChild(airText);
    var wrap = el("div", undefined, "now-progress holo-progress");
    var bar = el("span", undefined, "now-bar");
    bar.setAttribute("aria-hidden", "true");
    var fill = document.createElement("i");
    bar.appendChild(fill);
    var time = el("span", "", "now-time");
    wrap.appendChild(bar);
    wrap.appendChild(time);
    return { air: air, airText: airText, airBase: "holo-air" + (extraClass ? " " + extraClass : ""),
      progress: { wrap: wrap, base: "now-progress holo-progress", fill: fill, time: time } };
  }

  function paintMirror(parts, state) {
    var timing = nowPlayingTiming(state.nowPlaying, state);
    var air = airStatus(state, timing);
    parts.air.className = parts.airBase + " is-" + air.mode;
    parts.airText.textContent = air.text;
    paintProgress(parts.progress, state, timing);
    return { timing: timing, air: air };
  }

  // Once a second, like the tiles: the position moves and "On air" turns into "Last reported" the moment a name goes stale.
  function refreshMirrors() {
    if (!uiState.states) { return; }
    uiState.states.forEach(function (state) {
      var node = holo.nodes && holo.nodes[state.endpoint.id];
      if (node) {
        var result = paintMirror(node.mirror, state);
        node.now.className = "holo-now" + (result.air.mode === "off" ? " is-off-air" : (result.air.mode === "stale" ? " is-stale" : ""));
      }
      var stackPart = stackParts[state.endpoint.id];
      if (stackPart) { paintMirror(stackPart, state); }
    });
  }

  var stackParts = {};   // endpoint id -> the phone layout's label and bar (rebuilt with the layout on every frame)

  function stackNode(state) {
    var endpoint = state.endpoint;
    var node = el("div", undefined, "stack-node " + state.statusClass);
    var button = document.createElement("button");
    button.type = "button";
    button.setAttribute("aria-pressed", String(uiState.selectedEndpointId === endpoint.id));
    button.setAttribute("aria-label", endpoint.label + ", " + badgeText(state.status) + ". Tap for details.");
    var title = el("div", undefined, "stack-title");
    var names = el("div");
    names.appendChild(el("div", endpoint.label, "stack-name"));
    names.appendChild(el("div", endpoint.feedLabel, "stack-sub"));
    title.appendChild(names);
    title.appendChild(el("div", badgeText(state.status), "badge " + (state.status.badgeClass || "")));
    button.appendChild(title);
    var mirror = mirrorParts("stack-air");
    stackParts[endpoint.id] = mirror;
    button.appendChild(mirror.air);
    button.appendChild(mirror.progress.wrap);
    paintMirror(mirror, state);
    var byLabel = {};
    state.telemetry.forEach(function (row) { byLabel[row.label] = row.value; });
    [
      { label:"Motion", value:byLabel.Motion || "not reported" },
      { label:"Last heard", value:byLabel["Last heard"] || "never" }
    ].forEach(function (row) {
      var line = el("div", undefined, "stack-line");
      line.appendChild(el("b", row.label));
      line.appendChild(document.createTextNode(String(row.value)));
      button.appendChild(line);
    });
    button.addEventListener("click", function () { selectEndpoint(endpoint.id); });
    node.appendChild(button);
    return node;
  }

  // The phone layout: Core on top, a spine, then the playouts two to a row. Shown by CSS on
  // narrow screens in either orientation; the fixed-size map is shown on wide ones.
  function renderTopologyStack(states) {
    var stack = el("div", undefined, "topology-stack stack-overall-" + overallLevel(states));
    var critical = states.filter(function (s) { return s.status.level === "critical"; }).length;
    var watch = states.filter(function (s) { return s.status.level === "warning" || s.status.level === "attention" || s.status.level === "offline"; }).length;
    var hub = el("div", undefined, "stack-hub");
    hub.appendChild(el("div", "Core", "core-title"));
    hub.appendChild(el("div", critical + " critical / " + watch + " watch / " + states.length + " playouts", "node-sub"));
    stack.appendChild(hub);
    var body = el("div", undefined, "stack-body");
    for (var i = 0; i < states.length; i += 2) {
      var row = el("div", undefined, "stack-row");
      states.slice(i, i + 2).forEach(function (state) { row.appendChild(stackNode(state)); });
      body.appendChild(row);
    }
    stack.appendChild(body);
    return stack;
  }

  function renderTopology(states) {
    if (!uiState.userSelected) { uiState.selectedEndpointId = mostUrgentEndpointId(states); }
    var signature = states.map(function (state) { return state.endpoint.id; }).join("|");
    if (!holo.stage || holo.signature !== signature || !topologyRoot.contains(holo.stage)) {
      topologyRoot.textContent = "";
      buildHolo(states);
      topologyRoot.appendChild(holo.stage);
      holo.signature = signature;
    }
    updateHolo(states);
    var oldStack = topologyRoot.querySelector(".topology-stack");
    if (oldStack) { topologyRoot.removeChild(oldStack); }
    topologyRoot.appendChild(renderTopologyStack(states));

    var selected = states.find(function (state) { return state.endpoint.id === uiState.selectedEndpointId; }) || states[0];
    if (selected) {
      if (!topoTile || topoTile.id !== selected.endpoint.id || !topologyDetail.contains(topoTile.article)) {
        topologyDetail.textContent = "";
        topoTile = createTile(selected.endpoint.id, true);
        topologyDetail.appendChild(topoTile.article);
      }
      updateTile(topoTile, selected);
    }
    layoutHoloLines();
    if (uiState.scrollToDetail) {
      uiState.scrollToDetail = false;
      if (window.matchMedia("(max-width:980px)").matches) {
        topologyDetail.scrollIntoView({ behavior:"smooth", block:"start" });
      }
    }
  }

  function internetProbe(url) {
    var controller = window.AbortController ? new AbortController() : null;
    var stop = setTimeout(function () { if (controller) { controller.abort(); } }, PROBE_TIMEOUT_MS);
    var options = { mode:"no-cors", cache:"no-store", credentials:"omit", referrerPolicy:"no-referrer" };
    if (controller) { options.signal = controller.signal; }
    return fetch(url + "?_=" + Date.now(), options).then(
      function () { clearTimeout(stop); return true; },
      function () { clearTimeout(stop); return false; }
    );
  }

  function probeInternet() {
    return new Promise(function (resolve) {
      var pending = PROBES.length;
      PROBES.forEach(function (url) {
        internetProbe(url).then(function (reachable) {
          if (reachable) { resolve(true); return; }
          pending -= 1;
          if (pending === 0) { resolve(false); }
        });
      });
    });
  }

  function blindReason() {
    if (navigator.onLine === false) { return "This device is offline."; }
    if (uiState.internet === false) { return "This device has no internet connection."; }
    if (uiState.internet === true) { return "This device's internet works, but Core is not answering."; }
    return "Checking whether the problem is this device's internet or Core.";
  }

  function renderBanner(states) {
    var critical = states.filter(function (state) { return state.status.level === "critical"; });
    var vzLines = vzAlarmLines(uiState.lastData ? uiState.lastData.site_c_feed : null);     // a critical Site C ZIXI Feed stream turns the banner red too
    var attention = states.filter(function (state) {
      return state.status.level === "warning" || state.status.level === "attention" || state.status.level === "offline";
    });
    banner.textContent = "";
    var orb = el("div", undefined, "banner-orb");
    orb.setAttribute("aria-hidden", "true");
    var copy = el("div");
    var time = null;   // no "Updated ..." line: the header's Core clock already says the time
    if (uiState.blind) {
      banner.className = "banner status-blind";
      copy.appendChild(el("div", "Live feed lost", "banner-title"));
      copy.appendChild(el("div", !uiState.lastFrameAt ? blindReason() + " No live data has arrived yet, so there is nothing to show." : blindReason() + " Showing the last state received at " + (uiState.serverClock ? formatInZone(uiState.serverClock.ms, coreZone(), false) + " Core time" : new Date(uiState.lastFrameAt).toLocaleTimeString() + " on this device") + ". Treat it as unverified.", "banner-sub"));
      time = el("div", "No live data", "banner-time");
    } else if (critical.length || vzLines.length) {
      banner.className = "banner status-critical";
      copy.appendChild(el("div", vzBannerTitle(critical.length, vzLines.length), "banner-title"));
      copy.appendChild(el("div", critical.map(function (state) { return state.endpoint.label; }).concat(vzLines.map(function (line) { return line.short; })).join(", "), "banner-sub"));
    } else if (attention.length) {
      banner.className = attention.some(function (state) { return state.status.level === "attention" || state.status.level === "offline"; }) ? "banner status-attention" : "banner status-warning";
      copy.appendChild(el("div", attention.length + " endpoint" + (attention.length === 1 ? "" : "s") + " need review", "banner-title"));
      copy.appendChild(el("div", attention.map(function (state) { return state.endpoint.label + ": " + state.status.label; }).join(" / "), "banner-sub"));
    } else {
      banner.className = "banner status-healthy";
      copy.appendChild(el("div", "All six playout endpoints are healthy", "banner-title"));
    }
    banner.appendChild(orb);
    banner.appendChild(copy);
    if (time) { banner.appendChild(time); }
  }

  // ---- Counters ---------------------------------------------------------------------------------------------------
  // A number counts up to its new value (about 0.65 s) unless the device asks for reduced motion.
  function animateNumber(node, target, suffix) {
    var tail = suffix || "";
    if (typeof target !== "number" || !isFinite(target)) { node.textContent = "-"; node._value = null; return; }
    var from = typeof node._value === "number" ? node._value : 0;
    node._value = target;
    if (node._raf) { cancelAnimationFrame(node._raf); node._raf = 0; }
    if (from === target || REDUCED_MOTION.matches || !window.requestAnimationFrame) { node.textContent = String(target) + tail; return; }
    var startedAt = null;
    function step(stamp) {
      if (startedAt === null) { startedAt = stamp; }
      var t = Math.min(1, (stamp - startedAt) / 650);
      node.textContent = String(Math.round(from + (target - from) * (1 - Math.pow(1 - t, 3)))) + tail;
      if (t < 1) { node._raf = requestAnimationFrame(step); } else { node._raf = 0; }
    }
    node._raf = requestAnimationFrame(step);
  }

  var counterParts = {};
  function counter(id) {
    if (!counterParts[id]) {
      var box = document.getElementById(id);
      counterParts[id] = { box:box, value:box.querySelector(".counter-value b"), unit:box.querySelector(".counter-value i"), note:box.querySelector(".counter-note") };
    }
    return counterParts[id];
  }

  function setCounter(id, number, note, tone, suffix) {
    var part = counter(id);
    part.box.className = "counter" + (tone ? " tone-" + tone : "");
    animateNumber(part.value, number, suffix);
    if (note !== null && note !== undefined) { part.note.textContent = note; }
  }

  function sinceText(totalSeconds) {
    var s = Math.max(0, Math.floor(totalSeconds));
    if (s < 60) { return s + "s"; }
    var m = Math.floor(s / 60);
    if (m < 60) { return m + "m " + (s % 60 < 10 ? "0" : "") + (s % 60) + "s"; }
    var h = Math.floor(m / 60);
    if (h < 48) { return h + "h " + (m % 60 < 10 ? "0" : "") + (m % 60) + "m"; }
    return Math.floor(h / 24) + "d " + (h % 24) + "h";
  }

  function renderCounters(states, counters) {
    var healthy = states.filter(function (state) { return state.status.level === "healthy"; }).length;
    var critical = states.filter(function (state) { return state.status.level === "critical"; }).length;
    var watch = states.filter(function (state) { return state.status.level === "warning" || state.status.level === "attention" || state.status.level === "offline"; }).length;
    var watchNote = watch ? watch + " on watch" : "Nothing needs review";
    setCounter("counter-healthy", healthy, critical ? critical + " critical" : (watch ? watchNote : "All clear"), critical ? "alarm" : (watch ? "watch" : "good"));
    counter("counter-healthy").unit.textContent = "/ " + states.length;
    if (!counters) { return; }
    setCounter("counter-red24", counters.red_alarms_24h, "Emails are sent on critical only", counters.red_alarms_24h ? "watch" : "good");
    if (typeof counters.since_last_red_s === "number") {
      uiState.since = { seconds: counters.since_last_red_s, at: uiState.frameAt || Date.now() };
      counter("counter-since").note.textContent = "Monitoring for " + durationText(counters.listener_uptime_s || 0);
    } else {
      uiState.since = null;
      counter("counter-since").value.textContent = "None";
      counter("counter-since").note.textContent = "No critical alarm on record";
      counter("counter-since").box.className = "counter tone-good";
    }
  }

  function tickSince() {
    if (!uiState.since) { return; }
    var part = counter("counter-since");
    part.value.textContent = sinceText(uiState.since.seconds + (Date.now() - uiState.since.at) / 1000);
    part.box.className = "counter" + (uiState.since.seconds + (Date.now() - uiState.since.at) / 1000 < 600 ? " tone-watch" : " tone-good");
  }

  // ---- Core server vitals -----------------------------------------------------------------------------------------
  var vitalsEls = null;
  function vitals() {
    if (!vitalsEls) {
      vitalsEls = {
        gauge: document.getElementById("v-gauge"), arc: document.getElementById("v-arc"), cpu: document.getElementById("v-cpu"),
        load: document.getElementById("v-load"), loadNote: document.getElementById("v-load-note"), cores: document.getElementById("v-cores"),
        up: document.getElementById("v-up"), note: document.getElementById("v-note"), caption: document.getElementById("v-spark-caption"),
        area: document.getElementById("v-spark-area"), line: document.getElementById("v-spark-line")
      };
    }
    return vitalsEls;
  }

  function setMeter(id, percent, text) {
    var meter = document.getElementById(id);
    var fill = meter.querySelector(".meter-track span");
    var value = meter.querySelector("b");
    if (typeof percent !== "number") { fill.style.width = "0%"; value.textContent = "--"; meter.className = "meter"; return; }
    fill.style.width = Math.max(0, Math.min(100, percent)).toFixed(1) + "%";
    value.textContent = text || Math.round(percent) + "%";
    meter.className = "meter" + (percent >= 90 ? " hot" : (percent >= 75 ? " warn" : ""));
  }

  function renderSpark(history, sampleSeconds) {
    var v = vitals();
    var points = (history || []).filter(function (value) { return typeof value === "number"; });
    if (points.length < 2) {
      v.line.setAttribute("d", "");
      v.area.setAttribute("d", "");
      v.caption.textContent = "CPU history builds up here";
      return;
    }
    var width = 240;
    var height = 58;
    var pad = 3;
    var step = width / (points.length - 1);
    var coords = points.map(function (value, index) {
      return (index * step).toFixed(1) + " " + (height - pad - (Math.max(0, Math.min(100, value)) / 100) * (height - 2 * pad)).toFixed(1);
    });
    var path = "M " + coords.join(" L ");
    v.line.setAttribute("d", path);
    v.area.setAttribute("d", path + " L " + width + " " + height + " L 0 " + height + " Z");
    v.caption.textContent = "CPU, last " + durationText(points.length * (sampleSeconds || 5));
  }

  function renderVitals(system) {
    var v = vitals();
    var usable = system && system.available && typeof system.cpu_percent === "number";
    if (!usable) {
      v.cpu.textContent = "--";
      v.arc.style.strokeDashoffset = String(GAUGE_CIRCUMFERENCE);
      v.gauge.className = "gauge";
      v.load.textContent = "--";
      v.cores.textContent = "--";
      v.up.textContent = "";
      v.note.textContent = system && system.available === false ? "Server load is not available on this host." : "Waiting for the first reading from the server.";
      v.note.hidden = false;
      ["m-mem", "m-disk", "m-load"].forEach(function (id) { setMeter(id, null); });
      renderSpark([], 5);
      return;
    }
    var cpu = system.cpu_percent;
    animateNumber(v.cpu, Math.round(cpu), "%");
    v.arc.style.strokeDashoffset = String(GAUGE_CIRCUMFERENCE * (1 - Math.max(0, Math.min(100, cpu)) / 100));
    v.gauge.className = "gauge" + (cpu >= 90 ? " hot" : (cpu >= 70 ? " warn" : ""));
    v.gauge.setAttribute("aria-label", "CPU load " + Math.round(cpu) + " percent");
    var haveLoad = typeof system.load1 === "number";
    v.load.textContent = haveLoad ? system.load1.toFixed(2) : "--";
    v.loadNote.textContent = haveLoad ? system.load1.toFixed(2) + " / " + system.load5.toFixed(2) + " / " + system.load15.toFixed(2) + " (1, 5, 15 min)" : "1 / 5 / 15 min";
    v.cores.textContent = system.cores ? String(system.cores) : "--";
    v.up.textContent = typeof system.uptime_s === "number" ? "up " + durationText(system.uptime_s) : "";
    var perCore = haveLoad && system.cores ? system.load1 / system.cores : null;
    setMeter("m-mem", system.mem_used_percent, null);
    setMeter("m-disk", system.disk_used_percent, null);
    setMeter("m-load", perCore === null ? null : perCore * 100, perCore === null ? "" : perCore.toFixed(2));
    v.note.hidden = true;
    renderSpark(system.cpu_history, system.sample_seconds);
  }

  // ---- Live feed lost ------------------------------------------------------------------------------------------------
  // The page is blind when no frame has arrived for BLIND_AFTER_MS (the stream is down, or open but stalled, as on Wi-Fi that
  // has no uplink). It then says so and dims the state it is still showing, instead of presenting a frozen past as the present.
  function setBlind(blind) {
    if (uiState.blind === blind) { return; }
    uiState.blind = blind;
    document.body.classList.toggle("is-blind", blind);
    if (blind) { uiState.internet = null; uiState.internetCheckedAt = 0; }
    setLiveIndicator(!blind && !uiState.streamDown);
    if (blind) { hidePreloader(); }   // the banner explains better than a loading screen that is not going to finish
    tickClocks();   // Core's clock freezes and is relabelled at the same instant the banner changes, not a second later
    if (uiState.states) { renderBanner(uiState.states); }
  }

  function updateBlindState() {
    // Silent for BLIND_AFTER_MS since the last frame, or since the page opened if none ever came (a saved app shell opened with no connection).
    var silent = (Date.now() - (uiState.lastFrameAt || pageOpenedAt)) > BLIND_AFTER_MS;
    setBlind(!!silent);
    if (uiState.blind && Date.now() - uiState.internetCheckedAt > 15000) {
      uiState.internetCheckedAt = Date.now();
      probeInternet().then(function (reachable) {
        uiState.internet = reachable;
        if (uiState.blind && uiState.states) { renderBanner(uiState.states); }
      });
    }
  }

  // Self-healing, checked every second: the stream must always be connected, so there is no Reconnect button. A browser gives up on a live stream for good
  // when it is answered with an error (a proxy's 502 during a restart, Cloudflare's 524): it does not retry that by itself. Open it again here, at most every
  // 8 seconds. A stream that is open but has said nothing for STALL_RECONNECT_MS is started over, at most every RECONNECT_SPACING_MS. The screen lock is asked
  // for again every 30 s if the browser has not granted it.
  function keepAlive() {
    if (awake.wanted && !awake.lock && Date.now() - awake.triedAt > 30000) { requestAwake(); }
    if (uiState.offShown) {
      // Access is switched off: reconnecting would only be refused, so the page just asks, every few seconds, whether it is on again.
      if (Date.now() - uiState.offPollAt > ACCESS_POLL_MS) { uiState.offPollAt = Date.now(); followAccess(); }
      return;
    }
    var sinceConnect = Date.now() - uiState.lastConnectAt;
    var silentFor = Date.now() - (uiState.lastFrameAt || pageOpenedAt);
    if (eventSource && eventSource.readyState === 2 && sinceConnect > 8000) { connectStream(); }
    else if (silentFor > STALL_RECONNECT_MS && sinceConnect > RECONNECT_SPACING_MS) { connectStream(); }
  }

  // Once a second: the ages, the position in each item and the "since last critical alarm" count keep moving between frames.
  function tickLive() {
    tickClocks();
    updateBlindState();
    Object.keys(tiles).forEach(function (id) { refreshNowPlaying(tiles[id]); });
    refreshNowPlaying(topoTile);
    refreshMirrors();
    tickSince();
    vzTickAges();
    keepAlive();
  }

  // The standby listener says so on its page: a viewer must always know which copy of the dashboard they are looking at. Text only, never HTML.
  function renderStandbyStrip(block) {
    var strip = document.getElementById("standby-strip");
    if (!strip) { return; }
    if (!block || block.role !== "standby") { strip.hidden = true; return; }
    var primary = block.primary || {};
    var down = primary.state === "DOWN";
    var since = parseIso(primary.since);
    var when = isNaN(since) ? "" : " since " + new Date(since).toLocaleTimeString();
    strip.className = "standby-strip " + (down ? "standby-down" : "standby-up");
    strip.textContent = down
      ? "STANDBY is alerting: the Core watchdog has stopped answering" + when + ". Studio alerts now come from this copy."
      : "STANDBY: Core is answering. This page is a quiet second copy and sends no alerts.";
    strip.hidden = false;
  }

  // ---- The Site C feed card (the unpublished design notes) ----
  // Collapsed while all is well. It opens by itself the moment a stream turns critical, and it never closes on a viewer who opened it. Everything is written with textContent.
  // Opened, it groups the two streams as the owner named them (Main stream :5005, Backup stream :5004), prints each stream's UDP address, shows the rate live with the age of the
  // last report counting on every second, draws the last ten minutes, and lists the packet details. The streams' own names and addresses arrive as data from the listener.
  var vzLevelSeen = "";
  var vzDayOpen = {};
  // The live chart's window: 2, 5 or 10 minutes (the owner, 2026-11-08: a shorter window shows the movement better). Remembered on this device; anything else stored is ignored.
  var vzWindowChoices = [120, 300, 600];
  var vzWindowSeconds = 300;
  try { var vzSavedWindow = Number(window.localStorage.getItem("watchdog-vz-window")); if (vzWindowChoices.indexOf(vzSavedWindow) !== -1) { vzWindowSeconds = vzSavedWindow; } } catch (e) {}

  function vzEl(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined && text !== null) { node.textContent = text; }
    return node;
  }

  function vzRank(level) { return level === "critical" ? 3 : (level === "warning" ? 2 : (level === "ok" ? 1 : 0)); }

  // ---- The card's rules. None of these touches the page, so the tests run them on their own. ----
  // True when any stream is critical. The page siren listens to this as well as to the six playouts. Always a real true or false.
  function vzCritical(block) {
    if (!block || !Array.isArray(block.streams)) { return false; }
    return block.streams.some(function (s) { return !!s && s.level === "critical"; });
  }

  function vzLabel(s) { return (s && s.label) ? s.label : "Stream " + (s ? s.stream : "?"); }

  // The stream's full multicast address, written out exactly as the listener sends it (the page itself holds no address); empty from an older listener.
  function vzWhere(s) { return (s && s.address) ? s.address : ""; }

  // Main first, then backup. Data without roles (an older listener) keeps the order it came in. The list it is given is not changed.
  function vzOrder(streams) {
    var rank = { main: 0, backup: 1 };
    return (streams || []).map(function (s, i) { return { s: s, i: i }; }).sort(function (a, b) {
      var ra = (a.s && rank[a.s.role] !== undefined) ? rank[a.s.role] : 2;
      var rb = (b.s && rank[b.s.role] !== undefined) ? rank[b.s.role] : 2;
      return ra !== rb ? ra - rb : a.i - b.i;
    }).map(function (x) { return x.s; });
  }

  function vzHeadline(block) {
    if (!block || !block.reporting) { return "Not reporting yet"; }
    return vzOrder(block.streams).map(function (s) {
      return [vzLabel(s), vzWhere(s)].filter(Boolean).join(" ") + " " + s.word + ((s.level === "ok") ? " " + Number(s.mbit_per_s).toFixed(1) + " Mbit/s" : "");
    }).join("   |   ");
  }

  // The critical streams as plain lines for the banner and the incident feed, main first: short = "Site C ZIXI Feed: Main stream :5005 LOST", text = short plus the cause.
  function vzAlarmLines(block) {
    if (!block || !Array.isArray(block.streams)) { return []; }
    return vzOrder(block.streams).filter(function (s) { return !!s && s.level === "critical"; }).map(function (s) {
      var short = "Site C ZIXI Feed: " + [vzLabel(s), vzWhere(s)].filter(Boolean).join(" ") + " " + s.word;
      return { short: short, text: short + (s.cause ? " - " + s.cause : ""), since: (s.alarm && s.alarm.since) ? s.alarm.since : null };
    });
  }

  // The red banner's title: the playouts in alarm and, when a Site C stream is critical, the feed by name.
  function vzBannerTitle(playouts, streams) {
    var endpoints = playouts + " endpoint" + (playouts === 1 ? "" : "s");
    if (!streams) { return endpoints + " in critical alarm"; }
    return (playouts ? endpoints + " and the " : "") + "Site C ZIXI Feed in critical alarm";
  }

  function vzWindowLabel(seconds) { return seconds < 120 ? seconds + " s" : Math.round(seconds / 60) + " min"; }

  // The readings inside the chosen window, oldest first. A reading without a time or a number is left out.
  function vzWindow(live, nowMs, spanSeconds) {
    var start = nowMs - spanSeconds * 1000;
    var out = [];
    (live || []).forEach(function (p) {
      var ms = p ? Date.parse(p.t) : NaN;
      if (isNaN(ms) || typeof p.mbit !== "number" || ms < start || ms > nowMs + 1000) { return; }
      out.push({ ms: ms, mbit: p.mbit });
    });
    return out.sort(function (a, b) { return a.ms - b.ms; });
  }

  // The per-second readings the listener sends as {start, step_seconds, mbit: [...]}: number i was measured at start + i * step, and a null is a second nobody reported. Only real
  // numbers inside the window are kept (a reading a little ahead of this page's clock is kept, one far ahead is not), and at most 1200 numbers are ever read.
  function vzFineWindow(fine, nowMs, spanSeconds) {
    var out = [];
    if (!fine || !Array.isArray(fine.mbit)) { return out; }
    var startMs = Date.parse(fine.start);
    var step = Number(fine.step_seconds);
    if (isNaN(startMs) || !(step > 0)) { return out; }
    var from = nowMs - spanSeconds * 1000;
    var count = Math.min(fine.mbit.length, 1200);
    for (var i = 0; i < count; i += 1) {
      var value = fine.mbit[i];
      var ms = startMs + i * step * 1000;
      if (typeof value !== "number" || !isFinite(value) || ms < from || ms > nowMs + 1000) { continue; }
      out.push({ ms: ms, mbit: value });
    }
    return out;
  }

  // What the chart draws: the per-second readings when there are at least two in the window, else the ten-second ones (a listener whose senders have not been updated yet, or an
  // older listener). fine tells the caption and the dots which of the two it is.
  function vzChartPoints(s, nowMs, spanSeconds) {
    s = s || {};
    var fine = vzFineWindow(s.fine, nowMs, spanSeconds);
    if (fine.length >= 2) { return { points: fine, fine: true }; }
    return { points: vzWindow(s.live, nowMs, spanSeconds), fine: false };
  }

  // The line in pieces: cut wherever two neighbouring readings are further apart than maxGapMs (nobody reported in between), so a silence is never drawn as a made-up slope.
  function vzSplit(points, maxGapMs) {
    var pieces = [];
    var current = null;
    (points || []).forEach(function (p, i) {
      if (current === null || p.ms - points[i - 1].ms > maxGapMs) { current = []; pieces.push(current); }
      current.push(p);
    });
    return pieces;
  }

  // Which readings get a dot. Per-second readings: only the newest, and any reading alone between two silences (a line cannot show it); a dot for every second would bury the
  // line. Ten-second readings: a dot each, as long as there are at most 40.
  function vzDots(places, pieces, fine) {
    if (!fine) { return places.length <= 40 ? places : []; }
    var dots = pieces.filter(function (piece) { return piece.length === 1; }).map(function (piece) { return piece[0]; });
    if (places.length) { dots.push(places[places.length - 1]); }
    return dots;
  }

  // The chart's scale. A steady encoder drawn from zero is a flat line, so the scale is zoomed to the readings (at least a fifth of a Mbit/s tall, a quarter of it spare above and below).
  // It never goes under zero, and when the stream runs near the alarm line the scale reaches down to it so the line stays on the chart.
  function vzRange(points, expected, floor) {
    var low = null, high = null;
    (points || []).forEach(function (p) {
      if (!p || typeof p.mbit !== "number") { return; }
      if (low === null || p.mbit < low) { low = p.mbit; }
      if (high === null || p.mbit > high) { high = p.mbit; }
    });
    var alarm = Number(floor) || 0;
    if (low === null) { return { low: 0, high: Math.round(Math.max((Number(expected) || 0) * 1.25, alarm * 2, 1) * 100) / 100 }; }
    var need = Math.max(high - low, 0.2);
    var mid = (low + high) / 2;
    var from = Math.max(mid - need * 0.75, 0);
    var to = mid + need * 0.75;
    if (low < alarm * 2) { from = Math.min(from, alarm * 0.5); to = Math.max(to, alarm * 1.5); }
    return { low: Math.round(from * 100) / 100, high: Math.round(to * 100) / 100 };
  }

  // Where each reading goes on the chart: x by its age inside the window, y by its rate inside the scale (a rate outside the scale is held at the edge).
  function vzPlace(points, nowMs, spanSeconds, range, width, height) {
    var start = nowMs - spanSeconds * 1000;
    var tall = (range.high - range.low) || 1;
    return (points || []).map(function (p) {
      var v = Math.min(Math.max(p.mbit, range.low), range.high);
      return { x: Math.max(0, Math.min(width, (p.ms - start) / (spanSeconds * 1000) * width)), y: height - 2 - (v - range.low) / tall * (height - 4), ms: p.ms };
    });
  }

  // "last report 7 s ago": the age the listener reported plus the time since this page received it, so it counts on between frames and never runs backwards.
  function vzAgeText(base, receivedAtMs, nowMs) {
    if (typeof base !== "number") { return "no report yet"; }
    var s = Math.max(0, Math.floor(base + (nowMs - receivedAtMs) / 1000));
    return "last report " + (s < 120 ? s + " s" : Math.floor(s / 60) + " min " + (s % 60) + " s") + " ago";
  }

  function vzTickAges() {
    var nodes = document.querySelectorAll(".vz-age");
    var now = Date.now();
    for (var i = 0; i < nodes.length; i += 1) {
      var base = nodes[i].getAttribute("data-base");
      nodes[i].textContent = vzAgeText((base === null || base === "") ? null : Number(base), Number(nodes[i].getAttribute("data-at")), now);
    }
  }

  function vzSpark(history, floor) {
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("class", "vz-spark");
    svg.setAttribute("viewBox", "0 0 240 48");
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Lowest rate in each 10 minutes over the last 24 hours, with the alarm floor dashed");
    var points = [];
    (history || []).forEach(function (entry) {
      var when = parseIso(entry.t);
      if (!isNaN(when) && typeof entry.mbit === "number") { points.push({ t: when, v: entry.mbit }); }
    });
    var top = 8;
    points.forEach(function (p) { if (p.v * 1.15 > top) { top = p.v * 1.15; } });
    var end = Date.now(), span = 24 * 3600 * 1000;
    function px(t) { return Math.max(0, Math.min(240, (t - (end - span)) / span * 240)); }
    function py(v) { return 46 - Math.min(v, top) / top * 44; }
    var floorLine = document.createElementNS(ns, "path");
    floorLine.setAttribute("class", "vz-floor");
    floorLine.setAttribute("d", "M0 " + py(floor).toFixed(1) + " L240 " + py(floor).toFixed(1));
    svg.appendChild(floorLine);
    if (points.length > 1) {
      var d = points.map(function (p, i) { return (i ? "L" : "M") + px(p.t).toFixed(1) + " " + py(p.v).toFixed(1); }).join(" ");
      var line = document.createElementNS(ns, "path");
      line.setAttribute("class", "vz-line");
      line.setAttribute("d", d);
      svg.appendChild(line);
    }
    return svg;
  }

  // The live chart: the readings in the chosen window, drawn on a scale zoomed to them. The PCs measure the stream every second and report those readings, so the line has one
  // point a second and a single dot on the newest (a dot for each would bury the line); with no per-second readings yet it draws the ten-second ones with a dot each, as before.
  // The alarm line (dashed) and the usual rate (faint) are drawn when they fall inside the scale. The caption says exactly what scale is shown, so the zoom cannot mislead.
  function vzLive(s, block) {
    var ns = "http://www.w3.org/2000/svg";
    var width = 240, height = 96;
    var step = Number(block.live_step_seconds) || 10;
    var expected = Number(block.expected_mbit) || 0;
    var floor = Number(block.floor_mbit) || 0;
    var now = Date.now();
    var chart = vzChartPoints(s, now, vzWindowSeconds);
    var points = chart.points;
    var range = vzRange(points, expected, floor);
    var places = vzPlace(points, now, vzWindowSeconds, range, width, height);
    var pieces = chart.fine ? vzSplit(places, 6000) : [places];          // per-second line: cut where nobody reported for over six seconds
    var showFloor = floor > 0 && floor >= range.low && floor <= range.high;
    var showUsual = expected > 0 && expected >= range.low && expected <= range.high;
    var wrap = vzEl("div", "vz-live");
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("class", "vz-chart");
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("role", "img");
    var lowest = null, highest = null;
    points.forEach(function (p) {
      if (lowest === null || p.mbit < lowest) { lowest = p.mbit; }
      if (highest === null || p.mbit > highest) { highest = p.mbit; }
    });
    svg.setAttribute("aria-label", lowest === null
      ? "No readings in the last " + vzWindowLabel(vzWindowSeconds) + " yet"
      : "Rate over the last " + vzWindowLabel(vzWindowSeconds) + ": lowest " + lowest.toFixed(2) + ", highest " + highest.toFixed(2) + " Mbit/s.");
    function yOf(v) { return height - 2 - (Math.min(Math.max(v, range.low), range.high) - range.low) / ((range.high - range.low) || 1) * (height - 4); }
    function flat(cls, v) {
      var path = document.createElementNS(ns, "path");
      path.setAttribute("class", cls);
      path.setAttribute("d", "M0 " + yOf(v).toFixed(1) + " L" + width + " " + yOf(v).toFixed(1));
      svg.appendChild(path);
    }
    if (showUsual) { flat("vz-usual", expected); }
    if (showFloor) { flat("vz-floor", floor); }
    var strokes = pieces.filter(function (piece) { return piece.length > 1; }).map(function (piece) {
      return piece.map(function (p, i) { return (i ? "L" : "M") + p.x.toFixed(1) + " " + p.y.toFixed(1); }).join(" ");
    });
    if (strokes.length) {
      var line = document.createElementNS(ns, "path");
      line.setAttribute("class", "vz-line");
      line.setAttribute("d", strokes.join(" "));
      svg.appendChild(line);
    }
    var dotted = vzDots(places, pieces, chart.fine);
    if (dotted.length) {
      var dots = document.createElementNS(ns, "path");
      dots.setAttribute("class", "vz-dot");
      dots.setAttribute("d", dotted.map(function (p) { return "M" + p.x.toFixed(1) + " " + p.y.toFixed(1) + " L" + p.x.toFixed(1) + " " + p.y.toFixed(1); }).join(" "));
      svg.appendChild(dots);
    }
    wrap.appendChild(svg);
    wrap.appendChild(vzEl("div", "vz-chart-note", places.length
      ? "Last " + vzWindowLabel(vzWindowSeconds) + ", " + (chart.fine ? "a reading every second" : "a dot every " + step + " s") + ". " + "Scale " + range.low.toFixed(2) + " to " + range.high.toFixed(2) + " Mbit/s (zoomed to the readings)."
        + (showFloor ? " Dashed line: the " + floor + " Mbit/s alarm line." : " The " + floor + " Mbit/s alarm line is below this scale.")
        + (showUsual ? " Faint line: the usual " + expected + " Mbit/s." : "")
      : "Collecting readings. The line draws itself as they arrive."));
    return wrap;
  }

  function vzFact(list, term, value) {
    list.appendChild(vzEl("dt", "", term));
    list.appendChild(vzEl("dd", "", (value === undefined || value === null || value === "") ? "-" : String(value)));
  }

  // The window buttons are built once (a redraw of the tiles must never swallow a click); later calls only update which one is pressed.
  function vzBuildTools() {
    var tools = document.getElementById("vz-tools");
    if (!tools) { return; }
    if (tools.firstChild) {
      var pressed = tools.querySelectorAll(".vz-win");
      for (var i = 0; i < pressed.length; i += 1) { pressed[i].setAttribute("aria-pressed", String(vzWindowChoices[i] === vzWindowSeconds)); }
      return;
    }
    tools.appendChild(vzEl("span", "vz-tools-label", "Chart window"));
    vzWindowChoices.forEach(function (seconds) {
      var button = vzEl("button", "vz-win", vzWindowLabel(seconds));
      button.type = "button";
      button.setAttribute("aria-pressed", String(seconds === vzWindowSeconds));
      button.addEventListener("click", function () { vzSetWindow(seconds); });
      tools.appendChild(button);
    });
  }

  function vzSetWindow(seconds) {
    if (vzWindowChoices.indexOf(seconds) === -1) { return; }
    vzWindowSeconds = seconds;
    try { window.localStorage.setItem("watchdog-vz-window", String(seconds)); } catch (e) {}
    renderSiteC(uiState.lastData.site_c_feed);
  }

  function renderSiteC(block) {
    var card = document.getElementById("vz-panel");
    if (!card) { return; }
    if (!block || !block.streams) { card.hidden = true; return; }       // an older listener sends no such block
    card.hidden = false;
    vzBuildTools();
    var body = document.getElementById("vz-body");
    while (body.firstChild) { body.removeChild(body.firstChild); }
    var level = "nodata";
    var receivedAt = Date.now();
    vzOrder(block.streams).forEach(function (s) {
      if (vzRank(s.level) > vzRank(level)) { level = s.level; }
      var tile = vzEl("div", "vz-tile vz-" + (s.level === "no_data" ? "nodata" : s.level));
      var name = vzEl("div", "vz-name");
      name.appendChild(vzEl("span", "eyebrow", vzLabel(s)));
      name.appendChild(vzEl("span", "vz-word", s.word));
      tile.appendChild(name);
      if (s.address) { tile.appendChild(vzEl("div", "vz-addr", s.address)); }
      var big = vzEl("div", "vz-rate", Number(s.mbit_per_s).toFixed(1));
      big.appendChild(vzEl("i", "", "Mbit/s"));
      tile.appendChild(big);
      var witnesses = s.witnesses || [];
      var freshest = null;
      witnesses.forEach(function (w) { if (typeof w.age_seconds === "number" && (freshest === null || w.age_seconds < freshest)) { freshest = w.age_seconds; } });
      var age = vzEl("div", "vz-age", vzAgeText(freshest, receivedAt, receivedAt));
      age.setAttribute("data-base", freshest === null ? "" : String(freshest));
      age.setAttribute("data-at", String(receivedAt));
      tile.appendChild(age);
      var note = s.cause ? s.cause : ((s.heard_by && s.heard_by.length) ? "" : "No PC is listening");
      if (s.alarm && s.alarm.clearing) { note = (note ? note + ". " : "") + "Heard again; clearing."; }
      if (note) { tile.appendChild(vzEl("div", "vz-note", note)); }
      tile.appendChild(vzLive(s, block));
      var best = null;
      witnesses.forEach(function (w) { if (w.state !== "cannot_listen" && (best === null || w.mbit_per_s > best.mbit_per_s)) { best = w; } });
      var facts = vzEl("dl", "vz-facts");
      vzFact(facts, "Packets per second", best ? best.packets_per_s : null);
      vzFact(facts, "Packets in the window", best ? best.packets : null);
      vzFact(facts, "Counting window", (best && best.window_seconds !== undefined) ? best.window_seconds + " s" : null);
      vzFact(facts, "Seconds since the last packet", best ? best.idle_seconds : null);
      tile.appendChild(facts);
      var day = vzEl("details", "vz-day");
      day.open = !!vzDayOpen[s.stream];
      day.addEventListener("toggle", function () { vzDayOpen[s.stream] = day.open; });
      day.appendChild(vzEl("summary", "", "Last 24 hours"));
      day.appendChild(vzSpark(s.history, block.floor_mbit));
      tile.appendChild(day);
      body.appendChild(tile);
    });
    document.getElementById("vz-headline").textContent = vzHeadline(block);
    card.className = "vz vz-" + level;
    if (level === "critical" && vzLevelSeen !== "critical") { card.open = true; }
    vzLevelSeen = level;
  }

  function renderAll(data) {
    uiState.lastData = data || { sources: [], recent_events: [] };
    renderStandbyStrip(uiState.lastData.standby);
    renderSiteC(uiState.lastData.site_c_feed);
    var states = allEndpointStates(uiState.lastData);
    uiState.states = states;
    renderBanner(states);
    renderCounters(states, uiState.lastData.counters);
    renderVitals(uiState.lastData.system);
    if (pageMode === "topology") {
      renderTopology(states);
    } else {
      renderDashboard(states);
      renderIncidentFeed(states, uiState.lastData);
    }
    // A critical Site C stream sounds the same siren as a critical playout (owner, 2026-11-08).
    uiState.criticalActive = states.some(function (state) { return state.status.level === "critical"; }) || vzCritical(uiState.lastData.site_c_feed);
    if (!uiState.criticalActive && uiState.muted) {
      uiState.muted = false;   // silencing covered that one alarm; the next one must sound
      syncSoundUI();
    }
    setAlarm(uiState.criticalActive);
    renderSirenStrip();
    document.title = (uiState.criticalActive ? "ALARM - " : "") + "Clarix Broadcast Watchdog";
    tickLive();
  }

  function setLiveIndicator(connected) {
    liveDot.className = "live-dot" + (connected ? " connected" : " down");
    liveText.textContent = uiState.blind ? "No live data" : (connected ? "Live" : "Reconnecting");
    streamPill.textContent = connected ? "SSE connected" : "SSE reconnecting";
  }

  // One shared AudioContext for the page, with a limiter in front of the speakers so a loud
  // alarm can be as loud as the device allows without clipping.
  function ensureAudioCtx() {
    if (!audioCtx) {
      var Ctor = window.AudioContext || window.webkitAudioContext;
      if (Ctor) {
        audioCtx = new Ctor();
        limiter = audioCtx.createDynamicsCompressor();
        limiter.threshold.value = -3;
        limiter.knee.value = 0;
        limiter.ratio.value = 20;
        limiter.attack.value = 0.002;
        limiter.release.value = 0.1;
        limiter.connect(audioCtx.destination);
        audioCtx.onstatechange = function () {
          if (audioCtx.state === "running") { uiState.audioUnlocked = true; }
          renderSirenStrip();
        };
      }
    }
    if (audioCtx && audioCtx.state === "suspended" && alarm.on) { audioCtx.resume(); }
  }

  // Built once, on first use; later calls do nothing. This is the only place an oscillator is made.
  function buildAlarmTone() {
    if (alarm.osc || !audioCtx) { return; }
    var osc = audioCtx.createOscillator();
    var gain = audioCtx.createGain();
    osc.type = "triangle";                 // cleaner than a sawtooth at high volume
    osc.frequency.value = 620;
    gain.gain.value = 0.0001;
    osc.connect(gain);
    gain.connect(limiter);
    osc.start();
    alarm.osc = osc;
    alarm.gain = gain;
  }

  // The siren pattern (620 Hz to 1240 Hz sweeps) is written as pitch automation on that single
  // oscillator, a minute at a time. Nothing new is created each cycle.
  function scheduleSiren() {
    var now = audioCtx.currentTime;
    var freq = alarm.osc.frequency;
    freq.cancelScheduledValues(now);
    freq.setValueAtTime(620, now);
    var cycles = Math.ceil(SIREN_HORIZON_S / SIREN_CYCLE_S);
    for (var i = 0; i < cycles; i += 1) {
      var t = now + i * SIREN_CYCLE_S;
      freq.linearRampToValueAtTime(1240, t + 0.18);
      freq.setValueAtTime(1240, t + 0.28);
      freq.linearRampToValueAtTime(620, t + 0.48);
      freq.setValueAtTime(620, t + 0.62);
      freq.linearRampToValueAtTime(1240, t + 0.82);
      freq.linearRampToValueAtTime(620, t + 1.04);
      freq.setValueAtTime(620, t + SIREN_CYCLE_S);
    }
    alarm.scheduledUntil = now + SIREN_HORIZON_S;
  }

  function silenceAlarm() {
    if (!alarm.on) { return; }
    alarm.on = false;
    var now = audioCtx.currentTime;
    alarm.gain.gain.cancelScheduledValues(now);
    alarm.gain.gain.setTargetAtTime(0.0001, now, 0.02);
    alarm.osc.frequency.cancelScheduledValues(now);
    alarm.scheduledUntil = 0;
    // Release the audio hardware once the fade is done, unless the alarm came back meanwhile.
    setTimeout(function () {
      if (!alarm.on && audioCtx && audioCtx.state === "running") { audioCtx.suspend(); }
    }, 250);
  }

  // Safe to call as often as you like (it runs on every pushed frame): it only acts when the
  // wanted state changes, or when the scheduled pitch pattern is about to run out.
  function setAlarm(active) {
    var wanted = active && uiState.soundOn && !uiState.muted;
    if (!wanted) {
      if (audioCtx && alarm.osc) { silenceAlarm(); }
      return;
    }
    ensureAudioCtx();
    if (!audioCtx) { return; }
    buildAlarmTone();
    if (!alarm.on) {
      alarm.on = true;
      if (audioCtx.state === "suspended") { audioCtx.resume(); }
      alarm.gain.gain.cancelScheduledValues(audioCtx.currentTime);
      alarm.gain.gain.setTargetAtTime(ALARM_PEAK, audioCtx.currentTime, 0.02);
    }
    if (audioCtx.currentTime + 20 > alarm.scheduledUntil) { scheduleSiren(); }
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && alarm.on && audioCtx && audioCtx.state === "suspended") { audioCtx.resume(); }
  });

  function syncSoundUI() {
    soundToggle.setAttribute("aria-pressed", String(uiState.soundOn));
    soundToggle.textContent = "Alarm sound: " + (uiState.soundOn ? "on" : "off");
    muteToggle.setAttribute("aria-pressed", String(uiState.muted));
    muteToggle.textContent = uiState.muted ? "Siren silenced - tap to restore" : "Silence siren";
  }

  // Browsers only let a page make sound after the person has touched it. This runs on the first
  // tap, key press or click anywhere on the page: it creates and starts the audio context, and
  // from then on the page may start the siren by itself when an alarm arrives.
  function unlockAudio() {
    uiState.audioUnlocked = true;
    ensureAudioCtx();
    if (audioCtx && audioCtx.state === "suspended") {
      var started = audioCtx.resume();
      if (started && started.then) {
        started.then(function () {
          if (!alarm.on && audioCtx.state === "running") { audioCtx.suspend(); }
          renderSirenStrip();
        }, function () { renderSirenStrip(); });
      }
    }
    setAlarm(uiState.criticalActive);
    renderSirenStrip();
  }
  ["pointerdown", "keydown", "touchend"].forEach(function (name) {
    document.addEventListener(name, function handler() {
      if (uiState.audioUnlocked) { document.removeEventListener(name, handler); return; }
      unlockAudio();
    }, { passive: true });
  });

  // Why the siren would NOT be heard right now, or null when it would be.
  function sirenProblem() {
    if (!uiState.soundOn) { return { text: "Alarm sound is OFF.", fix: "Tap here to turn it on.", turnOn: true }; }
    if (uiState.muted) { return { text: "The siren was silenced for this alarm.", fix: "Tap here to hear it again.", unmute: true }; }
    if (!uiState.audioUnlocked || !audioCtx || (alarm.on && audioCtx.state !== "running")) {
      return { text: "The browser has not yet allowed this page to play sound.", fix: "Tap anywhere on the page once." };
    }
    return null;
  }

  // A tap anywhere on the page unlocks the browser's permission to play sound, which removes this strip. Removing it at that instant moved
  // everything below it up by the strip's height in the MIDDLE of the tap, so the finger came up on a different element and the tap missed. A
  // tall target (a playout tile) survives a shift of that size; a small one right under the strip (the 44 px update chip) did not. The strip now
  // leaves a moment after the tap has finished.
  var STRIP_LEAVES_AFTER_MS = 350;
  var stripLeaveTimer = null;

  function renderSirenStrip() {
    var problem = sirenProblem();
    if (!problem) {
      if (!sirenStrip.hidden && !stripLeaveTimer) {
        stripLeaveTimer = setTimeout(function () { stripLeaveTimer = null; if (!sirenProblem()) { sirenStrip.hidden = true; } }, STRIP_LEAVES_AFTER_MS);
      }
      return;
    }
    if (stripLeaveTimer) { clearTimeout(stripLeaveTimer); stripLeaveTimer = null; }
    var loud = uiState.criticalActive;
    sirenStrip.hidden = false;
    sirenStrip.className = "siren-strip" + (loud ? " alarm" : "");
    sirenStrip.textContent = (loud ? "ALARM ACTIVE, SIREN SILENT. " : "") + problem.text + " " + problem.fix;
  }

  sirenStrip.addEventListener("click", function () {
    var problem = sirenProblem();
    if (problem && problem.turnOn) {
      uiState.soundOn = true;
      try { window.localStorage.setItem("watchdog-sound", "on"); } catch (e) {}
    }
    if (problem && problem.unmute) { uiState.muted = false; }
    syncSoundUI();
    unlockAudio();
  });

  soundToggle.addEventListener("click", function () {
    uiState.soundOn = !uiState.soundOn;
    try { window.localStorage.setItem("watchdog-sound", uiState.soundOn ? "on" : "off"); } catch (e) {}
    syncSoundUI();
    if (uiState.soundOn) { unlockAudio(); } else { setAlarm(uiState.criticalActive); renderSirenStrip(); }
  });

  muteToggle.addEventListener("click", function () {
    uiState.muted = !uiState.muted;   // not remembered: see the note near the top of this script
    syncSoundUI();
    setAlarm(uiState.criticalActive);
    renderSirenStrip();
  });

  // The loading screen follows the real connection, not a timer: each line below is shown when
  // that step actually happens, and the screen leaves when the first live snapshot has been drawn.
  var preloader = document.getElementById("preloader");
  var loaderStep = document.getElementById("loader-step");
  var loaderSkip = document.getElementById("loader-skip");

  function setLoaderStep(text) {
    if (loaderStep && !firstFrameSeen) { loaderStep.textContent = text; }
  }

  function hidePreloader() {
    if (!preloader || preloader.classList.contains("done")) { return; }
    preloader.classList.add("done");
    setTimeout(function () { preloader.hidden = true; }, 600);
  }

  loaderSkip.addEventListener("click", hidePreloader);
  setTimeout(function () {
    if (!firstFrameSeen && preloader) {
      preloader.classList.add("slow");
      setLoaderStep("Still connecting. The server is slow to answer; retrying automatically.");
    }
  }, 8000);

  // ---- Staying alive -------------------------------------------------------------------------------------------------------------
  // An installed app or an open tab should not go dark by itself. The Screen Wake Lock keeps the screen on while the app is in front. The browser
  // drops it whenever the page is hidden, so it is asked for again each time the page returns, whenever the browser takes it away, on the first
  // tap, and on a slow timer. It stays off only if the operator switches it off here (remembered). A web page cannot stop a phone that is locked
  // with its power button or a PC that its power plan puts to sleep: where the browser has no Wake Lock the switch says so instead of pretending.
  var awake = { wanted: true, lock: null, asking: false, triedAt: 0, supported: !!(navigator.wakeLock && typeof navigator.wakeLock.request === "function") };
  var savedAwake = null;
  try { savedAwake = window.localStorage.getItem("watchdog-keep-awake"); } catch (e) { savedAwake = null; }
  awake.wanted = savedAwake !== "off";

  function syncAwakeUI() {
    if (!awake.supported) {
      awakeToggle.disabled = true;
      awakeToggle.setAttribute("aria-pressed", "false");
      awakeToggle.textContent = "Keep screen on: not supported here";
      return;
    }
    awakeToggle.setAttribute("aria-pressed", String(awake.wanted));
    awakeToggle.textContent = "Keep screen on: " + (!awake.wanted ? "off" : (awake.lock ? "on" : "on, waiting"));
  }

  function requestAwake() {
    awake.triedAt = Date.now();
    if (!awake.supported || !awake.wanted || awake.lock || awake.asking || document.hidden) { return; }
    awake.asking = true;
    var asked = null;
    try { asked = navigator.wakeLock.request("screen"); } catch (e) { asked = null; }
    if (!asked || typeof asked.then !== "function") { awake.asking = false; return; }
    asked.then(function (sentinel) {
      awake.asking = false;
      if (!awake.wanted) { try { sentinel.release(); } catch (e) {} return; }   // switched off while the request was on its way
      awake.lock = sentinel;
      sentinel.addEventListener("release", function () {
        if (awake.lock === sentinel) { awake.lock = null; }
        syncAwakeUI();
        if (awake.wanted && !document.hidden) { setTimeout(requestAwake, 1000); }
      });
      syncAwakeUI();
    }, function () { awake.asking = false; syncAwakeUI(); });   // refused (battery saver, no permission yet): stays "waiting"; the timer and the next tap try again
  }

  function releaseAwake() {
    var held = awake.lock;
    awake.lock = null;
    if (held) { try { held.release(); } catch (e) {} }
    syncAwakeUI();
  }

  awakeToggle.addEventListener("click", function () {
    awake.wanted = !awake.wanted;
    try { window.localStorage.setItem("watchdog-keep-awake", awake.wanted ? "on" : "off"); } catch (e) {}
    if (awake.wanted) { requestAwake(); } else { releaseAwake(); }
    syncAwakeUI();
  });
  // Some browsers only grant the lock after a touch: the first touch asks again.
  ["pointerdown", "keydown", "touchend"].forEach(function (name) {
    document.addEventListener(name, function () { requestAwake(); }, { passive: true });
  });

  // Coming back (to the foreground, from a sleep, onto the network): catch up at once rather than at the next timer tick, and open the live stream
  // again if it died while the page was away. A background page's timers are slowed or paused by the browser, so this cannot wait for them.
  function resumeNow() {
    requestAwake();
    tickLive();
    if (uiState.offShown) { followAccess(); return; }
    var streamDead = !eventSource || eventSource.readyState === 2;
    var silent = Date.now() - (uiState.lastFrameAt || pageOpenedAt) > 12000;
    if ((streamDead || silent) && Date.now() - uiState.lastConnectAt > 3000) { connectStream(); }
  }
  document.addEventListener("visibilitychange", function () { if (!document.hidden) { resumeNow(); } });
  ["pageshow", "online", "focus"].forEach(function (name) { window.addEventListener(name, resumeNow); });

  // ---- Access switched OFF or ON from the control panel: this page follows it by itself, with no refresh ---------------------------------------------------------
  // OFF: the listener sends this viewer one "access" message and ends its stream. The page keeps running underneath (which keeps the browser's permission to play the
  // siren), puts the unavailable screen over everything and stops reconnecting. ON: it asks the open /health answer every ACCESS_POLL_MS and, when access is on
  // again, takes the screen away and opens the stream. The super user's own stream is never ended, so nothing changes for him; his banner follows the frames.
  function readAccess() {
    return fetch("/health", { cache: "no-store", credentials: "omit" }).then(function (reply) { return reply.json(); }).then(function (data) {
      return data && data.access === "off" ? "off" : (data && data.access === "on" ? "on" : null);
    }).catch(function () { return null; });
  }

  function showOff() {
    if (!offScreen || uiState.offShown) { return; }
    uiState.offShown = true;
    uiState.criticalActive = false;
    if (eventSource) { eventSource.close(); }
    setAlarm(false);
    setLiveIndicator(false);
    offScreen.hidden = false;
    document.documentElement.style.overflow = "hidden";     // the dashboard underneath must not scroll behind the screen
    hidePreloader();
  }

  function hideOff() {
    if (!uiState.offShown) { return; }
    uiState.offShown = false;
    offScreen.hidden = true;
    document.documentElement.style.overflow = "";
    connectStream();
  }

  function followAccess() {
    if (!offScreen || Date.now() - uiState.accessCheckAt < 2000) { return; }
    uiState.accessCheckAt = Date.now();
    readAccess().then(function (access) {
      if (access === "off") { showOff(); } else if (access === "on") { hideOff(); }
    });
  }

  // The red strip for the super user while access is off. It follows the frames (his stream stays open), so it appears and disappears with the button, no refresh.
  function renderControlBanner(control) {
    var off = !!(control && control.enabled && control.access === "off");
    var node = document.getElementById("control-banner");
    if (!off) { if (node) { node.remove(); } return; }
    if (!node) {
      node = document.createElement("div");
      node.id = "control-banner";
      node.setAttribute("role", "status");
      node.style.cssText = "position:sticky;top:0;z-index:99999;background:#7a1f1f;color:#fff;font:600 14px/1.4 system-ui,sans-serif;padding:8px 12px;text-align:center";
      document.body.insertBefore(node, document.body.firstChild);
    }
    var since = control.off_since ? new Date(control.off_since) : null;
    var clock = since && !isNaN(since.getTime()) ? ("0" + since.getUTCHours()).slice(-2) + ":" + ("0" + since.getUTCMinutes()).slice(-2) + " UTC" : "an unknown time";
    node.textContent = "ACCESS OFF since " + clock;
  }

  // The people on shift, as plain text in the toolbar (their display names are public by his choice). A frame from an older server has no staff part: the chip then stays hidden.
  function renderShift(staff) {
    var chip = document.getElementById("shift-chip");
    if (!chip) { return; }
    var names = (staff && staff.on_shift) || [];
    if (!names.length) { chip.hidden = true; chip.textContent = ""; return; }
    chip.textContent = "On shift: " + names.join(", ");
    chip.hidden = false;
  }

  // Open critical failures, as plain text for everybody (his words, 2026-11-08: an alert with their name on the dashboard). Only DISPLAY NAMES are in the frame, the same names the "On shift"
  // chip already shows. A frame from an older server has no such part: the strip then stays hidden. Both rules are plain functions of the frame, so a test can run them on their own.
  function stopped(text) { return text.slice(-1) === "." ? text : text + "."; }     // a display name can end in a full stop ("Ada O."): never write two
  function alertAge(seconds) {
    var whole = Math.max(0, Math.round(Number(seconds) || 0));
    if (whole < 60) { return whole + "s"; }
    if (whole < 3600) { return Math.floor(whole / 60) + "m " + ("0" + (whole % 60)).slice(-2) + "s"; }
    return Math.floor(whole / 3600) + "h " + ("0" + Math.floor((whole % 3600) / 60)).slice(-2) + "m";
  }
  function alertLines(data) {
    var lines = [];
    var items = data && Array.isArray(data.items) ? data.items : [];
    items.forEach(function (item) {
      if (!item || typeof item.playout !== "string") { return; }
      var assigned = Array.isArray(item.assigned) ? item.assigned : [];
      var onIt = Array.isArray(item.on_it) ? item.on_it : [];
      var who = assigned.length ? "Assigned to " + stopped(assigned.join(", ")) + " " + (onIt.length ? stopped("On it: " + onIt.join(", ")) : "Nobody has said they are on it yet.") : "Nobody is assigned yet.";
      lines.push(item.playout + ": critical failure open for " + alertAge(item.age_s) + ". " + who);
    });
    var more = data ? Number(data.more) : 0;
    if (lines.length && more > 0) { lines.push("and " + more + " more open failure" + (more === 1 ? "" : "s") + "."); }
    return lines;
  }
  var staffAlertParts = null;    // built once, then only its text is updated, so a link being hovered or selected text survives a live frame
  function renderStaffAlerts(data) {
    var box = document.getElementById("staff-alert");
    if (!box) { return; }
    var lines = alertLines(data);
    if (!lines.length) { box.hidden = true; return; }
    if (!staffAlertParts) {
      var title = document.createElement("div");
      title.className = "staff-alert-title";
      title.textContent = "Critical failure open. Staff: fill in the resolve form, with what happened.";
      var rows = document.createElement("div");
      var link = document.createElement("a");
      link.rel = "noopener";
      link.textContent = "Open the staff page";
      link.hidden = true;
      box.appendChild(title);
      box.appendChild(rows);
      box.appendChild(link);
      staffAlertParts = { rows: rows, link: link };
    }
    while (staffAlertParts.rows.children.length > lines.length) { staffAlertParts.rows.removeChild(staffAlertParts.rows.lastChild); }
    while (staffAlertParts.rows.children.length < lines.length) { staffAlertParts.rows.appendChild(document.createElement("div")); }
    lines.forEach(function (line, index) { staffAlertParts.rows.children[index].textContent = line; });
    var address = data && typeof data.form_url === "string" ? data.form_url : "";
    var usable = /^https:\/\//.test(address);
    staffAlertParts.link.hidden = !usable;
    if (usable && staffAlertParts.link.getAttribute("href") !== address) { staffAlertParts.link.href = address; }
    box.hidden = false;
  }

  function connectStream() {
    uiState.lastConnectAt = Date.now();
    if (eventSource) { eventSource.close(); }
    setLiveIndicator(false);
    message.textContent = "Opening pushed telemetry stream.";
    setLoaderStep("Opening a secure connection to Core...");
    eventSource = new EventSource("/stream?token=" + encodeURIComponent(token));
    eventSource.onopen = function () {
      uiState.streamDown = false;
      setLiveIndicator(!uiState.blind);
      message.textContent = "";
      setLoaderStep("Connected. Waiting for the first live snapshot...");
    };
    eventSource.onmessage = function (evt) {
      try {
        var frame = JSON.parse(evt.data);
        uiState.streamDown = false;
        uiState.lastFrameAt = Date.now();
        uiState.frameAt = uiState.lastFrameAt;
        if (frame.clock && !isNaN(parseIso(frame.clock.server_time_utc))) {
          uiState.serverClock = { ms: parseIso(frame.clock.server_time_utc), at: uiState.frameAt, tz: frame.clock.timezone || "UTC" };
        }
        checkVersion(frame);
        if (uiState.blind) { setBlind(false); setLiveIndicator(true); }
        renderAll(frame);
        renderControlBanner(frame.control);
        renderShift(frame.staff);
        renderStaffAlerts(frame.staff_alerts);
        if (!firstFrameSeen) {
          firstFrameSeen = true;
          if (loaderStep) { loaderStep.textContent = "Live"; }
          hidePreloader();
        }
      } catch (e) {
        message.textContent = "Skipped one malformed telemetry frame.";
      }
    };
    eventSource.addEventListener("access", function (evt) {
      try { if (JSON.parse(evt.data).access === "off") { showOff(); } } catch (e) {}
    });
    eventSource.onerror = function () {
      uiState.streamDown = true;
      setLiveIndicator(false);
      message.textContent = "SSE is reconnecting automatically.";
      setLoaderStep("Cannot reach Core yet. Retrying...");
      followAccess();   // a stream that failed may have been refused because access is off: look, and put the unavailable screen up if so
      // A stream the listener refused is closed for good (the browser does not retry it): the usual cause is a key that was changed after this page loaded.
      if (eventSource && eventSource.readyState === 2) {
        renewViewKey().then(function (renewed) { if (renewed) { connectStream(); } });
      }
    };
  }

  // The view key this page carries comes from the sender key, so when the sender key is changed a page that has been open for days holds a key the
  // listener no longer accepts. Ask for the page again, quietly, and take only the new key from it. The page itself stays as it is: starting it over
  // would lose the browser's permission to play the siren on an unattended screen.
  function renewViewKey() {
    if (Date.now() - uiState.lastRenewAt < 30000) { return Promise.resolve(false); }
    uiState.lastRenewAt = Date.now();
    return fetch("/?_=" + Date.now(), { cache: "no-store", credentials: "omit" }).then(function (response) {
      return response.ok ? response.text() : "";
    }).then(function (html) {
      var found = /var token = "([0-9a-f]{64})";/.exec(html);
      if (found && found[1] !== token) { token = found[1]; return true; }
      return false;
    }).catch(function () { return false; });
  }

  // A page left open for days (a wall screen, an installed app) keeps the build it loaded with, so a fix that is deployed never reaches it. Every
  // frame names the build the listener serves now; when it differs, a chip offers a one-tap update. It never reloads by itself: a reload loses
  // the browser's permission to play sound, so an unattended screen would be left with a silent siren. The tap that updates also restores it.
  function checkVersion(frame) {
    if (!frame.version || frame.version === pageVersion || uiState.updateOffered) { return; }
    uiState.updateOffered = true;
    updateChip.hidden = false;
  }

  updateChip.addEventListener("click", function () {
    unlockAudio();
    window.location.reload();
  });

  window.addEventListener("resize", fitTopologyPlane);

__MATRIX_TILES_SCRIPT__
  function setPageMode() {
    var isTopology = pageMode === "topology";
    dashboardView.hidden = isTopology;
    topologyView.hidden = !isTopology;
    document.getElementById("matrix-tiles").hidden = isTopology;
    document.getElementById("nav-dashboard").className = isTopology ? "" : "active";
    document.getElementById("nav-topology").className = isTopology ? "active" : "";
    document.getElementById("page-title").textContent = "Clarix Watchdog";
    overview.className = isTopology ? "overview compact" : "overview";
    fitTopologyPlane();
  }

  setPageMode();
  syncSoundUI();
  syncAwakeUI();
  requestAwake();
  renderAll(uiState.lastData);
  mxTilesStart();
  connectStream();
  setInterval(tickLive, 1000);
})();
</script>
</body>
</html>
"""


# Which build of the page this is. Sent in every frame so a page that has been open for days can tell it is out of date (see checkVersion). It
# covers the page and the service worker, and not the access key, so it is the same for every viewer.
# The three matrix source tiles (the unpublished design notes section 10) are put in BEFORE the build id is taken, so a change to them reaches a page that has been open for days.
_TEMPLATE = (_TEMPLATE.replace("__MATRIX_TILES_STYLE__", matrix_pages.TILES_STYLE).replace("__MATRIX_TILES_HTML__", matrix_pages.TILES_HTML)
             .replace("__MATRIX_TILES_SCRIPT__", matrix_pages.TILES_SCRIPT))


PAGE_VERSION = hashlib.sha256((_TEMPLATE + SERVICE_WORKER_JS).encode("utf-8")).hexdigest()[:12]
