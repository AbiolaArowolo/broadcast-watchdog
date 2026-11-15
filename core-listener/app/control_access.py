# File:         control_access.py
# Purpose:      Who may use the listener while access is OFF: the control key, the short-lived browser session, the request gate, and the page users see.
# Inputs:       A configured key and an offered one, a session token, a request's method and path, and a timezone-aware time handed in by the caller.
# Outputs:      yes/no answers, a signed session token, one of three gate decisions (allow / page / data), and the self-contained "unavailable" page.
# Dependencies: Python standard library only. No network, no clock of its own (same shape as standby.py and control.py).
"""Why this file is shaped the way it is (plain words).

While access is OFF, a user must see only a branded message, yet the system itself must keep working: studios keep posting heartbeats, the other node keeps probing
/health, and the super user keeps a way in. So every request gets one of three answers:

    ALLOW  the request goes on to its normal handler (which still checks its own key),
    PAGE   an HTML 503 with the branded page (only for the two dashboard pages),
    DATA   a JSON 503 (everything else, including every path nobody has heard of).

The rule for the unknown is deliberate: while OFF, a path that is not on the short allow-list is CLOSED. A new route added later cannot leak data by being forgotten.

# LEARN: allow-list, not deny-list. A deny-list ("block /status and /stream") fails open the day someone adds /export. An allow-list fails closed.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime
from typing import Optional

from app import matrix_pages

ALLOW = "allow"
PAGE = "page"
DATA = "data"

MIN_KEY_CHARS = 24
MIN_SESSION_SECONDS = 60
MAX_SESSION_SECONDS = 86400
CONTACT_URL = "https://corp.example"
SUPPORT_EMAIL = "support@corp.example"
OFF_TEXT = ("Access to this monitoring service is switched off for now. To find out more, or to have access restored, please contact ClarixTech.")

_SESSION_VERSION = "v1"
_EXPIRY = re.compile(r"[0-9]{1,12}")
_MAC = re.compile(r"[0-9a-f]{64}")


# ---- The control key ----------------------------------------------------------------------------------------------------------------------------
def key_matches(configured, offered) -> bool:
    """True only when both are non-empty text and equal. An UNSET key opens nothing (even an empty offer): a missing setting must never be a master key.
    Compared in constant time on bytes, so the time taken does not reveal how much of a guess was right (and a non-ASCII guess is a miss, not a crash)."""
    if not isinstance(configured, str) or not configured.strip() or not isinstance(offered, str) or not offered:
        return False
    return hmac.compare_digest(configured.encode("utf-8"), offered.encode("utf-8"))


def control_key_problem(key, other_secrets) -> Optional[str]:
    """None when the key is fine or simply not set (the feature is off); else one plain sentence that never contains the key."""
    if not isinstance(key, str) or not key.strip():
        return None
    if len(key) < MIN_KEY_CHARS:
        return f"the control key must be at least {MIN_KEY_CHARS} characters long"
    if any(isinstance(other, str) and other and hmac.compare_digest(key.encode("utf-8"), other.encode("utf-8")) for other in (other_secrets or ())):
        return "the control key must be different from every other key"
    return None


# ---- The browser session ------------------------------------------------------------------------------------------------------------------------
def _signing_key(secret: str) -> bytes:
    """Derived from the control key with a label, so the session signature can never be confused with anything else made from the same key.
    Rotating the control key changes this, which ends every open session at once."""
    return hmac.new(secret.encode("utf-8"), b"clarix-control-session-v1", hashlib.sha256).digest()


_GENERATION = re.compile(r"[0-9a-f]{0,64}")


def _sign(secret: str, expiry: int, generation: str = "") -> str:
    """The signature covers the expiry AND the session generation. The generation is a value the server can change at any time (a log out, a new password): every token signed
    under the old one then stops verifying at once, which is how a stolen cookie is ended without keeping a list of sessions."""
    return hmac.new(_signing_key(secret), f"{_SESSION_VERSION}.{expiry}.{generation}".encode("ascii"), hashlib.sha256).hexdigest()


def _aware(moment) -> bool:
    return isinstance(moment, datetime) and moment.tzinfo is not None


def issue_session(secret: str, now: datetime, ttl_seconds: int = 3600, generation: str = "") -> str:
    """A signed token `v1.<expiry>.<signature>` for a cookie. It carries no secret and no name, only when it ends; nothing is stored on the server."""
    if not isinstance(secret, str) or not secret.strip():
        raise ValueError("a session needs a control key to be signed with")
    if not _aware(now):
        raise ValueError("now must be a timezone-aware datetime")
    if not MIN_SESSION_SECONDS <= int(ttl_seconds) <= MAX_SESSION_SECONDS:
        raise ValueError(f"a session lasts between {MIN_SESSION_SECONDS} and {MAX_SESSION_SECONDS} seconds")
    if not isinstance(generation, str) or not _GENERATION.fullmatch(generation):
        raise ValueError("the session generation must be lowercase hex")
    expiry = int(now.timestamp()) + int(ttl_seconds)
    return f"{_SESSION_VERSION}.{expiry}.{_sign(secret, expiry, generation)}"


def verify_session(secret, token, now, generation: str = "") -> bool:
    """True only for an unexpired token signed with THIS control key. Anything else, including rubbish of any type, is False and never raises."""
    if not isinstance(secret, str) or not secret.strip() or not isinstance(token, str) or not _aware(now):
        return False
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _SESSION_VERSION or not _EXPIRY.fullmatch(parts[1]) or not _MAC.fullmatch(parts[2]):
        return False
    expiry = int(parts[1])
    if not isinstance(generation, str) or not _GENERATION.fullmatch(generation):
        return False
    if not hmac.compare_digest(parts[2].encode("ascii"), _sign(secret, expiry, generation).encode("ascii")):
        return False
    return int(now.timestamp()) < expiry


# ---- The office PC's door pass ------------------------------------------------------------------------------------------------------------------
# A browser that showed a listed certificate at the standby's staff door is handed a pass, so the same PC is recognised on the watchdog address too (his choice, 2026-11-10). It is a
# signed expiry and nothing else: no name, no generation. It has its own label and its own version, so it can never be taken for a login session, and a session never for a pass.
_PASS_VERSION = "p1"
MIN_PASS_SECONDS = 60
MAX_PASS_SECONDS = 604800


def _pass_signature(secret: str, expiry: int) -> str:
    key = hmac.new(secret.encode("utf-8"), b"clarix-office-pass-v1", hashlib.sha256).digest()
    return hmac.new(key, f"{_PASS_VERSION}.{expiry}".encode("ascii"), hashlib.sha256).hexdigest()


def issue_pass(secret: str, now: datetime, ttl_seconds: int = 43200) -> str:
    """A signed token `p1.<expiry>.<signature>`. It carries no secret and no name, only when it ends; nothing is stored on the server."""
    if not isinstance(secret, str) or not secret.strip():
        raise ValueError("a pass needs a control key to be signed with")
    if not _aware(now):
        raise ValueError("now must be a timezone-aware datetime")
    if not MIN_PASS_SECONDS <= int(ttl_seconds) <= MAX_PASS_SECONDS:
        raise ValueError(f"a pass lasts between {MIN_PASS_SECONDS} and {MAX_PASS_SECONDS} seconds")
    expiry = int(now.timestamp()) + int(ttl_seconds)
    return f"{_PASS_VERSION}.{expiry}.{_pass_signature(secret, expiry)}"


def verify_pass(secret, token, now) -> bool:
    """True only for an unexpired pass signed with THIS control key. Anything else, including rubbish of any type, is False and never raises."""
    if not isinstance(secret, str) or not secret.strip() or not isinstance(token, str) or not _aware(now):
        return False
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _PASS_VERSION or not _EXPIRY.fullmatch(parts[1]) or not _MAC.fullmatch(parts[2]):
        return False
    expiry = int(parts[1])
    if not hmac.compare_digest(parts[2].encode("ascii"), _pass_signature(secret, expiry).encode("ascii")):
        return False
    return int(now.timestamp()) < expiry


# ---- The gate -----------------------------------------------------------------------------------------------------------------------------------
# What stays open while access is OFF, exactly: the studios' posts (their own key still applies), the other node's health probe, the admin reset (its own key),
# the control door itself (its own key), and harmless branding/icon files the unavailable page and an installed phone app use.
_OPEN_EXACT = frozenset({
    "/heartbeat", "/event", "/health", "/alerts/reset", "/robots.txt", "/manifest.json", "/sw.js", "/offline.html",
    "/favicon.ico", "/icon-192.png", "/icon-512.png", "/icon-512-maskable.png", "/apple-touch-icon.png",
})
_PAGE_PATHS = frozenset({"/", "/topology"})


def gate_request(method: str, path: str, access_on: bool, is_super: bool) -> str:
    """ALLOW, PAGE or DATA for one request. `path` is the path only; a query string is cut off first so it cannot change the answer."""
    if access_on or is_super:
        return ALLOW
    clean = str(path).split("?", 1)[0].split("#", 1)[0]
    if clean in _OPEN_EXACT or clean == "/control" or clean.startswith("/control/"):
        return ALLOW
    if str(method).upper() == "GET" and clean in _PAGE_PATHS:
        return PAGE
    return DATA


def unavailable_data() -> dict:
    return {"error": "service temporarily unavailable", "detail": "Access is switched off. Contact ClarixTech."}


# ---- The pages ----------------------------------------------------------------------------------------------------------------------------------
def login_page() -> str:
    """Where the super user types the control key. The script trades it for a session cookie (POST /control/session) and then opens the dashboard. No <form>
    (the page's security policy forbids form posts), no outside resource, and the key is never put in the address bar."""
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>ClarixTech Watchdog - control</title>
<style>
  body { margin:0; min-height:100vh; display:grid; place-items:center; padding:16px; background:#070b12; color:#eaf0fa; font:16px/1.5 system-ui, sans-serif; }
  main { width:100%; max-width:26rem; background:#0f1624; border:1px solid #22304a; border-radius:16px; padding:28px 24px; }
  label { display:block; margin:0 0 8px; color:#aab7cc; }
  input { box-sizing:border-box; width:100%; min-height:44px; padding:10px 12px; border-radius:10px; border:1px solid #3a4b6b; background:#070b12; color:#eaf0fa; font:inherit; }
  button { margin-top:14px; min-height:44px; padding:10px 22px; border:0; border-radius:10px; background:#5aa9ff; color:#04101f; font:700 1rem system-ui, sans-serif; cursor:pointer; }
  input:focus-visible, button:focus-visible { outline:3px solid #ffffff; outline-offset:3px; }
  #message { min-height:1.5em; margin:12px 0 0; color:#ffb4b4; }
</style>
</head>
<body>
<main>
  <h1>Control access</h1>
  <label for="key">Control key</label>
  <input id="key" type="password" autocomplete="off" spellcheck="false">
  <button id="go" type="button">Open</button>
  <p id="message" role="status" aria-live="polite"></p>
</main>
<script>
  var box = document.getElementById("key"), message = document.getElementById("message");
  function go() {
    message.textContent = "";
    fetch("/control/session", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({key: box.value}), credentials: "same-origin"})
      .then(function (reply) { if (reply.ok) { box.value = ""; location.href = "/control/panel"; } else { message.textContent = reply.status === 429 ? "Too many tries. Wait a minute." : "That key was not accepted."; } })
      .catch(function () { message.textContent = "The server could not be reached."; });
  }
  document.getElementById("go").addEventListener("click", go);
  box.addEventListener("keydown", function (event) { if (event.key === "Enter") { go(); } });
</script>
</body>
</html>
"""


# ---- The page -----------------------------------------------------------------------------------------------------------------------------------
def unavailable_page() -> str:
    """The branded page users see while access is OFF: dark broadcast glow, one self-contained file (one small script that only asks this site's own /health whether access
    is on again and then reloads, no outside font, image or link except the contact button), readable on a phone, motion switched off for people who ask their device to
    reduce it. Wording agreed in the unpublished design notes."""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="color-scheme" content="dark">
<title>ClarixTech Watchdog</title>
<style>
  :root {{ --bg:#070b12; --card:#0f1624; --line:#22304a; --text:#eaf0fa; --muted:#aab7cc; --accent:#5aa9ff; --accent-ink:#04101f; }}
  * {{ box-sizing:border-box; }}
  html, body {{ height:100%; margin:0; }}
  body {{ background:var(--bg); color:var(--text); font:16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; display:grid; place-items:center; padding:16px; overflow-x:hidden; }}
  .glow {{ position:fixed; inset:-20%; z-index:0; background:radial-gradient(40% 35% at 25% 30%, rgba(90,169,255,.28), transparent 70%), radial-gradient(35% 30% at 78% 70%, rgba(150,110,255,.22), transparent 70%); animation:drift 18s ease-in-out infinite alternate; }}
  .wave {{ position:fixed; left:0; right:0; top:50%; width:100%; height:120px; margin-top:-60px; z-index:0; opacity:.55; }}
  .wave path {{ fill:none; stroke:var(--accent); stroke-width:1.5; stroke-dasharray:14 10; animation:flow 6s linear infinite; }}
  main {{ position:relative; z-index:1; width:100%; max-width:30rem; background:rgba(15,22,36,.88); border:1px solid var(--line); border-radius:18px; padding:32px 26px; text-align:center; box-shadow:0 20px 60px rgba(0,0,0,.45); }}
  .brand {{ letter-spacing:.18em; text-transform:uppercase; font-size:.8rem; color:var(--muted); margin:0 0 14px; }}
  h1 {{ font-size:1.55rem; line-height:1.25; margin:0 0 12px; }}
  p {{ margin:0 0 22px; color:var(--muted); }}
  .button {{ display:inline-block; min-height:44px; padding:12px 26px; border-radius:12px; background:var(--accent); color:var(--accent-ink); font-weight:700; text-decoration:none; }}
  .button:focus-visible, .mail:focus-visible, .staff:focus-visible {{ outline:3px solid #ffffff; outline-offset:3px; }}
  .staff {{ display:block; margin-top:22px; font-size:.85rem; color:var(--muted); }}
  .mail {{ display:block; margin-top:18px; color:var(--text); }}
  @keyframes drift {{ from {{ transform:translate3d(-3%, -2%, 0) scale(1); }} to {{ transform:translate3d(3%, 2%, 0) scale(1.08); }} }}
  @keyframes flow {{ to {{ stroke-dashoffset:-96; }} }}
  @media (prefers-reduced-motion: reduce) {{ .glow, .wave path {{ animation:none; }} }}
</style>
</head>
<body>
<div class="glow" aria-hidden="true"></div>
<svg class="wave" viewBox="0 0 1200 120" preserveAspectRatio="none" aria-hidden="true" focusable="false"><path d="M0 60 L180 60 L220 20 L270 100 L320 38 L360 60 L560 60 L600 8 L650 112 L700 44 L740 60 L940 60 L980 24 L1030 96 L1080 52 L1120 60 L1200 60"/></svg>
<main>
  <p class="brand">ClarixTech</p>
  <h1>Service temporarily unavailable</h1>
  <p>{OFF_TEXT}</p>
  <a class="button" href="{CONTACT_URL}">Contact ClarixTech</a>
  <a class="mail" href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a>
  <a class="staff" href="/control/login">Staff sign in</a>
</main>
<script>
  // When the super user switches access back ON this page opens the dashboard by itself: it asks this site's open /health every 5 s. A failed check changes nothing, and it never
  // reloads more than once in 20 s (so an odd answer can never make it loop).
  (function () {{
    function resume() {{
      try {{
        var last = Number(sessionStorage.getItem("wd-resume") || 0);
        if (Date.now() - last < 20000) {{ return; }}
        sessionStorage.setItem("wd-resume", String(Date.now()));
      }} catch (e) {{}}
      location.reload();
    }}
    function look() {{
      fetch("/health", {{ cache: "no-store", credentials: "omit" }})
        .then(function (reply) {{ return reply.json(); }})
        .then(function (data) {{ if (data && data.access === "on") {{ resume(); }} }})
        .catch(function () {{}});
    }}
    setInterval(look, 5000);
  }})();
</script>
</body>
</html>
"""


def off_overlay_html() -> str:
    """The same message as a layer for a dashboard that is already open. It starts hidden; the page shows it the moment access is switched OFF and hides it again when access is ON,
    so nothing is refreshed and the page keeps the browser's permission to play the siren. No script (the dashboard's own code shows and hides it) and no outside resource."""
    return f"""<style>
#off-screen[hidden] {{ display:none; }}
#off-screen {{ position:fixed; inset:0; z-index:100000; display:grid; place-items:center; padding:16px; overflow:auto; background:#070b12; color:#eaf0fa; font:16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
#off-screen .card {{ width:100%; max-width:30rem; background:rgba(15,22,36,.95); border:1px solid #22304a; border-radius:18px; padding:32px 26px; text-align:center; box-shadow:0 20px 60px rgba(0,0,0,.45); }}
#off-screen .brand {{ letter-spacing:.18em; text-transform:uppercase; font-size:.8rem; color:#aab7cc; margin:0 0 14px; }}
#off-screen h1 {{ font-size:1.55rem; line-height:1.25; margin:0 0 12px; }}
#off-screen p {{ margin:0 0 22px; color:#aab7cc; }}
#off-screen a.go {{ display:inline-block; min-height:44px; padding:12px 26px; border-radius:12px; background:#5aa9ff; color:#04101f; font-weight:700; text-decoration:none; }}
#off-screen a.mail {{ display:block; margin-top:18px; color:#eaf0fa; }}
#off-screen a.staff {{ display:block; margin-top:22px; font-size:.85rem; color:#aab7cc; }}
#off-screen a:focus-visible {{ outline:3px solid #ffffff; outline-offset:3px; }}
</style>
<div id="off-screen" role="status" hidden>
  <div class="card">
    <p class="brand">ClarixTech</p>
    <h1>Service temporarily unavailable</h1>
    <p>{OFF_TEXT}</p>
    <a class="go" href="{CONTACT_URL}">Contact ClarixTech</a>
    <a class="mail" href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a>
    <a class="staff" href="/control/login">Staff sign in</a>
  </div>
</div>
"""


_PAGE_STYLE = """
  :root { --bg:#070b12; --card:#0f1624; --line:#22304a; --text:#eaf0fa; --muted:#aab7cc; --accent:#5aa9ff; --accent-ink:#04101f; --bad:#ff7a7a; --good:#5be3a1; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; padding:16px; }
  main { width:100%; max-width:56rem; margin:0 auto; }
  h1 { font-size:1.4rem; margin:0 0 4px; }
  h2 { font-size:1.05rem; margin:0 0 8px; color:var(--muted); font-weight:600; }
  p { margin:0 0 12px; color:var(--muted); }
  .card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:16px 18px; margin:0 0 14px; }
  label { display:block; margin:0 0 6px; color:var(--muted); }
  input { width:100%; min-height:48px; padding:10px 12px; margin:0 0 14px; border-radius:10px; border:1px solid #3a4b6b; background:var(--bg); color:var(--text); font:inherit; }
  button { min-height:48px; padding:10px 22px; border:0; border-radius:12px; background:var(--accent); color:var(--accent-ink); font:700 1rem system-ui, sans-serif; cursor:pointer; }
  button:disabled { opacity:.5; cursor:wait; }
  button.off { background:#ff8a8a; }
  button.on { background:var(--good); }
  button.quiet { background:transparent; color:var(--text); border:1px solid #3a4b6b; }
  input:focus-visible, button:focus-visible, a:focus-visible { outline:3px solid #ffffff; outline-offset:3px; }
  a { color:var(--accent); }
  #message { min-height:1.5em; margin:12px 0 0; color:var(--bad); }
  .row { display:flex; flex-wrap:wrap; gap:12px; }
  .nodes { display:grid; grid-template-columns:repeat(auto-fit, minmax(16rem, 1fr)); gap:14px; }
  dl { margin:0; display:grid; grid-template-columns:auto 1fr; gap:4px 14px; }
  dt { color:var(--muted); }
  dd { margin:0; font-weight:600; }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); }
  .banner { padding:10px 14px; border-radius:10px; margin:0 0 14px; background:#3b1a1a; color:#ffd6d6; font-weight:600; }
  h3 { font-size:.95rem; margin:16px 0 6px; color:var(--text); font-weight:600; }
  select { width:100%; min-height:48px; padding:10px 12px; margin:0 0 14px; border-radius:10px; border:1px solid #3a4b6b; background:var(--bg); color:var(--text); font:inherit; }
  summary { cursor:pointer; font-weight:600; margin:0 0 10px; }
  details.section { border-top:1px solid var(--line); margin:12px 0 0; padding-top:10px; }
  details.section > summary { color:var(--text); }
  input[type=checkbox] { width:24px; height:24px; min-height:24px; margin:0 6px 0 0; padding:0; vertical-align:middle; }
  .check { display:inline-flex; align-items:center; margin:0 16px 8px 0; color:var(--text); }
  .chips { display:flex; flex-wrap:wrap; gap:8px; margin:0 0 6px; }
  .chip { display:inline-flex; gap:8px; align-items:center; border:1px solid #3a4b6b; border-radius:999px; padding:6px 12px; color:var(--text); }
  .chip small { color:var(--muted); }
  .tag { color:#ffd27a; font-weight:600; }
  .item { border:1px solid var(--line); border-radius:10px; padding:10px 12px; margin:0 0 10px; }
  .item p { margin:0 0 6px; }
  .strong { color:var(--text); font-weight:600; }
  .grid2 { display:grid; grid-template-columns:repeat(auto-fit, minmax(13rem, 1fr)); gap:0 14px; align-items:start; }
  .hint { margin:6px 0 12px; color:var(--muted); font-size:.85rem; line-height:1.35; }
  button.small { min-height:44px; padding:6px 14px; font-size:.9rem; margin:0 8px 8px 0; }
  @media (prefers-reduced-motion: reduce) { * { animation:none !important; transition:none !important; } }
"""

_LOGIN_PASSWORD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>ClarixTech Watchdog - sign in</title>
<style>__STYLE__</style>
</head>
<body>
<main style="max-width:26rem;margin-top:12vh">
  <div class="card">
    <h1>Sign in</h1>
    <p>Staff and the super user sign in here.</p>
    <label for="user">Name</label>
    <input id="user" type="text" autocomplete="username" spellcheck="false">
    <label for="pw">Password</label>
    <input id="pw" type="password" autocomplete="current-password" spellcheck="false">
    <button id="go" type="button">Sign in</button>
    <p id="message" role="status" aria-live="polite"></p>
    __STAFF_WAY__
  </div>
</main>
<script>
  var user = document.getElementById("user"), pw = document.getElementById("pw"), message = document.getElementById("message"), go = document.getElementById("go");
  function signIn() {
    message.textContent = "";
    go.disabled = true;
    fetch("/control/session", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({user: user.value, password: pw.value}), credentials: "same-origin"})
      .then(function (reply) {
        if (reply.ok) {
          return reply.json().then(function (data) { pw.value = ""; location.href = (data && data.next === "/staff") ? "/staff" : "/control/panel"; }, function () { pw.value = ""; location.href = "/control/panel"; });
        }
        // The server says WHY for the two refusals that are not a wrong name or password (a staff sign-in on the wrong door, a lock with its wait); anything else keeps the old words.
        return reply.json().then(function (data) { return data; }, function () { return null; }).then(function (data) {
          var told = (reply.status === 403 || reply.status === 429) && data && typeof data.error === "string" && data.error;
          message.textContent = told || (reply.status === 429 ? "Too many wrong tries. Wait a few minutes and try again." : "That name or password was not accepted.");
          go.disabled = false;
        });
      })
      .catch(function () { message.textContent = "The server could not be reached."; go.disabled = false; });
  }
  go.addEventListener("click", signIn);
  pw.addEventListener("keydown", function (event) { if (event.key === "Enter") { signIn(); } });
</script>
</body>
</html>
"""

_PANEL_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>ClarixTech Watchdog - control</title>
<style>__STYLE__
__MATRIX_STYLE__</style>
</head>
<body>
<main>
  <h1>Watchdog control</h1>
  <p>Signed in as the super user. A button changes both nodes and shows what each one says afterwards. <a href="/">Back to the dashboard</a></p>
  <div id="banner" class="banner" hidden></div>
  <div class="nodes" id="nodes"></div>
  <div class="card">
    <h2>Buttons</h2>
    <div class="row">
      <button id="off" type="button" class="off">Switch access OFF</button>
      <button id="on" type="button" class="on">Switch access ON</button>
      <button id="core" type="button">Use Core</button>
      <button id="vps" type="button">Use the VPS</button>
      <button id="out" type="button" class="quiet">Log out</button>
    </div>
    <p id="message" role="status" aria-live="polite"></p>
  </div>
__MATRIX_CARD__
  <div class="card" id="staff">
    <h2>Staff <span id="slots"></span></h2>
    <p>People who sign in on the same page as you, each with their own cookie. You add, reset and disable them. A critical alert is assigned automatically to everyone on shift, so there is nothing to assign here. A shift is only Start and Finish, and can run up to 48 hours.</p>
    <p id="staffmsg" role="status" aria-live="polite"></p>
    <details id="sec-roster" class="section"><summary id="sum-roster">On shift now</summary>
      <div id="roster" class="chips"></div>
    </details>
    <details id="sec-failures" class="section" open><summary id="sum-failures">Open failures</summary>
      <div id="failures"></div>
    </details>
    <details id="sec-people" class="section"><summary id="sum-people">People</summary>
      <table><thead><tr><th scope="col">Username</th><th scope="col">Name shown to everyone</th><th scope="col">Status</th><th scope="col">Actions</th></tr></thead><tbody id="people"></tbody></table>
    </details>
    <details id="sec-add" class="section"><summary>Add a person</summary>
    <div class="grid2">
      <div><label for="add-user">Username</label><input id="add-user" type="text" autocomplete="off" spellcheck="false" maxlength="24"><div class="hint">3 to 24 lowercase letters, digits, dots, dashes</div></div>
      <div><label for="add-display">Name shown to everyone</label><input id="add-display" type="text" autocomplete="off" maxlength="30"><div class="hint">What the dashboard shows when they are on shift</div></div>
      <div><label for="add-pass">First password</label><input id="add-pass" type="password" autocomplete="new-password" spellcheck="false"><div class="hint">At least 4 characters. A simple word or number is fine.</div></div>
    </div>
    <button id="add-go" type="button" class="small">Add person</button>
    </details>
    <details id="sec-reset" class="section"><summary>Reset a password</summary>
    <div class="grid2">
      <div><label for="reset-user">Person</label><select id="reset-user"></select></div>
      <div><label for="reset-pass">New password</label><input id="reset-pass" type="password" autocomplete="new-password" spellcheck="false"></div>
    </div>
    <button id="reset-go" type="button" class="small">Reset password</button>
    </details>
    <details id="sec-done" class="section"><summary id="sum-done">Finished in the last 24 hours</summary>
      <div id="done"></div>
    </details>
  </div>
  <div class="card">
    <h2>Latency: when each node last heard each studio</h2>
    <table><thead><tr><th scope="col">Studio</th><th scope="col">This node heard</th><th scope="col">Other node heard</th><th scope="col">Difference</th></tr></thead><tbody id="studios"></tbody></table>
    <p id="trip"></p>
  </div>
</main>
<script>
  var buttons = ["off", "on", "core", "vps"].map(function (id) { return document.getElementById(id); });
  var messageBox = document.getElementById("message");
  function say(text, bad) { messageBox.textContent = text; messageBox.style.color = bad ? "var(--bad)" : "var(--good)"; }
  function busy(on) { buttons.forEach(function (button) { button.disabled = on; }); }
  function api(path, options) {
    return fetch(path, Object.assign({credentials: "same-origin", headers: {"Content-Type": "application/json"}}, options)).then(function (reply) {
      if (reply.status === 401) { location.href = "/control/login"; throw new Error("sign in"); }
      return reply.json().then(function (data) { return {status: reply.status, data: data}; });
    });
  }
  function el(tag, text, className) { var node = document.createElement(tag); if (text !== undefined) { node.textContent = text; } if (className) { node.className = className; } return node; }
  function card(title, info, reachable) {
    var box = el("div", undefined, "card"), list = el("dl");
    box.appendChild(el("h2", title));
    if (!reachable) {
      box.appendChild(el("p", "Not reached" + (info.reason ? ": " + info.reason : "") + ". Its buttons will repair it when it is back."));
      return box;
    }
    var peer = info.peer || {};
    [["Access", String(info.access || "?").toUpperCase()], ["Preferred", info.preferred], ["Alerting now", info.alerting_now ? "yes" : "no"],
     ["Sees the other node", peer.believed ? peer.believed + (peer.preferred ? " (says " + peer.preferred + " is preferred)" : "") : "not watched"]].forEach(function (pair) {
      list.appendChild(el("dt", pair[0])); list.appendChild(el("dd", pair[1]));
    });
    box.appendChild(list);
    return box;
  }
  function ago(node, seen) { return seen && node.server_time_utc ? Math.max(0, (Date.parse(node.server_time_utc) - Date.parse(seen)) / 1000) : null; }
  function render(state) {
    if (state.staff) { renderStaff(state.staff); }
    mxShow(!!(state.matrix && state.matrix.available));
    var mine = state.self, theirs = state.peer, nodes = document.getElementById("nodes");
    nodes.textContent = "";
    nodes.appendChild(card("This node: " + mine.node, mine, true));
    nodes.appendChild(card("The other node: " + (theirs.node || "?"), theirs, theirs.reachable));
    var banner = document.getElementById("banner");
    banner.hidden = !(mine.access === "off");
    banner.textContent = mine.access === "off" ? "Access is OFF: users see the unavailable page and get no alerts. You still do." : "";
    var body = document.getElementById("studios");
    body.textContent = "";
    var names = {};
    Object.keys(mine.studios || {}).forEach(function (name) { names[name] = 1; });
    Object.keys(theirs.studios || {}).forEach(function (name) { names[name] = 1; });
    Object.keys(names).sort().forEach(function (name) {
      var a = ago(mine, (mine.studios || {})[name]), b = theirs.reachable ? ago(theirs, (theirs.studios || {})[name]) : null, row = el("tr");
      row.appendChild(el("th", name)); row.lastChild.scope = "row";
      row.appendChild(el("td", a === null ? "never" : a.toFixed(1) + " s ago"));
      row.appendChild(el("td", b === null ? "no copy" : b.toFixed(1) + " s ago"));
      row.appendChild(el("td", a === null || b === null ? "-" : "other is " + Math.abs(b - a).toFixed(1) + " s " + (b - a >= 0 ? "behind" : "ahead")));
      body.appendChild(row);
    });
    document.getElementById("trip").textContent = theirs.reachable && theirs.round_trip_ms !== undefined ? "Round trip from this node to the other node: " + theirs.round_trip_ms + " ms. The studios report about every 10 s, so a few seconds of difference is normal." : "";
  }
  var staffNotes = {};
  function staffSay(text, bad) { var box = document.getElementById("staffmsg"); box.textContent = text; box.style.color = bad ? "var(--bad)" : "var(--good)"; }
  function staffTime(iso) { var moment = new Date(iso); return isNaN(moment.getTime()) ? "?" : moment.toLocaleString([], {weekday: "short", hour: "2-digit", minute: "2-digit", timeZone: "Africa/Lagos"}) + " WAT"; }
  function staffAct(payload, after) {
    staffSay("Working ...", false);
    api("/control/panel/staff", {method: "POST", body: JSON.stringify(payload)}).then(function (result) {
      if (result.data.ok) { staffSay("Done.", false); renderStaff(result.data, true); if (after) { after(); } }
      else { staffSay(result.data.error || "That did not work.", true); }
    }).catch(function () { staffSay("The server could not be reached.", true); });
  }
  function renderRoster(view) {
    var box = document.getElementById("roster");
    box.textContent = "";
    if (!view.roster.length) { box.appendChild(el("p", "Nobody is on shift.")); return; }
    view.roster.forEach(function (item) {
      var chip = el("span", item.display, "chip");
      chip.appendChild(el("small", staffTime(item.since)));
      if (item.long) { chip.appendChild(el("small", "over 48 hours", "tag")); }
      var finish = el("button", "Finish shift", "small quiet");
      finish.type = "button";
      finish.addEventListener("click", function () { staffAct({action: "finish_shift", username: item.username}); });
      chip.appendChild(finish);
      box.appendChild(chip);
    });
  }
  function renderPeople(view) {
    var body = document.getElementById("people");
    body.textContent = "";
    view.people.forEach(function (person) {
      var row = el("tr"), actions = el("td");
      row.appendChild(el("th", person.username)); row.lastChild.scope = "row";
      row.appendChild(el("td", person.display));
      row.appendChild(el("td", !person.active ? "disabled" : (person.on_shift ? "on shift" : "active")));
      if (person.active) {
        if (!person.on_shift) {
          var start = el("button", "Start shift", "small");
          start.type = "button";
          start.addEventListener("click", function () { staffAct({action: "start_shift", username: person.username}); });
          actions.appendChild(start);
        }
        var disable = el("button", "Disable", "small quiet");
        disable.type = "button";
        disable.addEventListener("click", function () {
          if (confirm("Disable " + person.display + "? They are signed out at once and lose their place. Their history stays.")) { staffAct({action: "disable_person", username: person.username}); }
        });
        actions.appendChild(disable);
      }
      var remove = el("button", "Delete", "small quiet");
      remove.type = "button";
      remove.addEventListener("click", function () {
        if (confirm("Delete " + person.display + "? This cannot be undone. They are signed out at once and their shift ends. Their history stays, and the name can be used again.")) { staffAct({action: "delete_person", username: person.username}); }
      });
      actions.appendChild(remove);
      row.appendChild(actions);
      body.appendChild(row);
    });
  }
  // A length of time written as the weekly report writes it: 45s, 4m 12s, 1h 05m.
  function staffSpan(seconds) {
    var whole = Math.round(seconds);
    if (whole < 60) { return whole + "s"; }
    if (whole < 3600) { return Math.floor(whole / 60) + "m " + ("0" + (whole % 60)).slice(-2) + "s"; }
    return Math.floor(whole / 3600) + "h " + ("0" + Math.floor((whole % 3600) / 60)).slice(-2) + "m";
  }
  // Who said "I am on it", with the length of time after the red. A press whose time could not be measured has after_s null and is shown without one, never as 0s.
  function staffSeen(item) { return item.display + " at " + staffTime(item.at) + (item.after_s !== null ? " (" + staffSpan(item.after_s) + " after the red)" : ""); }
  function renderFailures(view) {
    var list = document.getElementById("failures");
    list.textContent = "";
    if (!view.failures.length) { list.appendChild(el("p", "No open failures.")); return; }
    view.failures.forEach(function (failure) {
      var box = el("div", undefined, "item");
      box.appendChild(el("p", failure.display + ": " + failure.cause + (failure.latest_cause !== failure.cause ? " (latest: " + failure.latest_cause + ")" : ""), "strong"));
      box.appendChild(el("p", "Red at " + staffTime(failure.first_at) + ", " + failure.alarms + (failure.alarms === 1 ? " alarm" : " alarms") + (failure.alarms > 1 ? ", the last at " + staffTime(failure.last_at) : "") + "."));
      // Nothing to assign by hand (his words, 2026-11-08): the system gives a critical alert to everyone on shift. He sees who holds it and who said they are on it, and he can still mark it Done.
      box.appendChild(el("p", failure.people.length ? "With " + failure.people.map(function (person) { return person.display; }).join(", ") + "." : "Nobody has it yet."));
      var seen = failure.seen || [];
      box.appendChild(el("p", seen.length ? "On it: " + seen.map(function (item) { return staffSeen(item); }).join("; ") + "." : "Nobody has said they are on it yet."));
      var note = el("input");
      note.type = "text"; note.maxLength = 200; note.placeholder = "A short note (optional)"; note.value = staffNotes[failure.fid] || "";
      note.setAttribute("aria-label", "Note for " + failure.display);
      note.addEventListener("input", function () { staffNotes[failure.fid] = note.value; });
      var done = el("button", "Mark done", "small quiet");
      done.type = "button";
      done.addEventListener("click", function () { staffAct({action: "done", fid: failure.fid, note: note.value}, function () { delete staffNotes[failure.fid]; }); });
      box.appendChild(note);
      box.appendChild(done);
      list.appendChild(box);
    });
  }
  // ---- Long sections fold away (his standing rule): a native details/summary with the one-line status in the summary; the urgent one (open failures) is open ----
  function rosterSummary(names) { return "On shift now: " + (names.length ? names.join(", ") : "nobody"); }
  function failuresSummary(count) { return "Open failures: " + (count ? count : "none"); }
  function doneSummary(count) { return "Finished in the last 24 hours: " + (count ? count : "none"); }
  function peopleSummary(people) {
    var active = people.filter(function (person) { return person.active; }).length, on = people.filter(function (person) { return person.on_shift; }).length;
    return "People: " + active + " active, " + on + " on shift";
  }
  // true = open it, false = fold it, null = leave it as the person left it. First look: open only if something is urgent. After that: only when there is MORE that is urgent than before.
  function urgentOpen(previous, current) {
    if (previous === null) { return current > 0; }
    return current > previous ? true : null;
  }
  var urgentSeen = null;
  function renderStaff(view, force) {
    document.getElementById("slots").textContent = "(" + view.slots.active + " of " + view.slots.max + " places used)";
    document.getElementById("sum-roster").textContent = rosterSummary(view.roster.map(function (item) { return item.display; }));
    document.getElementById("sum-failures").textContent = failuresSummary(view.failures.length);
    document.getElementById("sum-people").textContent = peopleSummary(view.people);
    document.getElementById("sum-done").textContent = doneSummary(view.done.length);
    var wanted = urgentOpen(urgentSeen, view.failures.length);
    if (wanted !== null) { document.getElementById("sec-failures").open = wanted; }
    urgentSeen = view.failures.length;
    renderRoster(view);
    renderPeople(view);
    var typing = document.activeElement && document.getElementById("failures").contains(document.activeElement);
    if (force || !typing) { renderFailures(view); }
    var select = document.getElementById("reset-user"), keep = select.value;
    select.textContent = "";
    view.people.forEach(function (person) {
      if (!person.active) { return; }
      var option = el("option", person.display + " (" + person.username + ")");
      option.value = person.username;
      select.appendChild(option);
    });
    if (keep) { select.value = keep; }
    var finished = document.getElementById("done");
    finished.textContent = "";
    if (!view.done.length) { finished.appendChild(el("p", "Nothing finished in the last day.")); }
    view.done.forEach(function (item) {
      finished.appendChild(el("p", item.display + ", finished " + staffTime(item.done_at) + " by " + (item.done_by === "super" ? "you" : item.done_by) + (item.note ? ": " + item.note : ".")));
    });
  }
  function field(id) { return document.getElementById(id).value; }
  document.getElementById("add-go").addEventListener("click", function () {
    staffAct({action: "add_person", username: field("add-user").trim().toLowerCase(), display: field("add-display").trim(), password: field("add-pass")},
             function () { ["add-user", "add-display", "add-pass"].forEach(function (id) { document.getElementById(id).value = ""; }); });
  });
  document.getElementById("reset-go").addEventListener("click", function () {
    staffAct({action: "reset_password", username: field("reset-user"), password: field("reset-pass")}, function () { document.getElementById("reset-pass").value = ""; });
  });
  function load() { api("/control/panel/state", {method: "GET"}).then(function (result) { render(result.data); }).catch(function () {}); }
  function press(action, confirmText) {
    if (confirmText && !confirm(confirmText)) { return; }
    busy(true); say("Working ...", false);
    api("/control/panel/act", {method: "POST", body: JSON.stringify({action: action})}).then(function (result) {
      var data = result.data;
      say((data.messages || [data.error || "No answer."]).join(" "), !data.ok);
      if (data.self) { render(data); }
    }).catch(function () { say("The server could not be reached.", true); }).then(function () { busy(false); });
  }
  document.getElementById("off").addEventListener("click", function () { press("off", "Switch access OFF? Users lose the dashboard and all alerts; everyone except you. You will be mailed the details."); });
  document.getElementById("on").addEventListener("click", function () { press("on", ""); });
  document.getElementById("core").addEventListener("click", function () { press("use_core", ""); });
  document.getElementById("vps").addEventListener("click", function () { press("use_vps", ""); });
  document.getElementById("out").addEventListener("click", function () { fetch("/control/logout", {method: "POST", credentials: "same-origin"}).then(function () { location.href = "/control/login"; }); });
__MATRIX_SCRIPT__
  load();
  setInterval(load, 4000);
</script>
</body>
</html>
"""


def page_style() -> str:
    """The look the sign-in page, the control panel and the Staff page share."""
    return _PAGE_STYLE


_STAFF_WAY_ADDRESS = re.compile(r"https://[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~/-]*)?")


def login_page_password(staff_url=None) -> str:
    """Where the super user and staff sign in with a name and a password (the page shown once a password is set up). Self-contained: no outside resource, no form, nothing stored in the
    browser. `staff_url` is the office PCs' staff address, handed in by the server that makes the page (the template itself names no address); it becomes one plain link, and only when it is a
    plain https address, so nothing an attacker could shape ever lands in the page."""
    if isinstance(staff_url, str) and len(staff_url) <= 200 and _STAFF_WAY_ADDRESS.fullmatch(staff_url):
        way = f'<p class="hint">Staff: sign in at <a href="{staff_url}">the staff address</a>, from an office PC.</p>'
    else:
        way = ""
    return _LOGIN_PASSWORD_HTML.replace("__STYLE__", _PAGE_STYLE).replace("__STAFF_WAY__", way)


def panel_page() -> str:
    """The control page: both nodes side by side, the four buttons and a log-out, and a live table of when each node last heard each studio. It holds no secret: everything it shows
    is fetched after sign-in, and a press goes to the server, which checks the session again."""
    return (_PANEL_HTML.replace("__STYLE__", _PAGE_STYLE).replace("__MATRIX_STYLE__", matrix_pages.CARD_STYLE).replace("__MATRIX_CARD__", matrix_pages.CARD_HTML)
            .replace("__MATRIX_SCRIPT__", matrix_pages.CARD_SCRIPT))
