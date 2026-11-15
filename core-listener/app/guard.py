"""Who is talking to the listener, and over what: the pure logic behind a security review.

Kept apart from main.py on purpose. Nothing here touches FastAPI, the database or the clock by itself (every function is handed what it needs),
so it can be tested alone and read in one sitting. main.py only wires it in.

Route diagnostics: which network path did each sender's post really take, and was the key protected on it?
Per-sender keys: a key taken from one machine can only act as that machine.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
from collections import deque
from datetime import datetime, timedelta
from typing import NamedTuple, Optional

# ---- Route kinds -------------------------------------------------------------------------------------------------------------------------------
# Read from what the SENDER DIALLED (the Host header) and from headers only Cloudflare adds. The peer (TCP) address is deliberately not used: the
# listener runs behind Docker's port proxy, which rewrites every outside peer to the bridge gateway (172.x.0.1), so it cannot tell a LAN post from a
# Tailscale one. The Host header is exactly "the address the sender was configured with".
ROUTE_KINDS = ("lan", "tailscale", "cloudflare", "public-ip", "other")
UNTRUSTED_KINDS = ("cloudflare", "public-ip", "other")          # paths that cross, or may cross, networks nobody here controls

_TAILSCALE_NETWORKS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
# Spelled out rather than ipaddress's is_private: that also counts documentation ranges (203.0.113.0/24 ...) and loopback as private.
_LAN_NETWORKS = tuple(ipaddress.ip_network(text) for text in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fc00::/7", "fe80::/10"))
_CLOUDFLARE_HEADERS = ("cf-ray", "cf-connecting-ip", "cf-visitor", "cf-ipcountry")


def _host_only(value: str) -> str:
    """'192.168.7.183:8420' -> '192.168.7.183'; '[fd7a::1]:8420' -> 'fd7a::1'; a bare name or address is returned as is."""
    text = value.strip().lower()
    if text.startswith("["):
        end = text.find("]")
        return text[1:end] if end > 0 else ""
    if text.count(":") == 1:                       # name:port or ipv4:port (more than one colon is an unbracketed IPv6 literal)
        return text.split(":", 1)[0]
    return text


def _address_kind(host: str) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "tailscale" if host.endswith(".ts.net") else "other"      # a MagicDNS name is Tailscale; any other name is not something we can place
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        return "other"
    if any(address in network for network in _TAILSCALE_NETWORKS):         # checked before the LAN list: fd7a:... is also inside fc00::/7
        return "tailscale"
    if any(address in network for network in _LAN_NETWORKS):
        return "lan"
    return "public-ip"


def route_kind(headers, public_host: str = "", behind_trusted_proxy: bool = False) -> str:
    """One of ROUTE_KINDS for a request. `headers` needs only .get() with lower-case names (Starlette's Headers and a plain dict both work).

    A Cloudflare header means the request came through Cloudflare, whatever Host says (the tunnel may rewrite it). The public hostname WITHOUT those
    headers is `other`: somebody is talking to the origin directly while borrowing the name.

    `behind_trusted_proxy` is for the standby, which sits behind the VPS's own reverse proxy and not behind Cloudflare: the request was relayed by that
    proxy, so the public hostname IS how it arrived (`public-ip`), and any Cloudflare header is only a claim by the client and is ignored."""
    try:
        if not behind_trusted_proxy:
            for name in _CLOUDFLARE_HEADERS:
                if str(headers.get(name) or "").strip():
                    return "cloudflare"
        host = _host_only(str(headers.get("host") or ""))
        if not host:
            return "other"
        if host == str(public_host or "").lower():
            return "public-ip" if behind_trusted_proxy else "other"
        return _address_kind(host)
    except Exception:  # noqa: BLE001 -- classification is bookkeeping and must never break a heartbeat
        return "other"


def route_protection(kind: str, scheme: str) -> str:
    """'encrypted', 'cleartext' or 'unknown' for the sender's key on that route.

    Tailscale is WireGuard end to end, so it is encrypted whatever the application scheme says. Everywhere else the scheme decides: a plain-http post
    on the LAN is cleartext on a network we control, while the same post through Cloudflare or on a public address is cleartext on the open internet."""
    if kind == "tailscale":
        return "encrypted"
    if kind in ("lan", "cloudflare", "public-ip"):
        if scheme == "https":
            return "encrypted"
        return "cleartext" if scheme == "http" else "unknown"
    return "unknown"


# ---- What /status shows --------------------------------------------------------------------------------------------------------------------------
ROUTE_WINDOW_SECONDS = 24 * 3600     # how long a route is remembered after a sender last used it
ROUTE_FRESH_SECONDS = 900            # "is exposed right now" only looks at posts this recent
ROUTE_MAX_SOURCES = 64               # the source name comes from the caller, so the record is bounded like the other per-source records


class RouteLog:
    """Per source: the route of its latest post and when each route was last used. No address of any machine is ever stored, only the kind."""

    def __init__(self, max_sources: int = ROUTE_MAX_SOURCES, window_seconds: int = ROUTE_WINDOW_SECONDS, fresh_seconds: int = ROUTE_FRESH_SECONDS):
        self._max_sources = max_sources
        self._window = timedelta(seconds=window_seconds)
        self._fresh = timedelta(seconds=fresh_seconds)
        self._by_source: dict = {}      # source -> {"kind", "protection", "at": datetime, "last_by_kind": {kind: datetime}}

    def clear(self) -> None:
        self._by_source.clear()

    def note(self, source_id: str, kind: str, protection: str, now: datetime) -> None:
        kind = kind if kind in ROUTE_KINDS else "other"
        entry = self._by_source.get(source_id)
        if entry is None:
            if len(self._by_source) >= self._max_sources:
                self._by_source.pop(next(iter(self._by_source)))        # oldest-inserted goes first: the cap protects memory, not history
            entry = {"last_by_kind": {}}
            self._by_source[source_id] = entry
        entry.update(kind=kind, protection=protection, at=now)
        entry["last_by_kind"][kind] = now

    def snapshot(self, now: datetime) -> dict:
        shown: dict = {}
        for source_id, entry in self._by_source.items():
            if now - entry["at"] > self._window:
                continue
            recent = {kind: when for kind, when in entry["last_by_kind"].items() if now - when <= self._window}
            shown[source_id] = {"kind": entry["kind"], "protection": entry["protection"], "at": entry["at"].isoformat(),
                                "last_by_kind": {kind: when.isoformat() for kind, when in sorted(recent.items())}}
        return shown

    def exposed(self, now: datetime) -> list:
        """Sources whose latest post, within the last 15 minutes, put the key on an untrusted route without encryption."""
        return sorted(source_id for source_id, entry in self._by_source.items()
                      if now - entry["at"] <= self._fresh and entry["kind"] in UNTRUSTED_KINDS and entry["protection"] != "encrypted")


# ---- Per-sender keys ------------------------------------------------------------------------------------------------------
# Until now all four senders held ONE key and `hostname` in the body was whatever the caller wrote, so a key taken from one machine could post as any
# of them (hide a dead player, forge a heartbeat). Now every sender can hold its OWN key, valid only for its own name:
#
#   WATCHDOG_SENDER_KEY_<NAME>            the sender's current key         (NAME upper-case, "-" becomes "_": BACKUP-SERVER -> BACKUP_SERVER)
#   WATCHDOG_SENDER_KEY_<NAME>_PREVIOUS   the key it is moving away from, accepted for that one sender only while ITS window is open
#   WATCHDOG_SENDER_AUTH                  both (default) | per-sender
#
# `both` is today's behaviour plus the new keys: the old shared key still posts for anyone, so senders can move over one at a time. `per-sender` is
# the end state: the shared key becomes an ADMIN key (it reads, and resets alarms) and can no longer post heartbeats or events.
# The keys are independent random values, not derived from one secret: there is nothing to work out another sender's key from, and rotating one
# sender touches no other.
AUTH_MODES = ("both", "per-sender")
SENDER_KEY_MIN_LENGTH = 24
AUTH_FRESH_SECONDS = 900             # "heard on its own key" only counts posts this recent
AUTH_MISMATCH_SECONDS = 24 * 3600    # how long a refused cross-source attempt stays visible
AUTH_MAX_ENTRIES = 64


class KeyConfigError(Exception):
    """The key settings are unusable. The listener refuses to start rather than run with a binding that is quietly weaker than intended.
    Messages name the variable, never its value."""


def sender_key_var(source_id: str, previous: bool = False) -> str:
    return "WATCHDOG_SENDER_KEY_" + re.sub(r"[^A-Z0-9]", "_", source_id.upper()) + ("_PREVIOUS" if previous else "")


def parse_auth_mode(setting) -> str:
    value = str(setting or "").strip().lower()
    if value in ("", "both"):
        return "both"
    if value == "per-sender":
        return "per-sender"
    # A typo must not silently leave the looser mode on: stop the start instead.
    raise KeyConfigError("WATCHDOG_SENDER_AUTH must be 'both' or 'per-sender'")


def load_sender_keys(environ, source_ids) -> dict:
    """{source: (current, previous)} for each source that has a key set; an unset or empty value is simply absent ("")."""
    loaded: dict = {}
    owner_of: dict = {}                       # key -> source that holds it, to catch one key used for two sources
    for source in source_ids:
        pair = []
        for previous in (False, True):
            variable = sender_key_var(source, previous)
            value = str(environ.get(variable) or "").strip()
            if value:
                if len(value) < SENDER_KEY_MIN_LENGTH or re.search(r"\s", value):
                    raise KeyConfigError(f"{variable} is too short or contains whitespace (at least {SENDER_KEY_MIN_LENGTH} characters, no spaces)")
                holder = owner_of.get(value)
                if holder is not None and holder != source:
                    raise KeyConfigError(f"{variable} holds a key that another sender also holds: each sender needs its own key")
                owner_of[value] = source
            pair.append(value)
        current, previous_key = pair
        if previous_key and previous_key == current:
            previous_key = ""                 # moving to the very same key: no window to speak of
        if current or previous_key:
            loaded[source] = (current, previous_key)
    return loaded


class Identity(NamedTuple):
    kind: str                                # "own" | "own-previous" | "shared" | "none"
    source: Optional[str] = None             # the sender a key is bound to; None for shared and none


NOBODY = Identity("none", None)


class Keyring:
    """Who a presented key belongs to. Holds only the per-sender keys; the shared keys are handed in at each call, so a rotation that changes them is
    seen at once."""

    def __init__(self, sender_keys: dict, mode: str = "both"):
        self._keys = dict(sender_keys)
        self.mode = mode

    def identify(self, token, shared_keys) -> Identity:
        if not isinstance(token, str) or not token:
            return NOBODY
        raw = token.encode("utf-8")
        found = NOBODY
        # Every candidate is compared, even after a match: stopping early would let a caller learn by timing which key it came close to.
        for source, (current, previous) in self._keys.items():
            for kind, key in (("own", current), ("own-previous", previous)):
                if key and hmac.compare_digest(raw, key.encode("utf-8")) and found is NOBODY:
                    found = Identity(kind, source)
        shared_hit = False
        for key in shared_keys:
            if hmac.compare_digest(raw, key.encode("utf-8")):
                shared_hit = True
        return Identity("shared", None) if shared_hit and found is NOBODY else found

    def may_post(self, identity: Identity) -> bool:
        """Heartbeats and events: any sender key, and the shared key only while the mode is `both`."""
        if identity.kind in ("own", "own-previous"):
            return True
        return identity.kind == "shared" and self.mode == "both"

    @staticmethod
    def is_admin(identity: Identity) -> bool:
        """Resetting an alarm: only the shared (admin) key, never a sender's."""
        return identity.kind == "shared"

    @staticmethod
    def may_act_as(identity: Identity, source_id: str) -> bool:
        """A sender's key acts only as its own name, exactly. The shared key has no binding (and may not post at all in `per-sender` mode)."""
        if identity.kind in ("own", "own-previous"):
            return identity.source == source_id
        return True

    def keyed_sources(self) -> list:
        return sorted(source for source, (current, previous) in self._keys.items() if current or previous)

    def windows_open(self) -> list:
        return sorted(source for source, (_current, previous) in self._keys.items() if previous)

    def digest(self) -> str:
        """keyset_digest of the keys in force (names and 12 hex characters, never a key): what lets two listeners, or a tool and a listener, be compared."""
        return keyset_digest(self._keys)


# ---- The sender-keys FILE (standby): a key rotation on Core reaches the standby as a file change, with no restart ----------------------------------
# Core keeps its keys in .env, which only a recreated container re-reads. A standby must not be restarted to take a new key (a restart would forget what it
# believes about Core in the middle of a takeover), so it can read the same WATCHDOG_SENDER_KEY_* lines from one file and notice when that file changes.
# Rules: strict (anything unexpected is an error, never ignored), all-or-nothing (a file with any problem changes nothing: the keys in force stay in force and
# the problem is counted and reported), and never lock everyone out (an empty file is a problem, not "no keys"). Messages name a setting, never a value.
KEYS_FILE_MAX_BYTES = 8192
_KEYS_FILE_LINE = re.compile(r"^(WATCHDOG_SENDER_KEY_[A-Z0-9_]+)=(.*)$")


def parse_sender_keys_file(text: str, source_ids) -> dict:
    """{VARIABLE: value} for the sender-key settings in a keys file, ready for load_sender_keys(). Raises KeyConfigError for anything but comments, blank lines
    and `WATCHDOG_SENDER_KEY_<NAME>[_PREVIOUS]=value` for a known sender, each at most once, with no quote, backslash or space in the value."""
    if len(text.encode("utf-8")) > KEYS_FILE_MAX_BYTES:
        raise KeyConfigError(f"the keys file is larger than {KEYS_FILE_MAX_BYTES} bytes")
    allowed = {sender_key_var(source, previous) for source in source_ids for previous in (False, True)}
    found: dict = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _KEYS_FILE_LINE.match(line)
        if not match:
            raise KeyConfigError(f"line {number} of the keys file is not NAME=value for a sender key setting")
        name, value = match.groups()
        if name not in allowed:
            raise KeyConfigError(f"line {number}: {name} is not a sender key setting this listener knows")
        if name in found:
            raise KeyConfigError(f"line {number}: {name} appears twice")
        if re.search(r"[\"'\\\s]", value):
            raise KeyConfigError(f"{name} contains a quote, a backslash or a space")
        found[name] = value
    if not any(found.values()):
        raise KeyConfigError("the keys file holds no sender key (an empty file would lock every sender out)")
    return found


def keyset_digest(sender_keys: dict) -> str:
    """12 hex characters that stand for a whole set of sender keys, so two listeners (or a tool and a listener) can be compared without a key ever being shown.
    It cannot be turned back into a key: SHA-256, cut to 48 bits, over keys that are long and random. "" for no keys."""
    lines = [f"{source}|{current}|{previous}" for source, (current, previous) in sorted(sender_keys.items()) if current or previous]
    if not lines:
        return ""
    return hashlib.sha256(("clarix-keyset-v1\n" + "\n".join(lines)).encode("utf-8")).hexdigest()[:12]


class KeyFileWatcher:
    """Reads the keys file once at start (a bad file stops the start) and then notices changes. poll() returns the new keys only when the file changed AND the whole
    file is valid AND the keys really differ; otherwise None, and a bad file is counted once, not on every poll."""

    def __init__(self, path: str, source_ids):
        self.path = path
        self._sources = tuple(source_ids)
        self.errors = 0                      # files judged bad since start
        self.last_error = ""                 # about the latest judgement; "" while the file in force is the file on disk
        self.loaded_at: Optional[datetime] = None
        self.digest = ""                     # keyset_digest of the keys in force
        self._seen = None                    # signature of the file last judged (good or bad); None while it is missing

    def _signature(self):
        info = os.stat(self.path)
        return (info.st_mtime_ns, info.st_size, info.st_ino)

    def _judge(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8", newline="") as handle:
                text = handle.read(KEYS_FILE_MAX_BYTES + 1)
        except FileNotFoundError as error:
            raise KeyConfigError("the keys file is missing") from error
        except UnicodeDecodeError as error:
            raise KeyConfigError("the keys file is not valid UTF-8 text") from error
        except OSError as error:
            raise KeyConfigError(f"the keys file cannot be read ({type(error).__name__})") from error
        return load_sender_keys(parse_sender_keys_file(text, self._sources), self._sources)

    def load(self, now: Optional[datetime] = None) -> dict:
        """The keys at start. Raises KeyConfigError, which stops the start."""
        keys = self._judge()
        try:
            self._seen = self._signature()
        except OSError:
            self._seen = None
        self.digest = keyset_digest(keys)
        self.loaded_at = now
        return dict(keys)

    def _problem(self, message: str) -> None:
        self.errors += 1
        self.last_error = message

    def poll(self, now: datetime) -> Optional[dict]:
        try:
            signature = self._signature()
        except OSError:
            if self._seen is not None:                       # the file just disappeared: say so once
                self._seen = None
                self._problem("the keys file is missing")
            return None
        if signature == self._seen:
            return None
        self._seen = signature
        try:
            keys = self._judge()
        except KeyConfigError as error:
            self._problem(str(error))
            return None
        self.last_error = ""
        digest = keyset_digest(keys)
        if digest == self.digest:
            return None                                      # rewritten, same keys: nothing to hand over
        self.digest = digest
        self.loaded_at = now
        return dict(keys)


class RefusalLog:
    """When posts were refused for a wrong key (times only, bounded), so a standby can notice that a studio's key no longer matches."""
    MAX_ENTRIES = 256

    def __init__(self):
        self._times: deque = deque(maxlen=self.MAX_ENTRIES)

    def note(self, now: datetime) -> None:
        self._times.append(now)

    def recent(self, now: datetime, window_seconds: float) -> int:
        cutoff = now - timedelta(seconds=window_seconds)
        return sum(1 for moment in self._times if moment >= cutoff)

    def stored(self) -> int:
        return len(self._times)

    def clear(self) -> None:
        self._times.clear()


# ---- Read-side limits -----------------------------------------------------------------------------------
# The dashboard page is public and carries a read-only key, so anyone who can load it can open streams and ask for /status. Writes were already bounded;
# these bound the reads PER VISITOR, so one noisy client cannot use up what everybody shares. They are fairness limits, not authentication, and they are a
# second line: the first is Cloudflare in front (rate limiting and, if chosen, Cloudflare Access) and the host firewall that closes the direct path.
STREAMS_PER_VISITOR = 8                # a page, plus a reconnect in flight, plus a few wall screens behind one address
STATUS_PER_MINUTE = 60
MAX_VISITORS = 2048                    # memory bound: the visitor key comes from the network, so the table cannot grow without limit
# Cloudflare's headers are believed only from a peer inside these networks: the Docker bridge range the tunnel container lives in (and loopback). A client
# that reaches the port from anywhere else (the LAN, Tailscale, the open internet) gets keyed by its own address, however many headers it writes.
DEFAULT_TRUSTED_PROXIES = (ipaddress.ip_network("172.16.0.0/12"), ipaddress.ip_network("127.0.0.1/32"))


def parse_trusted_proxies(setting) -> tuple:
    """WATCHDOG_TRUSTED_PROXY_CIDRS: comma-separated networks. Empty means the default. A bad entry stops the start (a mistyped trust list must not silently
    trust everything or nothing)."""
    text = str(setting or "").strip()
    if not text:
        return DEFAULT_TRUSTED_PROXIES
    try:
        return tuple(ipaddress.ip_network(part.strip()) for part in text.split(",") if part.strip())
    except ValueError as error:
        raise KeyConfigError("WATCHDOG_TRUSTED_PROXY_CIDRS holds an entry that is not a network (" + str(error)[:60] + ")") from error


def parse_forwarded_trust(setting) -> tuple:
    """WATCHDOG_TRUST_FORWARDED_FROM: the networks (normally just 127.0.0.1) whose X-Forwarded-For and X-Forwarded-Proto are believed. Empty means OFF, which is
    what Core uses (it is behind Cloudflare, not behind a plain reverse proxy). The standby sets it to the loopback address its Caddy connects from. While it is
    set, Cloudflare's headers are never believed. A bad entry stops the start: a mistyped trust list must not silently trust everything or nothing."""
    text = str(setting or "").strip()
    if not text:
        return ()
    try:
        return tuple(ipaddress.ip_network(part.strip()) for part in text.split(",") if part.strip())
    except ValueError as error:
        raise KeyConfigError("WATCHDOG_TRUST_FORWARDED_FROM holds an entry that is not an address or network (" + str(error)[:60] + ")") from error


def forwarded_peer_trusted(peer_host, forwarded_trust) -> bool:
    """True when the TCP peer is one of the networks named in WATCHDOG_TRUST_FORWARDED_FROM. An empty list (the setting is off) trusts nobody."""
    if not forwarded_trust:
        return False
    return _peer_is_trusted(peer_host, forwarded_trust, frozenset())


def read_default_gateways(path: str = "/proc/net/route"):
    """The default-route gateways of THIS container (on Docker, the bridge gateway: the host itself), or None when the routing table cannot be read."""
    try:
        with open(path, encoding="ascii") as handle:
            return parse_default_gateways(handle.read())
    except OSError:
        return None


def parse_default_gateways(route_table: str) -> frozenset:
    """Gateways of the default routes in /proc/net/route text. The addresses are 8 hex digits, least-significant byte first (0100150A is 10.21.0.1)."""
    found = set()
    for line in str(route_table or "").splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 8 and fields[1] == "00000000" and fields[7] == "00000000":
            try:
                found.add(str(ipaddress.IPv4Address(bytes.fromhex(fields[2])[::-1])))
            except ValueError:
                continue
    return frozenset(found)


def _looks_like_bridge_gateway(peer) -> bool:
    """The fallback guess, used only when the routing table is unreadable: Docker gives a bridge's gateway (the host itself) the address x.y.0.1."""
    return peer.version == 4 and (int(peer) & 0xFFFF) == 1


def _peer_is_trusted(peer_host, trusted, gateways=None) -> bool:
    """True when the peer is inside a trusted network. The container's gateway never counts when it is merely inside a range: connections relayed by the
    host (from the LAN, from Tailscale, through the port proxy) arrive as the gateway, and those are the clients that could write Cloudflare headers
    themselves. `gateways` is the exact set read from the routing table; None (unreadable) falls back to the x.y.0.1 pattern. Naming a gateway address
    as a /32 is the operator's explicit choice and is honoured."""
    try:
        peer = ipaddress.ip_address(str(peer_host or "").strip())
    except ValueError:
        return False
    is_gateway = (str(peer) in gateways) if gateways is not None else _looks_like_bridge_gateway(peer)
    for network in trusted:
        if peer.version != network.version or peer not in network:
            continue
        if network.num_addresses > 1 and is_gateway:
            continue
        return True
    return False


def visitor_key(headers, peer_host, trusted=DEFAULT_TRUSTED_PROXIES, gateways=None, forwarded_trust=()) -> str:
    """Who is asking, for per-visitor limits ONLY. Behind the Cloudflare tunnel the TCP peer is always the tunnel, so the visitor is the address Cloudflare
    reports in CF-Connecting-IP, believed only when (a) the peer is inside the trusted networks (the tunnel's own) and is not the container's gateway, and
    (b) the request carries Cloudflare's other marker (CF-Ray) too. X-Forwarded-For, X-Real-IP and the like are never believed: a visitor can write those
    himself. An IPv6 visitor is one visitor per /64 (he owns the whole /64). Anything else is keyed by the TCP peer."""
    try:
        if forwarded_trust:
            # Behind the VPS's own reverse proxy (the standby): the proxy's X-Forwarded-For is believed, from that one peer only, and Cloudflare's headers
            # are only claims by the client. The right-most entry is the one the proxy itself added; anything to its left is what the client wrote.
            if forwarded_peer_trusted(peer_host, forwarded_trust):
                forwarded = str(headers.get("x-forwarded-for") or "").split(",")[-1].strip()
                if forwarded:
                    address = ipaddress.ip_address(forwarded)
                    if isinstance(address, ipaddress.IPv6Address):
                        address = ipaddress.ip_network(f"{address}/64", strict=False).network_address
                    return "xff:" + str(address)
        else:
            reported = str(headers.get("cf-connecting-ip") or "").strip()
            if reported and str(headers.get("cf-ray") or "").strip() and _peer_is_trusted(peer_host, trusted, gateways):
                address = ipaddress.ip_address(reported)
                if isinstance(address, ipaddress.IPv6Address):
                    address = ipaddress.ip_network(f"{address}/64", strict=False).network_address
                return "cf:" + str(address)
    except ValueError:
        pass                                   # not an address: fall through to the peer
    except Exception:  # noqa: BLE001 -- bookkeeping must never break a request
        pass
    peer = str(peer_host or "").strip()[:64]
    return "peer:" + (peer or "unknown")


class StreamHandle:
    """One live stream's place in a StreamPool. `evicted` becomes True when the pool took the slot back for a newcomer; the stream's generator checks it."""

    __slots__ = ("visitor", "number", "evicted")

    def __init__(self, visitor: str, number: int):
        self.visitor = visitor
        self.number = number
        self.evicted = False


class StreamPool:
    """The live streams of one audience, with a fixed capacity, a cap per visitor and fairness when the pool is full.

    Why this exists (the audit's remaining worry): a handful of internet addresses could fill every stream and blank the dashboard for the real viewers. The
    origin cannot beat a large botnet (that is Cloudflare's job: rate limiting, Bot Fight Mode) but it can make a few addresses useless:

    * each visitor holds at most `per_visitor` streams;
    * when the pool is full, a newcomer takes ONE stream from the visitor who holds the most, but only if that visitor holds at least TWO more than the newcomer
      does. Evictions therefore settle at an equal share and never ping-pong (an evicted visitor who reconnects holds as many as the others and displaces nobody);
    * a slot is held until the response is over (the listener cuts a stalled client off after a send timeout), and given back exactly once: `release` is safe to
      repeat, and an evicted handle has already given its slot up.

    As many different addresses as there are slots, each holding one, cannot be told apart from real viewers at this level; that is the honest limit.
    Holds no address: visitors are keys the caller chose, and the snapshot shows counts only. The pool never holds more than `capacity` handles, so it is bounded."""

    def __init__(self, capacity: int, per_visitor: int):
        self.capacity = max(0, int(capacity))
        self.per_visitor = max(1, int(per_visitor))
        self.evictions = 0
        self._by_visitor: dict = {}              # visitor -> [handles], oldest first
        self._total = 0
        self._next = 0

    def admit(self, visitor: str):
        """(handle, "ok"), or (None, "visitor-cap") when this visitor already holds its share, or (None, "full") when the pool is full and nobody can fairly be displaced."""
        mine = len(self._by_visitor.get(visitor, ()))
        if mine >= self.per_visitor:
            return None, "visitor-cap"
        if self._total >= self.capacity and not self._evict_for(mine):
            return None, "full"
        self._next += 1
        handle = StreamHandle(visitor, self._next)
        self._by_visitor.setdefault(visitor, []).append(handle)
        self._total += 1
        return handle, "ok"

    def _evict_for(self, mine: int) -> bool:
        if not self._by_visitor:
            return False
        heaviest = max(self._by_visitor, key=lambda name: len(self._by_visitor[name]))       # first of equals: the visitor that has been here longest
        if len(self._by_visitor[heaviest]) < mine + 2:
            return False
        victim = self._by_visitor[heaviest].pop(0)
        if not self._by_visitor[heaviest]:
            del self._by_visitor[heaviest]
        victim.evicted = True
        self._total -= 1
        self.evictions += 1
        return True

    def release(self, handle) -> None:
        streams = self._by_visitor.get(handle.visitor)
        if streams is None or handle not in streams:
            return                                # already released, or evicted (an evicted stream gave its slot up when it was evicted)
        streams.remove(handle)
        if not streams:
            del self._by_visitor[handle.visitor]
        self._total -= 1

    def count(self, visitor: str) -> int:
        return len(self._by_visitor.get(visitor, ()))

    def total(self) -> int:
        return self._total

    def snapshot(self) -> dict:
        return {"open": self._total, "capacity": self.capacity}


class VisitorRate:
    """At most `limit` requests per `window_seconds` for each visitor (a sliding window). A refused request is not recorded, so hammering while refused
    does not extend the wait. When the table is full, visitors whose requests have all expired are forgotten first; only if every remaining visitor is
    still inside its window is the oldest forgotten (a read-only route: availability matters more than an exact count under that kind of flood)."""

    def __init__(self, limit: int = STATUS_PER_MINUTE, window_seconds: float = 60.0, max_visitors: int = MAX_VISITORS):
        self._limit = limit
        self._window = window_seconds
        self._max_visitors = max_visitors
        self._seen: dict = {}                  # visitor -> deque of request times (at most `limit` of them)

    def _recent(self, visitor: str, now: float):
        history = self._seen.get(visitor)
        if history is not None:
            while history and now - history[0] >= self._window:
                history.popleft()
            if not history:
                self._seen.pop(visitor, None)
                return None
        return history

    def _make_room(self, now: float) -> None:
        for visitor in list(self._seen):
            self._recent(visitor, now)         # drops every visitor whose requests have all left the window
        while len(self._seen) >= self._max_visitors:
            self._seen.pop(next(iter(self._seen)))

    def allow(self, visitor: str, now: float) -> bool:
        history = self._recent(visitor, now)
        if history is not None and len(history) >= self._limit:
            return False
        if history is None:
            if len(self._seen) >= self._max_visitors:
                self._make_room(now)
            history = self._seen.setdefault(visitor, deque())
        history.append(now)
        return True

    def retry_after(self, visitor: str, now: float) -> int:
        history = self._recent(visitor, now)
        if not history:
            return 0
        return max(1, int(self._window - (now - history[0])) + 1)

    def recent(self, visitor: str) -> int:
        return len(self._seen.get(visitor, ()))

    def visitors(self) -> int:
        return len(self._seen)


class AuthLog:
    """What /status shows about credentials: which kind of key each source last used, and refused attempts to act as someone else. Names only."""

    def __init__(self):
        self._used: dict = {}                # source -> (credential kind, datetime)
        self._mismatches: dict = {}          # (key of, claimed) -> [datetime, count]

    def clear(self) -> None:
        self._used.clear()
        self._mismatches.clear()

    def note(self, source_id: str, credential: str, now: datetime) -> None:
        if source_id not in self._used and len(self._used) >= AUTH_MAX_ENTRIES:
            self._used.pop(next(iter(self._used)))
        self._used[source_id] = (credential, now)

    def used(self, now: datetime) -> dict:
        fresh = timedelta(seconds=AUTH_FRESH_SECONDS)
        return {source: {"credential": credential, "at": when.isoformat()}
                for source, (credential, when) in sorted(self._used.items()) if now - when <= fresh}

    def mismatch(self, key_of: str, claimed: str, now: datetime) -> int:
        """Records a refused attempt and returns how many times this pair has been tried (so the caller can log the first and then only now and then)."""
        pair = (key_of, claimed)
        if pair not in self._mismatches and len(self._mismatches) >= AUTH_MAX_ENTRIES:
            self._mismatches.pop(next(iter(self._mismatches)))
        entry = self._mismatches.setdefault(pair, [now, 0])
        entry[0], entry[1] = now, entry[1] + 1
        return entry[1]

    def mismatches(self, now: datetime) -> list:
        keep = timedelta(seconds=AUTH_MISMATCH_SECONDS)
        rows = [{"key_of": pair[0], "claimed": pair[1], "at": when.isoformat(), "count": count}
                for pair, (when, count) in self._mismatches.items() if now - when <= keep]
        return sorted(rows, key=lambda row: row["at"], reverse=True)
