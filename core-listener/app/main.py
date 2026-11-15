"""
Clarix Broadcast Watchdog -- central listener (Phase 1: detection only).

One listener for all four sources: main and backup-server's simple process
heartbeats, plus Region B (SITE-B) and Region A (SITE-A), whose own local
detectors already produce much richer signals (SITE-B's tiered transition
classification; SITE-A's real freeze/black/silence/loudness events from the
actual decoded output) than a liveness ping. Two endpoints reflect that:

  POST /heartbeat  -- "I'm alive", from any source, on a steady interval.
                      Tracked in `sources`; a background loop alerts on
                      MISSED_INTERVALS_BEFORE_ALERT consecutive misses
                      (hysteresis -- one missed beat is not an alert). A
                      missing heartbeat means "can't confirm this machine is
                      alive" -- a connectivity problem, not proof the playout
                      itself is broken, and never grounds for auto-recovery.
                      A heartbeat also carries the running instances, checked
                      against WATCHDOG_EXPECTED_INSTANCES (a count, or a list
                      of feed names).
  POST /event      -- a specific, already-classified finding from a site's
                      own detector (a transition overrun tier, a freeze/
                      black/silence event, a restart, a suppressed alert).
                      Logged to `events` with its own severity. This is
                      where SITE-B's and SITE-A's detectors report their
                      real content-level findings, separate from mere
                      liveness.

Both endpoints funnel into the same `record_event` function and the same
`events` table, so the eventual dashboard has one place to read from
regardless of which site or signal type produced a finding.

Explicitly out of scope for Phase 1: no recovery actions, no dashboard UI
beyond a plain status readout, no dependency on the audio-normalizer stack
or Clarix Edge.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import math
import os
import re
import secrets
import shutil
import smtplib
import sqlite3
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from app.dashboard import MANIFEST_JSON, OFFLINE_HTML, PAGE_VERSION, SERVICE_WORKER_JS, OFF_SCREEN_MARKER, STAFF_LINK_MARKER, render_dashboard_html, render_topology_html
from app.guard import (STATUS_PER_MINUTE, STREAMS_PER_VISITOR, AuthLog, KeyConfigError, KeyFileWatcher, Keyring, RefusalLog, RouteLog, StreamPool, VisitorRate,
                       forwarded_peer_trusted, keyset_digest, load_sender_keys, parse_auth_mode, parse_forwarded_trust, parse_trusted_proxies, read_default_gateways,
                       route_kind, route_protection, visitor_key)
from app.standby import (HealthConfig, HealthInputs, CoreWatch, WatchConfig, assess_health, parse_expect_copies, parse_probe_url, parse_role, standdown_notice,
                         summarize_problems, takeover_notice)
from app.telemetry import SAMPLE_SECONDS, HeartbeatCounter, SystemSampler, fleet_counters
from app import control, control_access, control_login, matrix, staff, staff_pages, site_c_feed

_ICON_DIR = Path(__file__).resolve().parent / "static_icons"
_ICONS = {name: (_ICON_DIR / name).read_bytes() for name in
          ("icon-192.png", "icon-512.png", "icon-512-maskable.png", "apple-touch-icon.png", "favicon-32.png")}
NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("watchdog_listener")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


# The zone Core's time is shown in on the dashboard and in alert emails. Core's operating system keeps its clock in UTC, but the
# server is in New York and that is the time its operators mean by "Core time", so that is the default. The browser does the conversion
# on the dashboard (it knows every zone); the server only names the zone and sends its own clock.
DISPLAY_TIMEZONE = (os.environ.get("WATCHDOG_DISPLAY_TIMEZONE") or "America/New_York").strip() or "America/New_York"


def _display_zone():
    """The display zone as a tzinfo, or None when this machine has no zone data for it (a bare Windows dev PC) or the name is unknown."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(DISPLAY_TIMEZONE)
    except Exception:  # noqa: BLE001 -- missing data or a bad name must never break an email
        return None


def _stamp_text(moment: datetime) -> str:
    """A moment as Core's local time with the zone's name, then UTC in brackets: '2026-10-12 08:21:36 PM EDT (00:21:36 UTC)'.
    UTC alone when the display zone is UTC or cannot be loaded."""
    utc = moment.astimezone(timezone.utc)
    zone = _display_zone()
    if zone is None or DISPLAY_TIMEZONE.upper() == "UTC":
        return utc.strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"{utc.astimezone(zone).strftime('%Y-%m-%d %I:%M:%S %p %Z')} ({utc.strftime('%H:%M:%S')} UTC)"


def _clock_snapshot(now: datetime) -> dict:
    """Core's own clock, so a viewer can see the SERVER's time beside their device's time. The browser keeps it running on its own
    timer from the moment the frame arrives, so a wrong clock on the viewing device never shifts it."""
    return {"server_time_utc": now.astimezone(timezone.utc).isoformat(), "timezone": DISPLAY_TIMEZONE}


# Core's own vital signs and the dashboard's counters (see telemetry.py). Memory only: nothing here is stored.
STARTED_AT = datetime.now(timezone.utc)
_system = SystemSampler()
_heartbeats = HeartbeatCounter()

DB_PATH = Path(os.environ.get("WATCHDOG_DB_PATH", "/data/watchdog.db"))
# Expected heartbeat interval must match (or exceed) the edge script's own
# -IntervalSeconds. Alert only after several consecutive misses, never one.
HEARTBEAT_INTERVAL_SECONDS = int(os.environ.get("WATCHDOG_HEARTBEAT_INTERVAL_SECONDS", "10"))
MISSED_INTERVALS_BEFORE_ALERT = int(os.environ.get("WATCHDOG_MISSED_INTERVALS_BEFORE_ALERT", "3"))
CHECK_LOOP_SECONDS = 5
# What a heartbeat may say is untrusted input that is broadcast to every viewer every few seconds, so it is bounded:
MAX_TEXT_CHARS = 300                   # a file name or a detail line
MAX_ITEM_SECONDS = 7 * 24 * 3600       # no playout item is longer than a week
MAX_INTERVAL_SECONDS = 24 * 3600       # nor is a sender's reporting interval
MAX_SOURCE_ID_CHARS = 64
MAX_EVENT_TYPE_CHARS = 96
MAX_DETAIL_CHARS = 16 * 1024

SMTP_HOST = os.environ.get("WATCHDOG_SMTP_HOST")
SMTP_PORT = int(os.environ.get("WATCHDOG_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("WATCHDOG_SMTP_USER")
SMTP_PASS = os.environ.get("WATCHDOG_SMTP_PASS")
ALERT_EMAIL_TO = os.environ.get("WATCHDOG_ALERT_EMAIL_TO", "support@client.example")
ALERT_EMAIL_FROM = os.environ.get("WATCHDOG_ALERT_EMAIL_FROM", SMTP_USER or "")
SMTP_TLS_CONTEXT = ssl.create_default_context()


def _parse_expected(spec):
    """One machine's entry from WATCHDOG_EXPECTED_INSTANCES: a whole number of instances,
    or a non-empty list of feed names. Anything else is a config mistake, so fail at startup."""
    if isinstance(spec, int) and not isinstance(spec, bool):
        return spec
    if isinstance(spec, list) and spec and all(isinstance(feed, str) for feed in spec):
        return list(spec)
    raise ValueError(
        "WATCHDOG_EXPECTED_INSTANCES entries must be a whole number or a non-empty "
        f"list of feed names, got {spec!r}"
    )


# What each machine should be running, chosen per machine:
#   a count      {"MAIN": 2}                              -- where process names do not identify the feed
#   feed names   {"BACKUP-SERVER": ["usa", "caribbean"]}  -- where the process name does identify the feed
# A machine not listed here is never judged. Bad JSON stops startup on purpose.
EXPECTED_INSTANCES: dict[str, int | list[str]] = {
    str(name): _parse_expected(spec)
    for name, spec in json.loads(os.environ.get("WATCHDOG_EXPECTED_INSTANCES") or "{}").items()
}
# Display names for the feed labels the edge senders report.
FEED_NAMES = {"usa": "USA", "caribbean": "Caribbean"}

# The four machines this project watches, fixed rather than derived from whatever happens to
# be in the database -- a machine that has never sent a heartbeat (SITE-B, before its go-live)
# must still show up on the dashboard as its own known, named state, not silently disappear.
KNOWN_SOURCE_IDS = ["SITE-A", "SITE-B", "MAIN", "BACKUP-SERVER"]
# Events a site's detector posts when its playout PROGRAM is not running at all (SITE-B's Insta shut down). They are ongoing markers like a
# pause: red the moment the first one arrives, refreshed every 30 s while it lasts, closed with `phase: "end"` when the player is back.
PLAYER_DOWN_WORDS = ("player_down", "player_not_running", "insta_not_running", "insta_closed", "insta_stopped")

INTERNAL_STATUS_EVENT_TYPES = {
    "endpoint_critical",
    "instance_missing",
    "instances_restored",
    "heartbeat_lost",
    "heartbeat_recovered",
    "alert_reset",
}

# The operator view is six playouts, not four machines. These fixed endpoint
# definitions are the contract that prevents a missing process from vanishing
# from the dashboard just because it disappeared from the latest heartbeat.
# "latched" marks the two machines whose own edge sender measures engine activity: a red
# alarm there is held until that sender confirms the player is advancing again.
PLAYOUT_ENDPOINTS = [
    {"endpoint_id": "SITE-A", "source_id": "SITE-A", "display_name": "SITE-A", "feed_label": "Output Decode", "kind": "source"},
    {"endpoint_id": "SITE-B", "source_id": "SITE-B", "display_name": "SITE-B", "feed_label": "Playlist Engine", "kind": "source"},
    {"endpoint_id": "MAIN-USA", "source_id": "MAIN", "display_name": "MAIN - USA", "feed_label": "USA", "kind": "feed", "feed": "usa", "latched": True},
    {"endpoint_id": "MAIN-Caribbean", "source_id": "MAIN", "display_name": "MAIN - Caribbean", "feed_label": "Caribbean", "kind": "feed", "feed": "caribbean", "latched": True},
    {"endpoint_id": "BACKUP-USA", "source_id": "BACKUP-SERVER", "display_name": "BACKUP - USA", "feed_label": "USA", "kind": "feed", "feed": "usa", "latched": True},
    {"endpoint_id": "BACKUP-Caribbean", "source_id": "BACKUP-SERVER", "display_name": "BACKUP - Caribbean", "feed_label": "Caribbean", "kind": "feed", "feed": "caribbean", "latched": True},
]

# One ladder for every "is it moving" signal: yellow after 45 s, red after 60 s.
WARNING_OVERDUE_SECONDS = int(os.environ.get("WATCHDOG_WARNING_OVERDUE_SECONDS", "45"))
CRITICAL_OVERDUE_SECONDS = int(os.environ.get("WATCHDOG_CRITICAL_OVERDUE_SECONDS", "60"))
# To release a held critical alarm the edge sender must report this many seconds of steady engine
# activity, from an engine that has itself been up at least the startup grace. Together they
# stop a freshly restarted process (which is busy loading) from clearing an alarm by itself.
CLEAR_CONFIRM_SECONDS = int(os.environ.get("WATCHDOG_CLEAR_CONFIRM_SECONDS", "15"))
ENGINE_STARTUP_GRACE_SECONDS = int(os.environ.get("WATCHDOG_ENGINE_STARTUP_GRACE_SECONDS", "30"))
# A detector "ongoing" marker that has been neither refreshed nor closed for this long no longer
# proves the fault is still happening. It is shown as unconfirmed (yellow) for the next
# ONGOING_UNCONFIRMED_SECONDS, then ignored, so a lost "end" event cannot leave a tile stuck.
ONGOING_FRESH_SECONDS = int(os.environ.get("WATCHDOG_ONGOING_FRESH_SECONDS", "120"))
# Dead air (black, frozen and silent together: the SITE-A detector's `off_air` with cause `black_freeze_silence`) looks exactly like a programme change.
# It is not CRITICAL until it has lasted this long, whatever limit the detector itself uses (its 5 s default raised a critical for a 7.4 s moment on
# 2026-10-15, which would have been a critical alarm and an e-mail). A lost input (any other cause) is critical at once.
DEAD_AIR_RED_SECONDS = int(os.environ.get("WATCHDOG_DEAD_AIR_RED_SECONDS", "20"))
# SITE-B is judged on its playlist timing alone (nothing decodes its output), so a player that is running but has nothing playing (stalled, paused, silent and
# off air) used to stay "Healthy" for hours. A source in IDLE_ALARM_SOURCES whose heartbeat says the player is running and names no playing item for this
# long is critical. The clock is `_idle_since`, written by Core into the stored heartbeat (a sender's own claim is never used). A fixed number, not a setting.
# A player that has decoded media in the last 15 seconds is playing something even when the playlist names nothing (a midnight FileBlock hour). SITE-B's detector says so with
# `media_active: true` (only the JSON true counts): that is no idle spell, so no idle alarm, and the 10 minutes start again from zero when the activity stops.
IDLE_ALARM_SECONDS = 600
IDLE_ALARM_SOURCES = ("SITE-B",)
# A playout process that is missing from its heartbeat (or whose launcher or engine is reported down) is not called critical until it has been gone this long. A player that an
# operator closes and opens again, or that restarts itself, is back within 10 to 25 seconds; each of those used to raise a critical alarm and an e-mail (seven on BACKUP-SERVER in one
# day). For this long it is shown as a warning ("Process Missing < 45s"), never silently, and nothing is mailed. Core keeps the clock (`_absent`, per feed, stored with the heartbeat);
# a sender's own copy is thrown away. A fixed number, for MAIN and BACKUP-SERVER (the sources that report their players one by one).
INSTANCE_DOWN_GRACE_SECONDS = 45
ONGOING_UNCONFIRMED_SECONDS = int(os.environ.get("WATCHDOG_ONGOING_UNCONFIRMED_SECONDS", "1800"))

# Shared secret every request must present in the X-Watchdog-Token header.
# Required: the Tailscale/LAN paths and the Cloudflare Tunnel fallback all use
# it, and without it anyone who can reach the port could post fake heartbeats
# and mask a real outage. Fail closed: no token means no startup.
SHARED_TOKEN = os.environ.get("WATCHDOG_SHARED_TOKEN", "")
if not SHARED_TOKEN:
    raise RuntimeError("WATCHDOG_SHARED_TOKEN is not set; refusing to start without authentication")
PREVIOUS_SHARED_TOKEN = os.environ.get("WATCHDOG_PREVIOUS_SHARED_TOKEN", "")
WRITE_TOKENS = tuple(dict.fromkeys(token for token in (SHARED_TOKEN, PREVIOUS_SHARED_TOKEN) if token))


def _token_matches(value: Optional[str]) -> bool:
    # compare_digest takes the same time whether the first or last character is wrong.
    # A plain != stops at the first mismatch, which can leak the secret one character at a time.
    if not isinstance(value, str) or not value:
        return False
    return any(hmac.compare_digest(value.encode("utf-8"), token.encode("utf-8")) for token in WRITE_TOKENS)


# Per-sender keys: each sender can hold its own key, valid only for its own name. The shared key above stays the admin key (reads,
# resets alarms) and, while WATCHDOG_SENDER_AUTH is `both` (the default), still posts for anyone so senders can be moved over one at a time. See app/guard.py.
#
# A standby can read the same keys from ONE FILE instead (WATCHDOG_SENDER_KEYS_FILE): a key rotation on Core then reaches it as a file change that a polling
# loop (keys_reload_loop) picks up without a restart, which would forget what a standby believes about Core. Core leaves the setting empty and works exactly as
# before. With the file set, sender keys in the environment are refused (two places for one secret is how a stale key survives), and a bad file stops the start.
SENDER_KEYS_FILE = os.environ.get("WATCHDOG_SENDER_KEYS_FILE", "").strip()
KEY_WATCHER: Optional[KeyFileWatcher] = None
try:
    if SENDER_KEYS_FILE:
        if any(name.startswith("WATCHDOG_SENDER_KEY_") and os.environ.get(name) for name in os.environ):
            raise KeyConfigError("WATCHDOG_SENDER_KEYS_FILE is set, so the sender keys must not also be set in the environment (WATCHDOG_SENDER_KEY_*)")
        KEY_WATCHER = KeyFileWatcher(SENDER_KEYS_FILE, KNOWN_SOURCE_IDS)
        _initial_sender_keys = KEY_WATCHER.load(datetime.now(timezone.utc))
    else:
        _initial_sender_keys = load_sender_keys(os.environ, KNOWN_SOURCE_IDS)
    KEYRING = Keyring(_initial_sender_keys, parse_auth_mode(os.environ.get("WATCHDOG_SENDER_AUTH")))
except KeyConfigError as error:
    raise RuntimeError(f"invalid sender key settings: {error}") from error
AUTH = AuthLog()
REFUSALS = RefusalLog()                      # when a post was refused for a wrong key: a standby uses it to tell "a studio's key no longer matches" from "a studio is silent"


def _identify(token):
    return KEYRING.identify(token, WRITE_TOKENS)


def require_token(x_watchdog_token: str = Header(default=None)) -> None:
    """Heartbeats and events: a sender's own key, or the shared key while the mode is `both`."""
    if not KEYRING.may_post(_identify(x_watchdog_token)):
        REFUSALS.note(datetime.now(timezone.utc))
        raise HTTPException(status_code=401, detail="missing or invalid X-Watchdog-Token")


def require_admin_token(x_watchdog_token: str = Header(default=None)) -> None:
    """Operator actions (resetting a held alarm): only the shared key. A sender's key can never do this, however it was obtained."""
    if not KEYRING.is_admin(_identify(x_watchdog_token)):
        raise HTTPException(status_code=401, detail="missing or invalid X-Watchdog-Token")


def _authorize_source(request, source_id) -> None:
    """Called once the body has named its source: a sender's key may only act as that sender. Refused before anything is stored."""
    try:
        identity = _identify(request.headers.get("x-watchdog-token"))
    except Exception:  # noqa: BLE001 -- a malformed request is the dependency's business; this only adds the binding check
        return
    name = _clip_text(str(source_id), 64)
    now = datetime.now(timezone.utc)
    if not KEYRING.may_act_as(identity, source_id):
        REFUSALS.note(now)
        count = AUTH.mismatch(identity.source, name, now)
        if count == 1 or count % 50 == 0:
            logger.warning("[auth] the key bound to %s was used to post as %s and was refused (%d time(s) so far); one of them is misconfigured or the key has been copied", identity.source, name, count)
        raise HTTPException(status_code=403, detail="this key is not valid for that source")
    if identity.kind != "none":
        AUTH.note(name, identity.kind, now)


def _sender_auth_status(now: datetime, detail: bool = False) -> dict:
    """Names only, never a key: the mode, who has an own key, whose changeover window is open, which kind of key each source last used, and refused attempts.
    `detail` (only for a caller holding the admin key) adds the key-set digest, which the sync tool compares between Core and the standby."""
    status = {"mode": KEYRING.mode, "keyed_sources": KEYRING.keyed_sources(), "windows_open": KEYRING.windows_open(),
              "used": AUTH.used(now), "mismatches": AUTH.mismatches(now)}
    if detail:
        status["keyset"] = KEYRING.digest()
    return status


def _is_admin_request(request) -> bool:
    """True when the request carries the admin (shared) key. The public dashboard carries only a view key, so it never gets the `detail` views."""
    try:
        return KEYRING.is_admin(_identify(request.headers.get("x-watchdog-token")))
    except Exception:  # noqa: BLE001 -- a malformed header is simply not an admin
        return False


# Two keys, because the dashboard is a PUBLIC page that must carry a key to open its live stream. The sender key (above) lets a caller POST heartbeats,
# events and alarm resets, so it must never be put in a page: anyone who opened the dashboard could read it and forge heartbeats (hiding a dead player)
# or raise false alarms. The page carries a VIEW key instead, which can only read (/status, /stream). It is derived from the sender key with HMAC, so
# there is nothing new to store, it is the same after every restart (open tabs keep working), and it cannot be turned back into the sender key.
VIEW_KEY_LABEL = b"clarix-watchdog-view-v1"


def _view_tokens(sender_keys) -> tuple:
    """One view key per sender key, in the same order (the current sender key first)."""
    return tuple(hmac.new(key.encode("utf-8"), VIEW_KEY_LABEL, hashlib.sha256).hexdigest() for key in sender_keys)


# While a sender-key rotation is open (the old key still in WATCHDOG_PREVIOUS_SHARED_TOKEN) every page that is already open holds the view key of the OLD
# sender key, so the listener accepts the view key of each key it still accepts for writing. A page loaded now is given the first (current) one.
VIEW_TOKENS = _view_tokens(WRITE_TOKENS)
VIEW_TOKEN = VIEW_TOKENS[0]


def _view_token_matches(value: Optional[str]) -> bool:
    """Reading accepts a view key (pages) or the sender key (senders, the deploy check, the weekly report). Writing accepts only the sender key."""
    if _token_matches(value):
        return True
    return isinstance(value, str) and any(hmac.compare_digest(value.encode("utf-8"), token.encode("utf-8")) for token in VIEW_TOKENS)


def require_view_token(x_watchdog_token: str = Header(default=None)) -> None:
    if not _view_token_matches(x_watchdog_token):
        raise HTTPException(status_code=401, detail="missing or invalid X-Watchdog-Token")


# ---- Plain http becomes https, and https tells the browser to stay on https ---------------------------------------------------------------
# Cloudflare's "Always Use HTTPS" is off for this zone, so http://watchdog.corp.example used to answer in the clear, page (and the access key
# embedded in it) included. The stored Cloudflare tokens cannot create a redirect rule, and the zone-wide switch would touch every other site on
# the zone, so the app does it for its own hostname. It trusts only what Cloudflare itself reports about the visitor (CF-Visitor, X-Forwarded-Proto):
# a LAN sender, the container health check and the tests carry neither and are left exactly as before. Only page visits (GET, HEAD) are redirected:
# a redirected POST is retried as a GET, so a sender posting to an http:// address would break. HSTS is the fast and safe half: once a browser has
# seen it, it goes straight to https and never makes the http round trip again for 180 days. No includeSubDomains and no preload: the zone's other
# hostnames are not ours to commit. To undo: serve "max-age=0" for one visit, and remove the middleware line below.
PUBLIC_HOST = os.environ.get("WATCHDOG_PUBLIC_HOST", "watchdog.corp.example")
# The standby sits behind the VPS's own reverse proxy (Caddy), not behind Cloudflare. WATCHDOG_TRUST_FORWARDED_FROM names the one peer (127.0.0.1) whose
# X-Forwarded-For and X-Forwarded-Proto are believed; empty (Core) means off and nothing below changes. While it is set Cloudflare's headers are not believed.
try:
    FORWARDED_TRUST = parse_forwarded_trust(os.environ.get("WATCHDOG_TRUST_FORWARDED_FROM"))
except KeyConfigError as error:
    raise RuntimeError(f"invalid forwarded-header trust setting: {error}") from error
HSTS_VALUE = "max-age=15552000"
SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    # The dashboard is public on purpose, but it should not be listed or cached by search engines. Crawlers that read headers
    # honour this on every answer; /robots.txt below asks the ones that look for a file.
    (b"x-robots-tag", b"noindex, nofollow, noarchive, nosnippet"),
    (b"permissions-policy", b"camera=(), geolocation=(), microphone=(), payment=(), usb=()"),
    (b"content-security-policy", b"default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' https://www.gstatic.com https://www.cloudflare.com; worker-src 'self'; manifest-src 'self'; font-src 'self'"),
)


def _with_security_headers(headers, include_hsts: bool = False) -> list[tuple[bytes, bytes]]:
    result = list(headers or [])
    present = {name.lower() for name, _value in result}
    additions = list(SECURITY_HEADERS)
    if include_hsts:
        additions.append((b"strict-transport-security", HSTS_VALUE.encode("latin-1")))
    result.extend((name, value) for name, value in additions if name not in present)
    return result


def visitor_scheme(headers: dict) -> str:
    """'http' or 'https' as Cloudflare reports the visitor's connection, or '' when the request did not come through Cloudflare."""
    try:
        reported = json.loads(headers.get("cf-visitor", ""))
        scheme = str(reported.get("scheme", "")).lower() if isinstance(reported, dict) else ""
        if scheme in ("http", "https"):
            return scheme
    except (ValueError, TypeError):
        pass
    forwarded = str(headers.get("x-forwarded-proto", "")).split(",")[0].strip().lower()
    return forwarded if forwarded in ("http", "https") else ""


def https_redirect_target(method: str, scheme: str, path: str, query: str) -> Optional[str]:
    """Where a plain-http page visit should go, or None. Always our own host (a constant, never the request's Host header: no open redirect)."""
    if scheme != "http" or method not in ("GET", "HEAD"):
        return None
    return "https://" + PUBLIC_HOST + "/" + path.lstrip("/") + ("?" + query if query else "")


class HttpsPolicy:
    """Pure ASGI middleware (not BaseHTTPMiddleware, so the live /stream is passed through untouched)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        scheme = visitor_scheme(headers)
        target = https_redirect_target(scope.get("method", ""), scheme, scope.get("path", "/"), scope.get("query_string", b"").decode("latin-1"))
        if target:
            headers = [(b"location", target.encode("latin-1")), (b"content-length", b"0"), (b"cache-control", b"no-store")]
            await send({"type": "http.response.start", "status": 301, "headers": _with_security_headers(headers)})
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_with_security(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = _with_security_headers(message.get("headers", []), include_hsts=scheme == "https")
            await send(message)

        await self.app(scope, receive, send_with_security)


ENABLE_DOCS = _env_bool("WATCHDOG_ENABLE_DOCS", False)
app = FastAPI(
    title="Clarix Broadcast Watchdog - Listener",
    version="0.4.0",
    docs_url="/docs" if ENABLE_DOCS else None,
    redoc_url="/redoc" if ENABLE_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_DOCS else None,
)
def _session_generation() -> str:
    """The value every session is signed under. It changes when the super user logs out (a new random value, stored) and when his password hash changes, so a stolen cookie ends
    with either; it is a hash, never the password or its hash."""
    return hashlib.sha256((CONTROL["session_gen"] + "|" + SUPER_PASSWORD_HASH).encode("utf-8")).hexdigest()[:32]


def _headers_are_super(headers: dict) -> bool:
    """True when the request carries the control key (header `x-watchdog-control`, for the PC tool) or a valid signed session cookie (for the browser). A node
    without a working control key has no super user."""
    if not _control_enabled():
        return False
    if control_access.key_matches(CONTROL_KEY, headers.get("x-watchdog-control")):
        return True
    now = datetime.now(timezone.utc)
    for part in str(headers.get("cookie", "")).split(";"):
        name, _, value = part.strip().partition("=")
        if name == CONTROL_COOKIE and control_access.verify_session(CONTROL_KEY, value, now, _session_generation()):
            return True
    return False


class ControlGate:
    """Pure ASGI middleware (like HttpsPolicy, so the live /stream is passed through untouched). While access is OFF it answers everything that is not on the short
    allow-list (control_access.gate_request) with the branded page (HTML) or a plain 503 (JSON), unless the caller is the super user. While access is ON it does nothing."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or _access_on():
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        decision = control_access.gate_request(scope.get("method", ""), scope.get("path", "/"), False, _headers_are_super(headers))
        if decision == control_access.ALLOW:
            await self.app(scope, receive, send)
            return
        if decision == control_access.PAGE:
            body, kind = control_access.unavailable_page().encode("utf-8"), b"text/html; charset=utf-8"
        else:
            body, kind = json.dumps(control_access.unavailable_data()).encode("utf-8"), b"application/json"
        await send({"type": "http.response.start", "status": 503,
                    "headers": [(b"content-type", kind), (b"content-length", str(len(body)).encode("ascii")), (b"cache-control", b"no-store"), (b"retry-after", b"3600")]})
        await send({"type": "http.response.body", "body": body})


class _Peer:
    def __init__(self, host):
        self.host = host


class _ScopeRequest:
    """Just enough of a request (its scope, lower-cased headers and client) for the helpers that look at one, built from an ASGI scope by a pure middleware that has no Request object."""

    def __init__(self, scope):
        self.scope = scope
        self.headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        client = scope.get("client")
        self.client = _Peer(client[0]) if client else None


class OfficePassIssuer:
    """Pure ASGI middleware (like HttpsPolicy, so the live /stream is passed through untouched). On the STANDBY, EVERY answer to a request that arrived on the staff door carries a fresh door
    pass, whatever the page, the status or whether anyone signed in: the door is where the PC showed its listed certificate, and an office PC that only ever uses the Staff page must still be
    recognised on the watchdog address (his report, 2026-11-10). Off the door, and on Core (which has no door), it does nothing; a pass cannot renew itself."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not IS_STANDBY:
            await self.app(scope, receive, send)
            return
        pass_headers = _office_pass_headers(_ScopeRequest(scope))
        if not pass_headers:
            await self.app(scope, receive, send)
            return
        cookie = pass_headers["Set-Cookie"].encode("latin-1")

        async def send_with_pass(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [(b"set-cookie", cookie)]
            await send(message)

        await self.app(scope, receive, send_with_pass)


app.add_middleware(ControlGate)          # added first, so the issuer and then HttpsPolicy (added next) wrap it and its answers get the pass and the security headers too
app.add_middleware(OfficePassIssuer)
app.add_middleware(HttpsPolicy)

# The weakest point left after the redirect: a site whose heartbeats go to an http:// address sends the access key in the clear (a redirected POST is
# not an option, see HttpsPolicy). The listener cannot read a sender's configuration, but Cloudflare reports how each request arrived, so it remembers
# which sources posted over plain http and shows them in /status (and the log) before anyone decides to refuse those posts.
PLAIN_HTTP_WINDOW_SECONDS = 900
PLAIN_HTTP_MAX_SOURCES = 64
def _reject_modes(setting: str) -> tuple[bool, bool]:
    """(refuse plain http that arrived through Cloudflare, refuse ALL plain http including the LAN path) for WATCHDOG_REJECT_PLAIN_HTTP_POSTS.
    `internet` is safe to switch on today: no sender posts to the public hostname over http. `true` / `all` also refuses MAIN and BACKUP-SERVER's
    http://192.168.7.183:8420 path, so use it only after those two have an encrypted way to reach the listener."""
    value = str(setting or "").strip().lower()
    if value == "internet":
        return True, False
    if value in ("1", "true", "yes", "on", "all"):
        return True, True
    return False, False


REJECT_PLAIN_HTTP_INTERNET, REJECT_PLAIN_HTTP_POSTS = _reject_modes(os.environ.get("WATCHDOG_REJECT_PLAIN_HTTP_POSTS", "false"))
PLAIN_HTTP_POSTS: dict = {}          # source id -> ISO time of its latest post that arrived over plain http
_plain_http_logged: dict = {}        # source id -> when it was last logged (once an hour is enough)


def _peer_of(request):
    return getattr(getattr(request, "client", None), "host", None)


def _behind_trusted_proxy(request) -> bool:
    """True when WATCHDOG_TRUST_FORWARDED_FROM is set and this request's TCP peer is the front proxy it names (never true on Core)."""
    return bool(FORWARDED_TRUST) and forwarded_peer_trusted(_peer_of(request), FORWARDED_TRUST)


def request_scheme(request) -> str:
    """Prefer the trusted proxy scheme, then use the ASGI scheme for direct sender traffic."""
    try:
        if FORWARDED_TRUST:
            # Behind the standby's own proxy: only that proxy's X-Forwarded-Proto counts; a client writing it (or CF-Visitor) itself is ignored.
            if _behind_trusted_proxy(request):
                forwarded = str(request.headers.get("x-forwarded-proto", "")).split(",")[0].strip().lower()
                if forwarded in ("http", "https"):
                    return forwarded
            scope_scheme = str(getattr(request, "scope", {}).get("scheme", "")).lower()
            return scope_scheme if scope_scheme in ("http", "https") else ""
        reported = visitor_scheme(request.headers)
        if reported:
            return reported
        scope_scheme = str(getattr(request, "scope", {}).get("scheme", "")).lower()
        return scope_scheme if scope_scheme in ("http", "https") else ""
    except Exception:  # noqa: BLE001 -- request bookkeeping must never break a health path
        return ""


def _plain_http_refused(request) -> bool:
    if REJECT_PLAIN_HTTP_POSTS:                                  # `all`: the LAN path too
        return request_scheme(request) == "http"
    if REJECT_PLAIN_HTTP_INTERNET:                               # `internet`: only what came in through Cloudflare over http
        return visitor_scheme(request.headers) == "http"
    return False


def _note_plain_http(request, source_id) -> None:
    try:
        if not source_id or request_scheme(request) != "http":
            return
        if _plain_http_refused(request):
            raise HTTPException(status_code=426, detail="plain HTTP POSTs are disabled; use HTTPS")
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 -- a heartbeat must never fail because of this bookkeeping
        return
    name = _clip_text(str(source_id), 64)
    now = datetime.now(timezone.utc)
    if name not in PLAIN_HTTP_POSTS and len(PLAIN_HTTP_POSTS) >= PLAIN_HTTP_MAX_SOURCES:
        PLAIN_HTTP_POSTS.pop(next(iter(PLAIN_HTTP_POSTS)))
    PLAIN_HTTP_POSTS[name] = now.isoformat()
    last = _plain_http_logged.get(name)
    if last is None or (now - last).total_seconds() > 3600:
        _plain_http_logged[name] = now
        logger.warning("[plain-http] %s posted over plain http: its access key crossed the internet unencrypted; its address should be https://", name)


def _plain_http_senders(now: datetime) -> dict:
    """Sources that posted over plain http in the last 15 minutes (source id -> when).
    Since 2ed1749 this includes LAN and Tailscale posts (plain http inside a private network or WireGuard), so it names every sender by design.
    The question "did a key cross the open internet unprotected?" is answered by `exposed_senders` below, not by this list."""
    return {name: when for name, when in PLAIN_HTTP_POSTS.items()
            if (_age_seconds(when, now) or 0.0) <= PLAIN_HTTP_WINDOW_SECONDS}


# Which network path each sender's posts really take (kind only, never an address) and whether its key was protected on it. `routes` in /status answers
# "which road does each sender use" and, during a fallback test, shows the moment a sender switches from Tailscale to the Cloudflare tunnel.
ROUTES = RouteLog()


def _note_route(request, source_id) -> None:
    try:
        if not source_id:
            return
        kind = route_kind(request.headers, PUBLIC_HOST, _behind_trusted_proxy(request))
        ROUTES.note(_clip_text(str(source_id), 64), kind, route_protection(kind, request_scheme(request)), datetime.now(timezone.utc))
    except Exception:  # noqa: BLE001 -- this bookkeeping must never fail a heartbeat
        return


# Sender-key rotation: while the old key is still accepted, say which sender is still using it, so the old key is retired only when every sender has
# been seen on the new one (and a name nobody recognises under "previous" is someone using the leaked key).
KEY_GENERATION_WINDOW_SECONDS = 900
KEY_GENERATION_MAX_SOURCES = 64
KEY_GENERATION: dict = {}            # source id -> ("current" | "previous", ISO time of its latest post)


def _key_generation(value: Optional[str]) -> Optional[str]:
    """Which accepted sender key a post carried: 'current', 'previous' (only while WATCHDOG_PREVIOUS_SHARED_TOKEN is set) or None."""
    if not isinstance(value, str) or not value:
        return None
    if hmac.compare_digest(value.encode("utf-8"), SHARED_TOKEN.encode("utf-8")):
        return "current"
    if PREVIOUS_SHARED_TOKEN and hmac.compare_digest(value.encode("utf-8"), PREVIOUS_SHARED_TOKEN.encode("utf-8")):
        return "previous"
    return None


def _note_key_generation(request, source_id) -> None:
    try:
        if not source_id:
            return
        generation = _key_generation(request.headers.get("x-watchdog-token"))
        if generation is None:
            return
        name = _clip_text(str(source_id), 64)
        if name not in KEY_GENERATION and len(KEY_GENERATION) >= KEY_GENERATION_MAX_SOURCES:
            KEY_GENERATION.pop(next(iter(KEY_GENERATION)))
        KEY_GENERATION[name] = (generation, datetime.now(timezone.utc).isoformat())
    except Exception:  # noqa: BLE001 -- this bookkeeping must never fail a heartbeat
        return


def _sender_keys(now: datetime) -> dict:
    """Sources seen in the last 15 minutes, by the sender key their latest post used."""
    seen: dict = {"previous": [], "current": []}
    for name, (generation, when) in KEY_GENERATION.items():
        if (_age_seconds(when, now) or 0.0) <= KEY_GENERATION_WINDOW_SECONDS:
            seen[generation].append(name)
    return {generation: sorted(names) for generation, names in seen.items()}

# sqlite3 is a blocking library, and every DB call here happens inside an
# `async def` -- without this lock, two coroutines (an incoming request and
# the background staleness_checker_loop) can both reach for the database in
# the same event-loop tick and collide, surfacing as "database is locked"
# even on a single-process, single-worker service. The lock fully serializes
# access; at this service's scale (a handful of sources, infrequent writes)
# that's correctly simple, not a bottleneck.
_db_lock = asyncio.Lock()
INGEST_WINDOW_SECONDS = 60
MAX_INGEST_REQUESTS_PER_MINUTE = _env_int("WATCHDOG_MAX_INGEST_REQUESTS_PER_MINUTE", 600, 60, 10000)
_ingest_lock = asyncio.Lock()
_ingest_timestamps = deque()


async def _ingest_request_allowed() -> bool:
    """Bound authenticated write work before JSON parsing and database access."""
    now = time.monotonic()
    cutoff = now - INGEST_WINDOW_SECONDS
    async with _ingest_lock:
        while _ingest_timestamps and _ingest_timestamps[0] <= cutoff:
            _ingest_timestamps.popleft()
        if len(_ingest_timestamps) >= MAX_INGEST_REQUESTS_PER_MINUTE:
            return False
        _ingest_timestamps.append(now)
        return True


@contextmanager
def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # better read/write concurrency than the default, belt-and-suspenders with the lock above
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS sources (
                source_id TEXT PRIMARY KEY,
                last_seen_utc TEXT NOT NULL,
                last_payload_json TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'healthy',
                state_changed_utc TEXT NOT NULL,
                expected_interval_seconds INTEGER NOT NULL DEFAULT 10,
                instance_alert TEXT
            )"""
        )
        # Upgrade path for tables created before these columns existed.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(sources)").fetchall()]
        if "expected_interval_seconds" not in cols:
            conn.execute("ALTER TABLE sources ADD COLUMN expected_interval_seconds INTEGER NOT NULL DEFAULT 10")
        if "instance_alert" not in cols:
            # The last judged instance state ('ok' or 'missing'). Stored, so a restart
            # neither repeats an alert nor forgets one.
            conn.execute("ALTER TABLE sources ADD COLUMN instance_alert TEXT")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                source_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'info',
                detail TEXT
            )"""
        )
        # The dashboard's counters ask for "events in the last 24 hours" and "the latest critical alarm" every few seconds for each viewer.
        # A standby's few remembered facts (when a probe of Core last succeeded). Core never reads or writes it.
        conn.execute("CREATE TABLE IF NOT EXISTS standby_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp_utc)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type_timestamp ON events(event_type, timestamp_utc)")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS endpoint_alerts (
                endpoint_id TEXT PRIMARY KEY,
                alert_level TEXT NOT NULL,
                root_cause TEXT,
                state_changed_utc TEXT NOT NULL,
                last_email_key TEXT
            )"""
        )
        # The staff section's log: one row per action, never changed or deleted (staff.py explains why). Both nodes keep every row.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS staff_log (
                id TEXT PRIMARY KEY,
                at_utc TEXT NOT NULL,
                node TEXT NOT NULL,
                kind TEXT NOT NULL,
                person TEXT NOT NULL,
                data TEXT NOT NULL
            )"""
        )
        site_c_feed.init_tables(conn)          # the Site C feed monitor's four small tables (the unpublished design notes)
        matrix.init_table(conn)                 # the AV matrix audit log (the unpublished design notes); NOT the events table, which the public dashboard shows


# Severities a site's own detector may report. "normal" is intentionally
# excluded -- a detector shouldn't bother posting its routine non-events.
KNOWN_SEVERITIES = {"info", "minor", "informational", "warning", "major", "critical"}


def _insert_event_locked(
    conn: sqlite3.Connection,
    timestamp_utc: str,
    source_id: str,
    event_type: str,
    detail: str = "",
    severity: str = "info",
) -> None:
    conn.execute(
        "INSERT INTO events (timestamp_utc, source_id, event_type, severity, detail) VALUES (?, ?, ?, ?, ?)",
        (timestamp_utc, source_id, event_type, severity, detail),
    )


async def record_event(source_id: str, event_type: str, detail: str = "", severity: str = "info") -> None:
    """Single ingestion point for anything worth logging/alerting on --
    heartbeat-loss, a site detector's own classified finding (SITE-B's
    tiers, SITE-A's freeze/black/silence), all through one place so the
    eventual dashboard has one table to read regardless of source or signal
    type. Acquires the DB lock itself -- callers that already hold it (the
    staleness checker) must release it first, since asyncio.Lock is not
    reentrant."""
    now = datetime.now(timezone.utc).isoformat()
    async with _db_lock:
        with db() as conn:
            _insert_event_locked(conn, now, source_id, event_type, detail, severity)
    logger.info("[record_event] source=%s type=%s severity=%s detail=%s", source_id, event_type, severity, detail)


# Station brand colors. Green = good news, yellow = worth knowing but not
# urgent, orange = needs attention. Three colors, not a severity-per-shade
# system -- deliberately simple.
_GREEN, _YELLOW, _ORANGE, _RED = "#52c41a", "#d4b106", "#fa8c16", "#f5222d"
_KIND_COLORS = {
    "heartbeat recovered": _GREEN,
    "instances restored": _GREEN,
    "heartbeat lost": _ORANGE,
    "instance missing": _ORANGE,
    "critical": _RED, "major": _ORANGE, "warning": _ORANGE,
    "info": _YELLOW, "informational": _YELLOW, "minor": _YELLOW,
}


# Known instance-list field names across the different edge senders (main's
# Insta Playout pairs use feed/launcher_pid/engine_pid; backup-server's AdMax
# uses feed/process_name/pid_value) -- rendered as one readable line each,
# never as a raw nested JSON blob.
def _humanize_instance(inst: dict) -> str:
    feed = inst.get("feed")
    label = feed.upper() if feed and feed != "unknown" else inst.get("slot", "instance")
    if "process_name" in inst:
        bits = [inst.get("process_name", "?")]
        if inst.get("pid_value"):
            bits.append(f"PID {inst['pid_value']}")
    else:
        bits = []
        if inst.get("launcher_present") is not None:
            bits.append("launcher " + ("up" if inst["launcher_present"] else "DOWN"))
        if inst.get("engine_present") is not None:
            bits.append("engine " + ("up" if inst["engine_present"] else "DOWN"))
    return f"{label}: " + ", ".join(bits) if bits else label


def _humanize_json(raw) -> list[tuple[str, str]]:
    """Turns a JSON payload/detail blob into readable (label, value) rows
    instead of dumping it verbatim -- that raw-JSON dump was exactly the
    'gibberish' complaint. Falls back to showing the raw string only if it
    genuinely isn't JSON."""
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return [("Detail", raw)]
    elif isinstance(raw, dict):
        data = dict(raw)  # copy: popping "instances" below must not mutate the caller's payload
    else:
        return [("Detail", str(raw))]
    if not isinstance(data, dict):
        return [("Detail", str(data))]  # valid JSON, but a list or number rather than an object

    rows: list[tuple[str, str]] = []
    instances = data.pop("instances", None)
    # Already shown as the card title or alert time, or internal cadence noise.
    skip_keys = {"hostname", "timestamp", "expected_interval_seconds"}
    for key, value in data.items():
        if key in skip_keys or str(key).startswith("_"):   # "_pause" and the like are the listener's own bookkeeping, not the sender's report
            continue
        label = key.replace("_", " ").capitalize()
        rows.append((label, str(value)))
    if instances:
        for inst in instances:
            rows.append(("Instance", _humanize_instance(inst) if isinstance(inst, dict) else str(inst)))
    return rows


LOGO_CID = "clarix-watchdog-logo"


def _safe_header(value, limit: int = 200) -> str:
    return " ".join(str(value).replace("\r", " ").replace("\n", " ").split())[:limit]


def build_alert_message(subject: str, sender: str, recipient: str, plain: str, html: str) -> MIMEMultipart:
    """The alert as a multipart/related message: text and HTML alternatives, plus the Bulldog logo
    attached inline (referenced by the HTML as cid:clarix-watchdog-logo). Inline attachment rather
    than a web link, because mail apps often block remote images and this works offline too.
    The small round picture beside an email in an inbox is NOT part of the message; it is the
    sending account's own profile photo, which only that account can change."""
    msg = MIMEMultipart("related")
    msg["Subject"] = _safe_header(subject)
    msg["From"] = _safe_header(sender)
    msg["To"] = _safe_header(recipient)
    alternatives = MIMEMultipart("alternative")
    alternatives.attach(MIMEText(plain, "plain"))
    alternatives.attach(MIMEText(html, "html"))
    msg.attach(alternatives)
    logo = MIMEImage(_ICONS["icon-192.png"], _subtype="png")
    logo.add_header("Content-ID", f"<{LOGO_CID}>")
    logo.add_header("Content-Disposition", "inline", filename="clarix-watchdog.png")
    msg.attach(logo)
    return msg


def _render_alert_html(source_id: str, kind: str, summary: str, rows: list[tuple[str, str]]) -> str:
    """Dark-background HTML, Station colors -- plain-text clients fall back
    to the MIMEText alt part below. `rows` is already-humanized (label,
    value) pairs; nothing here ever renders a raw JSON string."""
    color = _KIND_COLORS.get(kind.lower(), _YELLOW)
    row_html = "".join(
        f'<tr><td style="padding:7px 14px;color:#8a8a8a;font-size:13px;vertical-align:top;white-space:nowrap;">{html.escape(str(label), quote=True)}</td>'
        f'<td style="padding:7px 14px;color:#e8e8e8;font-size:13px;">{html.escape(str(value), quote=True)}</td></tr>'
        for label, value in rows
    )
    safe_kind = html.escape(str(kind), quote=True)
    safe_source_id = html.escape(str(source_id), quote=True)
    safe_summary = html.escape(str(summary), quote=True)
    return f"""\
<html><body style="margin:0;padding:24px;background:#0f0f0f;font-family:-apple-system,Segoe UI,Arial,sans-serif;">
  <div style="max-width:560px;margin:0 auto;background:#1a1a1a;border-radius:10px;overflow:hidden;border:1px solid #2a2a2a;">
    <table role="presentation" style="width:100%;border-collapse:collapse;background:#111827;"><tr>
      <td style="padding:14px 0 14px 22px;width:52px;"><img src="cid:{LOGO_CID}" width="44" height="44" alt="Clarix Watchdog" style="display:block;border-radius:10px;border:0;"></td>
      <td style="padding:14px 22px 14px 12px;color:#f5f5f5;font-size:16px;font-weight:700;letter-spacing:0.01em;">Clarix Watchdog<br><span style="color:#8a94a6;font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;">Station broadcast monitoring</span></td>
    </tr></table>
    <div style="padding:18px 22px 14px;">
      <span style="display:inline-block;background:{color};color:#0f0f0f;font-size:11px;font-weight:700;letter-spacing:0.04em;text-transform:uppercase;padding:3px 10px;border-radius:12px;">{safe_kind}</span>
      <div style="color:#f5f5f5;font-size:20px;font-weight:700;margin-top:10px;">{safe_source_id}</div>
    </div>
    <div style="height:1px;background:linear-gradient(90deg,{_GREEN},{_YELLOW},{_ORANGE});"></div>
    <div style="padding:16px 22px 4px;color:#d8d8d8;font-size:14px;line-height:1.5;">{safe_summary}</div>
    <table style="width:100%;border-collapse:collapse;margin-top:8px;">{row_html}</table>
    <div style="padding:14px 22px;color:#5a5a5a;font-size:11px;border-top:1px solid #2a2a2a;margin-top:8px;">
      Clarix Broadcast Watchdog &middot; Station &middot; {_stamp_text(datetime.now(timezone.utc))}
    </div>
  </div>
</body></html>"""


def send_alert_email(source_id: str, kind: str, summary: str, rows: list[tuple[str, str]], subject_extra: str = "", force: bool = False) -> None:
    """kind: 'heartbeat lost' / 'heartbeat recovered' / a severity like
    'warning' -- drives the accent color. summary: one plain sentence
    stating the actual issue. rows: (label, value) pairs, e.g.
    [("Reason", "no heartbeat for 95s"), ("Last seen", "...")].
    source_id is the name shown in the subject and heading (for a playout alarm, the playout's
    own name such as "MAIN - USA", not just its machine); subject_extra is appended to the
    subject, e.g. the root cause."""
    subject = _safe_header(f"[Watchdog] {'[STANDBY] ' if IS_STANDBY else ''}{source_id}: {kind}" + (f" - {subject_extra}" if subject_extra else ""))
    if not force and not _delivery_allowed():
        SUPPRESSED["email"] += 1
        logger.info("[main.py::send_alert_email] decision | action=suppressed role=standby core=UP subject=%s", subject[:80])
        return
    recipient = _alert_recipient()
    if not recipient:
        logger.info("[main.py::send_alert_email] decision | action=silent access=off reason=no_super_user_address subject=%s", subject[:80])
        return
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and recipient):
        logger.warning("[send_alert_email] SMTP not configured -- would have sent: %s | %s", subject, summary)
        return
    try:
        plain = summary + "\n\n" + "\n".join(f"{label}: {value}" for label, value in rows)
        msg = build_alert_message(subject, ALERT_EMAIL_FROM, recipient, plain, _render_alert_html(source_id, kind, summary, rows))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.starttls(context=SMTP_TLS_CONTEXT)
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(ALERT_EMAIL_FROM, [recipient], msg.as_string())
        logger.info("[send_alert_email] sent: %s", subject)
    except Exception as exc:  # noqa: BLE001 -- a failed notification must never crash the checker loop
        logger.error("[send_alert_email] FAILED to send (%r): %s", exc, subject)


# Policy (the owner, 2026-10-11): email only when something is CRITICAL. The green "recovered" and "restored" notices are still recorded
# as events (the dashboard shows them) but are not mailed unless this is switched on.
EMAIL_RECOVERY_NOTICES = os.environ.get("WATCHDOG_EMAIL_RECOVERY_NOTICES", "false").strip().lower() in ("1", "true", "yes", "on")


def _send_recovery_notice(source_id: str, kind: str, summary: str, rows: list[tuple[str, str]]) -> None:
    """The "all clear" emails. Off by default: a critical alarm is mailed once when it turns critical, and nothing else is."""
    if not EMAIL_RECOVERY_NOTICES:
        logger.info("[recovery-notice] not mailed (red-only email policy): %s %s", source_id, kind)
        return
    send_alert_email(source_id, kind, summary, rows)


# ---- Redundancy: who tells you when CORE ITSELF is the problem ----------------------------------------------------------------
# Everything funnels through this server, so if it is down, or cannot reach the internet, or cannot log in to the email account, no
# alarm reaches anyone. Two opt-in channels that do not depend on the email path:
#   WATCHDOG_PUSH_URL    an ntfy-style topic URL: each CRITICAL alarm is also pushed to the phone (urgent priority).
#   WATCHDOG_DEADMAN_URL a "dead man's switch" check-in URL (healthchecks.io style): Core pings it every minute while it is healthy and
#                        STOPS when the database or the email login stops working; the outside service alerts you when the pings stop.
def _is_web_url(value) -> bool:
    """http or https with a host and nothing that could smuggle another line or space into a request. urllib would also open file: and ftp: addresses."""
    if not isinstance(value, str) or not value or any(ch.isspace() or ord(ch) < 32 for ch in value):
        return False
    parts = urllib.parse.urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def _checked_web_url(setting: str, value) -> str:
    """The setting's value if it is a usable http(s) address, else "" (the channel stays off) with a log line that names the setting, never the value."""
    text = str(value or "").strip()
    if not text:
        return ""
    if not _is_web_url(text):
        logger.error("[config] %s is not an http(s) address with a host, so it is ignored (that channel stays off)", setting)
        return ""
    return text


PUSH_URL = _checked_web_url("WATCHDOG_PUSH_URL", os.environ.get("WATCHDOG_PUSH_URL", ""))
DEADMAN_URL = _checked_web_url("WATCHDOG_DEADMAN_URL", os.environ.get("WATCHDOG_DEADMAN_URL", ""))
DEADMAN_INTERVAL_SECONDS = int(os.environ.get("WATCHDOG_DEADMAN_INTERVAL_SECONDS", "60"))
SMTP_CHECK_EVERY_SECONDS = 1800

# ---- Standby role (spec: the unpublished design notes) ------------------------------------------------------------------------------------------------------
# WATCHDOG_ROLE=standby makes this listener a quiet second copy on another network: it receives the studios' copies of every post, runs the same checks,
# and DELIVERS nothing while Core is believed up. It probes Core's /health and listens to the studios' `primary_ok` flag; only when both say Core is
# gone (app/standby.py) does it send one takeover message and start delivering. Empty/unset (Core) = `primary`: nothing below changes anything.
try:
    ROLE = parse_role(os.environ.get("WATCHDOG_ROLE"))
    IS_STANDBY = ROLE == "standby"
    # Core can watch the VPS too (WATCHDOG_PEER_URL, the VPS's /health): that is what lets the VPS be the preferred node with Core as the automatic fallback.
    PRIMARY_URL = parse_probe_url(os.environ.get("WATCHDOG_PRIMARY_URL") or os.environ.get("WATCHDOG_PEER_URL") or ("https://watchdog.corp.example/health" if IS_STANDBY else ""))
    WATCH_CONFIG = WatchConfig(
        down_after_failures=_env_int("WATCHDOG_STANDBY_FAILURES_TO_DOWN", 3, 1, 20),
        veto_true_seconds=_env_int("WATCHDOG_STANDBY_VETO_TRUE_SECONDS", 90, 10, 3600),
        up_after_successes=_env_int("WATCHDOG_STANDBY_SUCCESSES_TO_UP", 2, 1, 20),
        veto_false_seconds=_env_int("WATCHDOG_STANDBY_VETO_FALSE_SECONDS", 60, 10, 3600),
        min_down_seconds=_env_int("WATCHDOG_STANDBY_MIN_DOWN_SECONDS", 120, 0, 3600),
        veto_max_seconds=_env_int("WATCHDOG_STANDBY_VETO_MAX_SECONDS", 600, 60, 86400),
    )
except ValueError as error:
    raise RuntimeError(f"invalid standby setting: {error}") from error
if IS_STANDBY and KEYRING.mode != "per-sender":
    # The studios' `primary_ok` can keep a standby quiet. In `both` mode the shared key could post it in any studio's name, so a standby accepts posts only
    # from keys that belong to one studio each (per-sender): a copied key can then only speak for its own studio.
    raise RuntimeError("a standby must run with WATCHDOG_SENDER_AUTH=per-sender (and a key per studio), so the shared key cannot vouch for Core in any studio's name")
PROBE_INTERVAL_SECONDS = _env_int("WATCHDOG_PRIMARY_PROBE_SECONDS", 30, 5, 600)
PROBE_TIMEOUT_SECONDS = _env_int("WATCHDOG_PRIMARY_PROBE_TIMEOUT_SECONDS", 8, 1, 60)
WATCH = CoreWatch(WATCH_CONFIG, datetime.now(timezone.utc))
SUPPRESSED = {"email": 0, "push": 0}          # deliveries a standby held back while Core was believed up (counts only)

# ---- Control phase (spec: the unpublished design notes) --------------------------------------------------------------------------------------------------
# Each node (Core and the VPS) keeps its own control state in its own database: access ON/OFF and the PREFERRED node. The preferred node alerts; the other one
# watches it (PRIMARY_URL; on Core that is WATCHDOG_PEER_URL) and takes over by itself when it is down. Without a valid WATCHDOG_CONTROL_KEY the whole phase is
# off and the listener behaves exactly as it did before.
NODE = control.VPS if IS_STANDBY else control.CORE
CONTROL_KEY = os.environ.get("WATCHDOG_CONTROL_KEY", "").strip()
_control_key_problem = control_access.control_key_problem(CONTROL_KEY, (SHARED_TOKEN, PREVIOUS_SHARED_TOKEN, *VIEW_TOKENS))
if _control_key_problem:
    logger.error("[config] WATCHDOG_CONTROL_KEY is ignored (%s), so the control phase stays off", _control_key_problem)
    CONTROL_KEY = ""
SUPER_EMAIL_TO = os.environ.get("WATCHDOG_SUPER_EMAIL_TO", "").strip()               # the super user's own address: gets alerts while access is OFF
SUPER_PUSH_URL = _checked_web_url("WATCHDOG_SUPER_PUSH_URL", os.environ.get("WATCHDOG_SUPER_PUSH_URL", ""))      # his own private phone-push topic
CONTROL_NOTICE_TO = os.environ.get("WATCHDOG_CONTROL_NOTICE_TO", "").strip()         # the ONE mailbox told "offline" and "back" (support@client.example)
CONTROL_NOTICE_SECONDS = _env_int("WATCHDOG_CONTROL_NOTICE_SECONDS", 15, 1, 3600)
CONTROL_SESSION_SECONDS = 3600
CONTROL_COOKIE = "watchdog_control"
CONTROL = {"state": control.ControlState(), "problems": (), "session_gen": ""}      # what this node remembers: loaded at start, stored before every change takes effect
PEER = {"preferred": None}                                       # who the other node says is preferred (from its /health); None = not known
CONTROL_STORE_KEYS = tuple(control.to_store(control.ControlState()))
_control_rate = VisitorRate(30)                                  # control requests a minute for each visitor: slows any guessing of the key
_notice_warned = {"done": False}
SUPER_USER = os.environ.get("WATCHDOG_SUPER_USER", "").strip()                       # the super user's sign-in name (his e-mail address)
SUPER_PASSWORD_HASH = os.environ.get("WATCHDOG_SUPER_PASSWORD_HASH", "").strip()     # a salted scrypt hash (control_login.py); the password itself is stored nowhere
_login_throttle = control_login.LoginThrottle()                                      # five wrong tries lock an address for 15 minutes; 30 from anywhere lock everyone
_login_slots = asyncio.Semaphore(2)    # at most two password checks at once (16 MiB each): a burst of guesses must not be able to use up a 256 MB standby
PANEL_ACTIONS = ("off", "on", "use_core", "use_vps")

# ---- AV matrix switching (the unpublished design notes) ---------------------------------------------------------------------------------------------------------------
# Only Core (the one node on the studio network) has a matrix: WATCHDOG_MATRIX_HOST is a literal private IPv4 address. Everywhere else the switcher is None and the matrix's own
# endpoints do not exist (404). The standby never talks to the matrix: it relays a press from the staff door to Core over the same internal-key channel the ON/OFF buttons use.
MATRIX_SETTINGS, _MATRIX_PROBLEMS = matrix.settings_from_environ(os.environ)
for _problem in _MATRIX_PROBLEMS:
    logger.error("[main.py::matrix_settings] error | problem=%s", _problem)
MATRIX_SWITCHER = matrix.Switcher(matrix.UdpLink(MATRIX_SETTINGS.host, MATRIX_SETTINGS.port)) if MATRIX_SETTINGS else None
_matrix_rate = VisitorRate(60)       # matrix requests a minute for each visitor: its own allowance, so the card's polling can never use up the 30 a minute the panel's buttons have
MATRIX_SWITCH_TIMEOUT_SECONDS = 25   # a switch can take about twelve seconds in the worst case (the unpublished design notes); the relay must wait longer than that
MATRIX_MAIL_WAIT_SECONDS = 3         # the failure mail is waited for at most this long, so a slow mail server can never hold back the answer to a failed press (the mail still goes out)
# The ways a press is said to have arrived. A key holder (the standby relaying) may CLAIM these four; anything else is recorded as control-key. "core-site" is not claimable: only Core
# itself, having seen a super session on its own address, writes it.
MATRIX_VIA = ("staff-door", "standby-site", "office-pc", "office-pass", "control-key")
# The office PC's DOOR PASS (his choice, 2026-11-10; the unpublished design notes section 12): the standby hands a signed 3-day pass to a browser on EVERY answer it gives on the staff door (so the PC
# showed a listed certificate), whatever the page and whether or not anyone signed in; the pass is shared with the watchdog address through the parent domain of the two addresses, and both
# nodes accept it as an office PC for the matrix and for nothing else. Every answer at the door renews it, so a PC that uses the door never loses it. WATCHDOG_OFFICE_PASS=off stops it
# being issued or accepted.
OFFICE_PASS_COOKIE = "watchdog_office"
OFFICE_PASS_SECONDS = 72 * 3600
OFFICE_PASS_PATH = "/control/panel/matrix"            # the browser sends it to this path and no other, so no other page or app on the shared domain ever sees it
OFFICE_PASS_ON = os.environ.get("WATCHDOG_OFFICE_PASS", "on").strip().lower() != "off"

# ---- Staff: accounts, shifts, failures (the rules are in staff.py; this file stores the rows and answers the requests) --------------------------------------------
STAFF_COOKIE = "watchdog_staff"
STAFF_PAGE_LIMIT = 40              # rows in one page when the two nodes copy rows to each other (a reply is read up to 64 KB)
STAFF_INGEST_MAX = 60              # rows accepted in one post from the other node
STAFF_SYNC_SECONDS = 60
STAFF_SYNC_MAX_PAGES = 50
STAFF_PANEL_ACTIONS = ("add_person", "reset_password", "disable_person", "delete_person", "start_shift", "finish_shift", "assign", "unassign", "done")

# A staff person's wrong tries are counted against the NAME, not the address (his report of 2026-11-02: the two office PCs share one address, so five slips by one person used to lock every
# person out for fifteen minutes, the right password included). A name locks for two minutes after five wrong tries in ten; only a very busy address (many names, forty wrong tries) is stopped
# as a whole, also for two minutes. The door is reachable only from the office PCs' certificates, so brute force is not the risk here: being locked out of a shift is.
STAFF_TRIES, STAFF_ADDRESS_TRIES = 5, 40
STAFF_WINDOW_SECONDS, STAFF_LOCK_SECONDS = 600.0, 120.0
NO_GLOBAL_LOCK = 10 ** 9                                             # the throttle's "everyone at once" lock is not wanted for staff

# Where a staff person signs in from the app: the office PCs' staff address. The operator may set another one (WATCHDOG_STAFF_URL); only a plain https address is ever used.
STAFF_URL_DEFAULT = "https://staff.corp.example/staff"
_STAFF_URL_OK = re.compile(r"https://[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~/-]*)?")


def _staff_url_from(environ) -> str:
    text = str(environ.get("WATCHDOG_STAFF_URL", "")).strip()
    return text if len(text) <= 200 and _STAFF_URL_OK.fullmatch(text) else STAFF_URL_DEFAULT


STAFF_URL = _staff_url_from(os.environ)

# Where "Back to the dashboard" on the Staff page (and its 120-second return) goes: the public dashboard on Core (his choice, 2026-11-08), which hears all six playouts itself. The operator
# may set another one (WATCHDOG_DASHBOARD_URL); only a plain https address is ever used, the same rule as the staff address above.
DASHBOARD_URL_DEFAULT = "https://watchdog.corp.example/"


def _dashboard_url_from(environ) -> str:
    text = str(environ.get("WATCHDOG_DASHBOARD_URL", "")).strip()
    return text if len(text) <= 200 and _STAFF_URL_OK.fullmatch(text) else DASHBOARD_URL_DEFAULT


DASHBOARD_URL = _dashboard_url_from(os.environ)


def _door_port_from(environ) -> int:
    """The port of the STAFF DOOR as the launcher (serve.py) reports it, or 0 for none. Only the launcher sets WATCHDOG_STAFF_DOOR_BOUND, and only after the second port is
    really open; the operator's own request for a door is deliberately not read here. So a listener that was started some other way, such as the standby on the VPS,
    has no door and takes no staff sign-in at all."""
    text = str(environ.get("WATCHDOG_STAFF_DOOR_BOUND", "")).strip()
    if not (text.isascii() and text.isdigit()):
        return 0
    return int(text) if 1024 <= int(text) <= 65535 else 0


STAFF_DOOR_PORT = _door_port_from(os.environ)


def _on_staff_door(request) -> bool:
    """True only when the request arrived on the staff door's own socket. uvicorn fills scope["server"] from the socket that accepted the connection, never from anything in the request,
    so no header a client writes (Host, X-Forwarded-Port, ...) can change it. The number must be a real int equal to the door's."""
    server = request.scope.get("server")
    return bool(STAFF_DOOR_PORT) and isinstance(server, (tuple, list)) and len(server) == 2 and type(server[1]) is int and server[1] == STAFF_DOOR_PORT


def _new_staff_person_throttle():
    return control_login.LoginThrottle(per_visitor=STAFF_TRIES, global_limit=NO_GLOBAL_LOCK, window_seconds=STAFF_WINDOW_SECONDS, lock_seconds=STAFF_LOCK_SECONDS)


def _new_staff_address_throttle():
    return control_login.LoginThrottle(per_visitor=STAFF_ADDRESS_TRIES, global_limit=NO_GLOBAL_LOCK, window_seconds=STAFF_WINDOW_SECONDS, lock_seconds=STAFF_LOCK_SECONDS)


_staff_throttle = _new_staff_person_throttle()          # keyed by the staff NAME; staff sign-ins count against this, never against his: guessing staff names must not be able to lock the super user out
_staff_address_throttle = _new_staff_address_throttle()  # keyed by the address, with a much higher bar: it only stops one address trying very many names
_staff_rate = VisitorRate(600)                       # staff page requests a minute for each address (a studio's staff share one address and the page refreshes every few seconds)
_staff_cache = {"state": None}                       # the picture replayed from every row; replaced whenever a row is added, so it is always current
_STAFF_DUMMY = {"hash": ""}                          # a hash nobody knows, checked when the name is unknown so an unknown name and a wrong password take the same time

# ---- "Standby alive": a standby that judges itself every minute (see app/standby.py assess_health) ------------------------------------------------
# WATCHDOG_STANDBY_ALIVE_URL is the ping address of an outside check (Healthchecks.io "Standby alive", period 2 min, grace 3 min). It is pinged only while the
# standby is READY; on a problem the fail address is pinged with the reasons; if the standby or its machine is gone the pings stop and the same service emails.
# Empty = the self-check still runs and shows in /status, but nothing is pinged. Core has its own check ("Core alive"); a standby never pings that one.
try:
    STANDBY_ALIVE_URL = _checked_web_url("WATCHDOG_STANDBY_ALIVE_URL", os.environ.get("WATCHDOG_STANDBY_ALIVE_URL", "")) if IS_STANDBY else ""
    ALIVE_SECONDS = _env_int("WATCHDOG_STANDBY_ALIVE_SECONDS", 60, 5, 3600)
    HEALTH_CONFIG = HealthConfig(
        expect_copies=parse_expect_copies(os.environ.get("WATCHDOG_STANDBY_EXPECT_COPIES")),
        copy_max_age_seconds=_env_int("WATCHDOG_STANDBY_COPY_MAX_AGE_SECONDS", 300, 30, 86400),
        probe_max_age_seconds=max(180, 6 * PROBE_INTERVAL_SECONDS),
        min_free_mb=_env_int("WATCHDOG_STANDBY_MIN_FREE_MB", 200, 0, 10_000_000),
        startup_grace_seconds=_env_int("WATCHDOG_STANDBY_ALIVE_GRACE_SECONDS", 120, 0, 3600),
    )
except ValueError as error:
    raise RuntimeError(f"invalid standby setting: {error}") from error
if IS_STANDBY and HEALTH_CONFIG.expect_copies:
    _unknown_copies = [name for name in HEALTH_CONFIG.expect_copies if name not in KNOWN_SOURCE_IDS]
    if _unknown_copies:
        raise RuntimeError(f"invalid standby setting: WATCHDOG_STANDBY_EXPECT_COPIES names a studio this listener does not know: {_unknown_copies[0][:30]}")
PROBE_STATE = {"last_at": None, "proof_saved_at": None}   # when the Core probe loop last completed a probe (the self-check requires it to be recent); when the proof was last stored
PROBE_USER_AGENT = "ClarixWatchdog-Standby/1.0 (+https://standby.corp.example)"
PROOF_KEY = "primary_last_ok"                 # in the standby_state table: when a probe of Core last SUCCEEDED, so a restart does not lose the proof that the probe works
PROOF_SAVE_EVERY_SECONDS = 300
HEALTH_STATE = {"ok": None, "problems": [], "checked_at": None, "refused_recently": 0}   # the latest self-check, for /status
KEYS_POLL_SECONDS = _env_int("WATCHDOG_SENDER_KEYS_POLL_SECONDS", 5, 1, 300)


def send_push_notice(title: str, message: str, force: bool = False) -> None:
    """Second channel for CRITICAL alarms. Off unless WATCHDOG_PUSH_URL is set. Never raises: a failed push must not stop the email.
    A standby delivers only while it has taken over (`force` is for the stand-down notice, sent as the gate closes) and tags the title."""
    if not (PUSH_URL or SUPER_PUSH_URL):
        return
    if not force and not _delivery_allowed():
        SUPPRESSED["push"] += 1
        logger.info("[main.py::send_push_notice] decision | action=suppressed role=standby core=UP title=%s", title[:60])
        return
    target = _push_target()
    if not target:
        logger.info("[main.py::send_push_notice] decision | action=silent access=off reason=no_super_user_topic title=%s", title[:60])
        return
    if IS_STANDBY:
        title = "[STANDBY] " + title
    try:
        request = urllib.request.Request(
            target, data=message.encode("utf-8"), method="POST",
            headers={"Title": title.encode("ascii", "replace").decode("ascii"), "Priority": "urgent", "Tags": "rotating_light"},
        )
        with urllib.request.urlopen(request, timeout=10):  # nosec B310 -- scheme checked: only an http(s) address with a host is ever stored in PUSH_URL
            pass
        logger.info("[push] sent: %s", title)
    except Exception as exc:  # noqa: BLE001
        logger.error("[push] FAILED (%r): %s", exc, title)


def check_smtp_login() -> Optional[bool]:
    """True if the email account accepts the login right now, False if it does not (revoked password, blocked account), None when email is
    not configured. Logs in and out only: nothing is sent."""
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        return None
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls(context=SMTP_TLS_CONTEXT)
            server.login(SMTP_USER, SMTP_PASS)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("[smtp-check] login failed (%r)", exc)
        return False


def ping_deadman(url: str) -> bool:
    if not _is_web_url(url):
        return False
    try:
        with urllib.request.urlopen(url, timeout=10) as reply:  # nosec B310 -- scheme checked just above: http(s) with a host only
            return 200 <= reply.status < 300
    except Exception as exc:  # noqa: BLE001
        logger.warning("[deadman] ping failed (%r)", exc)
        return False


def deadman_should_ping(smtp_ok: Optional[bool], db_ok: bool) -> bool:
    """Healthy means the database answers AND (if email is configured) the email login works. When it is not, stop pinging on purpose:
    the outside service then tells you that Core cannot do its job."""
    return db_ok and smtp_ok is not False


async def deadman_loop() -> None:
    smtp_ok: Optional[bool] = None
    last_smtp_check = None
    while True:
        try:
            if last_smtp_check is None or time.monotonic() - last_smtp_check >= SMTP_CHECK_EVERY_SECONDS:
                smtp_ok = await asyncio.to_thread(check_smtp_login)
                last_smtp_check = time.monotonic()
            db_ok = True
            try:
                async with _db_lock:
                    with db() as conn:
                        conn.execute("SELECT 1").fetchone()
            except Exception as exc:  # noqa: BLE001
                db_ok = False
                logger.error("[deadman] database did not answer (%r)", exc)
            if deadman_should_ping(smtp_ok, db_ok):
                await asyncio.to_thread(ping_deadman, DEADMAN_URL)
        except Exception as exc:  # noqa: BLE001 -- this loop must never die
            logger.error("[deadman_loop] error (continuing): %r", exc)
        await asyncio.sleep(DEADMAN_INTERVAL_SECONDS)


def _control_enabled() -> bool:
    """The control phase works only with a valid WATCHDOG_CONTROL_KEY. Without one every node behaves exactly as it did before this phase."""
    return bool(CONTROL_KEY)


def _access_on() -> bool:
    """Access is OFF only while a working control key exists, so a damaged or missing key can never leave a node locked with nobody able to switch it back on."""
    return CONTROL["state"].access_on or not _control_enabled()


def _delivery_allowed() -> bool:
    """May THIS node alert people now? Before the control phase: Core always, a standby only while it has taken over. Now (control.may_alert): the preferred
    node always, the other node only when the preferred one is down or the two disagree about who is preferred. Core with no peer to watch never steps aside."""
    if not _control_enabled() or (not IS_STANDBY and not PRIMARY_URL):
        return (not IS_STANDBY) or WATCH.delivery_allowed()
    return control.may_alert(NODE, CONTROL["state"], control.PeerView(down=WATCH.delivery_allowed(), preferred=PEER["preferred"]))


def _alert_recipient() -> str:
    """Who an alert e-mail goes to: the normal list, or (access OFF) only the super user's own address. "" means nobody."""
    if not _control_enabled():
        return ALERT_EMAIL_TO
    route = control.route_delivery(CONTROL["state"])
    return ALERT_EMAIL_TO if route.users else (SUPER_EMAIL_TO if route.super_user else "")


def _push_target() -> str:
    """Which phone-push topic an alert goes to: the shared one, or (access OFF) only the super user's private one. "" means none."""
    if not _control_enabled():
        return PUSH_URL
    route = control.route_delivery(CONTROL["state"])
    return PUSH_URL if route.users else (SUPER_PUSH_URL if route.super_user else "")


def send_plain_mail(subject: str, body: str, to: str) -> bool:
    """One plain e-mail (the control mails). True only when the mail server accepted it; a missing setting or any failure is False, never an exception."""
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and to):
        logger.warning("[send_plain_mail] SMTP or recipient not configured -- would have sent: %s", _safe_header(subject))
        return False
    try:
        msg = build_alert_message(_safe_header(subject), ALERT_EMAIL_FROM, to, body, f'<pre style="font:14px/1.5 sans-serif">{html.escape(body)}</pre>')
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.starttls(context=SMTP_TLS_CONTEXT)
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(ALERT_EMAIL_FROM, [address.strip() for address in to.split(",") if address.strip()], msg.as_string())
        logger.info("[send_plain_mail] sent: %s", _safe_header(subject))
        return True
    except Exception as exc:  # noqa: BLE001 -- a failed notification must never crash a loop
        logger.error("[send_plain_mail] FAILED to send (%r): %s", exc, _safe_header(subject))
        return False


def deadman_enabled() -> bool:
    """Only Core pings the `Core alive` check: if a standby pinged it, a dead Core would never be reported."""
    return bool(DEADMAN_URL) and not IS_STANDBY


async def _studio_states() -> list:
    """[(source, state)] as this listener sees the studios, for the takeover and stand-down messages. A database problem gives an empty list and a log line."""
    try:
        async with _db_lock:
            with db() as conn:
                rows = conn.execute("SELECT source_id, state FROM sources ORDER BY source_id").fetchall()
        return [(row["source_id"], row["state"]) for row in rows]
    except Exception as exc:  # noqa: BLE001 -- a message with an empty studio list is better than no message
        logger.error("[main.py::_studio_states] error | error=%r action=continue_with_empty_list", exc)
        return []


async def _apply_transition(change, studios=None) -> None:
    """One takeover or stand-down message (e-mail and phone push, sent even though the gate is closing) when the standby's belief about Core just changed."""
    if change is None:
        return
    if not IS_STANDBY:
        await _apply_peer_transition(change)           # Core watching the VPS (the VPS is preferred and Core is its fallback)
        return
    now = datetime.now(timezone.utc)
    if studios is None:
        studios = await _studio_states()
    if change.kind == "down":
        notice, kind, event_type, severity = takeover_notice(WATCH.snapshot(now), studios, PUBLIC_HOST), "critical", "primary_down", "critical"
    else:
        notice, kind, event_type, severity = standdown_notice(change.previous_since or change.at, change.at, studios, PUBLIC_HOST), "heartbeat recovered", "primary_up", "info"
    logger.info("[main.py::_apply_transition] decision | action=%s reason=%s", change.kind, change.reason[:120])
    await asyncio.to_thread(send_alert_email, "CORE", kind, notice.summary, notice.rows, notice.title, force=True)
    await asyncio.to_thread(send_push_notice, notice.push_title, notice.push_body, force=True)
    try:
        await record_event("CORE", event_type, notice.summary, severity)
    except Exception as exc:  # noqa: BLE001 -- the history entry is secondary to the messages already sent
        logger.error("[main.py::_apply_transition] error | error=%r action=event_not_recorded", exc)


async def _note_primary_report(source_id, payload) -> None:
    """A studio's copy of a post may say whether its post to Core was accepted (`primary_ok`). Only a standby listens; a bad value is ignored."""
    if not IS_STANDBY or not isinstance(payload, dict) or "primary_ok" not in payload:
        return
    await _apply_transition(WATCH.record_report(source_id, payload.get("primary_ok"), datetime.now(timezone.utc)))


def probe_primary(url: str, timeout: float) -> tuple:
    """(ok, error) for one GET of Core's /health: ok only for HTTP 200 whose JSON says status ok. Never raises; the error text is short and has no address in it."""
    PEER["preferred"] = None            # unknown unless this very probe proves otherwise
    if not _is_web_url(url):
        return False, "no usable probe address"
    try:
        # Python's default web-client name is on Cloudflare's browser-signature block list (HTTP 403, "error code: 1010"), which made the first real probe of Core fail every time.
        request = urllib.request.Request(url, headers={"User-Agent": PROBE_USER_AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as reply:  # nosec B310 -- scheme checked just above: http(s) with a host only
            status, body = reply.status, reply.read(2048)
        if status != 200:
            return False, f"HTTP {status}"
        health = json.loads(body.decode("utf-8", "replace"))
        if isinstance(health, dict) and health.get("status") == "ok":
            PEER["preferred"] = health.get("preferred") if health.get("preferred") in control.NODES else None
            return True, ""
        return False, "the health page did not say ok"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 -- any failure to get a good answer counts as a failed probe
        return False, f"{type(exc).__name__}: {str(exc)[:80]}"


def _write_state(key: str, value: str) -> None:
    with db() as conn:
        conn.execute("INSERT INTO standby_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def _read_state(key: str) -> Optional[str]:
    with db() as conn:
        row = conn.execute("SELECT value FROM standby_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


async def _persist_proof(moment: datetime) -> None:
    """Stores when a probe of Core last succeeded. A failure to store is logged and never stops the probe loop."""
    try:
        async with _db_lock:
            _write_state(PROOF_KEY, WATCH.last_ok_at.isoformat() if WATCH.last_ok_at else moment.isoformat())
        PROBE_STATE["proof_saved_at"] = moment
    except Exception as exc:  # noqa: BLE001
        logger.error("[main.py::_persist_proof] error | error=%r action=continue_without_storing", exc)


async def _restore_proof() -> None:
    """After a restart: the remembered time of the last good probe, so the standby can still tell a dead Core from a blocked probe. Anything unreadable is ignored."""
    try:
        async with _db_lock:
            text = _read_state(PROOF_KEY)
        parsed = _parse_utc(text)
        if parsed is not None and parsed.tzinfo is not None:
            WATCH.restore_proven(parsed)
            logger.info("[main.py::_restore_proof] decision | action=proof_restored last_ok=%s", parsed.isoformat())
    except Exception as exc:  # noqa: BLE001
        logger.error("[main.py::_restore_proof] error | error=%r action=continue_without_proof", exc)


async def primary_watch_step(now=None, studios=None) -> None:
    """One probe of Core, fed to the decision logic; sends the takeover or stand-down message if the belief changed."""
    moment = now or datetime.now(timezone.utc)
    ok, error = await asyncio.to_thread(probe_primary, PRIMARY_URL, PROBE_TIMEOUT_SECONDS)
    PROBE_STATE["last_at"] = datetime.now(timezone.utc)          # a probe completed (good or bad): the self-check needs the loop to be alive, not Core to be well
    change = WATCH.record_probe(ok, moment, error)
    if ok:
        saved_at = PROBE_STATE["proof_saved_at"]
        if saved_at is None or (moment - saved_at).total_seconds() >= PROOF_SAVE_EVERY_SECONDS:
            await _persist_proof(moment)
    await _apply_transition(change, studios)


async def primary_watch_loop() -> None:
    while True:
        try:
            await primary_watch_step()
        except Exception as exc:  # noqa: BLE001 -- the watching loop must never die
            logger.error("[main.py::primary_watch_loop] error (continuing): %r", exc)
        await asyncio.sleep(PROBE_INTERVAL_SECONDS)


async def _apply_peer_transition(change) -> None:
    """Core watching the VPS. When the VPS is the preferred node and stops answering, Core takes over (see _delivery_allowed) and says so once; when the VPS
    answers again Core stands down and says so once. While Core is the preferred node nothing is mailed: the 'Standby alive' check already tells the super
    user that the VPS is gone, and a second mail about it would only be noise."""
    now = datetime.now(timezone.utc)
    logger.info("[main.py::_apply_peer_transition] decision | action=peer_%s preferred=%s reason=%s", change.kind, CONTROL["state"].preferred, change.reason[:120])
    if CONTROL["state"].preferred != control.VPS:
        return
    if change.kind == "down":
        text, kind, event_type, severity = control.peer_down_text(control.VPS, control.CORE, str(WATCH.snapshot(now).get("last_error", ""))), "critical", "peer_down", "critical"
    else:
        text, kind, event_type, severity = (control.peer_up_text(control.VPS, control.CORE, (change.at - (change.previous_since or change.at)).total_seconds()),
                                            "heartbeat recovered", "peer_up", "info")
    rows = [("Preferred node", control.VPS), ("Alerting now", control.CORE if change.kind == "down" else control.VPS)]
    await asyncio.to_thread(send_alert_email, control.VPS, kind, text.summary, rows, text.title, force=True)
    await asyncio.to_thread(send_push_notice, text.title, text.summary, force=True)
    try:
        await record_event(control.VPS, event_type, text.summary, severity)
    except Exception as exc:  # noqa: BLE001 -- the history entry is secondary to the messages already sent
        logger.error("[main.py::_apply_peer_transition] error | error=%r action=event_not_recorded", exc)


def _write_states(values: dict) -> None:
    """Several key/value rows in ONE transaction: either the whole control state is stored or none of it."""
    with db() as conn:
        conn.executemany("INSERT INTO standby_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", list(values.items()))


async def _store_control(new_state) -> None:
    """Stores the state FIRST, and only then makes it the state in force: if the database refuses, nothing has changed."""
    async with _db_lock:
        _write_states(control.to_store(new_state))
    CONTROL["state"] = new_state


async def _load_control() -> None:
    """After a start: the remembered control state. A damaged value falls back field by field (control.from_store) and is reported; an unreadable database leaves
    the defaults (access ON, Core preferred) and says so."""
    try:
        async with _db_lock:
            rows = {key: value for key in CONTROL_STORE_KEYS if (value := _read_state(key)) is not None}
        loaded = control.from_store(rows)
        CONTROL["state"], CONTROL["problems"] = loaded.state, loaded.problems
        async with _db_lock:
            stored_generation = _read_state("control_session_gen") or ""
        CONTROL["session_gen"] = stored_generation if re.fullmatch(r"[0-9a-f]{0,64}", stored_generation) else ""
        logger.info("[main.py::_load_control] decision | action=loaded access=%s preferred=%s pending=%s problems=%d", "on" if loaded.state.access_on else "off",
                    loaded.state.preferred, loaded.state.pending_notice, len(loaded.problems))
    except Exception as exc:  # noqa: BLE001 -- start with the defaults rather than not at all
        CONTROL["problems"] = ("the control state could not be read, so the defaults are in force",)
        logger.error("[main.py::_load_control] error | error=%r action=continue_with_defaults", exc)


async def apply_control_command(payload: dict, notify: bool = True, now=None) -> list:
    """Applies one command (`access` and/or `preferred`) to THIS node and returns its effects. Everything is worked out first and stored in one go, so a bad
    value changes nothing. ValueError: a bad value. PermissionError: this node has no way to watch the other one, so it must not step aside."""
    moment = now or datetime.now(timezone.utc)
    state, effects = CONTROL["state"], []
    if "access" in payload:
        change = control.set_access(state, control.parse_access(payload["access"]), moment, notify)
        state, effects = change.state, effects + list(change.effects)
    if "preferred" in payload:
        node = control.parse_preferred(payload["preferred"])
        if node != NODE and not PRIMARY_URL:
            raise PermissionError("this node has no address for the other node's health page, so it cannot step aside")
        change = control.set_preferred(state, node, moment)
        state, effects = change.state, effects + list(change.effects)
    if state != CONTROL["state"]:
        await _store_control(state)
        for effect in effects:
            try:
                await record_event("CONTROL", effect, f"access={'on' if state.access_on else 'off'} preferred={state.preferred}", "info")
            except Exception as exc:  # noqa: BLE001 -- the history entry is secondary; the change itself is stored
                logger.error("[main.py::apply_control_command] error | error=%r action=event_not_recorded", exc)
    return effects


def _super_user_messages(effects, state) -> list:
    """What the super user is mailed about a change (every change is announced to him)."""
    now = datetime.now(timezone.utc)
    messages = []
    if "access_off" in effects:
        messages.append(control.offline_message(state.off_at or now))
    if "access_on" in effects:
        messages.append(control.restored_message(state.off_at, state.access_changed_at or now))
    if "preferred_changed" in effects:
        messages.append(control.preference_message(state.preferred, now))
    return messages


async def control_notice_step() -> Optional[str]:
    """Sends the one queued control mail ("offline" or "back"), if any, to the support mailbox only. Returns the kind sent, or None. A mail that fails stays
    queued and is tried again, so a mail-server outage delays it and never loses it."""
    state = CONTROL["state"]
    kind = state.pending_notice
    if kind is None:
        return None
    if not CONTROL_NOTICE_TO:
        if not _notice_warned["done"]:
            _notice_warned["done"] = True
            logger.error("[main.py::control_notice_step] error | a control mail is queued but WATCHDOG_CONTROL_NOTICE_TO is empty, so it cannot be sent")
        return None
    now = datetime.now(timezone.utc)
    message = control.offline_message(state.off_at or now) if kind == control.OFFLINE else control.restored_message(state.off_at, state.access_changed_at or now)
    if not await asyncio.to_thread(send_plain_mail, message.subject, message.body, CONTROL_NOTICE_TO):
        return None
    await _store_control(control.notice_sent(CONTROL["state"], kind))
    return kind


async def control_notice_loop() -> None:
    while True:
        try:
            await control_notice_step()
        except Exception as exc:  # noqa: BLE001 -- this loop must never die
            logger.error("[main.py::control_notice_loop] error (continuing): %r", exc)
        await asyncio.sleep(CONTROL_NOTICE_SECONDS)


def _control_status(now: datetime, detail: bool = False) -> dict:
    """The control block of /status, the stream and /control: states and times, never a key. The reasons for a fallback (`problems`) are for the super user only."""
    block = {"enabled": _control_enabled(), "node": NODE}
    if not _control_enabled():
        return block
    block.update(control.snapshot(CONTROL["state"], CONTROL["problems"] if detail else ()))
    block["alerting_now"] = _delivery_allowed()
    block["peer"] = {"watched": bool(PRIMARY_URL), "believed": WATCH.state if PRIMARY_URL else None, "preferred": PEER["preferred"]}
    block["login"] = _password_login_enabled()          # true once a sign-in name and password hash are set; never the values
    return block


def _control_banner() -> str:
    """A thin strip at the top of both dashboards while access is OFF (only the super user ever sees a page then). Which node is preferred is NOT shown here: visitors are
    not told, and the control panel is where it is read. The page keeps this strip up to date by itself from its live frames (the element is found by its id)."""
    if not _control_enabled():
        return ""
    state = CONTROL["state"]
    if state.access_on:
        return ""
    since = state.off_at.astimezone(timezone.utc).strftime("%H:%M UTC") if state.off_at else "an unknown time"
    return ('<div id="control-banner" role="status" style="position:sticky;top:0;z-index:99999;background:#7a1f1f;color:#fff;font:600 14px/1.4 system-ui,sans-serif;padding:8px 12px;'
            'text-align:center">' + html.escape("ACCESS OFF since " + since) + "</div>")


def _super_target(signed_in: bool, staff_name: Optional[str] = None) -> tuple:
    """(address, text) of the sign-in link. The text is already escaped: a display name is typed by a person, so it never goes into a page raw."""
    if signed_in:
        return "/control/panel", "Control panel"
    if staff_name:
        return "/staff", html.escape(staff_name)
    return "/control/login", "Sign in"


def _super_link(signed_in: bool, staff_name: Optional[str] = None) -> str:
    """The fallback for a page with no place for the link: a small, quiet pill at the bottom-right ('Sign in' for everyone, 'Control panel' once the super user is signed in). It only
    leads to the sign-in page or the panel, which do their own checks; it carries no secret and no state."""
    href, text = _super_target(signed_in, staff_name)
    return ('<style>a.wd-super:focus-visible{outline:3px solid #fff;outline-offset:2px}</style>'
            f'<a class="wd-super" href="{href}" style="position:fixed;right:18px;bottom:12px;z-index:99998;padding:6px 12px;border-radius:999px;background:rgba(15,22,36,.85);'
            f'color:#eaf0fa;font:600 12px/1.2 system-ui,sans-serif;text-decoration:none;border:1px solid #3a4b6b">{text}</a>')


def _with_super_link(page: str, request, staff_name: Optional[str] = None) -> str:
    """The sign-in link in the dashboard's toolbar, in the place the page keeps for it (it looks like the toolbar's buttons, so it is easy to find); on a page with no such place, a small
    pill just before </body>. Nothing is offered where the control phase is off (the door does not exist there): the place is simply emptied."""
    if not _control_enabled():
        return page.replace(STAFF_LINK_MARKER, "")
    signed_in = _control_caller_is_super(request)
    if STAFF_LINK_MARKER in page:
        href, text = _super_target(signed_in, staff_name)
        # A visitor who is not signed in as anyone is also shown the way for STAFF: the office PCs' staff address (his request of 2026-11-02, "sign in from the app"). The address is the
        # operator's setting and was checked to be a plain https address when it was read, and it is escaped again here.
        staff_way = "" if signed_in or staff_name else f'<a class="staff-link" href="{html.escape(STAFF_URL, quote=True)}">Staff sign in</a>'
        return page.replace(STAFF_LINK_MARKER, f'<a class="staff-link" href="{href}">{text}</a>{staff_way}')
    link = _super_link(signed_in, staff_name)
    end = page.rfind("</body>")
    return page[:end] + link + page[end:] if end >= 0 else page + link


def _with_banner(page: str) -> str:
    banner = _control_banner()
    if not banner:
        return page
    start = page.find("<body")
    end = page.find(">", start) + 1 if start >= 0 else 0
    return page[:end] + banner + page[end:] if end else banner + page


def _with_off_screen(page: str) -> str:
    """The unavailable screen, hidden, put in the place the page keeps for it, so an open dashboard can show it the moment access is switched OFF (and hide it when ON) with no
    refresh. Where the control phase is off the place is simply emptied."""
    return page.replace(OFF_SCREEN_MARKER, control_access.off_overlay_html() if _control_enabled() else "")


def _install_sender_keys(new_keys: dict) -> None:
    """Swaps the key ring for one built from the new keys. In-flight requests finish with the old object; the next request sees the new one."""
    global KEYRING
    KEYRING = Keyring(new_keys, KEYRING.mode)


async def keys_reload_loop() -> None:
    """Notices a changed sender-keys file and installs the new keys without a restart. A file with any problem changes nothing and is counted once."""
    assert KEY_WATCHER is not None
    reported_errors = KEY_WATCHER.errors
    while True:
        try:
            new_keys = await asyncio.to_thread(KEY_WATCHER.poll, datetime.now(timezone.utc))
            if new_keys is not None:
                _install_sender_keys(new_keys)
                logger.info("[main.py::keys_reload_loop] decision | action=keys_reloaded keyset=%s keyed=%s windows_open=%s", KEYRING.digest(), KEYRING.keyed_sources(), KEYRING.windows_open())
            if KEY_WATCHER.errors != reported_errors:
                reported_errors = KEY_WATCHER.errors
                logger.error("[main.py::keys_reload_loop] error | action=keys_unchanged problem=%s (the keys already in force stay in force)", KEY_WATCHER.last_error[:200])
        except Exception as exc:  # noqa: BLE001 -- this loop must never die: it is the only way a rotation reaches a running standby
            logger.error("[main.py::keys_reload_loop] error (continuing): %r", exc)
        await asyncio.sleep(KEYS_POLL_SECONDS)


def _utc_or_none(text) -> Optional[datetime]:
    parsed = _parse_utc(text) if text else None
    return parsed if parsed is None or parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def _gather_health_inputs(now: datetime, smtp_ok: Optional[bool]) -> HealthInputs:
    """Measures what assess_health judges. Each measurement that fails becomes a problem or an unknown, never an exception."""
    db_ok, seen = True, {}
    try:
        async with _db_lock:
            with db() as conn:
                conn.execute("SELECT 1").fetchone()
                rows = conn.execute("SELECT source_id, last_seen_utc FROM sources").fetchall()
        seen = {row["source_id"]: _utc_or_none(row["last_seen_utc"]) for row in rows if row["source_id"] in HEALTH_CONFIG.expect_copies}
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        logger.error("[main.py::_gather_health_inputs] error | what=database error=%r", exc)
    try:
        free_mb: Optional[float] = shutil.disk_usage(str(DB_PATH.parent)).free / (1024 * 1024)
    except Exception as exc:  # noqa: BLE001 -- not measurable is "unknown", which is not a reason to page
        free_mb = None
        logger.warning("[main.py::_gather_health_inputs] warning | what=disk_space error=%r", exc)
    return HealthInputs(
        now=now, started_at=STARTED_AT, last_probe_at=PROBE_STATE["last_at"], db_ok=db_ok, free_mb=free_mb,
        key_error=KEY_WATCHER.last_error if KEY_WATCHER else "", keyed_sources=tuple(KEYRING.keyed_sources()),
        copy_seen={name: seen.get(name) for name in HEALTH_CONFIG.expect_copies}, refusals_recent=REFUSALS.recent(now, HEALTH_CONFIG.refusals_window_seconds), smtp_ok=smtp_ok,
        probe_proven=WATCH.probe_proven(now), probe_error=str(WATCH.snapshot(now).get("last_error", "")),
    )


def ping_alive(url: str, summary: str) -> bool:
    """The "Standby alive" check: a plain GET to the ping address when everything is in order; when `summary` names problems, a POST to its /fail address with
    the reasons as the body (the outside service emails at once). Never raises."""
    if not _is_web_url(url):
        return False
    try:
        if summary:
            request = urllib.request.Request(url.rstrip("/") + "/fail", data=summary.encode("utf-8"), method="POST", headers={"Content-Type": "text/plain; charset=utf-8"})
        else:
            request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=10) as reply:  # nosec B310 -- scheme checked just above: http(s) with a host only
            return 200 <= reply.status < 300
    except Exception as exc:  # noqa: BLE001
        logger.warning("[main.py::ping_alive] warning | ping failed error=%r", exc)
        return False


async def standby_alive_step(smtp_ok: Optional[bool]) -> list:
    """One self-check: measure, judge, remember for /status, and ping the outside check. Returns the problems found."""
    now = datetime.now(timezone.utc)
    inputs = await _gather_health_inputs(now, smtp_ok)
    problems = assess_health(inputs, HEALTH_CONFIG)
    HEALTH_STATE.update(ok=not problems, problems=[summarize_problems([text]) for text in problems], checked_at=now.isoformat(), refused_recently=inputs.refusals_recent)
    if problems:
        logger.warning("[main.py::standby_alive_step] decision | action=not_ready problems=%s", summarize_problems(problems)[:300])
    if STANDBY_ALIVE_URL:
        await asyncio.to_thread(ping_alive, STANDBY_ALIVE_URL, summarize_problems(problems))
    return problems


async def standby_alive_loop() -> None:
    smtp_ok: Optional[bool] = None
    last_smtp_check = None
    while True:
        try:
            if last_smtp_check is None or time.monotonic() - last_smtp_check >= SMTP_CHECK_EVERY_SECONDS:
                smtp_ok = await asyncio.to_thread(check_smtp_login)
                last_smtp_check = time.monotonic()
            await standby_alive_step(smtp_ok)
        except Exception as exc:  # noqa: BLE001 -- if the self-check itself breaks, say nothing: the pings stop and the outside service raises the alarm
            logger.error("[main.py::standby_alive_loop] error (continuing, no ping sent): %r", exc)
        await asyncio.sleep(ALIVE_SECONDS)


def code_fingerprint(folder: Path) -> str:
    """12 hex characters standing for the listener's own code (every .py file in the app folder, line endings ignored), so a tool can tell whether Core and a
    standby run the same code without reading either. "" if the folder cannot be read."""
    try:
        digest = hashlib.sha256()
        for path in sorted(folder.glob("*.py")):
            digest.update(path.name.encode("utf-8") + b"\0" + path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
        return digest.hexdigest()[:12]
    except OSError:
        return ""


CODE_FINGERPRINT = code_fingerprint(Path(__file__).resolve().parent)


def _standby_status(now: datetime, detail: bool = False) -> dict:
    """The /status and stream block: just the role on Core; on a standby also what it believes about Core and what it has held back. Counts and states only.
    Anyone with the (public) view key sees whether the self-check is ok and when it last ran; the reasons, the key file's last error and the refusal count are for
    the admin key only (`detail`), because "the email login does not work" is not something to publish."""
    if not IS_STANDBY:
        return {"role": "primary", "code": CODE_FINGERPRINT}
    keys = {"file": KEY_WATCHER is not None, "errors": KEY_WATCHER.errors if KEY_WATCHER else 0}
    health = {"ok": HEALTH_STATE["ok"], "checked_at": HEALTH_STATE["checked_at"]}
    if detail:
        keys.update(loaded_at=KEY_WATCHER.loaded_at.isoformat() if KEY_WATCHER and KEY_WATCHER.loaded_at else None,
                    last_error=KEY_WATCHER.last_error[:200] if KEY_WATCHER else "")
        health = dict(HEALTH_STATE)
    return {"role": "standby", "primary": WATCH.snapshot(now), "suppressed": dict(SUPPRESSED), "probe_every_seconds": PROBE_INTERVAL_SECONDS,
            "code": CODE_FINGERPRINT, "keys": keys, "health": health}


def _absent_for(absent, feed: str, now: datetime) -> float:
    """Seconds this feed has been absent according to the clocks in `absent` ({feed: ISO time}); 0 when it has no usable clock (it has only just been seen missing)."""
    raw = absent.get(feed) if isinstance(absent, dict) else None
    try:
        since = _aware_utc(raw) if isinstance(raw, str) else None
    except Exception:  # noqa: BLE001 -- a damaged clock is "just now", never an error
        since = None
    if since is None or since > now:
        return 0.0
    return (now - since).total_seconds()


def evaluate_instances(payload: dict, expectation, absent=None, now: Optional[datetime] = None) -> tuple[Optional[bool], list[str]]:
    """Judges one heartbeat against what its machine should be running.

    Returns (verdict, problems): True when everything expected is running, False when
    something is missing (problems says what, in plain words), None when the machine is
    not judged (no expectation configured, or the heartbeat reports no count).

    With `absent` (the clocks Core keeps for missing feeds) and `now`, a feed that has been missing for less than INSTANCE_DOWN_GRACE_SECONDS does not count yet: if
    that is all that is missing the reading is not judged (None), so the stored state is left as it was and a blip raises and restores nothing."""
    if expectation is None:
        return None, []
    instances = [inst for inst in (payload.get("instances") or []) if isinstance(inst, dict)]
    if isinstance(expectation, int):
        count = payload.get("instance_count")
        if not isinstance(count, int):
            return None, []
        if count >= expectation:
            return True, []
        return False, [f"{count} of {expectation} instances running"]
    running = {str(inst.get("feed")) for inst in instances}
    missing = [feed for feed in expectation if feed not in running]
    if not missing:
        return True, []
    if absent is not None and now is not None:
        missing = [feed for feed in missing if _absent_for(absent, str(feed).lower(), now) >= INSTANCE_DOWN_GRACE_SECONDS]
        if not missing:
            return None, []
    return False, [f"{FEED_NAMES.get(feed, feed.upper())} feed not running" for feed in missing]


def next_alert_action(current_alert: Optional[str], verdict: Optional[bool]) -> Optional[str]:
    """'missing' on the first short reading after a full one, 'restored' on the first full
    reading after a 'missing' one, None otherwise. Because the state is stored, a machine
    that is already short when the listener starts is flagged once, and a restart never
    repeats an alert. An unjudged reading (verdict None) changes nothing."""
    if verdict is None:
        return None
    if verdict is False and current_alert != "missing":
        return "missing"
    if verdict is True and current_alert == "missing":
        return "restored"
    return None


def _describe_expectation(expectation) -> str:
    if isinstance(expectation, int):
        return f"{expectation} playout instances"
    names = [FEED_NAMES.get(feed, feed.upper()) for feed in expectation]
    return " and ".join(names) + " feeds"


def _validate_text(value, field: str, limit: int) -> tuple[Optional[str], Optional[str]]:
    if not isinstance(value, str):
        return None, f"payload field '{field}' must be a string"
    value = value.strip()
    if not value:
        return None, f"payload field '{field}' must not be empty"
    if len(value) > limit:
        return None, f"payload field '{field}' exceeds the {limit}-character limit"
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None, f"payload field '{field}' contains a control character"
    return value, None


def _event_detail(payload: dict) -> tuple[Optional[str], Optional[str]]:
    raw = payload.get("detail", "")
    if isinstance(raw, str):
        detail = raw
    else:
        try:
            detail = json.dumps(raw, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            return None, "payload field 'detail' must be JSON-serializable"
    if len(detail) > MAX_DETAIL_CHARS:
        return None, f"payload field 'detail' exceeds the {MAX_DETAIL_CHARS}-character limit"
    return detail, None


async def _ingest_rate_limit_response() -> Optional[JSONResponse]:
    if await _ingest_request_allowed():
        return None
    return JSONResponse(
        {"error": "ingest rate limit reached; retry later"},
        status_code=429,
        headers={"Retry-After": str(INGEST_WINDOW_SECONDS)},
    )


def _reject_plain_http(request) -> None:
    if _plain_http_refused(request):
        raise HTTPException(status_code=426, detail="plain HTTP POSTs are disabled; use HTTPS")


# ---- The Site C feed (the unpublished design notes) --------------------------------------------------------------------------------------------------
# The playout PCs count the encoder's two multicast streams and put the numbers in their heartbeat (field `site_c_feed`). site_c_feed.py judges them; the three
# functions below only connect it to the heartbeat, the alarm channels and the daily clean-up. Every one of them is written so that a fault HERE can never cost a heartbeat
# or a playout alarm: monitoring of the playouts comes first.
SITE_C_SETTINGS = site_c_feed.settings_from_environ()


def _site_c_step_locked(conn, source_id: str, payload: dict, now_dt: datetime) -> list:
    """Stores this heartbeat's reading and judges both streams; returns the alarms to raise or restore once the lock is released. Never raises."""
    try:
        site_c_feed.ingest_locked(conn, source_id, site_c_feed.parse_report(payload.get("site_c_feed")), now_dt, SITE_C_SETTINGS)
        return site_c_feed.evaluate_locked(conn, now_dt, SITE_C_SETTINGS)
    except Exception as exc:  # noqa: BLE001 -- the Site C monitor must not be able to disturb the heartbeat
        logger.error("[main.py::_site_c_step_locked] error (the heartbeat is not affected): %r", exc)
        return []


def _site_c_snapshot(conn, now_dt: datetime) -> dict:
    try:
        return site_c_feed.snapshot(conn, now_dt, SITE_C_SETTINGS)
    except Exception as exc:  # noqa: BLE001 -- a broken panel must not break /status or the live stream
        logger.error("[main.py::_site_c_snapshot] error (status is not affected): %r", exc)
        return {"reporting": False, "streams": []}


def prune_old_site_c_history(conn, now: datetime) -> int:
    return site_c_feed.prune_locked(conn, now)


async def _site_c_alarm(item: dict) -> None:
    """One Site C stream alarm: the event first (so the history is right whatever the mail does), then the red e-mail and phone push the playouts' red alarms use, then
    the failure for the staff section, keyed SITE_C-5004 or SITE_C-5005 so it is held, shown and assigned like any other failure. A restore is recorded and noted."""
    stream, cause = item["stream"], item["cause"]
    name = site_c_feed.stream_title(stream)      # "Site C main stream (UDP 5005)": the owner's names, so a mail or a staff failure says which stream it is
    rows = [("Stream", name), ("Cause", cause)] + [
        (f"Heard by {w['source']}", f"{w['mbit_per_s']:.2f} Mbit/s, {w['state']}, no packet for {w['idle_seconds']} s (report {w['age_seconds']} s old)") for w in item["witnesses"]]
    if item["action"] == "raise":
        await record_event("SITE-C", "site_c_feed_critical", json.dumps({"stream": stream, "kind": item["kind"], "cause": cause, "witnesses": item["witnesses"]}), "critical")
        try:
            await asyncio.to_thread(send_alert_email, name, "CRITICAL", f"{name}: {cause}", rows, cause)
            await asyncio.to_thread(send_push_notice, f"Watchdog: {name} CRITICAL", cause)
        finally:
            await _staff_note_failure({"endpoint_id": f"SITE_C-{stream}", "root_cause": cause[:80], "display_name": name})
    else:
        await record_event("SITE-C", "site_c_feed_restored", json.dumps({"stream": stream}), "info")
        await asyncio.to_thread(_send_recovery_notice, name, "stream restored", f"{name} is being heard again.", rows)


@app.post("/heartbeat", dependencies=[Depends(require_token)])
async def heartbeat(request: Request) -> JSONResponse:
    limited = await _ingest_rate_limit_response()
    if limited:
        return limited
    payload, refused = await _json_body(request)
    if refused:
        return refused
    source_id = payload.get("hostname")
    if not source_id:
        return JSONResponse({"error": "payload missing 'hostname'"}, status_code=400)
    source_id, validation_error = _validate_text(source_id, "hostname", MAX_SOURCE_ID_CHARS)
    if validation_error:
        return JSONResponse({"error": validation_error}, status_code=400)
    _authorize_source(request, source_id)
    _note_plain_http(request, source_id)
    _note_route(request, source_id)
    _note_key_generation(request, source_id)
    await _note_primary_report(source_id, payload)

    # Each source's own natural cadence, not one global assumption -- main's
    # process check runs every ~10s, SITE-B's detector heartbeats every 5
    # minutes. A source that doesn't send this keeps the global default.
    expected_interval = _interval_seconds(payload.get("expected_interval_seconds", HEARTBEAT_INTERVAL_SECONDS))

    _heartbeats.record()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    expectation = EXPECTED_INSTANCES.get(source_id)
    endpoint_emails = []
    async with _db_lock:
        with db() as conn:
            row = conn.execute(
                "SELECT state, instance_alert, last_payload_json FROM sources WHERE source_id = ?", (source_id,)
            ).fetchone()
            previous_state = row["state"] if row else None
            current_alert = row["instance_alert"] if row else None
            # How long each item has spent paused travels with the stored heartbeat (see _with_pause_marks); it is read back from the last one here.
            payload = _with_pause_marks(payload, _json_obj(row["last_payload_json"]) if row else None)
            payload = _with_hour_hint(payload, _json_obj(row["last_payload_json"]) if row else None)
            payload = _with_idle_mark(payload, _json_obj(row["last_payload_json"]) if row else None, source_id, now_dt)
            payload = _with_absent_marks(payload, _json_obj(row["last_payload_json"]) if row else None, expectation, now_dt)
            # Judged while the lock is held, so the stored alert state and the decision always agree.
            verdict, problems = evaluate_instances(payload, expectation, payload.get("_absent") or {}, now_dt)
            alert_action = next_alert_action(current_alert, verdict)
            # A reading that cannot be judged leaves the stored state as it was.
            new_alert = current_alert if verdict is None else ("missing" if verdict is False else "ok")
            conn.execute(
                """INSERT INTO sources (source_id, last_seen_utc, last_payload_json, state, state_changed_utc, expected_interval_seconds, instance_alert)
                   VALUES (?, ?, ?, 'healthy', ?, ?, ?)
                   ON CONFLICT(source_id) DO UPDATE SET
                     last_seen_utc = excluded.last_seen_utc,
                     last_payload_json = excluded.last_payload_json,
                     state = 'healthy',
                     state_changed_utc = CASE WHEN sources.state != 'healthy' THEN excluded.state_changed_utc ELSE sources.state_changed_utc END,
                     expected_interval_seconds = excluded.expected_interval_seconds,
                     instance_alert = excluded.instance_alert
                """,
                (source_id, now, json.dumps(payload), now, expected_interval, new_alert),
            )
            _sources, _events, _endpoints, endpoint_emails = _evaluate_and_sync_locked(conn, now_dt, now)
            site_c_actions = _site_c_step_locked(conn, source_id, payload, now_dt)
    # Lock released above -- record_event/send_alert_email below acquire it
    # themselves (asyncio.Lock isn't reentrant, so holding it here would deadlock).
    # Mail goes through a worker thread: smtplib blocks for up to its 20 s timeout, and doing
    # that on the event loop would stall every heartbeat and every dashboard stream behind it.

    for endpoint in endpoint_emails:
        await _send_red_alarm(endpoint)

    for item in site_c_actions:
        try:
            await _site_c_alarm(item)
        except Exception as exc:  # noqa: BLE001 -- the playouts' own alarms and the heartbeat's answer come first
            logger.error("[main.py::heartbeat] site_c alarm error (heartbeat is not affected): %r", exc)

    if previous_state == "heartbeat_lost":
        await record_event(source_id, "heartbeat_recovered", f"instance_count={payload.get('instance_count')}")
        await asyncio.to_thread(
            _send_recovery_notice,
            source_id, "heartbeat recovered",
            f"{source_id} is reporting again.",
            [("Recovered at", now)] + _humanize_json(payload),
        )

    if alert_action == "missing":
        await record_event(source_id, "instance_missing", "; ".join(problems), severity="critical")
    elif alert_action == "restored":
        await record_event(source_id, "instances_restored", "all expected instances running again")
        await asyncio.to_thread(
            _send_recovery_notice,
            source_id, "instances restored",
            f"All expected playout instances are running again ({_describe_expectation(expectation)}).",
            [("Restored at", now)] + _humanize_json(payload),
        )

    return JSONResponse({"status": "ok"})


@app.post("/event", dependencies=[Depends(require_token)])
async def event(request: Request) -> JSONResponse:
    """A site's own detector reports an already-classified finding here --
    SITE-B's transition-overrun tiers, SITE-A's freeze/black/silence/
    loudness events, a restart, a suppressed alert. This endpoint does not
    replace that site's own detection logic or thresholds; it only relays
    what the site already decided to report, into one shared log."""
    limited = await _ingest_rate_limit_response()
    if limited:
        return limited
    payload, refused = await _json_body(request)
    if refused:
        return refused
    source_key = "hostname" if "hostname" in payload else "source_id"
    source_id, source_error = _validate_text(payload.get(source_key), source_key, MAX_SOURCE_ID_CHARS)
    event_type, event_error = _validate_text(payload.get("event_type"), "event_type", MAX_EVENT_TYPE_CHARS)
    if source_error or event_error:
        error = source_error or event_error
        return JSONResponse({"error": error}, status_code=400)
    _authorize_source(request, source_id)
    _note_plain_http(request, source_id)
    _note_route(request, source_id)
    _note_key_generation(request, source_id)
    await _note_primary_report(source_id, payload)

    severity = payload.get("severity", "info")
    if not isinstance(severity, str) or severity not in KNOWN_SEVERITIES:
        severity = "info"
    detail, detail_error = _event_detail(payload)
    if detail_error:
        return JSONResponse({"error": detail_error}, status_code=400)

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    async with _db_lock:
        with db() as conn:
            _insert_event_locked(conn, now, source_id, event_type, detail, severity)
            _sources, _events, _endpoints, endpoint_emails = _evaluate_and_sync_locked(conn, now_dt, now)
    logger.info("[record_event] source=%s type=%s severity=%s detail=%s", source_id, event_type, severity, detail)

    for endpoint in endpoint_emails:
        await _send_red_alarm(endpoint)

    return JSONResponse({"status": "ok"})


@app.post("/alerts/reset", dependencies=[Depends(require_admin_token)])
async def reset_alert(request: Request) -> JSONResponse:
    """Operator escape valve for a held critical alarm: marks one endpoint's stored alert healthy so the
    next evaluation starts from what the sensors say now. A sensor that has genuinely failed would
    otherwise leave an alarm held for good. Needs the shared token (an ops script, not a button on
    the public page), and is logged as an event so it is never silent."""
    limited = await _ingest_rate_limit_response()
    if limited:
        return limited
    payload, refused = await _json_body(request)
    if refused:
        return refused
    _reject_plain_http(request)
    endpoint_id, endpoint_error = _validate_text(payload.get("endpoint_id"), "endpoint_id", MAX_SOURCE_ID_CHARS)
    if endpoint_error:
        return JSONResponse({"error": endpoint_error}, status_code=400)
    known = {endpoint["endpoint_id"] for endpoint in PLAYOUT_ENDPOINTS}
    if endpoint_id not in known:
        return JSONResponse({"error": f"endpoint_id must be one of {sorted(known)}"}, status_code=400)
    now = datetime.now(timezone.utc).isoformat()
    async with _db_lock:
        with db() as conn:
            conn.execute(
                "UPDATE endpoint_alerts SET alert_level = 'healthy', root_cause = 'Manual reset', "
                "state_changed_utc = ?, last_email_key = NULL WHERE endpoint_id = ?",
                (now, endpoint_id),
            )
            _insert_event_locked(conn, now, endpoint_id, "alert_reset", "held alarm reset by operator", "info")
    logger.info("[reset_alert] endpoint=%s", endpoint_id)
    return JSONResponse({"status": "ok", "endpoint_id": endpoint_id})


def _latest_event(conn: sqlite3.Connection, source_id: str) -> Optional[dict]:
    """The single most recent event row for one machine, or None if it has never posted one.
    This is what the dashboard uses to judge a no-instance-model machine (SITE-A, SITE-B) as
    currently at fault or not -- both heartbeat-driven events and a site's own detector findings
    land in the same `events` table, so one query covers both signal types."""
    row = conn.execute(
        "SELECT timestamp_utc, event_type, severity, detail FROM events "
        "WHERE source_id = ? ORDER BY id DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    return dict(row) if row else None


def _sources_snapshot(conn: sqlite3.Connection) -> list[dict]:
    """Every machine's current row, in the shape both /status and /stream return. Always
    includes all four known machines, even one that has never sent a heartbeat (e.g. SITE-B
    before go-live) -- a machine the dashboard doesn't know about yet is a real, visible state,
    not a row that's just missing. Each source also carries `expected_instances` (what it
    should be running) and `latest_event` (its most recent finding of any kind), which is what
    the dashboard uses to judge per-feed health. Caller must already hold `_db_lock`."""
    by_id = {r["source_id"]: dict(r) for r in conn.execute("SELECT * FROM sources").fetchall()}
    sources = []
    for source_id in KNOWN_SOURCE_IDS:
        row = by_id.pop(source_id, None) or {
            "source_id": source_id, "last_seen_utc": None, "last_payload_json": "{}",
            "state": "never_reported", "state_changed_utc": None,
            "expected_interval_seconds": None, "instance_alert": None,
        }
        row["expected_instances"] = EXPECTED_INSTANCES.get(source_id)
        row["latest_event"] = _latest_event(conn, source_id)
        sources.append(row)
    # Anything reporting outside the known four (e.g. the old SELFTEST probe) still shows up
    # too, just listed after the four the dashboard always expects.
    for source_id, row in by_id.items():
        row["expected_instances"] = EXPECTED_INSTANCES.get(source_id)
        row["latest_event"] = _latest_event(conn, source_id)
        sources.append(row)
    return sources


def _recent_events_snapshot(conn: sqlite3.Connection, limit: int, source: Optional[str] = None) -> list[dict]:
    """Newest events in a compact, shared shape for /status and /stream."""
    if source:
        rows = conn.execute(
            "SELECT * FROM events WHERE source_id = ? ORDER BY id DESC LIMIT ?", (source, limit)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def title_case_event(value: str) -> str:
    return " ".join(part.capitalize() for part in str(value).replace("-", "_").split("_") if part)


def _json_obj(raw) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _number_value(value) -> Optional[float]:
    """A real, finite number, or None. Infinity and NaN are refused: Python's json accepts them from a sender and writes them
    back out, and a browser's JSON.parse rejects the whole frame that carries one."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            number = float(value)
        elif isinstance(value, str):
            number = float(value.strip())
        else:
            return None
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clip_text(value, limit: int = MAX_TEXT_CHARS) -> str:
    """Text from a sender, as text and no longer than `limit` characters: a name that arrives as a nested object or as a megabyte
    must not be copied to every viewer's screen every five seconds."""
    text = str(value).strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _interval_seconds(value) -> int:
    """A sender's reporting interval, from untrusted input: a whole number of seconds between 1 and a day, else the default."""
    try:
        seconds = int(value)
    except (TypeError, ValueError, OverflowError):
        return HEARTBEAT_INTERVAL_SECONDS
    return min(MAX_INTERVAL_SECONDS, max(1, seconds))


MAX_BODY_BYTES = 64 * 1024   # the largest real heartbeat is a few kilobytes; the whole stored payload is re-sent to every viewer


async def _json_body(request):
    """The request body as a JSON object: (payload, None), or (None, a ready-made error response). The body is read in pieces and
    refused the moment it passes MAX_BODY_BYTES, so a huge or endless upload is never held in memory or stored, and one malformed
    heartbeat is a 4xx for its sender instead of a 500 or a megabyte rebroadcast to every dashboard."""
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > MAX_BODY_BYTES:
            return None, JSONResponse({"error": f"payload larger than {MAX_BODY_BYTES} bytes"}, status_code=413)
    try:
        payload = json.loads(bytes(received))
    except (ValueError, RecursionError):
        return None, JSONResponse({"error": "payload is not valid JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return None, JSONResponse({"error": "payload must be a JSON object"}, status_code=400)
    return payload, None


def _strict_json(value) -> str:
    """JSON that a browser's JSON.parse always accepts. Python writes NaN and Infinity as bare words, which are not JSON, and
    Starlette's JSONResponse refuses them outright (a 500). Anything non-finite that gets this far becomes null. The common case
    pays nothing: the clean-up runs only when such a word is present in the text."""
    text = json.dumps(value)
    if "NaN" in text or "Infinity" in text:
        text = json.dumps(json.loads(text, parse_constant=lambda _constant: None), allow_nan=False)
    return text


def _detail_text(event: dict) -> str:
    raw = event.get("detail")
    if isinstance(raw, str):
        return raw.lower()
    if isinstance(raw, dict):
        return json.dumps(raw).lower()
    return ""


def _parse_utc(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_seconds(value: Optional[str], now: datetime) -> Optional[float]:
    parsed = _parse_utc(value)
    return max(0.0, (now - parsed).total_seconds()) if parsed else None


# A heartbeat takes a second or two to arrive, so anything under this is just travel time.
CLOCK_SKEW_NOTICE_SECONDS = 5


def _clock_text(source: Optional[dict], payload: dict) -> str:
    """How far a site's own clock is from Core's (which keeps NTP time), judged from the timestamp its
    heartbeat carries against the moment the heartbeat arrived. A clock that is minutes off makes every
    time in that PC's own logs disagree with the other sites (MAIN was 3 min 42 s slow on 2026-10-11)."""
    sent = _parse_utc(payload.get("timestamp")) if isinstance(payload, dict) else None
    received = _parse_utc(source.get("last_seen_utc")) if source else None
    if sent is None or received is None:
        return "Not reported"
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)   # senders that omit the zone send UTC
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    offset = (sent - received).total_seconds()      # negative: the site's clock is behind Core's
    if abs(offset) < CLOCK_SKEW_NOTICE_SECONDS:
        return "In step with Core"
    minutes, seconds = divmod(int(round(abs(offset))), 60)
    amount = f"{minutes} min {seconds} s" if minutes else f"{seconds} s"
    return f"{amount} {'behind' if offset < 0 else 'ahead of'} Core: fix this PC's time sync"


def _fault_active_seconds(detail: dict, event: dict, now: datetime) -> tuple[float, float]:
    """(estimated seconds the fault has been running, seconds since the event was posted).
    The detector reports how long the fault had lasted when it posted; the time since then is
    added so a fault keeps climbing toward the thresholds between posts. With no reported
    duration the event's own age is the only evidence there is."""
    age = _age_seconds(event.get("timestamp_utc"), now) or 0.0
    for key in ("duration_s", "duration_seconds", "ongoing_s", "elapsed_s"):
        reported = _number_value(detail.get(key))
        if reported is not None:
            return reported + age, age
    return age, age


def _pick_instance(endpoint: dict, payload: dict) -> Optional[dict]:
    """The one reported process that belongs to this playout, matched by its feed name.
    A player that is gone is simply not found, so it can never be mistaken for its sibling
    (the old position-based pick turned the surviving player into "USA" when USA died)."""
    if endpoint["kind"] != "feed":
        return None
    instances = [inst for inst in payload.get("instances", []) if isinstance(inst, dict)]
    matches = [inst for inst in instances if str(inst.get("feed", "")).lower() == endpoint["feed"]]
    # If a sender ever reports two processes for one feed (an engine plus its client window),
    # the one that carries the activity measurement is the engine.
    matches.sort(key=lambda inst: 0 if "advance_age_seconds" in inst else 1)
    return matches[0] if matches else None


def _first_present(holders: tuple, *keys: str):
    """The first non-empty value of any of `keys`, looking through each holder dict in turn."""
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        for key in keys:
            value = holder.get(key)
            if value not in (None, ""):
                return value
    return None


def _aware_utc(value) -> Optional[datetime]:
    """An ISO time as an aware UTC datetime; a time with no zone is taken to be UTC (what the senders mean)."""
    parsed = _parse_utc(value)
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---- Pauses and the position in an item ---------------------------------------------------------------------------------------------------
# Insta (MAIN, SITE-B) and AdMax (BACKUP) resume a paused item WHERE IT STOPPED, so a bar drawn from the true start would run ahead by the length of
# the pause. SITE-B's detector moves the start forward itself (learned 2026-10-14, see the unpublished design notes). For MAIN and BACKUP-SERVER, Core counts
# the time each player spent quiet (no media reads: a pause, or a stall) per item, and takes it off the position. The count travels INSIDE the stored
# heartbeat as `_pause`, so it survives a listener restart and needs no new table. Short quiet spells (under 15 s) are not counted: a few seconds of drift.
PAUSE_MIN_SECONDS = 15.0
SUSPECT_FRESH_SECONDS = 90.0         # a heartbeat younger than this must have a believable start
SUSPECT_OVERRUN_SECONDS = 900.0      # an item more than 15 minutes past its declared length is a wrong start, not an overrun
SUSPECT_EARLY_SECONDS = 120.0        # a start more than 2 minutes in the future is a wrong start
# A sender that labels its LOCAL clock digits with a Z (SITE-A's first detector build: EDT, so four hours early) is out by whole hours. For an item
# under 55 minutes exactly one whole-hour shift puts the position inside the item, so the start can be put right with no guess: the true position is in
# [0, length + 2 min], which spans less than the 3600 s between candidates. For a longer item two shifts could fit, so there the listener uses the shift it
# REMEMBERS for that sender (`_hour_hint`, learned from its short items, stored with the heartbeat so it survives a restart) and applies it only if the
# result is believable. Until a short item has been seen, ASSUMED_LABEL_HOURS stands in (SITE-A's detector v1.3.0 on a PC whose clock is EDT); a sender
# that has been put right says so with its next short item (hint 0), which switches the assumption off.
HOUR_FIX_MAX_SECONDS = 3300.0
ASSUMED_LABEL_HOURS = {"SITE-A": 4}


def _whole_hour_shift(position: float, duration: float) -> int:
    """The whole-hour shift that puts `position` (seconds since the labelled start) inside an item of `duration` seconds, or 0 when none does."""
    hours = round((position - duration / 2.0) / 3600.0)
    if hours != 0 and abs(hours) <= 14 and -60.0 <= position - 3600.0 * hours <= duration + 120.0:
        return hours
    return 0


def _start_is_believable(position: float, duration: float) -> bool:
    return -SUSPECT_EARLY_SECONDS <= position <= duration + SUSPECT_OVERRUN_SECONDS


def _with_hour_hint(payload: dict, previous: Optional[dict]) -> dict:
    """Stores `_hour_hint` in a heartbeat: the whole-hour shift this sender's item starts were last found to be out by (0: they are right). Only an
    item under 55 minutes can show it with certainty; any other heartbeat carries the previous value forward."""
    try:
        before = previous.get("_hour_hint") if isinstance(previous, dict) else None
        hint = before if isinstance(before, int) and not isinstance(before, bool) else None
        started = _aware_utc(_first_present((payload,), "current_item_started_utc", "current_item_started_at"))
        duration = _number_value(_first_present((payload,), "current_item_duration_s"))
        fix_time = _aware_utc(payload.get("timestamp"))
        if started is not None and fix_time is not None and duration is not None and 0 < duration <= HOUR_FIX_MAX_SECONDS:
            position = (fix_time - started).total_seconds()
            if _start_is_believable(position, duration):
                hint = 0
            else:
                shift = _whole_hour_shift(position, duration)
                if shift:
                    hint = shift
        if hint is not None:
            payload["_hour_hint"] = hint
    except Exception:  # noqa: BLE001 -- this bookkeeping must never fail a heartbeat
        pass
    return payload


def _payload_has_item(payload: dict) -> bool:
    """True when the heartbeat names something playing, in any of the fields the dashboard's now-playing line reads (a blank or non-text value names nothing)."""
    candidate = payload.get("now_playing")
    if isinstance(candidate, dict) and isinstance(candidate.get("filename"), str) and candidate["filename"].strip():
        return True
    return any(isinstance(payload.get(key), str) and payload[key].strip() for key in ("current_item", "current_program", "program", "title", "filename"))


def _media_is_active(payload: dict) -> bool:
    """True only for the JSON true: the detector says a player decoded media in the last 15 seconds. Text, numbers, null or a missing field never count."""
    return payload.get("media_active") is True


def _idle_start_before(previous: Optional[dict], now: datetime) -> Optional[datetime]:
    """When the previous stored heartbeat says the idle spell began, or None when it says nothing usable (missing, damaged, not text, or in the future)."""
    try:
        raw = previous.get("_idle_since") if isinstance(previous, dict) else None
        since = _aware_utc(raw) if isinstance(raw, str) else None
    except Exception:  # noqa: BLE001 -- a damaged old value only restarts the clock
        return None
    return since if since is not None and since <= now else None


def _with_idle_mark(payload: dict, previous: Optional[dict], source_id: str, now: datetime) -> dict:
    """Stores `_idle_since` in the heartbeat of a source in IDLE_ALARM_SOURCES while its player says it is running and the heartbeat names no item: the time that
    spell began (carried from the previous heartbeat, else now). Any other heartbeat carries no mark. The sender's own `_idle_since`, if it sends one, is thrown
    away first: only Core's clock counts. Stored with the heartbeat, so it survives a restart and needs no table."""
    payload.pop("_idle_since", None)
    try:
        if source_id in IDLE_ALARM_SOURCES and payload.get("player_running") is True and not _payload_has_item(payload) and not _media_is_active(payload):
            payload["_idle_since"] = (_idle_start_before(previous, now) or now).isoformat()
    except Exception:  # noqa: BLE001 -- this bookkeeping must never fail a heartbeat
        payload.pop("_idle_since", None)
    return payload


def _feed_is_down(payload: dict, feed: str) -> bool:
    """True when this feed's player is not in the heartbeat, or its launcher or its engine is reported down (the same three conditions the tile judges)."""
    wanted = str(feed).lower()
    matches = [inst for inst in (payload.get("instances") or []) if isinstance(inst, dict) and str(inst.get("feed", "")).lower() == wanted]
    if not matches:
        return True
    matches.sort(key=lambda inst: 0 if "advance_age_seconds" in inst else 1)
    return matches[0].get("engine_present") is False or matches[0].get("launcher_present") is False


def _with_absent_marks(payload: dict, previous: Optional[dict], expectation, now: datetime) -> dict:
    """Stores `_absent` in the heartbeat of a machine whose players are named (a list of feeds): per feed that is down right now, when Core first saw it down (carried from the
    previous heartbeat while it stays down, else now). A feed that is back loses its clock. The sender's own `_absent` is thrown away first: only Core's clock counts. Stored with
    the heartbeat, so it survives a restart and needs no table."""
    payload.pop("_absent", None)
    try:
        if isinstance(expectation, (list, tuple)):
            before = previous.get("_absent") if isinstance(previous, dict) and isinstance(previous.get("_absent"), dict) else {}
            marks = {}
            for feed in expectation:
                name = str(feed).lower()
                if not _feed_is_down(payload, name):
                    continue
                raw = before.get(name)
                try:
                    since = _aware_utc(raw) if isinstance(raw, str) else None
                except Exception:  # noqa: BLE001
                    since = None
                marks[name] = (since if since is not None and since <= now else now).isoformat()
            if marks:
                payload["_absent"] = marks
    except Exception:  # noqa: BLE001 -- this bookkeeping must never fail a heartbeat
        payload.pop("_absent", None)
    return payload


def _down_seconds(endpoint: dict, payload: dict, now: datetime) -> Optional[float]:
    """For a feed endpoint whose player is down: how long it has been down (0 when it has no clock yet). None for everything else."""
    if endpoint["kind"] != "feed" or not _feed_is_down(payload, endpoint["feed"]):
        return None
    return _absent_for(payload.get("_absent"), endpoint["feed"], now)


def _remembered_shift(payload: dict, source: Optional[dict]) -> int:
    hint = payload.get("_hour_hint")
    if isinstance(hint, int) and not isinstance(hint, bool):
        return hint
    return ASSUMED_LABEL_HOURS.get(str((source or {}).get("source_id") or ""), 0)


def _instance_item_key(instance: dict) -> Optional[str]:
    """Which item an instance is playing, for the pause count: its file name plus its start. None when either is unknown."""
    candidate = instance.get("now_playing")
    if not isinstance(candidate, dict) or not candidate.get("filename"):
        return None
    if candidate.get("estimated") is True:
        return None   # a schedule estimate is wall-clock, not a player position: no pause arithmetic applies to it
    started = _aware_utc(_first_present((candidate,), "started_utc", "started_at"))
    if started is None:
        return None
    return f"{_clip_text(candidate['filename'])}|{started.isoformat()}"


def _with_pause_marks(payload: dict, previous: Optional[dict]) -> dict:
    """Adds `_pause` to a heartbeat: per feed, the item, the seconds already paused (`closed_s`) and the pause in progress (`open_s`).
    `previous` is the last stored heartbeat of the same sender. A quiet spell counts as a pause when it is at least 15 s old and the sender flags
    it as one (`pause_quiet`) or says the engine is not advancing. A new item starts from zero."""
    instances = payload.get("instances")
    if not isinstance(instances, list):
        return payload
    before = previous.get("_pause") if isinstance(previous, dict) and isinstance(previous.get("_pause"), dict) else {}
    marks = {}
    for instance in instances:
        if not isinstance(instance, dict):
            continue
        feed = str(instance.get("feed") or "")
        key = _instance_item_key(instance)
        if not feed or key is None:
            continue
        prior = before.get(feed) if isinstance(before.get(feed), dict) else {}
        same_item = prior.get("item") == key
        closed = (_number_value(prior.get("closed_s")) or 0.0) if same_item else 0.0
        opened = (_number_value(prior.get("open_s")) or 0.0) if same_item else 0.0
        age = _number_value(instance.get("advance_age_seconds"))
        quiet = age is not None and age >= PAUSE_MIN_SECONDS and (instance.get("pause_quiet") is True or instance.get("advancing") is False)
        if quiet:
            opened = age
        else:
            closed += opened
            opened = 0.0
        marks[feed] = {"item": key, "closed_s": round(min(closed, MAX_ITEM_SECONDS), 1), "open_s": round(min(opened, MAX_ITEM_SECONDS), 1)}
    if marks:
        payload["_pause"] = marks
    return payload


def _pause_seconds(payload: dict, instance: Optional[dict]) -> tuple[float, bool]:
    """(seconds this item has spent paused so far, whether it is paused right now), from the marks stored with the heartbeat."""
    marks = payload.get("_pause")
    if not instance or not isinstance(marks, dict):
        return 0.0, False
    mark = marks.get(str(instance.get("feed") or ""))
    key = _instance_item_key(instance)
    if not isinstance(mark, dict) or key is None or mark.get("item") != key:
        return 0.0, False
    closed = _number_value(mark.get("closed_s")) or 0.0
    opened = _number_value(mark.get("open_s")) or 0.0
    return closed + opened, opened > 0


def _pause_event_open(events: list[dict], now: datetime) -> bool:
    """True while the newest `paused` event among these is open (not ended) and still fresh: SITE-B's detector reports a pause of 15 s or more as
    `playout_paused` and refreshes it every 30 s, long before the 45 s amber ladder shows anything on the tile."""
    newest = None
    for event in events:
        if "paused" not in str(event.get("event_type", "")).lower():
            continue
        stamp = _parse_utc(event.get("timestamp_utc"))
        if stamp is not None and (newest is None or stamp > newest[0]):
            newest = (stamp, event)
    if newest is None:
        return False
    event = newest[1]
    if str(_json_obj(event.get("detail")).get("phase", "")).lower() in {"end", "resolved", "complete"}:
        return False
    age = _age_seconds(event.get("timestamp_utc"), now)
    return age is not None and age <= ONGOING_FRESH_SECONDS


def _now_playing_timing(payload: dict, instance: Optional[dict], candidate: Optional[dict], source: Optional[dict], now: Optional[datetime],
                        pause_open: bool = False) -> dict:
    """How old the displayed file name is, and where it is in the item, so the dashboard never presents a
    stale name as fact. `age_s` is how long ago Core last heard the sender. Optional sender fields
    (documented in the unpublished design notes): when the item started, its length and the next item. Inside a
    `now_playing` object they are `started_utc`, `duration_s`, `next`; at the top of a heartbeat they carry
    the `current_item_` prefix (`current_item_started_utc`, `current_item_duration_s`, `next_item`), so an
    unrelated field with a similar name is never mistaken for them. The position in the item is measured
    between the sender's OWN two timestamps when it sends them, so a wrong clock on the sender cannot make
    an item look finished early. While the item is paused the position stands still (no time since the
    heartbeat is added) and the time already paused is taken off. A start that cannot be true for a fresh
    heartbeat (hours early, or in the future: a clock label with the wrong zone) is ignored, not drawn."""
    reported = source.get("last_seen_utc") if source else None
    age = _age_seconds(reported, now) if (reported and now) else None
    outer = (instance, payload)
    started = _aware_utc(
        _first_present((candidate,), "started_utc", "started_at")
        or _first_present(outer, "current_item_started_utc", "current_item_started_at")
    )
    duration = _number_value(
        _first_present((candidate,), "duration_s") or _first_present(outer, "current_item_duration_s")
    )
    if duration is not None and not (0 < duration <= MAX_ITEM_SECONDS):
        duration = None
    paused_s, paused_now = _pause_seconds(payload, instance)
    paused = bool(pause_open or paused_now)
    corrected_hours = 0
    fix_time = _aware_utc(payload.get("timestamp"))
    if started is not None and duration is not None and fix_time is not None and age is not None and age <= SUSPECT_FRESH_SECONDS:
        position = (fix_time - started).total_seconds()
        if not _start_is_believable(position, duration):
            if duration <= HOUR_FIX_MAX_SECONDS:
                hours = _whole_hour_shift(position, duration)
            else:
                # two shifts could fit: use the one this sender is known to have, and only if it makes the start believable
                remembered = _remembered_shift(payload, source)
                hours = remembered if remembered and -60.0 <= position - 3600.0 * remembered <= duration + 120.0 else 0
            if hours:
                started = started + timedelta(hours=hours)
                corrected_hours = hours
    elapsed = None
    if started is not None:
        sender_time = _aware_utc(payload.get("timestamp"))
        if sender_time is not None:
            elapsed = (sender_time - started).total_seconds() + (0.0 if paused else (age or 0.0)) - paused_s
        elif now is not None:
            elapsed = (now - started).total_seconds() - paused_s
    suspect = False
    if elapsed is not None and age is not None and age <= SUSPECT_FRESH_SECONDS:
        if elapsed < -SUSPECT_EARLY_SECONDS or (duration is not None and elapsed > duration + SUSPECT_OVERRUN_SECONDS):
            suspect = True
            elapsed = None
    next_item = _first_present((candidate,), "next") or _first_present(outer, "next_item")
    return {
        "reported_utc": reported,
        "age_s": round(age, 1) if age is not None else None,
        "started_utc": started.isoformat() if started else None,
        "duration_s": duration,
        "elapsed_s": round(min(max(0.0, elapsed), 30 * 24 * 3600.0), 1) if elapsed is not None else None,
        "paused": paused,
        "estimated": bool(isinstance(candidate, dict) and candidate.get("estimated") is True),
        "timing_suspect": suspect,
        "timing_corrected_hours": corrected_hours,
        "next": _clip_text(next_item) if isinstance(next_item, (str, int, float)) and str(next_item).strip() else None,
    }


def _now_playing(payload: dict, instance: Optional[dict], source: Optional[dict] = None, now: Optional[datetime] = None, pause_open: bool = False) -> dict:
    for candidate in ((instance or {}).get("now_playing"), payload.get("now_playing")):
        if isinstance(candidate, dict) and candidate.get("filename"):
            detail = _clip_text(candidate.get("result") or "")
            duration = candidate.get("duration")
            if isinstance(duration, str) and duration and not duration.startswith("-"):
                detail = (detail + " / " if detail else "") + f"{_clip_text(duration, 40)} long"
            return {"title": _clip_text(candidate["filename"]), "detail": _clip_text(detail or "Reported by heartbeat"),
                    **_now_playing_timing(payload, instance, candidate, source, now, pause_open)}
    for key in ("current_item", "current_program", "program", "title", "filename"):
        if isinstance(payload.get(key), str) and payload[key].strip():
            return {"title": _clip_text(payload[key]), "detail": "Reported by heartbeat",
                    **_now_playing_timing(payload, instance, None, source, now, pause_open)}
    return {"title": "Not reported by sender", "detail": "No normalized now-playing field in this heartbeat",
            **_now_playing_timing(payload, instance, None, source, now, pause_open)}


def _pid_text(instance: Optional[dict], payload: dict) -> str:
    values = []
    if instance:
        for label, key in (("launcher", "launcher_pid"), ("engine", "engine_pid"), ("", "pid_value"), ("", "pid")):
            if instance.get(key):
                values.append(f"{label} {instance[key]}" if label else str(instance[key]))
    if not values:
        for key in ("pid", "detector_pid", "engine_pid", "process_id"):
            if payload.get(key):
                values.append(str(payload[key]))
    return ", ".join(values) if values else "not reported"


def _engine_text(instance: Optional[dict], source: Optional[dict], payload: dict) -> str:
    if instance:
        if instance.get("engine_present") is False:
            return "engine halted"
        if instance.get("launcher_present") is False:
            return "launcher missing"
        if instance.get("process_name"):
            return f"running: {instance['process_name']}"
        return "process pair running"
    return str(payload.get("engine_state") or payload.get("state") or payload.get("status") or (source and source.get("state")) or "not reporting")


def _event_status(event: Optional[dict], now: datetime) -> Optional[dict]:
    if not event:
        return None
    event_type = str(event.get("event_type", "")).lower()
    if event_type in INTERNAL_STATUS_EVENT_TYPES:
        return None
    detail = _json_obj(event.get("detail"))
    phase = str(detail.get("phase", "")).lower()
    if phase in {"end", "resolved", "complete"}:
        return None
    severity = str(event.get("severity", "info")).lower()
    critical_signal = any(word in event_type for word in (
        "freeze", "frozen", "paused", "stale", "not_advancing",
        "black", "silence", "off_air", "off-air", "offair",
    )) or any(word in event_type for word in PLAYER_DOWN_WORDS)
    if critical_signal:
        title = title_case_event(event_type)
        active, age = _fault_active_seconds(detail, event, now)
        if age > ONGOING_FRESH_SECONDS:
            # Opened but never refreshed and never closed. That no longer proves the fault is
            # still happening, and a lost "end" event must not hold a tile on alarm for hours.
            if age <= ONGOING_UNCONFIRMED_SECONDS:
                return {
                    "level": "warning", "label": "Unconfirmed", "root_cause": f"{title} - no end event",
                    "detail": f"Detector reported {title} {age / 60:.0f} min ago and never reported it ended, so it is not treated as active.",
                    "email_eligible": False,
                }
            return None
        if any(word in event_type for word in PLAYER_DOWN_WORDS):
            return {"level": "critical", "label": "Player Down", "root_cause": "Player Not Running",
                    "detail": "Detector reports the playout program is not running.", "email_eligible": True}
        if "off" in event_type:
            if str(detail.get("cause", "")).lower() == "black_freeze_silence" and active < DEAD_AIR_RED_SECONDS:
                return {"level": "warning", "label": "Warning", "root_cause": f"Dead Air < {DEAD_AIR_RED_SECONDS}s",
                        "detail": f"Black, frozen and silent for {active:.0f}s so far. A programme change looks the same, so it turns red at {DEAD_AIR_RED_SECONDS}s.",
                        "email_eligible": False}
            return {"level": "critical", "label": "Critical Alarm", "root_cause": "Off-Air Status", "detail": "Detector reports an active off-air condition."}
        if active >= CRITICAL_OVERDUE_SECONDS:
            return {"level": "critical", "label": "Critical Alarm", "root_cause": f"{title} > {CRITICAL_OVERDUE_SECONDS}s", "detail": f"Detector reports {active:.0f}s active duration."}
        if active >= WARNING_OVERDUE_SECONDS:
            return {"level": "warning", "label": "Warning", "root_cause": f"{title} > {WARNING_OVERDUE_SECONDS}s", "detail": f"Detector reports {active:.0f}s active duration."}
        if severity in {"critical", "major"}:
            return {"level": "critical", "label": "Critical Alarm", "root_cause": title, "detail": "Detector reports an active critical event."}
        return None  # shorter than the yellow threshold: recorded in the feed, not on the tile
    if severity in {"critical", "major"}:
        return {"level": "critical", "label": "Critical Alarm", "root_cause": title_case_event(event_type), "detail": "Detector reports an active critical event."}
    if severity in {"warning", "minor"}:
        title = title_case_event(event_type)
        age = _age_seconds(event.get("timestamp_utc"), now) or 0.0
        if age > ONGOING_FRESH_SECONDS:
            # The same rule as for freeze, black, silence and pause above. An ordinary warning event that is never refreshed and never
            # closed must not hold a tile amber for hours: SITE-B sat on "Transition Stuck Suspected" for about ten hours after the
            # playlist had long moved on, because its detector posted that one event and nothing to close it.
            if age <= ONGOING_UNCONFIRMED_SECONDS:
                return {
                    "level": "warning", "label": "Unconfirmed", "root_cause": f"{title} - no end event",
                    "detail": f"Detector reported {title} {age / 60:.0f} min ago and never reported it ended, so it is not treated as active.",
                    "email_eligible": False,
                }
            return None
        return {"level": "warning", "label": "Warning", "root_cause": title, "detail": "Detector reports an active warning event."}
    return None


def _event_matches_endpoint(event: dict, endpoint: dict) -> bool:
    if event.get("source_id") != endpoint["source_id"]:
        return False
    if endpoint["kind"] == "source":
        return True

    detail = _json_obj(event.get("detail"))
    scoped_values = [
        detail.get("endpoint"),
        detail.get("endpoint_id"),
        detail.get("feed"),
        detail.get("channel"),
        detail.get("playout"),
    ]
    scoped_text = " ".join(str(value).lower() for value in scoped_values if value)
    endpoint_tokens = {
        str(endpoint.get("endpoint_id", "")).lower(),
        str(endpoint.get("display_name", "")).lower(),
        str(endpoint.get("feed_label", "")).lower(),
    }
    if endpoint.get("feed"):
        endpoint_tokens.add(str(endpoint["feed"]).lower())
    if scoped_text:
        return any(token and token in scoped_text for token in endpoint_tokens)

    text = " ".join([
        str(event.get("event_type", "")).lower(),
        _detail_text(event),
    ])
    if endpoint["kind"] == "feed":
        feed = str(endpoint["feed"]).lower()
        label = str(endpoint["feed_label"]).lower()
        return f"{feed} feed" in text or f"{label} feed" in text or endpoint["display_name"].lower() in text
    return False


def _endpoint_events(events: list[dict], endpoint: dict) -> list[dict]:
    return [event for event in events if _event_matches_endpoint(event, endpoint)]


def _latest_endpoint_status_event(events: list[dict], now: datetime) -> tuple[Optional[dict], Optional[dict]]:
    closed_types: set[str] = set()
    for event in events:
        event_type = str(event.get("event_type", "")).lower()
        if event_type in INTERNAL_STATUS_EVENT_TYPES:
            continue
        detail = _json_obj(event.get("detail"))
        phase = str(detail.get("phase", "")).lower()
        if phase in {"end", "resolved", "complete"}:
            closed_types.add(event_type)
            continue
        if event_type in closed_types:
            continue
        status = _event_status(event, now)
        if status:
            return event, status
    return None, None


def _heartbeat_status(source: Optional[dict], now: datetime) -> dict:
    if not source or not source.get("last_seen_utc"):
        return {"level": "critical", "label": "Player Down", "root_cause": "Heartbeat Missing", "detail": "No heartbeat has reached Core.", "email_eligible": False}
    expected = int(source.get("expected_interval_seconds") or HEARTBEAT_INTERVAL_SECONDS)
    overdue = (_age_seconds(source.get("last_seen_utc"), now) or 0) - expected
    if source.get("state") == "heartbeat_lost" or overdue >= CRITICAL_OVERDUE_SECONDS:
        return {"level": "critical", "label": "Player Down", "root_cause": f"Heartbeat Timeout > {CRITICAL_OVERDUE_SECONDS}s", "detail": f"Last heartbeat is {overdue:.0f}s past the expected interval.", "email_eligible": True}
    if overdue >= WARNING_OVERDUE_SECONDS:
        return {"level": "warning", "label": "Warning", "root_cause": f"Heartbeat Delay > {WARNING_OVERDUE_SECONDS}s", "detail": f"Last heartbeat is {overdue:.0f}s past the expected interval.", "email_eligible": False}
    return {"level": "healthy", "label": "Healthy", "root_cause": "Normal", "detail": "Heartbeat is inside the expected cadence.", "email_eligible": False}


def _instance_status(endpoint: dict, instance: Optional[dict], down_for: Optional[float] = 0.0) -> Optional[dict]:
    """A feed whose player is missing, whose engine is halted or whose launcher is gone. For the first INSTANCE_DOWN_GRACE_SECONDS (`down_for`, 0 when it has no clock yet) it is a
    WARNING that is never mailed; after that it is the critical it always was."""
    if endpoint["kind"] != "feed":
        return None
    if not instance:
        cause, detail = "Process Missing", "Expected playout process is absent from the heartbeat."
    elif instance.get("engine_present") is False:
        cause, detail = "Engine Halted", "The launcher reported that the playout engine is not running."
    elif instance.get("launcher_present") is False:
        cause, detail = "Launcher Missing", "The expected launcher process is not running."
    else:
        return None
    if down_for is not None and down_for < INSTANCE_DOWN_GRACE_SECONDS:
        return {"level": "warning", "label": "Warning", "root_cause": f"{cause} < {INSTANCE_DOWN_GRACE_SECONDS}s",
                "detail": f"{detail} Gone for {down_for:.0f}s; it becomes critical if it lasts {INSTANCE_DOWN_GRACE_SECONDS}s.", "email_eligible": False}
    return {"level": "critical", "label": "Player Down", "root_cause": cause, "detail": detail, "email_eligible": True}


def _source_player_status(endpoint: dict, payload: dict) -> Optional[dict]:
    """SITE-A and SITE-B are judged from their own detector. A detector that watches its playout program says `player_running: false` in
    its heartbeat the moment the program is gone, and that is an immediate red (the heartbeat itself keeps arriving, so nothing else would
    notice). Only a real JSON false counts: text, 0 or a missing field never raise an alarm. MAIN and BACKUP use their own instance checks."""
    if endpoint["kind"] != "source" or payload.get("player_running") is not False:
        return None
    return {"level": "critical", "label": "Player Down", "root_cause": "Player Not Running",
            "detail": "The site's detector reports that the playout program is not running.", "email_eligible": True}


def _source_idle_status(endpoint: dict, payload: dict, now: datetime) -> Optional[dict]:
    """The player says it is running but has named nothing playing for IDLE_ALARM_SECONDS (see the constant). Only for the sources listed there; a player that says it
    is NOT running is the separate Player Not Running alarm, and a heartbeat that says nothing about it never raises this one."""
    if endpoint["kind"] != "source" or endpoint["source_id"] not in IDLE_ALARM_SOURCES:
        return None
    if payload.get("player_running") is not True or _payload_has_item(payload) or _media_is_active(payload):
        return None
    raw = payload.get("_idle_since")
    since = _aware_utc(raw) if isinstance(raw, str) else None
    if since is None:
        return None
    idle = (now - since).total_seconds()
    if idle < IDLE_ALARM_SECONDS:
        return None
    return {"level": "critical", "label": "Critical Alarm", "root_cause": f"No Item Playing > {IDLE_ALARM_SECONDS}s",
            "detail": f"The player is running but its heartbeat has named nothing playing for {idle:.0f}s (alarm after {IDLE_ALARM_SECONDS}s). Nothing may be on air.",
            "email_eligible": True}


OFF_AIR_SIGNATURE = ("black", "freeze", "silence")


def _off_air_signature(endpoint: dict, events: list[dict], now: datetime) -> Optional[dict]:
    """Black, frozen AND silent at the same time: the output carries nothing, which is what a switched-off player or a lost feed looks like to a
    detector that decodes the output (SITE-A). It used to wait for any ONE marker to reach 60 s and then raised a separate red for each (Freeze,
    Black, Silence: one email apiece). Black plus freeze alone is deliberately NOT enough: SITE-A's history holds real black-and-frozen episodes of
    16 s and 28 s with no silence, which must stay quiet. The third marker (silence, posted at 45 s) makes it red, under ONE cause, so the whole
    episode is one alarm and one email. `events` are this endpoint's events, newest first."""
    if endpoint["kind"] != "source":
        return None
    for kind in OFF_AIR_SIGNATURE:
        latest = next((event for event in events if str(event.get("event_type", "")).lower() == kind), None)
        if latest is None:
            return None
        if str(_json_obj(latest.get("detail")).get("phase", "")).lower() in {"end", "resolved", "complete"}:
            return None
        if str(latest.get("severity", "")).lower() not in {"warning", "minor", "major", "critical"}:
            return None
        if (_age_seconds(latest.get("timestamp_utc"), now) or 0.0) > ONGOING_FRESH_SECONDS:
            return None      # not refreshed lately: no longer proof that it is still happening
    return {"level": "critical", "label": "Critical Alarm", "root_cause": "Off Air",
            "detail": "Black, frozen and silent together: nothing is on the output (the player or its feed is switched off).", "email_eligible": True}


def _advancement_status(fields: Optional[dict], now: datetime) -> Optional[dict]:
    if not fields:
        return None
    if fields.get("advance_alarm_confirmed") is not True:
        return None
    age = None
    for key in ("advance_age_seconds", "not_advancing_seconds", "stale_seconds", "playlist_age_seconds", "log_age_seconds"):
        age = _number_value(fields.get(key))
        if age is not None:
            break
    if age is None:
        for key in ("advance_last_write_utc", "last_advance_utc", "playlist_last_write_utc", "log_last_write_utc"):
            age = _age_seconds(fields.get(key), now)
            if age is not None:
                break

    advancing = fields.get("advancing")
    status_text = str(fields.get("advance_status") or fields.get("playout_motion") or fields.get("motion_state") or "").lower()
    stale_by_flag = advancing is False or status_text in {"stale", "paused", "frozen", "not advancing", "not_advancing", "halted"}
    if age is None and not stale_by_flag:
        return None
    if age is None:
        age = CRITICAL_OVERDUE_SECONDS
    cpu = _number_value(fields.get("engine_cpu_cores"))
    reads = _number_value(fields.get("media_reads_mb_per_s"))
    figures = []
    if cpu is not None:
        figures.append(f"engine CPU {cpu:.2f} cores")
    if reads is not None:
        figures.append(f"media reads {reads:.2f} MB/s")   # a paused player keeps its CPU busy but stops reading its media
    basis = f" ({', '.join(figures)})" if figures else ""
    if age >= CRITICAL_OVERDUE_SECONDS:
        return {
            "level": "critical",
            "label": "Critical Alarm",
            "root_cause": f"Paused/Frozen > {CRITICAL_OVERDUE_SECONDS}s",
            "detail": f"The playout engine has shown no activity for {age:.0f}s{basis}. The player may be paused or frozen.",
            "email_eligible": True,
        }
    if age >= WARNING_OVERDUE_SECONDS:
        return {
            "level": "warning",
            "label": "Warning",
            "root_cause": f"Paused/Frozen > {WARNING_OVERDUE_SECONDS}s",
            "detail": f"The playout engine has shown no activity for {age:.0f}s{basis}.",
            "email_eligible": False,
        }
    return None


def _motion_confirmed(fields: Optional[dict]) -> bool:
    """True only when the edge sender itself vouches that this player is actively advancing:
    steady engine activity for CLEAR_CONFIRM_SECONDS, from an engine up at least the startup
    grace. A restart alone can never satisfy this, which is what keeps a critical alarm from
    clearing just because a process came back."""
    if not fields or fields.get("advance_alarm_confirmed") is not True or fields.get("advancing") is not True:
        return False
    steady = _number_value(fields.get("advance_confirmed_seconds"))
    uptime = _number_value(fields.get("engine_uptime_seconds"))
    return (
        steady is not None and steady >= CLEAR_CONFIRM_SECONDS
        and uptime is not None and uptime >= ENGINE_STARTUP_GRACE_SECONDS
    )


def _motion_text(endpoint: dict, payload: dict, fields: Optional[dict], held: bool) -> str:
    """One plain sentence for the tile: what, exactly, is being watched for motion here."""
    if endpoint["kind"] == "source":
        if endpoint["endpoint_id"] == "SITE-A":
            return "Decoded output checks (freeze, black, silence)"
        return "Playlist timing only, not the picture"
    capable = isinstance(payload.get("activity_monitor"), dict)
    if not capable or not fields or fields.get("advance_alarm_confirmed") is not True:
        return "Process presence only (motion not measured)"
    age = _number_value(fields.get("advance_age_seconds"))
    cpu = _number_value(fields.get("engine_cpu_cores"))
    if held:
        return f"Alarm held: waiting for {CLEAR_CONFIRM_SECONDS}s of confirmed playout"
    if age is not None and age >= WARNING_OVERDUE_SECONDS:
        if fields.get("pause_quiet") is True:
            return f"Paused or stopped: no media read for {age:.0f}s"
        return f"No engine activity for {age:.0f}s"
    if _motion_confirmed(fields):
        return f"Advancing (engine {cpu:.1f} cores)" if cpu is not None else "Advancing"
    return "Confirming activity after start"


def _advancement_rows(fields: Optional[dict], now: datetime) -> list[dict]:
    if not fields:
        return []
    rows = []
    age = None
    for key in ("advance_age_seconds", "not_advancing_seconds", "stale_seconds", "playlist_age_seconds", "log_age_seconds"):
        age = _number_value(fields.get(key))
        if age is not None:
            break
    if age is None:
        for key in ("advance_last_write_utc", "last_advance_utc", "playlist_last_write_utc", "log_last_write_utc"):
            age = _age_seconds(fields.get(key), now)
            if age is not None:
                break
    if age is not None:
        rows.append({"label": "Advancement age", "value": f"{age:.0f}s"})
    cpu = _number_value(fields.get("engine_cpu_cores"))
    if cpu is not None:
        rows.append({"label": "Engine CPU", "value": f"{cpu:.2f} cores in the last interval"})
    steady = _number_value(fields.get("advance_confirmed_seconds"))
    if steady is not None:
        rows.append({"label": "Steady activity", "value": f"{steady:.0f}s"})
    source = fields.get("advance_source") or fields.get("motion_source") or fields.get("playlist_path") or fields.get("log_path")
    if source:
        rows.append({"label": "Motion source", "value": str(source)})
    last_write = fields.get("advance_last_write_utc") or fields.get("last_advance_utc") or fields.get("playlist_last_write_utc") or fields.get("log_last_write_utc")
    if last_write:
        rows.append({"label": "Last motion", "value": str(last_write)})
    return rows


def _event_rows_for_endpoint(events: list[dict], endpoint: dict, limit: int = 4) -> list[dict]:
    rows = []
    for event in events:
        if not _event_matches_endpoint(event, endpoint):
            continue
        detail = _json_obj(event.get("detail"))
        rows.append({
            "event_type": title_case_event(event.get("event_type", "")),
            "severity": event.get("severity", "info"),
            "timestamp_utc": event.get("timestamp_utc"),
            "message": detail.get("basis") or detail.get("reason") or detail.get("summary") or detail.get("message") or title_case_event(event.get("event_type", "")),
        })
        if len(rows) >= limit:
            break
    return rows


def _apply_hold(endpoint: dict, status: dict, stored: Optional[dict], payload: dict, fields: Optional[dict]) -> tuple[dict, bool]:
    """Keeps a critical alarm critical until the edge confirms the player is advancing again.

    Only for endpoints whose sender measures engine activity (it announces that with an
    `activity_monitor` block): for a sender that cannot give that confirmation, holding would
    mean an alarm that never clears. Returns (status, held)."""
    if not endpoint.get("latched") or not isinstance(payload.get("activity_monitor"), dict):
        return status, False
    if not stored or stored.get("alert_level") != "critical" or status["level"] == "critical":
        return status, False
    if _motion_confirmed(fields):
        return status, False
    return {
        "level": "critical",
        "label": "Critical Alarm",
        "root_cause": stored.get("root_cause") or "Alarm held",
        "detail": (
            "Alarm held: it clears only when the edge confirms this playout is advancing again "
            f"({CLEAR_CONFIRM_SECONDS}s of steady engine activity, engine up at least {ENGINE_STARTUP_GRACE_SECONDS}s). "
            "A process restart alone does not clear it."
        ),
        "email_eligible": False,  # a continuation of the alarm already mailed, never a new one
    }, True


def _endpoint_snapshot(endpoint: dict, source: Optional[dict], recent_events: list[dict], now: datetime, stored: Optional[dict] = None) -> dict:
    payload = _json_obj(source.get("last_payload_json") if source else "")
    instance = _pick_instance(endpoint, payload)
    endpoint_events = _endpoint_events(recent_events, endpoint)
    latest, event_state = _latest_endpoint_status_event(endpoint_events, now)
    if not latest and source and source.get("latest_event") and _event_matches_endpoint(source["latest_event"], endpoint):
        latest = source["latest_event"]
        event_state = _event_status(latest, now)
    heartbeat = _heartbeat_status(source, now)
    instance_state = _instance_status(endpoint, instance, _down_seconds(endpoint, payload, now))
    advancement_basis = instance if instance else payload
    advancement_state = _advancement_status(advancement_basis, now)
    player_state = _source_player_status(endpoint, payload)
    idle_state = _source_idle_status(endpoint, payload, now)
    off_air_state = _off_air_signature(endpoint, endpoint_events, now)
    status_candidates = (heartbeat, instance_state, player_state, idle_state, off_air_state, advancement_state, event_state)
    status = next((state for state in status_candidates if state and state["level"] == "critical"), None)
    status = status or next((state for state in status_candidates if state and state["level"] == "warning"), heartbeat)
    status, held = _apply_hold(endpoint, status, stored, payload, instance)
    now_playing = _now_playing(payload, instance, source, now, _pause_event_open(endpoint_events, now))
    motion_text = _motion_text(endpoint, payload, instance, held)
    diagnostics = [
        {"label": "Interval", "value": f"{source.get('expected_interval_seconds') if source else HEARTBEAT_INTERVAL_SECONDS}s"},
        {"label": "Last heard", "value": source.get("last_seen_utc") if source else "never"},
        {"label": "Clock", "value": _clock_text(source, payload)},
        {"label": "Process IDs", "value": _pid_text(instance, payload)},
        {"label": "Engine state", "value": _engine_text(instance, source, payload)},
        {"label": "Motion", "value": motion_text},
        {"label": "Root cause", "value": status["root_cause"]},
    ]
    diagnostics.extend(_advancement_rows(advancement_basis, now))
    return {
        **endpoint,
        "status_level": status["level"],
        "status_label": status["label"],
        "root_cause": status["root_cause"],
        "status_detail": status["detail"],
        "email_eligible": status.get("email_eligible", True),
        "held": held,
        "motion_text": motion_text,
        "last_seen_utc": source.get("last_seen_utc") if source else None,
        "expected_interval_seconds": source.get("expected_interval_seconds") if source else HEARTBEAT_INTERVAL_SECONDS,
        "now_playing": now_playing,
        "pid_text": _pid_text(instance, payload),
        "engine_state": _engine_text(instance, source, payload),
        "diagnostics": diagnostics,
        "recent_events": _event_rows_for_endpoint(recent_events, endpoint),
    }


def _endpoint_snapshots(sources: list[dict], recent_events: list[dict], now: datetime, stored: Optional[dict] = None) -> list[dict]:
    by_source = {source["source_id"]: source for source in sources}
    stored = stored or {}
    return [
        _endpoint_snapshot(endpoint, by_source.get(endpoint["source_id"]), recent_events, now, stored.get(endpoint["endpoint_id"]))
        for endpoint in PLAYOUT_ENDPOINTS
    ]


def _stored_alerts(conn: sqlite3.Connection) -> dict[str, dict]:
    return {row["endpoint_id"]: dict(row) for row in conn.execute("SELECT * FROM endpoint_alerts").fetchall()}


def _evaluate_and_sync_locked(conn: sqlite3.Connection, now_dt: datetime, now_iso: str):
    """One pass of the whole judgement: snapshot every source, judge the six playouts against what
    was stored last time (so a held alarm stays held), persist the result, and return who needs a
    critical email. Caller must hold `_db_lock`."""
    sources = _sources_snapshot(conn)
    recent_events = _recent_events_snapshot(conn, 50)
    stored = _stored_alerts(conn)
    endpoints = _endpoint_snapshots(sources, recent_events, now_dt, stored)
    emails = _sync_endpoint_alerts_locked(conn, endpoints, now_iso, stored)
    return sources, recent_events, endpoints, emails


def _sync_endpoint_alerts_locked(conn: sqlite3.Connection, endpoints: list[dict], now_iso: str, stored: Optional[dict] = None) -> list[dict]:
    rows = stored if stored is not None else _stored_alerts(conn)
    emails = []
    for endpoint in endpoints:
        old = rows.get(endpoint["endpoint_id"])
        email_key = f"{endpoint['endpoint_id']}|{endpoint['status_level']}|{endpoint['root_cause']}"
        changed = not old or old["alert_level"] != endpoint["status_level"] or old["root_cause"] != endpoint["root_cause"]
        if changed:
            conn.execute(
                """INSERT INTO endpoint_alerts (endpoint_id, alert_level, root_cause, state_changed_utc, last_email_key)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(endpoint_id) DO UPDATE SET
                     alert_level = excluded.alert_level,
                     root_cause = excluded.root_cause,
                     state_changed_utc = excluded.state_changed_utc,
                     last_email_key = CASE WHEN excluded.alert_level = 'critical' THEN endpoint_alerts.last_email_key ELSE NULL END
                """,
                (endpoint["endpoint_id"], endpoint["status_level"], endpoint["root_cause"], now_iso, None),
            )
        already_sent = old and old.get("last_email_key") == email_key
        if endpoint["status_level"] == "critical" and endpoint.get("email_eligible", True) and not already_sent:
            _insert_event_locked(conn, now_iso, endpoint["source_id"], "endpoint_critical", json.dumps({
                "endpoint": endpoint["display_name"],
                "root_cause": endpoint["root_cause"],
                "status": endpoint["status_label"],
                "message": endpoint["status_detail"],
            }), "critical")
            conn.execute("UPDATE endpoint_alerts SET last_email_key = ? WHERE endpoint_id = ?", (email_key, endpoint["endpoint_id"]))
            emails.append(endpoint)
    return emails


def _send_endpoint_critical_email(endpoint: dict) -> None:
    """Blocking (SMTP), so async callers run it with asyncio.to_thread. The subject and first rows
    name the playout itself and its explicit root cause, so the mail is actionable unopened."""
    send_alert_email(
        endpoint["display_name"],
        "CRITICAL",
        f"{endpoint['display_name']}: {endpoint['root_cause']}",
        [
            ("Playout Name", endpoint["display_name"]),
            ("Root Cause", endpoint["root_cause"]),
            ("Status", endpoint["status_label"]),
            ("Detail", endpoint["status_detail"]),
            ("Motion", endpoint.get("motion_text", "not reported")),
            ("Machine", endpoint["source_id"]),
            ("Now Playing", endpoint["now_playing"]["title"]),
            ("Process IDs", endpoint["pid_text"]),
        ],
        subject_extra=endpoint["root_cause"],
    )
    send_push_notice(
        f"Watchdog: {endpoint['display_name']} CRITICAL",
        f"{endpoint['root_cause']}. Now playing: {endpoint['now_playing']['title']}",
    )


# Read-side limits: the page is public and carries a read-only key, so reads are bounded PER VISITOR on top of the
# shared caps. The visitor is the address Cloudflare reports (CF-Connecting-IP), nothing a visitor can write himself; see guard.visitor_key.
MAX_STATUS_EVENTS = 200
MAX_STREAMS_PER_VISITOR = _env_int("WATCHDOG_MAX_STREAMS_PER_VISITOR", STREAMS_PER_VISITOR, 1, 64)
MAX_STATUS_PER_MINUTE = _env_int("WATCHDOG_MAX_STATUS_PER_MINUTE", STATUS_PER_MINUTE, 5, 6000)
_status_rate = VisitorRate(MAX_STATUS_PER_MINUTE)
# Cloudflare's headers are believed only from a peer inside these networks (the tunnel container's Docker network by default), so a client that reaches
# the port some other way cannot choose its own visitor key by writing them.
try:
    TRUSTED_PROXIES = parse_trusted_proxies(os.environ.get("WATCHDOG_TRUSTED_PROXY_CIDRS"))
except KeyConfigError as error:
    raise RuntimeError(f"invalid trusted proxy setting: {error}") from error


# The container's own gateway (the host, on Docker) read from its routing table, or None if unreadable: its address is never trusted to carry Cloudflare headers.
CONTAINER_GATEWAYS = read_default_gateways()
_untrusted_cf_logged: dict = {}      # peer address -> when it was last noted (once an hour is enough)


def _note_untrusted_cloudflare(peer) -> None:
    now = time.monotonic()
    last = _untrusted_cf_logged.get(peer)
    if last is not None and now - last < 3600:
        return
    if peer not in _untrusted_cf_logged and len(_untrusted_cf_logged) >= 64:
        _untrusted_cf_logged.pop(next(iter(_untrusted_cf_logged)))
    _untrusted_cf_logged[peer] = now
    logger.warning("[visitor] Cloudflare headers arrived from %s, which is not a trusted proxy, and were ignored: either a client is writing them itself, or "
                   "WATCHDOG_TRUSTED_PROXY_CIDRS does not cover the tunnel (then every public viewer counts as one visitor)", peer)


def _visitor_of(request) -> str:
    peer = _peer_of(request)
    key = visitor_key(request.headers, peer, TRUSTED_PROXIES, CONTAINER_GATEWAYS, FORWARDED_TRUST)
    try:
        if key.startswith("peer:") and not FORWARDED_TRUST and request.headers.get("cf-connecting-ip") and request.headers.get("cf-ray"):
            _note_untrusted_cloudflare(str(peer or "unknown")[:64])
    except Exception:  # noqa: BLE001 -- the note is only for the log
        pass
    return key


@app.get("/status", dependencies=[Depends(require_view_token)])
async def status(
    request: Request,
    limit: int = Query(default=50, ge=1, le=MAX_STATUS_EVENTS),
    source: Optional[str] = Query(default=None),
) -> JSONResponse:
    """Sources plus recent events, newest first. `limit` caps the event list (max 200);
    `source` narrows it to one machine, e.g. /status?source=SITE_A&limit=200. Each visitor gets MAX_STATUS_PER_MINUTE requests a minute."""
    visitor, moment = _visitor_of(request), time.monotonic()
    if not _status_rate.allow(visitor, moment):
        return JSONResponse({"error": "too many status requests; retry later"}, status_code=429,
                            headers={"Retry-After": str(_status_rate.retry_after(visitor, moment))})
    admin = _is_admin_request(request)
    async with _db_lock:
        with db() as conn:
            now_dt = datetime.now(timezone.utc)
            sources = _sources_snapshot(conn)
            recent_events = _recent_events_snapshot(conn, limit, source)
            endpoints = _endpoint_snapshots(sources, recent_events, now_dt, _stored_alerts(conn))
            counters = fleet_counters(conn, now_dt, STARTED_AT, _heartbeats)
            site_c = _site_c_snapshot(conn, now_dt)
    return JSONResponse(json.loads(_strict_json({"sources": sources, "recent_events": recent_events, "endpoints": endpoints, "site_c_feed": site_c,
                                                 "system": _system.snapshot(), "counters": counters,
                                                 "plain_http_senders": _plain_http_senders(now_dt),
                                                 "routes": ROUTES.snapshot(now_dt), "exposed_senders": ROUTES.exposed(now_dt),
                                                 "sender_auth": _sender_auth_status(now_dt, admin), "stream_pools": _stream_pools_status(), "standby": _standby_status(now_dt, admin),
                                                 "control": _control_status(now_dt, admin),
                                                 "sender_keys": _sender_keys(now_dt), "key_window_open": bool(PREVIOUS_SHARED_TOKEN),
                                                 "clock": _clock_snapshot(now_dt), "version": PAGE_VERSION})))


STREAM_PUSH_SECONDS = 5
MAX_STREAMS = _env_int("WATCHDOG_MAX_STREAMS", 32, 1, 512)                  # the PUBLIC pool: everyone arriving through Cloudflare or from an unrecognised route
MAX_PRIVATE_STREAMS = _env_int("WATCHDOG_MAX_PRIVATE_STREAMS", 16, 0, 256)  # the PRIVATE pool: viewers on the LAN or Tailscale (wall screens, operators)
# Two audiences, two pools, so the internet can never use up what the people in the building and on the tailnet depend on. Each pool caps every visitor and,
# when it is full, lets a newcomer displace one stream of whoever holds the most (see guard.StreamPool). A route is private only by what the viewer dialled
# (route_kind): a request through Cloudflare carries Cloudflare's headers and is always public, whatever Host it writes.
_public_streams = StreamPool(MAX_STREAMS, MAX_STREAMS_PER_VISITOR)
_private_streams = StreamPool(MAX_PRIVATE_STREAMS, MAX_STREAMS_PER_VISITOR)


def _stream_pool_for(request) -> StreamPool:
    return _private_streams if route_kind(request.headers, PUBLIC_HOST, _behind_trusted_proxy(request)) in ("lan", "tailscale") else _public_streams


def _stream_pools_status() -> dict:
    """How full each pool is: counts only, never an address."""
    return {"public": _public_streams.snapshot(), "private": _private_streams.snapshot()}
# A viewer that stops reading (a dead phone, a hostile client) would otherwise leave the server waiting in a send forever, holding a shared slot. Each
# chunk gets this long to be accepted by the connection; a viewer that cannot take one in this time is cut off. 30 s is far longer than a frame needs on
# the slowest link this runs over (a ship or satellite connection), and far shorter than "forever".
STREAM_SEND_TIMEOUT_SECONDS = _env_int("WATCHDOG_STREAM_SEND_TIMEOUT_SECONDS", 30, 5, 300)


class SseResponse(StreamingResponse):
    """The live stream, with two guarantees the plain StreamingResponse does not give:
    1. Nothing is held for a viewer that stops reading: every send has STREAM_SEND_TIMEOUT_SECONDS, then the connection is dropped.
    2. The viewer's slot is given back when the response ends, however it ends, even if the client vanished before the generator below ever started (its own
       `finally` would never run then). `on_close` is idempotent, so both paths may ask."""

    def __init__(self, *args, on_close, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_close = on_close

    async def stream_response(self, send):
        async def guarded(message):
            await asyncio.wait_for(send(message), timeout=STREAM_SEND_TIMEOUT_SECONDS)

        try:
            await super().stream_response(guarded)
        except asyncio.TimeoutError:
            logger.warning("[stream] a viewer stopped reading for %d s; its connection was closed and its slots given back", STREAM_SEND_TIMEOUT_SECONDS)
            raise
        finally:
            self._on_close()
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                await close()


@app.get("/stream")
async def stream(request: Request, token: str = Query(default="")) -> StreamingResponse:
    """Server-pushed status for the dashboard, replacing polling. A real bug motivated this:
    the dashboard polled /status on a 10s setInterval, but browsers throttle setInterval timers
    hard in a backgrounded tab -- a feed recovered while the tab sat in the background, and the
    page kept showing it as down (and kept the audible alarm going) until a manual reload, long
    past the 10s it should have taken. An open connection the server pushes to is not subject to
    that same throttling, which is exactly the "automatic realtime" this replaces polling with.

    EventSource (the browser API that consumes this) cannot set custom headers, so the token
    travels as a query parameter here instead of X-Watchdog-Token -- the one exception to this
    service's header-only rule, and not a real loosening of it: this page already embeds the
    token in its own source (see dashboard.py), so a same-origin request carrying it in the URL
    exposes nothing that loading the page didn't already."""
    if not _view_token_matches(token):
        raise HTTPException(status_code=401, detail="missing or invalid token")
    # The pool for this audience: one visitor holds only a few of its slots, and when it is full a newcomer may displace a stream of whoever holds the most.
    pool = _stream_pool_for(request)
    handle, reason = pool.admit(_visitor_of(request))
    if handle is None:
        if reason == "visitor-cap":
            raise HTTPException(status_code=429, detail="too many live streams from this address; close an old tab and retry")
        raise HTTPException(status_code=503, detail="stream capacity reached; retry later")

    super_stream = _control_caller_is_super(request)      # a stream the super user opened may stay open while access is OFF; nobody else's may

    async def pushes():
        try:
            while True:
                if handle.evicted or await request.is_disconnected():          # evicted: the pool gave this stream's slot to someone with fewer
                    break
                if not _access_on() and not super_stream:                      # access was switched OFF while this viewer was connected: tell it why, then end its stream
                    yield 'event: access\ndata: {"access": "off"}\n\n'
                    break
                if _control_enabled():
                    await _staff_state()                                           # keeps the roster current without touching the database on every push
                async with _db_lock:
                    with db() as conn:
                        now_dt = datetime.now(timezone.utc)
                        sources = _sources_snapshot(conn)
                        recent_events = _recent_events_snapshot(conn, 50)
                        endpoints = _endpoint_snapshots(sources, recent_events, now_dt, _stored_alerts(conn))
                        counters = fleet_counters(conn, now_dt, STARTED_AT, _heartbeats)
                        site_c = _site_c_snapshot(conn, now_dt)
                frame = {"sources": sources, "recent_events": recent_events, "endpoints": endpoints, "site_c_feed": site_c,
                         "system": _system.snapshot(), "counters": counters, "clock": _clock_snapshot(now_dt), "version": PAGE_VERSION, "staff_alerts": _staff_alerts_public(),
                         "standby": _standby_status(now_dt), "control": _control_status(now_dt), "staff": _staff_public()}
                yield f"data: {_strict_json(frame)}\n\n"
                await asyncio.sleep(STREAM_PUSH_SECONDS)
        finally:
            pool.release(handle)

    return SseResponse(
        pushes(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        on_close=lambda: pool.release(handle),
    )


# How long /health will wait for the database lock before calling the listener busy. A listener that cannot take its own lock for this long
# is stuck, and that is exactly what an outside monitor should be told.
HEALTH_LOCK_WAIT_SECONDS = 3.0


@app.get("/health")
async def health() -> JSONResponse:
    """For an EXTERNAL uptime monitor (and Docker's healthcheck): no token, no status data, answers in milliseconds. It proves the listener
    can take its own lock and read its database, so a listener that is up but stuck is reported as a failure. 200 = fine, 503 = not."""
    uptime = int((datetime.now(timezone.utc) - STARTED_AT).total_seconds())
    try:
        await asyncio.wait_for(_db_lock.acquire(), timeout=HEALTH_LOCK_WAIT_SECONDS)
    except asyncio.TimeoutError:
        return JSONResponse({"status": "busy", "database": "locked", "uptime_s": uptime}, status_code=503)
    try:
        with db() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception:  # noqa: BLE001 -- any failure to read the database is an unhealthy listener, never a crash of this endpoint
        return JSONResponse({"status": "degraded", "database": "error", "uptime_s": uptime}, status_code=503)
    finally:
        _db_lock.release()
    # `preferred` is what the OTHER node reads to notice that the two disagree about who is preferred (then both alert). `access` is what an open dashboard (and the unavailable
    # page) reads to follow the control panel's ON/OFF by itself. Both only when the control phase is on; /health stays open while access is OFF for exactly this reason.
    extra = {"preferred": CONTROL["state"].preferred, "access": "on" if CONTROL["state"].access_on else "off"} if _control_enabled() else {}
    return JSONResponse({"status": "ok", "database": "ok", "uptime_s": uptime, **extra})


async def _control_refusal(request) -> Optional[JSONResponse]:
    """None when the caller may go on to the control door, else the refusal. A node without a working control key has no door (404). Rate-limited first, so
    guessing the key is slow."""
    if not _control_enabled():
        return JSONResponse({"error": "not found"}, status_code=404)
    visitor, moment = _visitor_of(request), time.monotonic()
    if not _control_rate.allow(visitor, moment):
        return JSONResponse({"error": "too many control requests; retry later"}, status_code=429, headers={"Retry-After": str(_control_rate.retry_after(visitor, moment))})
    return None


async def _studio_last_seen() -> dict:
    """{studio: when this node last heard it (UTC text) or None}, for the super user's `latency` comparison. A database problem gives {} and a log line."""
    try:
        async with _db_lock:
            with db() as conn:
                rows = conn.execute("SELECT source_id, last_seen_utc FROM sources ORDER BY source_id").fetchall()
        return {row["source_id"]: row["last_seen_utc"] for row in rows}
    except Exception as exc:  # noqa: BLE001 -- the comparison is optional; the state itself is still shown
        logger.error("[main.py::_studio_last_seen] error | error=%r action=continue_with_empty", exc)
        return {}


def _control_caller_is_super(request) -> bool:
    return _headers_are_super({str(k).lower(): str(v) for k, v in request.headers.items()})


def _is_same_site_json(request) -> bool:
    """A change made with the browser's cookie must come from this site's own control page: a JSON body AND a same-origin request (Sec-Fetch-Site, or an Origin that
    names this host). The cookie is already SameSite=Strict; this is the second lock, so a forged cross-site request cannot flip the switch even if a browser ignores
    SameSite. A request that carries the control KEY in a header (the PC tool) has no ambient credential to abuse and is not asked for this."""
    headers = request.headers
    if not str(headers.get("content-type", "")).lower().startswith("application/json"):
        return False
    fetch_site = str(headers.get("sec-fetch-site", "")).lower()
    if fetch_site:
        return fetch_site == "same-origin"
    origin, host = str(headers.get("origin", "")), str(headers.get("host", ""))
    return bool(origin) and bool(host) and urllib.parse.urlsplit(origin).netloc == host


@app.get("/control")
async def control_get(request: Request) -> JSONResponse:
    """The control state of this node (super user only): access, preferred node, who is alerting now, what it believes about the other node."""
    refusal = await _control_refusal(request)
    if refusal is not None:
        return refusal
    if not _control_caller_is_super(request):
        return JSONResponse({"error": "missing or invalid control key"}, status_code=401)
    now = datetime.now(timezone.utc)
    return JSONResponse(json.loads(_strict_json({**_control_status(now, True), "server_time_utc": now.isoformat(), "studios": await _studio_last_seen()})))


@app.post("/control")
async def control_post(request: Request) -> JSONResponse:
    """Change this node's control state (super user only): {"access": "on"|"off"} and/or {"preferred": "CORE"|"VPS"}, plus {"notify": false} on all but one of
    the nodes the command is sent to, so the support mailbox gets one mail and not one from each node."""
    refusal = await _control_refusal(request)
    if refusal is not None:
        return refusal
    if not _control_caller_is_super(request):
        return JSONResponse({"error": "missing or invalid control key"}, status_code=401)
    if not control_access.key_matches(CONTROL_KEY, request.headers.get("x-watchdog-control")) and not _is_same_site_json(request):
        return JSONResponse({"error": "a change made from a browser must come from the control page itself"}, status_code=403)
    payload, failure = await _json_body(request)
    if failure is not None:
        return failure
    notify = payload.get("notify", True)
    if set(payload) - {"access", "preferred", "notify"} or not ({"access", "preferred"} & set(payload)) or not isinstance(notify, bool):
        return JSONResponse({"error": "send access (on/off) and/or preferred (CORE/VPS), and optionally notify (true/false)"}, status_code=400)
    try:
        effects = await apply_control_command(payload, notify)
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    except PermissionError as error:
        return JSONResponse({"error": str(error)}, status_code=409)
    except Exception as exc:  # noqa: BLE001 -- nothing was changed (the state is stored before it takes effect)
        logger.error("[main.py::control_post] error | error=%r action=nothing_changed", exc)
        return JSONResponse({"error": "the control state could not be stored; nothing was changed"}, status_code=500)
    if notify:
        await _announce_to_super_user(effects)
    return JSONResponse(json.loads(_strict_json({**_control_status(datetime.now(timezone.utc), True), "effects": effects})))


def _password_login_enabled() -> bool:
    return _control_enabled() and bool(SUPER_USER) and bool(SUPER_PASSWORD_HASH)


def _login_page_html() -> str:
    """The sign-in page that matches what is set up: name and password once a password exists, the old key page until then (so nobody is locked out in between)."""
    return control_access.login_page_password(staff_url=STAFF_URL) if _password_login_enabled() else control_access.login_page()


def _session_cookie(request, value: str, max_age: int) -> str:
    secure = "; Secure" if request_scheme(request) == "https" else ""
    return f"{CONTROL_COOKIE}={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}{secure}"


def _login_locked(wait: int) -> JSONResponse:
    return JSONResponse({"error": "too many wrong tries; wait and try again"}, status_code=429, headers={"Retry-After": str(wait)})


@app.post("/control/session")
async def control_session(request: Request) -> JSONResponse:
    """Signs the super user in. With a password set up: {"user", "password"}, checked against the stored scrypt hash, with lockout. Until one is set: the old {"key"} login.
    Either way the answer is a signed, HttpOnly, one-hour session cookie; nothing secret is ever stored in the browser."""
    refusal = await _control_refusal(request)
    if refusal is not None:
        return refusal
    payload, failure = await _json_body(request)
    if failure is not None:
        return failure
    if _password_login_enabled():
        typed = payload.get("user")
        candidate = typed.strip().lower() if isinstance(typed, str) else ""
        if candidate and candidate != SUPER_USER.strip().lower() and staff.username_problem(candidate) is None:
            return await _staff_sign_in(request, candidate, payload.get("password"))
        visitor = _visitor_of(request)
        wait = _login_throttle.locked_for(visitor, time.monotonic())
        if wait:
            return _login_locked(wait)
        async with _login_slots:
            # Checked AGAIN now that it is this attempt's turn: a burst that queued up before the lock cannot run its guesses after the lock has closed. The result is recorded
            # before the slot is given up, so the next attempt in the queue sees it.
            wait = _login_throttle.locked_for(visitor, time.monotonic())
            if wait:
                return _login_locked(wait)
            accepted = await asyncio.to_thread(control_login.check_login, SUPER_USER, SUPER_PASSWORD_HASH, payload.get("user"), payload.get("password"))
            if accepted:
                _login_throttle.record_success(visitor)
            else:
                _login_throttle.record_failure(visitor, time.monotonic())
        if not accepted:
            logger.warning("[main.py::control_session] decision | action=login_refused reason=wrong_name_or_password")
            return JSONResponse({"error": "That name or password was not accepted."}, status_code=401)
    elif not control_access.key_matches(CONTROL_KEY, payload.get("key")):
        return JSONResponse({"error": "missing or invalid control key"}, status_code=401)
    token = control_access.issue_session(CONTROL_KEY, datetime.now(timezone.utc), CONTROL_SESSION_SECONDS, _session_generation())
    return JSONResponse({"ok": True}, headers={"Set-Cookie": _session_cookie(request, token, CONTROL_SESSION_SECONDS), "Cache-Control": "no-store"})


@app.post("/control/logout")
async def control_logout(request: Request) -> JSONResponse:
    """Logs out: the cookie is emptied AND, when the caller really is signed in, every session on this node is ended (the session generation changes and is stored), so a copied
    cookie stops working at once instead of living out its hour. Without a valid session it only empties the cookie, so an outsider cannot log the super user out."""
    cleared = {"Set-Cookie": _session_cookie(request, "", 0), "Cache-Control": "no-store"}
    if not (_control_enabled() and _control_caller_is_super(request)):
        return JSONResponse({"ok": True}, headers=cleared)
    fresh = secrets.token_hex(16)
    try:
        async with _db_lock:
            _write_state("control_session_gen", fresh)
    except Exception as exc:  # noqa: BLE001 -- say so plainly: the other sessions were NOT ended
        logger.error("[main.py::control_logout] error | error=%r action=sessions_not_ended", exc)
        return JSONResponse({"ok": False, "error": "This browser is logged out, but other sessions could not be ended; try again."}, status_code=500, headers=cleared)
    CONTROL["session_gen"] = fresh
    return JSONResponse({"ok": True}, headers=cleared)


@app.get("/control/login")
async def control_login_page() -> Response:
    """Where the super user signs in (while access is OFF this is the way in for a browser)."""
    if not _control_enabled():
        return Response("not found", media_type="text/plain", status_code=404)
    return Response(_login_page_html(), media_type="text/html; charset=utf-8", headers=NO_CACHE_HEADERS)


# ---- The control panel: buttons that change BOTH nodes --------------------------------------------------------------------------------------------
def _peer_base() -> str:
    """https://host of the OTHER node, taken from the address this node already uses to watch it. Only https: the internal key must never travel in the clear."""
    parts = urllib.parse.urlsplit(PRIMARY_URL or "")
    return f"https://{parts.netloc}" if parts.scheme == "https" and parts.netloc else ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the internal key to wherever it points, so none is ever followed."""

    def redirect_request(self, *args, **kwargs):
        return None


def _peer_request(method: str, path: str, payload=None, timeout: int = 10):
    """(status, parsed JSON) for one call to the OTHER node's control door with the internal key; (None, {}) when it cannot be reached."""
    base = _peer_base()
    if not base or not CONTROL_KEY:
        return None, {}
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(base + path, data=body, method=method, headers={"X-Watchdog-Control": CONTROL_KEY, "Content-Type": "application/json",
                                                                                   "User-Agent": "ClarixWatchdog-Control/1.0"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as reply:  # nosec B310 -- https only, the host is the configured peer
            return reply.status, json.loads(reply.read(65536).decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        try:                                          # a refusal can carry its reason (409 busy ...): keep it when it is a small JSON object, otherwise there is none
            body = json.loads(error.read(65536).decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 -- an unreadable body is simply no body
            body = {}
        return error.code, body if isinstance(body, dict) else {}
    except Exception:  # noqa: BLE001 -- any failure to talk to the other node is simply "not reached"
        return None, {}


def _other_node() -> str:
    return control.VPS if NODE == control.CORE else control.CORE


async def _peer_view() -> tuple:
    """(reachable, data) for the other node's control door."""
    status, data = await asyncio.to_thread(_peer_request, "GET", "/control")
    return (status == 200 and isinstance(data, dict) and data.get("enabled") is True), (data if isinstance(data, dict) else {})


async def _panel_snapshot() -> dict:
    """What the panel shows: this node and the other one, each with its own clock and when it last heard each studio. No key, no hash."""
    now = datetime.now(timezone.utc)
    mine = {**_control_status(now, True), "server_time_utc": now.isoformat(), "studios": await _studio_last_seen()}
    began = time.perf_counter()
    reachable, theirs = await _peer_view()
    if reachable:
        peer = {**{key: value for key, value in theirs.items() if key != "effects"}, "reachable": True, "round_trip_ms": round((time.perf_counter() - began) * 1000)}
    else:
        peer = {"reachable": False, "node": _other_node(), "reason": "no answer"}
    return {"self": mine, "peer": peer, "staff": staff.panel_view(await _staff_state(), now)}


async def _announce_to_super_user(effects) -> None:
    """Mails the super user what just changed (every change is announced to him). Only the node told to notify calls this."""
    if not SUPER_EMAIL_TO:
        return
    for message in _super_user_messages(effects, CONTROL["state"]):
        await asyncio.to_thread(send_plain_mail, message.subject, message.body, SUPER_EMAIL_TO)


async def perform_panel_action(action: str) -> dict:
    """One button press. THIS node is changed and the OTHER node is asked to follow, the target (or the announcing node) first, then both are read back. Exactly one node is
    told to send the one mail. Returns {"ok", "messages", "self", "peer"}; when the other node cannot be reached this node is still set and the message says so."""
    messages, ok = [], True
    other = _other_node()
    mine = _control_status(datetime.now(timezone.utc), True)
    peer_up, theirs = await _peer_view()
    order = []                                  # (who, payload) in the order they are applied

    if action in ("off", "on"):
        wanted = action
        label = wanted.upper()
        payload = {"access": wanted}
        need_here, need_there = mine.get("access") != wanted, peer_up and theirs.get("access") != wanted
        here_alerts, there_alerts = bool(mine.get("alerting_now")), peer_up and bool(theirs.get("alerting_now"))
        announce_here = here_alerts or not there_alerts
        changing, reached = int(need_here) + int(need_there), 1 + int(peer_up)
        notifier = None
        if changing and changing == reached:
            notifier = ("here" if announce_here else "there") if (need_here and need_there) else ("here" if need_here else "there")
        if not (need_here or need_there):
            messages.append(f"Access is already {label} on both nodes. Nothing was sent." if peer_up else f"Access is already {label} on this node.")
        order = sorted([who for who, needed in (("here", need_here), ("there", need_there)) if needed], key=lambda who: who != notifier)
        done_text = f"Access is now {label} on both nodes (read back from each)."
        check = lambda state: state.get("access") == wanted  # noqa: E731
    else:
        target = control.CORE if action == "use_core" else control.VPS
        label = "Core" if target == control.CORE else "the VPS"
        payload = {"preferred": target}
        if target != NODE and not peer_up:
            return {"ok": False, "messages": [f"{target} could not be reached, so it cannot be made the preferred node and nothing was changed."], **await _panel_snapshot()}
        need_here, need_there = mine.get("preferred") != target, peer_up and theirs.get("preferred") != target
        changing, reached = int(need_here) + int(need_there), 1 + int(peer_up)
        first = "here" if target == NODE else "there"
        order = sorted([who for who, needed in (("here", need_here), ("there", need_there)) if needed], key=lambda who: who != first)
        notifier = order[0] if (order and changing == reached) else None
        if not order:
            messages.append(f"{label[0].upper() + label[1:]} is already the preferred node on {'both nodes' if peer_up else 'this node'}. Nothing was sent.")
        done_text = f"{label[0].upper() + label[1:]} is now the preferred node on both nodes (read back from each). {other if target == NODE else NODE} takes over by itself if {target} goes down."
        check = lambda state: state.get("preferred") == target  # noqa: E731

    for who in order:
        if who == "here":
            try:
                effects = await apply_control_command(payload, notify=notifier == "here")
                if notifier == "here":
                    await _announce_to_super_user(effects)
            except (ValueError, PermissionError) as error:
                ok = False
                messages.append(f"This node refused: {error}")
            except Exception as exc:  # noqa: BLE001 -- nothing was changed here (the state is stored before it takes effect)
                logger.error("[main.py::perform_panel_action] error | error=%r action=nothing_changed_here", exc)
                ok = False
                messages.append("This node could not store the change.")
        else:
            status, _ = await asyncio.to_thread(_peer_request, "POST", "/control", {**payload, "notify": notifier == "there"})
            if status != 200:
                ok = False
                messages.append(f"{other} did not accept the change.")
    if not peer_up:
        ok = False
        messages.append(f"{other} was not reached. This node is set; press the button again when {other} is back.")
    snapshot = await _panel_snapshot()
    if ok and order or (ok and not messages):
        if not check(snapshot["self"]) or (snapshot["peer"]["reachable"] and not check(snapshot["peer"])):
            ok = False
            messages.append("A node did not keep the change when it was read back.")
        elif not any("already" in text for text in messages):
            messages.insert(0, done_text)
    return {"ok": ok, "messages": messages, **snapshot}


@app.get("/control/panel")
async def control_panel(request: Request) -> Response:
    """The control page. Without a session the person is sent to the sign-in page."""
    if not _control_enabled():
        return Response("not found", media_type="text/plain", status_code=404)
    if not _control_caller_is_super(request):
        return Response("", status_code=302, headers={"Location": "/control/login", "Cache-Control": "no-store"})
    return Response(control_access.panel_page(), media_type="text/html; charset=utf-8", headers=NO_CACHE_HEADERS)


@app.get("/control/panel/state")
async def control_panel_state(request: Request) -> JSONResponse:
    refusal = await _control_refusal(request)
    if refusal is not None:
        return refusal
    if not _control_caller_is_super(request):
        return JSONResponse({"error": "sign in first"}, status_code=401)
    snapshot = await _panel_snapshot()
    snapshot["matrix"] = {"available": bool(IS_STANDBY or MATRIX_SWITCHER is not None)}      # whether the page may show the AV matrix card (the standby relays, Core answers if it has a matrix); the card's own endpoints check again
    return JSONResponse(json.loads(_strict_json(snapshot)))


@app.post("/control/panel/act")
async def control_panel_act(request: Request) -> JSONResponse:
    """A button press: {"action": "off" | "on" | "use_core" | "use_vps"}. Needs the session, a JSON body and a same-origin request."""
    refusal = await _control_refusal(request)
    if refusal is not None:
        return refusal
    if not _control_caller_is_super(request):
        return JSONResponse({"error": "sign in first"}, status_code=401)
    if not control_access.key_matches(CONTROL_KEY, request.headers.get("x-watchdog-control")) and not _is_same_site_json(request):
        return JSONResponse({"error": "a button press must come from the control page itself"}, status_code=403)
    payload, failure = await _json_body(request)
    if failure is not None:
        return failure
    action = payload.get("action")
    if action not in PANEL_ACTIONS:
        return JSONResponse({"error": "unknown action"}, status_code=400)
    try:
        result = await perform_panel_action(action)
    except Exception as exc:  # noqa: BLE001 -- say so plainly instead of a bare 500; a retry is safe (every action is idempotent)
        logger.error("[main.py::control_panel_act] error | error=%r", exc)
        return JSONResponse({"ok": False, "messages": ["Something went wrong; press the button again."]}, status_code=500)
    return JSONResponse(json.loads(_strict_json(result)))


# ---- AV matrix: the endpoints (the unpublished design notes section 3) ---------------------------------------------------------------------------------------------------
# Two layers. CORE's key endpoints (/control/matrix, /control/matrix/switch) touch the matrix and answer ONLY the internal control key, never a browser cookie. The BROWSER endpoints
# (/control/panel/matrix) exist on both nodes and answer exactly two kinds of caller (his rule of 2026-11-10): the signed-in SUPER USER from any address, and an OFFICE PC, meaning a
# request that arrived on the standby's staff door (the PC showed a listed certificate), with NO sign-in, staff accounts included. On Core they switch the matrix directly; on the
# standby they relay to Core. Everyone else gets 404 and learns nothing.
def _matrix_refusal(request) -> Optional[JSONResponse]:
    """None when the request may go on, else the refusal: no control phase means no matrix door (404), and a visitor over its own matrix allowance waits (429)."""
    if not _control_enabled():
        return JSONResponse({"error": "not found"}, status_code=404)
    visitor, moment = _visitor_of(request), time.monotonic()
    if not _matrix_rate.allow(visitor, moment):
        return JSONResponse({"error": "too many matrix requests; retry later"}, status_code=429, headers={"Retry-After": str(_matrix_rate.retry_after(visitor, moment))})
    return None


def _matrix_choice(payload: dict, extra=frozenset()) -> tuple:
    """(preset key, confirm, None), or (None, None, a 400 response). A request can only NAME a preset: no command, no output list, no field the endpoint does not list. The emergency
    preset needs confirm: true (the page asks first); the server refuses a request that does not say so, so the question cannot be skipped by accident. It is a guard against accidents,
    not against a caller who is allowed to press."""
    if set(payload) - ({"preset", "confirm"} | set(extra)):
        return None, None, JSONResponse({"error": "send only the preset and, for the emergency preset, confirm"}, status_code=400)
    key = payload.get("preset")
    if not isinstance(key, str) or key not in matrix.PRESETS:
        return None, None, JSONResponse({"error": "unknown preset"}, status_code=400)
    confirm = payload.get("confirm", False)
    if not isinstance(confirm, bool):
        return None, None, JSONResponse({"error": "confirm must be true or false"}, status_code=400)
    if matrix.PRESETS[key].needs_confirm and confirm is not True:
        return None, None, JSONResponse({"error": "this preset needs confirm: true"}, status_code=400)
    return key, confirm, None


async def _matrix_mail(outcome, operator: str) -> None:
    """Tells the super user about a switch that failed or could not be confirmed. Never raises: a broken mail server must not change what the page is told."""
    if not SUPER_EMAIL_TO:
        return
    title = matrix.PRESETS[outcome.preset].title
    body = (f"{outcome.message}\n\nPreset: {title}\nPressed by: {matrix.clean_text(operator, 80)}\nTime (UTC): {outcome.finished_at}\n"
            f"Before: {list(outcome.before) if outcome.before else 'not read'}\nAfter: {list(outcome.after) if outcome.after else 'not read'}\n"
            "Outputs are listed 1 to 8, each as the number of the input it carries. Check the matrix.")
    try:
        await asyncio.to_thread(send_plain_mail, f"[Watchdog] Matrix switch {outcome.state.upper()}: {title}", body, SUPER_EMAIL_TO)
    except Exception as exc:  # noqa: BLE001 -- the answer to the operator does not depend on the mail
        logger.error("[main.py::_matrix_mail] error | error=%r action=continue", exc)


async def _matrix_audit(outcome, operator: str, via: str, visitor: str) -> None:
    """One row of the audit log for a press that reached the matrix (every result but busy and not-set), and a mail when it failed or could not be confirmed. `operator` and `via` are
    only what the caller CLAIMS (anyone holding the key can write them), so the row also records `from`, the address the server itself saw. A logging problem is reported in the
    service log and never changes the answer."""
    detail = {"before": outcome.before and list(outcome.before), "after": outcome.after and list(outcome.after), "sent": outcome.sent, "attempts": outcome.attempts,
              "failed_outputs": outcome.failed_outputs, "restored": outcome.restored, "changed_elsewhere": outcome.changed_elsewhere, "message": outcome.message,
              "from": matrix.clean_text(visitor, 80)}
    try:
        async with _db_lock:
            with db() as conn:
                matrix.insert_log(conn, outcome.started_at, operator, outcome.preset, outcome.state, via, detail)
                matrix.prune(conn)
    except Exception as exc:  # noqa: BLE001 -- see the docstring
        logger.error("[main.py::_matrix_audit] error | error=%r action=answer_anyway", exc)
    logger.info("[main.py::_matrix_audit] output_returned | preset=%s result=%s operator=%s via=%s", outcome.preset, outcome.state, matrix.clean_text(operator, 80), via)
    if outcome.state in ("failed", "unverified"):
        try:
            await asyncio.wait_for(_matrix_mail(outcome, operator), timeout=MATRIX_MAIL_WAIT_SECONDS)
        except asyncio.TimeoutError:                  # the mail thread keeps going; the page is not kept waiting for a slow mail server
            logger.warning("[main.py::_matrix_audit] decision | action=answer_before_the_mail_finished")


async def _matrix_status_view(fresh: bool = False) -> dict:
    """The status the page shows: the matrix reading plus the last successful switch and the last few presses from the log. A locked or broken log leaves those two empty; it never
    takes the status (and so the buttons) down while the matrix itself is fine."""
    status = await asyncio.to_thread(MATRIX_SWITCHER.status, fresh)
    last, rows = None, []
    try:
        async with _db_lock:
            with db() as conn:
                last, rows = matrix.last_success(conn), matrix.recent(conn, 5)
    except Exception as exc:  # noqa: BLE001 -- see the docstring
        logger.error("[main.py::_matrix_status_view] error | error=%r action=status_without_the_log", exc)
    return {"enabled": True, **status, "last_success": last, "recent": rows}


async def _matrix_run(key: str, operator: str, via: str, visitor: str) -> tuple:
    """One press as ONE unit: the switch (in a worker thread), its audit row and mail, and a fresh status. Returns (outcome, status); status is None when the press was refused before
    anything was read or sent. The caller shields it, because the matrix keeps going when a browser or the tunnel drops the request, and the log must not skip that press."""
    outcome = await asyncio.to_thread(MATRIX_SWITCHER.switch, key)
    if outcome.state in ("busy", "not_configured"):
        return outcome, None
    await _matrix_audit(outcome, operator, via, visitor)
    return outcome, await _matrix_status_view()


async def _matrix_answer(key: str, operator: str, via: str, visitor: str) -> JSONResponse:
    """Runs one press on THIS node's matrix and shapes the answer: 200 with the outcome and a fresh status, or 409 (busy, preset without outputs) with nothing read or sent and no log row.
    Shielded: if the caller goes away mid-switch the switch, its log row and its mail still finish."""
    outcome, status = await asyncio.shield(_matrix_run(key, operator, via, visitor))
    if status is None:
        return JSONResponse(json.loads(_strict_json(outcome.as_dict())), status_code=409)
    return JSONResponse(json.loads(_strict_json({**outcome.as_dict(), "status": status})))


def _has_control_key(request) -> bool:
    return control_access.key_matches(CONTROL_KEY, request.headers.get("x-watchdog-control"))


@app.get("/control/matrix")
async def matrix_status(request: Request) -> JSONResponse:
    """CORE: the matrix as it reads now (the eight outputs, which preset is active, the last switches). Key only. `?fresh=1` reads the matrix again instead of reusing a reading
    under two seconds old."""
    refusal = _matrix_refusal(request)
    if refusal is not None:
        return refusal
    if not _has_control_key(request):                        # the key is checked BEFORE whether a matrix is attached, so a stranger gets the same answer from every node
        return JSONResponse({"error": "missing or invalid control key"}, status_code=401)
    if MATRIX_SWITCHER is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    fresh = str((getattr(request, "query_params", None) or {}).get("fresh", "")) == "1"
    return JSONResponse(json.loads(_strict_json(await _matrix_status_view(fresh))))


@app.post("/control/matrix/switch")
async def matrix_switch(request: Request) -> JSONResponse:
    """CORE: brings one preset into place and answers with what the matrix CONFIRMED. Key only. Body: {"preset", "confirm", "operator", "via"}. Busy and not-set answer 409."""
    refusal = _matrix_refusal(request)
    if refusal is not None:
        return refusal
    if not _has_control_key(request):
        return JSONResponse({"error": "missing or invalid control key"}, status_code=401)
    if MATRIX_SWITCHER is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    payload, failure = await _json_body(request)
    if failure is not None:
        return failure
    key, _confirm, bad = _matrix_choice(payload, {"operator", "via"})
    if bad is not None:
        return bad
    operator = payload.get("operator", "control key")
    if not isinstance(operator, str) or not 1 <= len(operator.strip()) <= 80:
        return JSONResponse({"error": "operator must be 1 to 80 characters"}, status_code=400)
    via = payload.get("via") if payload.get("via") in MATRIX_VIA else "control-key"
    return await _matrix_answer(key, operator, via, _visitor_of(request))


def _office_pass_domain(staff_url=None, dashboard_url=None) -> str:
    """The parent domain the door pass is shared through: what the staff address and the dashboard address have in common (staff.corp.example and watchdog.corp.example share
    corp.example). Empty when they share no real domain, or either is a bare machine name or an IP address: then no pass is issued or accepted."""
    hosts = []
    for url in (STAFF_URL if staff_url is None else staff_url, DASHBOARD_URL if dashboard_url is None else dashboard_url):
        try:
            host = (urllib.parse.urlsplit(str(url)).hostname or "").lower().rstrip(".")
        except ValueError:
            return ""
        labels = host.split(".")
        if len(labels) < 2 or labels[-1].isdigit() or not all(re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", label) for label in labels):
            return ""
        hosts.append(labels)
    shared = []
    for mine, theirs in zip(reversed(hosts[0]), reversed(hosts[1])):
        if mine != theirs:
            break
        shared.append(mine)
    return ".".join(reversed(shared)) if len(shared) >= 2 else ""


def _office_pass_headers(request) -> Optional[dict]:
    """The Set-Cookie header that hands THIS browser a door pass, or None. Only a request that arrived on this standby's staff door (so the PC showed a listed certificate) ever gets one:
    a pass cannot renew itself, and nobody else is given one. HttpOnly (no script can read it), Strict (never sent from another site), limited to the matrix path, 3 days."""
    domain = _office_pass_domain()
    if not (OFFICE_PASS_ON and domain and _control_enabled() and IS_STANDBY and _on_staff_door(request)):
        return None
    token = control_access.issue_pass(CONTROL_KEY, datetime.now(timezone.utc), OFFICE_PASS_SECONDS)
    secure = "; Secure" if request_scheme(request) == "https" else ""
    return {"Set-Cookie": f"{OFFICE_PASS_COOKIE}={token}; Domain={domain}; Path={OFFICE_PASS_PATH}; HttpOnly; SameSite=Strict; Max-Age={OFFICE_PASS_SECONDS}{secure}"}


def _office_pass_valid(request) -> bool:
    """True when the request carries an unexpired door pass signed with this control key (and the pass is switched on and has a shared domain to live in)."""
    if not (OFFICE_PASS_ON and _control_enabled() and _office_pass_domain()):
        return False
    now = datetime.now(timezone.utc)
    for part in str(request.headers.get("cookie", "")).split(";"):
        name, _, value = part.strip().partition("=")
        if name == OFFICE_PASS_COOKIE and control_access.verify_pass(CONTROL_KEY, value, now):
            return True
    return False


async def _matrix_caller(request) -> Optional[tuple]:
    """(operator, way in) for a browser caller who may use the matrix, else None (the answer is then 404 and the page stays silent). Three kinds of caller and no other: the signed-in
    super user, from ANY address (named as the super user); an OFFICE PC, a request that arrived on THIS standby's staff door (so the PC showed its listed certificate), who needs no
    sign-in; or a browser holding the DOOR PASS that such a PC was handed (the same PC on the watchdog address). A staff member signed in on the door is named in the log; anyone else
    on the door, or with a pass, is "office PC". A staff cookie anywhere else counts for nothing. Core has no staff door, so on Core only the super user and a pass holder pass."""
    on_door = bool(IS_STANDBY and _on_staff_door(request))
    if _control_caller_is_super(request):
        return (SUPER_USER or "super user"), ("staff-door" if on_door else ("standby-site" if IS_STANDBY else "core-site"))
    if on_door:
        return (matrix.clean_text(await _staff_display_of(request) or "", 80).strip() or "office PC"), "office-pc"
    if _office_pass_valid(request):
        return "office PC", "office-pass"
    return None


@app.get("/control/panel/matrix")
async def matrix_panel_status(request: Request) -> JSONResponse:
    """BROWSER: the matrix as it reads now, for the super user (any address) or an office PC. Core reads its own matrix directly; the standby asks Core through the relay, and Core
    not reached is said so plainly, with no routing, so no preset is ever shown as active."""
    refusal = _matrix_refusal(request)
    if refusal is not None:
        return refusal
    if await _matrix_caller(request) is None or (not IS_STANDBY and MATRIX_SWITCHER is None):
        return JSONResponse({"error": "not found"}, status_code=404)
    fresh = str((getattr(request, "query_params", None) or {}).get("fresh", "")) == "1"
    if not IS_STANDBY:
        return JSONResponse(json.loads(_strict_json({**await _matrix_status_view(fresh), "core_reached": True})))
    status, data = await asyncio.to_thread(_peer_request, "GET", "/control/matrix" + ("?fresh=1" if fresh else ""))
    if status == 200 and isinstance(data, dict):
        return JSONResponse(json.loads(_strict_json({**data, "core_reached": True})))
    if status is None:
        return JSONResponse({"enabled": True, "core_reached": False, "error": "Core was not reached."})
    if status == 404:
        return JSONResponse({"enabled": True, "core_reached": True, "configured": False, "error": "Core has no matrix connected (WATCHDOG_MATRIX_HOST is not set there)."})
    return JSONResponse({"enabled": True, "core_reached": True, "error": f"Core answered with code {status}."})


@app.post("/control/panel/matrix")
async def matrix_panel_switch(request: Request) -> JSONResponse:
    """BROWSER: one preset press from the super user (any address) or an office PC. It must come from the page itself (a same-origin JSON request: an office PC's browser shows its
    certificate to ANY page it visits, so this second lock is what stops a forged cross-site press). Core switches directly; the standby relays to Core. The browser can name only a
    preset (and confirm): never the operator, the way in or a command; both are decided here from who is asking."""
    refusal = _matrix_refusal(request)
    if refusal is not None:
        return refusal
    caller = await _matrix_caller(request)
    if caller is None or (not IS_STANDBY and MATRIX_SWITCHER is None):
        return JSONResponse({"error": "not found"}, status_code=404)
    if not _has_control_key(request) and not _is_same_site_json(request):
        return JSONResponse({"error": "a press must come from the control page itself"}, status_code=403)
    payload, failure = await _json_body(request)
    if failure is not None:
        return failure
    key, confirm, bad = _matrix_choice(payload)
    if bad is not None:
        return bad
    operator, via = caller
    if not IS_STANDBY:
        return await _matrix_answer(key, operator, via, _visitor_of(request))
    relay = {"preset": key, "confirm": confirm, "operator": operator, "via": via}
    status, data = await asyncio.to_thread(_peer_request, "POST", "/control/matrix/switch", relay, MATRIX_SWITCH_TIMEOUT_SECONDS)
    if status in (200, 400, 409) and isinstance(data, dict) and data:
        return JSONResponse(json.loads(_strict_json(data)), status_code=status)
    if status is None or (status or 0) >= 500:
        return JSONResponse({"ok": False, "state": "core_unreachable", "message": "Core did not answer in time. The matrix may or may not have switched: press Refresh to see what it shows."})
    return JSONResponse({"ok": False, "state": "core_refused", "message": f"Core did not accept the request (code {status}). Nothing was sent to the matrix."})


# ---- Staff: storing rows, signing in, the pages' data, and copying rows between the two nodes ----------------------------------------------------------------
def _staff_cookie(request, value: str, max_age: int) -> str:
    secure = "; Secure" if request_scheme(request) == "https" else ""
    return f"{STAFF_COOKIE}={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}{secure}"


def _staff_rows_locked(conn, after: str = "", limit: int = 0) -> list:
    """Every row (or one page after a cursor), in id order, as the plain dicts staff.py works with. A row whose data cannot be read is skipped, not fatal."""
    sql, args = "SELECT id, at_utc, node, kind, person, data FROM staff_log", []
    if after:
        sql, args = sql + " WHERE id > ?", [after]
    sql += " ORDER BY id" + (" LIMIT ?" if limit else "")
    rows = []
    for record in conn.execute(sql, args + ([limit] if limit else [])).fetchall():
        try:
            rows.append({"id": record["id"], "at": record["at_utc"], "node": record["node"], "kind": record["kind"], "person": record["person"], "data": json.loads(record["data"])})
        except ValueError:
            continue
    return rows


def _insert_staff_rows_locked(conn, rows: list) -> int:
    """INSERT OR IGNORE: a row that is already here is not added twice, which is what makes copying rows safe to repeat in any order."""
    added = 0
    for row in rows:
        added += conn.execute("INSERT OR IGNORE INTO staff_log (id, at_utc, node, kind, person, data) VALUES (?, ?, ?, ?, ?, ?)",
                              (row["id"], row["at"], row["node"], row["kind"], row["person"], json.dumps(row["data"], sort_keys=True))).rowcount
    return added


async def _staff_rows() -> list:
    async with _db_lock:
        with db() as conn:
            return _staff_rows_locked(conn)


async def _staff_state() -> dict:
    """The picture replayed from every row, kept in memory and replaced the moment a row is added."""
    cached = _staff_cache["state"]
    if cached is not None:
        return cached
    async with _db_lock:
        with db() as conn:
            state = staff.replay(_staff_rows_locked(conn))
        _staff_cache["state"] = state
    return state


async def _staff_write(make_row) -> dict:
    """Checks and stores ONE row in the same locked step, so two presses at once cannot both pass the same check. `make_row(state, now)` returns the row or raises staff.StaffError.
    The other node is told afterwards (best effort: it also pulls, so a missed push is only slower)."""
    now = datetime.now(timezone.utc)
    async with _db_lock:
        with db() as conn:
            rows = _staff_rows_locked(conn)
            row = make_row(staff.replay(rows), now)
            _insert_staff_rows_locked(conn, [row])
        _staff_cache["state"] = staff.replay(rows + [row])
    await _staff_push([row])
    return row


async def _staff_do(actor, action: str, args: dict) -> dict:
    return await _staff_write(lambda state, now: staff.plan(state, actor, action, args, now, NODE, secrets.token_hex(4)))


async def _staff_push(rows: list) -> None:
    try:
        await asyncio.to_thread(_peer_request, "POST", "/control/staff/rows", {"rows": rows})
    except Exception as exc:  # noqa: BLE001 -- the other node pulls what it missed; a failed push must never fail the action
        logger.warning("[main.py::_staff_push] could not tell the other node (%r); it will pull the rows", exc)


async def _staff_append_rows(rows: list) -> tuple:
    """(added, rejected) for rows that came from outside (the other node). Every row passes staff.row_problem first; the rest are counted and dropped."""
    now = datetime.now(timezone.utc)
    good = [row for row in rows if staff.row_problem(row, now) is None]
    added = 0
    if good:
        async with _db_lock:
            with db() as conn:
                added = _insert_staff_rows_locked(conn, good)
                every = _staff_rows_locked(conn)
            if added:
                _staff_cache["state"] = staff.replay(every)
    return added, len(rows) - len(good)


def _staff_digest(rows: list) -> str:
    """One short fingerprint of which rows a node holds (their ids, sorted). Two nodes with the same fingerprint hold the same rows."""
    ids = sorted({row["id"] for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)})
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()


async def _staff_summary() -> dict:
    async with _db_lock:
        with db() as conn:
            ids = [record[0] for record in conn.execute("SELECT id FROM staff_log ORDER BY id").fetchall()]
    return {"count": len(ids), "digest": hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()}


def _staff_public() -> dict:
    """What every visitor may see of the staff section: the display names of people on shift, and nothing else. Empty where the control phase is off and while access is OFF."""
    state = _staff_cache["state"]
    if state is None or not (_control_enabled() and _access_on()):
        return {"on_shift": []}
    return {"on_shift": staff.public_roster(state)}


def _staff_alerts_public() -> dict:
    """The open, recent failures for the dashboard strip (his words, 2026-11-08: an alert with their name on the dashboard): the playout, how long, and the DISPLAY NAMES that hold it and
    are on it. Display names are already public; no username, hash, note or cause text is here. Empty where the control phase is off and while access is OFF, like the roster."""
    state = _staff_cache["state"]
    if state is None or not (_control_enabled() and _access_on()):
        return {"items": [], "more": 0, "form_url": ""}
    return {**staff.public_alerts(state, datetime.now(timezone.utc)), "form_url": STAFF_URL}


async def _staff_caller(request) -> Optional[str]:
    """The username of a valid staff cookie, or None. His cookie is a different cookie signed another way, so it can never be taken for this one. A staff cookie counts only on the
    staff door: shown anywhere else it is no session at all."""
    if not _control_enabled() or not _on_staff_door(request):
        return None
    for part in str(request.headers.get("cookie", "")).split(";"):
        name, _, value = part.strip().partition("=")
        if name == STAFF_COOKIE:
            return staff.verify_session(CONTROL_KEY, value, datetime.now(timezone.utc), await _staff_state())
    return None


async def _staff_display_of(request) -> Optional[str]:
    name = await _staff_caller(request)
    return (await _staff_state())["people"][name]["display"] if name else None


async def _staff_dummy_hash() -> str:
    if not _STAFF_DUMMY["hash"]:
        _STAFF_DUMMY["hash"] = await asyncio.to_thread(control_login.hash_password, secrets.token_hex(16))
    return _STAFF_DUMMY["hash"]


def _staff_wait(person: str, visitor: str, now: float) -> int:
    """Whole seconds a staff sign-in must still wait: the longer of that NAME's lock and that ADDRESS's lock (0 = free to try)."""
    return max(_staff_throttle.locked_for(person, now), _staff_address_throttle.locked_for(visitor, now))


def _staff_record_failure(person: str, visitor: str) -> None:
    moment = time.monotonic()
    _staff_throttle.record_failure(person, moment)
    _staff_address_throttle.record_failure(visitor, moment)


def _staff_locked(wait: int) -> JSONResponse:
    """The refusal for a locked name or address, in words that say how long. It is the same for a name that exists and one that does not, so a lock never tells a stranger which names are real."""
    when = f"{-(-wait // 60)} minutes" if wait >= 90 else f"{wait} second{'' if wait == 1 else 's'}"
    return JSONResponse({"error": f"Too many wrong tries for this name. Try again in {when}."}, status_code=429, headers={"Retry-After": str(wait)})


async def _staff_sign_in(request, username: str, password) -> JSONResponse:
    """A staff person signs in on the same page as the super user. Own lockout counters; the same words for an unknown name, a wrong password and a disabled account; and no
    staff sign-in at all while access is OFF (only the super user gets through then). Staff sign in ONLY on the staff door (his rule of 2026-11-02: the two office PCs, reached
    over Tailscale); anywhere else the answer is the same whatever the password, and it is not a password guess, so it never counts toward a lockout."""
    if not _on_staff_door(request):
        return JSONResponse({"error": "Staff can sign in only on the office PCs, through the staff address."}, status_code=403)
    if not _access_on():
        return JSONResponse({"error": "Access is switched off. Staff can sign in again when it is switched on."}, status_code=403)
    visitor = _visitor_of(request)
    wait = _staff_wait(username, visitor, time.monotonic())
    if wait:
        return _staff_locked(wait)
    async with _login_slots:
        wait = _staff_wait(username, visitor, time.monotonic())
        if wait:
            return _staff_locked(wait)
        person = (await _staff_state())["people"].get(username)
        active = bool(person and person["active"])
        stored = person["hash"] if active else await _staff_dummy_hash()
        accepted = bool(await asyncio.to_thread(control_login.verify_password, stored, password)) and active
        if accepted:
            _staff_throttle.record_success(username)
        else:
            _staff_record_failure(username, visitor)
    if not accepted:
        logger.warning("[main.py::_staff_sign_in] decision | action=login_refused reason=wrong_name_or_password")
        return JSONResponse({"error": "That name or password was not accepted."}, status_code=401)
    token = staff.issue_session(CONTROL_KEY, username, person["tag"], datetime.now(timezone.utc))
    return JSONResponse({"ok": True, "next": "/staff"}, headers={"Set-Cookie": _staff_cookie(request, token, staff.SESSION_SECONDS), "Cache-Control": "no-store"})


async def _staff_note_failure(endpoint: dict) -> None:
    """Records one red alarm as a failure (or adds it to that PLAYOUT's open one: his rule of 2026-11-01 is one failure per playout, so the key is the endpoint_id, e.g.
    MAIN-USA, never the studio name MAIN that two playouts share). Only the node that is allowed to alert people records it (the rule that lets the e-mail go out): the standby
    hears the same event a moment later and must not add a second alarm to it. Never raises: it must not be able to disturb alerting. Nothing is recorded while access is
    OFF (his rule D3: alerts raised then leave no record) or where the control phase is off.
    It reads only three fields of `endpoint`: endpoint_id (the failure key: 1 to 40 letters, digits, dots, dashes or underscores, NO colon, e.g. MULTICAST-224.1.1.1-5004; an id the staff log
    cannot hold is skipped, never an error), display_name and root_cause (1 to 80 characters each). A monitor that sends its own red mail can therefore call this directly."""
    try:
        if not (_control_enabled() and _access_on() and _delivery_allowed()):      # only the node that may alert people records it, or both nodes would count one alarm
            return
        row = await _staff_write(lambda state, now: staff.failure_row(state, endpoint.get("endpoint_id"), endpoint.get("root_cause"), endpoint.get("display_name"), now, NODE, secrets.token_hex(4)))
    except staff.StaffError as error:
        logger.warning("[main.py::_staff_note_failure] skipped (%s)", error)
        return
    except Exception as exc:  # noqa: BLE001 -- a broken staff table must never stop or slow a red alarm
        logger.error("[main.py::_staff_note_failure] error (alerting is not affected): %r", exc)
        return
    await _staff_after_failure(endpoint, row)


def _auto_assign_plan(fid):
    """What _staff_write runs to give failure `fid` to everyone on shift. Nothing to do (nobody on shift, already held, too old) is a StaffError, which the callers expect and ignore."""
    def make(state, now):
        row = staff.auto_assign_row(state, fid, now, NODE, secrets.token_hex(4))
        if row is None:
            raise staff.StaffError("there is nobody on shift to give it to, or it is already held")
        return row
    return make


async def _staff_auto_assign(fid) -> bool:
    """Gives the failure to everyone on shift (his rule of 2026-11-08). True only when a row was written. Never raises: it may be run after a red e-mail or a shift start."""
    try:
        await _staff_write(_auto_assign_plan(fid))
        return True
    except staff.StaffError:
        return False
    except Exception as exc:  # noqa: BLE001 -- a broken staff table must never reach the alert or the shift start that called this
        logger.error("[main.py::_staff_auto_assign] error (nothing else is affected): %r", exc)
        return False


async def _staff_tell_nobody(endpoint: dict) -> None:
    """A new failure and NOBODY on shift: a second, clear e-mail and phone push to the same recipients the red e-mail went to (so the super user always sees it where he sees the red mail), saying
    that nobody has it. Not forced: it follows the same delivery rules as the red mail (only the node that may alert, only the super user while access is OFF). Never raises."""
    display, cause = endpoint["display_name"], endpoint["root_cause"]
    summary = f"{display}: {cause}. Nobody is on shift, so nobody has been assigned."
    rows = [("Playout Name", display), ("Root Cause", cause),
            ("Staff", "Nobody is on shift, so no one has been assigned. It goes to whoever starts a shift in the next 24 hours while it is still open, or assign it from the control panel.")]
    try:
        await asyncio.to_thread(send_alert_email, display, "NOBODY ON SHIFT", summary, rows, subject_extra=cause)
    except Exception as exc:  # noqa: BLE001 -- a failed notice must not stop anything
        logger.error("[main.py::_staff_tell_nobody] mail error (continuing): %r", exc)
    try:
        await asyncio.to_thread(send_push_notice, f"Watchdog: NOBODY ON SHIFT for {display}", f"{cause}. Nobody is assigned yet.")
    except Exception as exc:  # noqa: BLE001
        logger.error("[main.py::_staff_tell_nobody] push error (continuing): %r", exc)


async def _staff_after_failure(endpoint: dict, row: dict) -> None:
    """After a failure row is stored (and the red e-mail has long gone): give it to everyone on shift, and if it is a NEW failure and nobody is on shift, tell the super user's recipients.
    Never raises: monitoring comes first."""
    try:
        await _staff_auto_assign(row["data"]["fid"])
        if row["kind"] == "failure_opened" and staff.nobody_on_shift(await _staff_state(), datetime.now(timezone.utc)):
            await _staff_tell_nobody(endpoint)
    except Exception as exc:  # noqa: BLE001 -- alerting is already done and must stay done
        logger.error("[main.py::_staff_after_failure] error (alerting is not affected): %r", exc)


async def _staff_assign_waiting() -> None:
    """After a shift starts (by the person, or by the super user for them): hand every recent failure nobody holds to everyone now on shift. Done by the node that took the start, whether or
    not it is the one that alerts (the staff door is on the standby). Never raises: a shift start must not fail because of it."""
    try:
        if not (_control_enabled() and _access_on()):
            return
        for fid in staff.waiting_failures(await _staff_state(), datetime.now(timezone.utc)):
            await _staff_auto_assign(fid)
    except Exception as exc:  # noqa: BLE001
        logger.error("[main.py::_staff_assign_waiting] error (the shift is started all the same): %r", exc)


async def _send_red_alarm(endpoint: dict) -> None:
    """The red alarm's e-mail and push, exactly as before, THEN the failure for the staff section and, through it, the automatic assignment to everyone on shift (the unpublished design notes part 16).
    This is THE gate for a red alarm: every red e-mail goes through here, and what decides a red e-mail is the alarm STATE (status_level critical and email_eligible, in
    _sync_endpoint_alerts_locked), never the kind. So a new kind of alarm (his 2026-11-08 example: a lost Site C multicast stream) is assigned like the rest simply by being a critical
    alarm whose red e-mail is sent; there is no list of kinds to extend, and a test fails if one is written. The record is made after the mail, whatever the mail did (a failing mail still
    raises as it always did), and recording swallows its own errors: monitoring comes first."""
    try:
        await asyncio.to_thread(_send_endpoint_critical_email, endpoint)
    finally:
        await _staff_note_failure(endpoint)


def _staff_limited(request) -> Optional[JSONResponse]:
    visitor, moment = _visitor_of(request), time.monotonic()
    if not _staff_rate.allow(visitor, moment):
        return JSONResponse({"error": "too many requests; retry shortly"}, status_code=429, headers={"Retry-After": str(_staff_rate.retry_after(visitor, moment))})
    return None


def _staff_view_response(state: dict, name: str, extra=None, headers=None) -> JSONResponse:
    view = {"ok": True, **staff.staff_view(state, name, datetime.now(timezone.utc)), **(extra or {})}
    return JSONResponse(json.loads(_strict_json(view)), headers={"Cache-Control": "no-store", **(headers or {})})


@app.get("/staff")
async def staff_page(request: Request) -> Response:
    """The Staff page. Without a staff session the person is sent to the sign-in page (the super user to his own panel)."""
    if not _control_enabled():
        return Response("not found", media_type="text/plain", status_code=404)
    if await _staff_caller(request):
        return Response(staff_pages.staff_page(DASHBOARD_URL), media_type="text/html; charset=utf-8", headers=NO_CACHE_HEADERS)
    target = "/control/panel" if _control_caller_is_super(request) else "/control/login"
    return Response("", status_code=302, headers={"Location": target, "Cache-Control": "no-store"})


@app.get("/staff/state")
async def staff_state(request: Request) -> JSONResponse:
    if not _control_enabled():
        return JSONResponse({"error": "not found"}, status_code=404)
    limited = _staff_limited(request)
    if limited is not None:
        return limited
    name = await _staff_caller(request)
    if not name:
        return JSONResponse({"error": "sign in first"}, status_code=401)
    return _staff_view_response(await _staff_state(), name)


async def _staff_change_password(request, name: str, payload: dict) -> JSONResponse:
    """A person changes their OWN password: the current one must be right (wrong ones count toward the staff lockout), the new one follows the simple STAFF rule (4 characters or more). The old cookie ends
    at once (it carries a tag of the old password) and a new one is issued in the same answer, so the person stays signed in."""
    visitor = _visitor_of(request)
    wait = _staff_wait(name, visitor, time.monotonic())
    if wait:
        return _staff_locked(wait)
    new = payload.get("new")
    problem = staff.staff_password_problem(new)
    async with _login_slots:
        person = (await _staff_state())["people"].get(name)
        correct = bool(person and person["active"]) and bool(await asyncio.to_thread(control_login.verify_password, person["hash"], payload.get("current")))
        if correct:
            _staff_throttle.record_success(name)
        else:
            _staff_record_failure(name, visitor)
        if not correct:
            return JSONResponse({"error": "Your current password was not accepted."}, status_code=401)
        if problem:
            return JSONResponse({"error": problem[:1].upper() + problem[1:] + "."}, status_code=400)
        stored = await asyncio.to_thread(control_login.hash_password, new)
    await _staff_do(staff.Actor("staff", name), "change_password", {"password_hash": stored})
    state = await _staff_state()
    token = staff.issue_session(CONTROL_KEY, name, state["people"][name]["tag"], datetime.now(timezone.utc))
    return _staff_view_response(state, name, headers={"Set-Cookie": _staff_cookie(request, token, staff.SESSION_SECONDS)})


@app.post("/staff/act")
async def staff_act(request: Request) -> JSONResponse:
    """What a staff person can do: start or finish their own shift, say "I am on it" for an open failure (the name is the cookie's, never the request's), mark Done a failure assigned
    to them, change their own password. Nothing else, whatever is sent."""
    if not _control_enabled():
        return JSONResponse({"error": "not found"}, status_code=404)
    limited = _staff_limited(request)
    if limited is not None:
        return limited
    name = await _staff_caller(request)
    if not name:
        return JSONResponse({"error": "sign in first"}, status_code=401)
    if not _is_same_site_json(request):
        return JSONResponse({"error": "a button press must come from the staff page itself"}, status_code=403)
    payload, failure = await _json_body(request)
    if failure is not None:
        return failure
    action, actor = payload.get("action"), staff.Actor("staff", name)
    try:
        if action in ("start_shift", "finish_shift"):
            await _staff_do(actor, action, {})
        elif action == "done":
            await _staff_do(actor, "done", {"fid": payload.get("fid"), "note": payload.get("note")})
        elif action == "acknowledge":
            await _staff_do(actor, "acknowledge", {"fid": payload.get("fid")})
        elif action == "change_password":
            return await _staff_change_password(request, name, payload)
        else:
            return JSONResponse({"error": "That action is not available here."}, status_code=400)
    except staff.StaffError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    except Exception as exc:  # noqa: BLE001 -- say so plainly instead of a bare 500; nothing was stored
        logger.error("[main.py::staff_act] error | error=%r", exc)
        return JSONResponse({"error": "That could not be saved. Try again."}, status_code=500)
    if action == "start_shift":
        await _staff_assign_waiting()                       # the failures nobody holds go to the person who has just come on shift
    return _staff_view_response(await _staff_state(), name)


@app.post("/staff/logout")
async def staff_logout(request: Request) -> JSONResponse:
    """Empties this browser's staff cookie. Nothing else changes (a shift is its own record and is not ended by signing out)."""
    return JSONResponse({"ok": True}, headers={"Set-Cookie": _staff_cookie(request, "", 0), "Cache-Control": "no-store"})


async def _panel_staff_args(action: str, payload: dict) -> dict:
    """The arguments staff.plan wants, from what the panel sent. A password is checked against his rules and hashed here (a slow hash, run off the event loop and two at a time);
    it is never stored, logged or sent anywhere in the clear."""
    if action in ("add_person", "reset_password"):
        username, password = payload.get("username"), payload.get("password")
        problem = staff.username_problem(username) if action == "add_person" else None
        if action == "add_person":
            problem = problem or staff.display_problem(payload.get("display"))
        problem = problem or staff.staff_password_problem(password)
        if problem:
            raise staff.StaffError(problem[:1].upper() + problem[1:] + ("" if problem.endswith(".") else "."))
        async with _login_slots:
            stored = await asyncio.to_thread(control_login.hash_password, password)
        args = {"username": username, "password_hash": stored}
        if action == "add_person":
            args["display"] = payload.get("display")
        return args
    if action in ("disable_person", "delete_person", "start_shift", "finish_shift"):
        return {"username": payload.get("username")}
    if action == "assign":
        return {"fid": payload.get("fid"), "people": payload.get("people")}
    if action == "unassign":
        return {"fid": payload.get("fid"), "username": payload.get("username")}
    return {"fid": payload.get("fid"), "note": payload.get("note")}


@app.get("/control/panel/staff")
async def control_panel_staff(request: Request) -> JSONResponse:
    refusal = await _control_refusal(request)
    if refusal is not None:
        return refusal
    if not _control_caller_is_super(request):
        return JSONResponse({"error": "sign in first"}, status_code=401)
    return JSONResponse(json.loads(_strict_json({"ok": True, **staff.panel_view(await _staff_state(), datetime.now(timezone.utc))})))


@app.post("/control/panel/staff")
async def control_panel_staff_act(request: Request) -> JSONResponse:
    """The super user's staff buttons: add, reset a password, disable, finish someone's shift, assign, unassign, mark Done. Needs his session (or the key), a JSON body and a same-origin request."""
    refusal = await _control_refusal(request)
    if refusal is not None:
        return refusal
    if not _control_caller_is_super(request):
        return JSONResponse({"error": "sign in first"}, status_code=401)
    if not control_access.key_matches(CONTROL_KEY, request.headers.get("x-watchdog-control")) and not _is_same_site_json(request):
        return JSONResponse({"error": "a button press must come from the control page itself"}, status_code=403)
    payload, failure = await _json_body(request)
    if failure is not None:
        return failure
    action = payload.get("action")
    if action not in STAFF_PANEL_ACTIONS:
        return JSONResponse({"error": "unknown action"}, status_code=400)
    try:
        await _staff_do(staff.SUPER, action, await _panel_staff_args(action, payload))
    except staff.StaffError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    except Exception as exc:  # noqa: BLE001 -- say so plainly instead of a bare 500; nothing was stored
        logger.error("[main.py::control_panel_staff_act] error | error=%r", exc)
        return JSONResponse({"error": "That could not be saved. Press the button again."}, status_code=500)
    if action == "start_shift":
        await _staff_assign_waiting()                       # the same rule when he starts a shift for someone
    if action in ("reset_password", "add_person", "delete_person") and isinstance(payload.get("username"), str):
        _staff_throttle.record_success(payload["username"])             # a new password, a new account or a gone one starts with no wrong tries held against the name
    return JSONResponse(json.loads(_strict_json({"ok": True, **staff.panel_view(await _staff_state(), datetime.now(timezone.utc))})))


async def _peer_refusal(request) -> Optional[JSONResponse]:
    """None for the other node (it carries the control key); otherwise the refusal. The wrong-guess limiter only counts callers who do NOT have the key, so a long catch-up of rows
    is never slowed by it."""
    if not _control_enabled():
        return JSONResponse({"error": "not found"}, status_code=404)
    if control_access.key_matches(CONTROL_KEY, request.headers.get("x-watchdog-control")):
        return None
    limited = await _control_refusal(request)
    return limited or JSONResponse({"error": "missing or invalid control key"}, status_code=401)


@app.get("/control/staff/summary")
async def control_staff_summary(request: Request) -> JSONResponse:
    refusal = await _peer_refusal(request)
    return refusal or JSONResponse(await _staff_summary())


@app.get("/control/staff/rows")
async def control_staff_rows(request: Request) -> JSONResponse:
    """One page of rows after a cursor, for the other node. Only the control key opens it (a session is not enough)."""
    refusal = await _peer_refusal(request)
    if refusal is not None:
        return refusal
    params = getattr(request, "query_params", None) or {}
    after = str(params.get("after", ""))[:80]
    try:
        limit = max(1, min(int(params.get("limit", STAFF_PAGE_LIMIT)), STAFF_PAGE_LIMIT))
    except ValueError:
        limit = STAFF_PAGE_LIMIT
    async with _db_lock:
        with db() as conn:
            rows = _staff_rows_locked(conn, after, limit)
    return JSONResponse({"rows": rows})


@app.post("/control/staff/rows")
async def control_staff_ingest(request: Request) -> JSONResponse:
    """Rows from the other node. Each one is checked like any outside input; the valid ones are added (never twice), the rest are counted and dropped."""
    refusal = await _peer_refusal(request)
    if refusal is not None:
        return refusal
    payload, failure = await _json_body(request)
    if failure is not None:
        return failure
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) > STAFF_INGEST_MAX:
        return JSONResponse({"error": f"send rows as a list of at most {STAFF_INGEST_MAX}"}, status_code=400)
    added, rejected = await _staff_append_rows(rows)
    return JSONResponse({"added": added, "rejected": rejected})


async def _staff_sync_once() -> Optional[dict]:
    """Asks the other node which rows it holds and pulls the ones this node lacks, page by page. None when it cannot be reached. Quiet (one small request) when both already
    hold the same rows. Each node does this to the other, so together they converge, in whatever order rows were made."""
    status, theirs = await asyncio.to_thread(_peer_request, "GET", "/control/staff/summary")
    if status != 200 or not isinstance(theirs, dict) or not isinstance(theirs.get("digest"), str):
        return None
    if theirs["digest"] == (await _staff_summary())["digest"]:
        return {"pulled": 0}
    pulled, after = 0, ""
    for _ in range(STAFF_SYNC_MAX_PAGES):
        status, page = await asyncio.to_thread(_peer_request, "GET", f"/control/staff/rows?after={urllib.parse.quote(after)}&limit={STAFF_PAGE_LIMIT}")
        rows = page.get("rows") if status == 200 and isinstance(page, dict) else None
        if not isinstance(rows, list) or not rows:
            break
        added, _rejected = await _staff_append_rows(rows)
        pulled += added
        last = rows[-1].get("id") if isinstance(rows[-1], dict) else None
        if not isinstance(last, str) or last <= after or len(rows) < STAFF_PAGE_LIMIT:
            break
        after = last
    return {"pulled": pulled}


async def staff_sync_loop() -> None:
    """Every minute: copy what the other node has and this one lacks. A failure is logged and never stops the loop."""
    await asyncio.sleep(5)
    while True:
        try:
            if _control_enabled():
                await _staff_sync_once()
        except Exception as exc:  # noqa: BLE001 -- the loop must never die
            logger.error("[staff_sync_loop] error (continuing): %r", exc)
        await asyncio.sleep(STAFF_SYNC_SECONDS)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """The server embeds its own token into the page at render time (see
    dashboard.py) -- the page loads straight into live status, nothing to
    type. /status itself still checks the token; this only removes the
    manual step for someone loading the page directly."""
    return HTMLResponse(_with_off_screen(_with_super_link(_with_banner(render_dashboard_html(VIEW_TOKEN)), request, await _staff_display_of(request))), headers=NO_CACHE_HEADERS)


@app.get("/topology", response_class=HTMLResponse)
async def topology(request: Request) -> HTMLResponse:
    """Second live view: six playout endpoint nodes feeding the Core listener."""
    return HTMLResponse(_with_off_screen(_with_super_link(_with_banner(render_topology_html(VIEW_TOKEN)), request, await _staff_display_of(request))), headers=NO_CACHE_HEADERS)


@app.get("/offline.html", response_class=HTMLResponse)
async def offline_page() -> HTMLResponse:
    """The branded screen a phone shows when it cannot reach this server. The service worker caches
    exactly this one static page (no status data), so it works with no connection at all. No token
    and no data on it, by design: it must never be able to look healthy."""
    return HTMLResponse(OFFLINE_HTML, headers=NO_CACHE_HEADERS)


@app.get("/robots.txt")
async def robots() -> Response:
    """Asks every crawler to stay out. Public and without data, like the manifest; the X-Robots-Tag header on every answer is the stronger half."""
    return Response("User-agent: *\nDisallow: /\n", media_type="text/plain")


@app.get("/manifest.json")
async def manifest() -> Response:
    """Makes the dashboard installable as a PWA -- on Android/Chrome this is what turns a
    mobile-browser visit into an actual "Add to Home Screen" / "Install app" prompt, per
    the owner's ask. No login and no data here, so it needs no token."""
    return Response(MANIFEST_JSON, media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker() -> Response:
    """The other half of installability: Chrome requires a registered service worker with a
    fetch handler before it will offer the install prompt. See dashboard.py for why it
    deliberately caches nothing -- a live status tool must never show stale data as if current."""
    return Response(SERVICE_WORKER_JS, media_type="application/javascript", headers=NO_CACHE_HEADERS)


@app.get("/icon-192.png")
async def icon_192() -> Response:
    return Response(_ICONS["icon-192.png"], media_type="image/png")


@app.get("/icon-512.png")
async def icon_512() -> Response:
    return Response(_ICONS["icon-512.png"], media_type="image/png")


@app.get("/icon-512-maskable.png")
async def icon_512_maskable() -> Response:
    return Response(_ICONS["icon-512-maskable.png"], media_type="image/png")


@app.get("/apple-touch-icon.png")
async def apple_touch_icon() -> Response:
    return Response(_ICONS["apple-touch-icon.png"], media_type="image/png")


@app.get("/favicon.ico")
async def favicon() -> Response:
    # Browsers probe this exact path by convention regardless of the <link rel="icon"> tag.
    return Response(_ICONS["favicon-32.png"], media_type="image/png")


# The event log is the only table that grows. Nothing on the dashboard reads history this old, so anything older is removed once a day.
EVENT_RETENTION_DAYS = int(os.environ.get("WATCHDOG_EVENT_RETENTION_DAYS", "180"))


def prune_old_events(conn, now: datetime, days: int = EVENT_RETENTION_DAYS) -> int:
    """Delete events older than `days` days. Returns how many were removed."""
    cutoff = (now - timedelta(days=days)).isoformat()
    return conn.execute("DELETE FROM events WHERE timestamp_utc < ?", (cutoff,)).rowcount


# After the LISTENER itself has been down (Core rebooted), every source's last heartbeat is minutes old when it comes back, and without this the very first
# check would raise a "heartbeat lost" critical alarm (an e-mail and a push) for every source before any sender could report in. A real outage that is
# still there when the grace ends is raised then, at most this many seconds later than it would have been.
LISTENER_STARTUP_GRACE_SECONDS = _env_int("WATCHDOG_LISTENER_STARTUP_GRACE_SECONDS", 90, 0, 600)


def _in_startup_grace(now: datetime, started: datetime, grace_seconds: int) -> bool:
    return grace_seconds > 0 and (now - started).total_seconds() < grace_seconds


async def staleness_checker_loop() -> None:
    """Background loop: escalates overdue endpoint telemetry without polling from the browser."""
    last_prune_day = None
    while True:
        try:
            now = datetime.now(timezone.utc)
            now_iso = now.isoformat()
            if _in_startup_grace(now, STARTED_AT, LISTENER_STARTUP_GRACE_SECONDS):
                await asyncio.sleep(CHECK_LOOP_SECONDS)
                continue
            if now.date() != last_prune_day:
                async with _db_lock:
                    with db() as conn:
                        removed = prune_old_events(conn, now)
                        prune_old_site_c_history(conn, now)
                last_prune_day = now.date()
                if removed:
                    logger.info("[prune] removed %s events older than %s days", removed, EVENT_RETENTION_DAYS)
            newly_lost = []
            endpoint_emails = []
            async with _db_lock:
                with db() as conn:
                    rows = conn.execute("SELECT * FROM sources WHERE state = 'healthy'").fetchall()
                    for row in rows:
                        last_seen = datetime.fromisoformat(row["last_seen_utc"])
                        age = (now - last_seen).total_seconds()
                        threshold_seconds = row["expected_interval_seconds"] + CRITICAL_OVERDUE_SECONDS
                        if age > threshold_seconds:
                            conn.execute(
                                "UPDATE sources SET state = 'heartbeat_lost', state_changed_utc = ? WHERE source_id = ?",
                                (now_iso, row["source_id"]),
                            )
                            newly_lost.append((row["source_id"], age, threshold_seconds, row["last_payload_json"]))
                    _sources, _events, _endpoints, endpoint_emails = _evaluate_and_sync_locked(conn, now, now_iso)

            for source_id, age, threshold_seconds, last_payload_json in newly_lost:
                await record_event(source_id, "heartbeat_lost", f"no heartbeat for {age:.0f}s (threshold {threshold_seconds}s)")
            for endpoint in endpoint_emails:
                await _send_red_alarm(endpoint)
        except Exception as exc:  # noqa: BLE001 -- the checker loop must never die
            logger.error("[staleness_checker_loop] error (continuing): %r", exc)
        await asyncio.sleep(CHECK_LOOP_SECONDS)


async def system_sampler_loop() -> None:
    """Reads Core's CPU, memory and disk every few seconds. The first reading only sets the baseline a CPU
    percentage is measured against, so a second one is taken a moment later to have a number from the start."""
    _system.sample()
    await asyncio.sleep(0.5)
    while True:
        try:
            _system.sample()
        except Exception as exc:  # noqa: BLE001 -- vital signs are decoration; they must never take the listener down
            logger.error("[system_sampler_loop] error (continuing): %r", exc)
        await asyncio.sleep(SAMPLE_SECONDS)


@app.on_event("startup")
async def on_startup() -> None:
    init_db()
    await _load_control()
    if IS_STANDBY or PRIMARY_URL:
        await _restore_proof()
    asyncio.create_task(staleness_checker_loop())
    asyncio.create_task(system_sampler_loop())
    if _control_enabled():
        await _staff_state()
        asyncio.create_task(staff_sync_loop())
    if deadman_enabled():
        asyncio.create_task(deadman_loop())
        logger.info("Dead man's switch enabled: pinging every %ss while the database and the email login work.", DEADMAN_INTERVAL_SECONDS)
    if _control_enabled():
        asyncio.create_task(control_notice_loop())
        logger.info("[main.py::on_startup] decision | control=on node=%s preferred=%s access=%s", NODE, CONTROL["state"].preferred, "on" if CONTROL["state"].access_on else "off")
    if PRIMARY_URL:
        asyncio.create_task(primary_watch_loop())
        logger.info("[main.py::on_startup] decision | role=%s watching=%s_health every=%ss failures_to_takeover=%s", ROLE, "core" if IS_STANDBY else "vps", PROBE_INTERVAL_SECONDS, WATCH_CONFIG.down_after_failures)
    elif IS_STANDBY:
        logger.error("[main.py::on_startup] error | role=standby but WATCHDOG_PRIMARY_URL is empty, so Core is never probed and the standby will never take over")
    if IS_STANDBY:
        asyncio.create_task(standby_alive_loop())
        logger.info("[main.py::on_startup] decision | role=standby self_check_every=%ss ping=%s expect_copies=%s", ALIVE_SECONDS, "on" if STANDBY_ALIVE_URL else "off", list(HEALTH_CONFIG.expect_copies))
    if KEY_WATCHER is not None:
        asyncio.create_task(keys_reload_loop())
        logger.info("[main.py::on_startup] decision | sender_keys=file watching=%s every=%ss keyset=%s", SENDER_KEYS_FILE, KEYS_POLL_SECONDS, KEYRING.digest())
    # Logging the effective config makes a mis-set value visible at startup, not only when it misbehaves.
    logger.info(
        "Watchdog listener started. heartbeat_interval=%ss missed_before_alert=%s smtp_configured=%s expected_instances=%s",
        HEARTBEAT_INTERVAL_SECONDS, MISSED_INTERVALS_BEFORE_ALERT, bool(SMTP_HOST and SMTP_USER), EXPECTED_INSTANCES,
    )
