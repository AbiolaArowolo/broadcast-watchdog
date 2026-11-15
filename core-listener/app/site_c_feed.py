# File:         site_c_feed.py
# Purpose:      Judges the Site C feed: the encoder's two multicast streams (UDP 5004 and 5005) as the playout PCs hear them. The PCs count the bytes and report them in
#               their ordinary heartbeat (field `site_c_feed`); this module keeps the latest reading of each PC, decides per stream whether it is fine, a warning, LOST or
#               BELOW THE FLOOR, raises and clears an alarm without repeating it, and prepares what the dashboard panel shows. The rules are in the unpublished design notes.
# Inputs:       The `site_c_feed` field of a heartbeat (parse_report), a sqlite connection held under the caller's lock, and a clock passed in (this file never reads one).
# Outputs:      Verdicts, alarm steps ("raise" / "restore" / nothing), and a JSON-ready snapshot for /status.
# Dependencies: Python standard library only. main.py calls the *_locked functions while it holds its database lock and sends the e-mail / push / staff failure itself.
import ipaddress
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

STREAMS = ("5004", "5005")
# The owner's names (2026-11-08): 5005 is the MAIN stream, 5004 the BACKUP stream; the panel lists main first. STREAMS stays the order the streams are judged in.
STREAM_ROLES = {"5005": ("main", "Main stream"), "5004": ("backup", "Backup stream")}
DISPLAY_ORDER = ("5005", "5004")
# Bounds for what a PC may claim. A sender is trusted with a key, not with sanity: anything outside these is treated as not reported.
MAX_MBIT = 10_000.0
MAX_PACKETS = 1_000_000_000
MAX_IDLE_SECONDS = 10_000_000.0
MAX_WINDOW_SECONDS = 3600.0
MAX_ERROR_CHARS = 80
HISTORY_HOURS = 24
HISTORY_BUCKET_MINUTES = 10
LIVE_SECONDS = 600                 # the opened panel draws the last ten minutes ...
LIVE_STEP_SECONDS = 10             # ... one point per ten seconds (the senders report every ten seconds)
RECENT_SECONDS = LIVE_SECONDS + 60  # raw readings are kept a minute longer than the chart shows, then removed as new ones arrive
KEEP_DAYS = 8
# The per-second readings (the unpublished design notes, "The per-second readings"): the PCs add their last seconds' readings to the heartbeat; they only feed the live chart.
FINE_STEP_SECONDS = 1                 # the chart's finest grain: one number a second
MAX_SAMPLES_PER_STREAM = 30           # the most one heartbeat may carry for a stream (the newest are kept)
MAX_SAMPLE_AGE_SECONDS = 3600.0
MIN_SAMPLE_SPAN_SECONDS = 0.05
MAX_SAMPLE_SPAN_SECONDS = 60.0


@dataclass(frozen=True)
class Settings:
    """The owner's numbers (2026-11-08). Frozen: a setting is read once, never changed while the listener runs."""
    lost_after_seconds: float = 20.0       # no packet at all for this long = LOST
    floor_mbit: float = 1.0                # "below 1 mb" (his words) ...
    floor_seconds: float = 120.0           # ... "for up to 120 secs" = BELOW THE FLOOR
    clear_seconds: float = 30.0            # the stream must be heard again for this long before an alarm clears (no flapping)
    stale_seconds: float = 60.0            # a PC whose last report is older than this is not a witness (a silent PC is the heartbeat-lost alarm's business)
    group: str = "224.1.1.1"               # the encoder's multicast group: only shown, as udp://@group:port, on the panel (the PCs choose what they join)
    expected_mbit: float = 5.0             # the rate the encoder normally sends: only the faint "usual" line on the live chart, never a threshold


def _positive(environ, name: str, default: float) -> float:
    try:
        value = float(str(environ.get(name, "")).strip())
    except ValueError:
        return default
    return value if math.isfinite(value) and value > 0 else default


def _multicast(environ, name: str, default: str) -> str:
    """An IPv4 multicast address (224.0.0.0 to 239.255.255.255), or the default. Anything else (a normal address, text, an IPv6 address, an address with a port) is
    refused, so a typo in the settings can never put a wrong address on the panel."""
    try:
        address = ipaddress.IPv4Address(str(environ.get(name, "")).strip())
    except ValueError:
        return default
    return str(address) if address.is_multicast else default


def settings_from_environ(environ=None) -> Settings:
    environ = os.environ if environ is None else environ
    base = Settings()
    return Settings(
        lost_after_seconds=_positive(environ, "WATCHDOG_SITE_C_LOST_AFTER_SECONDS", base.lost_after_seconds),
        floor_mbit=_positive(environ, "WATCHDOG_SITE_C_FLOOR_MBIT", base.floor_mbit),
        floor_seconds=_positive(environ, "WATCHDOG_SITE_C_FLOOR_SECONDS", base.floor_seconds),
        clear_seconds=_positive(environ, "WATCHDOG_SITE_C_CLEAR_SECONDS", base.clear_seconds),
        stale_seconds=_positive(environ, "WATCHDOG_SITE_C_STALE_SECONDS", base.stale_seconds),
        group=_multicast(environ, "WATCHDOG_SITE_C_GROUP", base.group),
        expected_mbit=_positive(environ, "WATCHDOG_SITE_C_EXPECTED_MBIT", base.expected_mbit),
    )


def stream_title(stream: str) -> str:
    """How an alarm, an e-mail and a staff failure name a stream: "Site C ZIXI Feed main stream (UDP 5005)"."""
    role = STREAM_ROLES.get(stream)
    return f"Site C ZIXI Feed {role[0]} stream (UDP {stream})" if role else f"Site C ZIXI Feed {stream}"


# ---- What a PC says -------------------------------------------------------------------------------------------------------------------------------
def _number(value, low: float, high: float) -> Optional[float]:
    """A real number inside [low, high], or None. A bool is refused on purpose: in Python True is the number 1, which would let `true` pass for a bitrate."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and low <= number <= high else None


def _count(value) -> Optional[int]:
    number = _number(value, 0, MAX_PACKETS)
    return int(number) if number is not None and number == int(number) else None


def _samples(value) -> list:
    """One stream's per-second samples [age, span, Mbit/s, packets], cleaned and sorted oldest first, at most MAX_SAMPLES_PER_STREAM (the newest). A sample that is not exactly
    four sane numbers is dropped on its own; the rest of the list and the rest of the report are unaffected."""
    if not isinstance(value, list):
        return []
    kept = []
    for raw in value:
        if not isinstance(raw, list) or len(raw) != 4:
            continue
        age, span = _number(raw[0], 0, MAX_SAMPLE_AGE_SECONDS), _number(raw[1], MIN_SAMPLE_SPAN_SECONDS, MAX_SAMPLE_SPAN_SECONDS)
        mbit, packets = _number(raw[2], 0, MAX_MBIT), _count(raw[3])
        if age is None or span is None or mbit is None or packets is None:
            continue
        kept.append({"age": age, "span": span, "mbit_per_s": mbit, "packets": packets})
    kept.sort(key=lambda sample: sample["age"])                # newest first ...
    return list(reversed(kept[:MAX_SAMPLES_PER_STREAM]))       # ... keep the newest ones, hand them on oldest first


def parse_report(value) -> Optional[dict]:
    """The `site_c_feed` field of a heartbeat, cleaned, or None when it is missing or unusable. Never raises: a bad field must not cost the rest of the heartbeat."""
    if not isinstance(value, dict) or not isinstance(value.get("listening"), bool) or not isinstance(value.get("streams"), dict):
        return None
    window = _number(value.get("window_seconds"), 0.001, MAX_WINDOW_SECONDS)
    if window is None:
        return None
    error = value.get("error")
    error = re.sub(r"[^\x20-\x7e]", "?", error)[:MAX_ERROR_CHARS] if isinstance(error, str) else ""
    streams = {}
    for stream in STREAMS:
        raw = value["streams"].get(stream)
        if not isinstance(raw, dict):
            continue
        mbit, packets, idle = _number(raw.get("mbit_per_s"), 0, MAX_MBIT), _count(raw.get("packets")), _number(raw.get("idle_seconds"), 0, MAX_IDLE_SECONDS)
        if mbit is None or packets is None or idle is None:
            continue
        streams[stream] = {"mbit_per_s": mbit, "packets": packets, "idle_seconds": idle}
    seconds = {}
    raw_seconds = value.get("seconds")
    if value["listening"] and isinstance(raw_seconds, dict):
        for stream in STREAMS:
            samples = _samples(raw_seconds.get(stream))
            if samples:
                seconds[stream] = samples
    return {"listening": value["listening"], "window_seconds": window, "error": error, "streams": streams, "seconds": seconds}


# ---- Judgement (pure) -----------------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Witness:
    """One PC's current testimony about one stream."""
    source_id: str
    mbit_per_s: float
    packets: int
    idle_seconds: float
    age_seconds: float
    below_for_seconds: Optional[float]      # how long the rate has been under the floor, or None when it is not under it


@dataclass(frozen=True)
class Verdict:
    level: str                    # ok | warning | critical | no_data
    kind: Optional[str]           # lost | floor, only when critical
    word: str                     # what the panel prints
    cause: str                    # plain words, short enough for the staff failure (80 characters)
    heard_by: tuple               # the PCs that hear the stream normally
    any_fine: bool                # at least one PC hears it normally: this is what lets a critical alarm start clearing


def classify(w: Witness, settings: Settings) -> str:
    """fine | low (under the floor, not yet for long) | floor (under it for floor_seconds) | lost (no packet for lost_after_seconds). Silence beats the rate: a PC that says
    "5 Mbit/s" and "nothing for a minute" in one breath is believed about the silence."""
    if w.idle_seconds >= settings.lost_after_seconds:
        return "lost"
    if w.mbit_per_s < settings.floor_mbit:
        return "floor" if w.below_for_seconds is not None and w.below_for_seconds >= settings.floor_seconds else "low"
    return "fine"


def _duration(seconds: float) -> str:
    whole = int(seconds)
    if whole < 120:
        return f"{whole} s"
    if whole < 7200:
        minutes, rest = divmod(whole, 60)
        return f"{minutes} min" + (f" {rest} s" if rest else "")
    return f"{whole // 3600} h {whole % 3600 // 60} min"


def _describe(w: Witness, state: str, settings: Settings) -> str:
    if state == "lost":
        return f"{w.source_id}: no packets for {_duration(w.idle_seconds)}"
    return f"{w.source_id}: {w.mbit_per_s:.1f} Mbit/s, under {settings.floor_mbit:g} for {_duration(w.below_for_seconds or 0)}"


def judge(witnesses, settings: Settings) -> Verdict:
    """The whole decision for one stream. Critical only when EVERY listening PC is lost or under the floor long enough; one PC disagreeing is a warning that names it
    (that points at the PC or its network, not at the encoder)."""
    witnesses = list(witnesses)
    if not witnesses:
        return Verdict("no_data", None, "NO DATA", "", (), False)
    states = [classify(w, settings) for w in witnesses]
    fine = tuple(w.source_id for w, state in zip(witnesses, states) if state == "fine")
    if len(fine) == len(witnesses):
        return Verdict("ok", None, "OK", "", fine, True)
    if all(state in ("lost", "floor") for state in states):
        if all(state == "lost" for state in states):
            return Verdict("critical", "lost", "LOST", f"Stream lost: no packets for {_duration(min(w.idle_seconds for w in witnesses))}", fine, False)
        below = [w.below_for_seconds for w in witnesses if w.below_for_seconds is not None]
        return Verdict("critical", "floor", f"BELOW {settings.floor_mbit:g} MBIT/S",
                       f"Under {settings.floor_mbit:g} Mbit/s for {_duration(min(below) if below else settings.floor_seconds)}", fine, False)
    bad = "; ".join(_describe(w, state, settings) for w, state in zip(witnesses, states) if state != "fine")
    return Verdict("warning", None, "WARNING", bad[:120], fine, bool(fine))


# ---- The alarm's life (pure) ----------------------------------------------------------------------------------------------------------------------
def empty_alert() -> dict:
    return {"level": "ok", "kind": None, "cause": "", "changed_utc": None, "clear_since_utc": None}


def _utc(text) -> datetime:
    return datetime.fromisoformat(text)


def next_step(row: dict, verdict: Verdict, now: datetime, settings: Settings) -> tuple:
    """(new stored alarm row, action) where action is "raise", "restore" or None. The row passed in is never changed. A critical verdict raises ONE alarm per episode; the
    episode ends only when the stream has been heard again (a verdict with any_fine) for clear_seconds in a row, so a stream that flaps does not mail all night."""
    stamp = now.isoformat()
    new = dict(row)
    if verdict.level == "critical":
        if row["level"] != "critical":
            return {"level": "critical", "kind": verdict.kind, "cause": verdict.cause, "changed_utc": stamp, "clear_since_utc": None}, "raise"
        new.update(kind=verdict.kind, cause=verdict.cause, clear_since_utc=None)      # lost may turn into "low" inside one episode: same alarm, better words
        return new, None
    if row["level"] == "critical":
        if not verdict.any_fine:                       # no data, or every PC still bad: the clearing clock does not run
            new["clear_since_utc"] = None
            return new, None
        since = row.get("clear_since_utc") or stamp
        if (now - _utc(since)).total_seconds() >= settings.clear_seconds:
            return {"level": "ok", "kind": None, "cause": "", "changed_utc": stamp, "clear_since_utc": None}, "restore"
        new["clear_since_utc"] = since
        return new, None
    return new, None


# ---- The database layer (called under main.py's lock) ---------------------------------------------------------------------------------------------
def init_tables(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS site_c_feed_latest (
        source_id TEXT NOT NULL, stream TEXT NOT NULL, taken_utc TEXT NOT NULL, mbit_per_s REAL NOT NULL, packets INTEGER NOT NULL, idle_seconds REAL NOT NULL,
        listening INTEGER NOT NULL, error TEXT NOT NULL DEFAULT '', below_since_utc TEXT, PRIMARY KEY (source_id, stream))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS site_c_feed_alerts (
        stream TEXT PRIMARY KEY, level TEXT NOT NULL, kind TEXT, cause TEXT NOT NULL DEFAULT '', changed_utc TEXT, clear_since_utc TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS site_c_feed_minutes (
        minute_utc TEXT NOT NULL, stream TEXT NOT NULL, source_id TEXT NOT NULL, mbit_sum REAL NOT NULL, samples INTEGER NOT NULL, PRIMARY KEY (minute_utc, stream, source_id))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_site_c_feed_minutes_time ON site_c_feed_minutes(minute_utc)")
    # Every reading of the last few minutes (not just the latest): what the opened panel draws as its live chart and shows as packets per second.
    conn.execute("""CREATE TABLE IF NOT EXISTS site_c_feed_recent (
        taken_utc TEXT NOT NULL, stream TEXT NOT NULL, source_id TEXT NOT NULL, mbit_per_s REAL NOT NULL, packets INTEGER NOT NULL, window_seconds REAL NOT NULL,
        PRIMARY KEY (source_id, stream, taken_utc))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_site_c_feed_recent_time ON site_c_feed_recent(taken_utc)")
    # The per-second readings of the last few minutes: one row per PC, stream and second, filed by the second Core received it in minus the sample's age (a PC's own clock is
    # never trusted). A table that is new on an upgrade is created empty; a listener rolled back to the older code ignores it.
    conn.execute("""CREATE TABLE IF NOT EXISTS site_c_feed_seconds (
        second_utc TEXT NOT NULL, stream TEXT NOT NULL, source_id TEXT NOT NULL, mbit_per_s REAL NOT NULL, packets INTEGER NOT NULL, span_seconds REAL NOT NULL,
        PRIMARY KEY (source_id, stream, second_utc))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_site_c_feed_seconds_time ON site_c_feed_seconds(second_utc)")


def _rows(conn, sql: str, args=()) -> list:
    cursor = conn.execute(sql, args)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def ingest_locked(conn, source_id: str, report: Optional[dict], now: datetime, settings: Optional[Settings] = None) -> None:
    """Stores one PC's latest reading of each stream (and adds it to the per-minute history). `report` is parse_report's answer; None changes nothing."""
    settings = settings or Settings()
    if not report:
        return
    taken = now.isoformat()
    for stream in STREAMS:
        reading = report["streams"].get(stream)
        if report["listening"] and reading is None:
            continue                                   # this PC said nothing about this stream: its old reading simply goes stale
        previous = _rows(conn, "SELECT taken_utc, listening, below_since_utc FROM site_c_feed_latest WHERE source_id = ? AND stream = ?", (source_id, stream))
        if not report["listening"]:
            reading = {"mbit_per_s": 0.0, "packets": 0, "idle_seconds": 0.0}
            below_since = None
        elif reading["mbit_per_s"] >= settings.floor_mbit:
            below_since = None
        else:
            # The "under the floor" clock continues only across readings that follow each other. After a silence longer than a witness stays fresh nobody saw what
            # happened, so the clock starts again rather than claim two minutes it did not observe.
            old = previous[0] if previous else None
            continuing = old and old["listening"] and old["below_since_utc"] and (now - _utc(old["taken_utc"])).total_seconds() <= settings.stale_seconds
            below_since = old["below_since_utc"] if continuing else taken
        conn.execute(
            """INSERT INTO site_c_feed_latest (source_id, stream, taken_utc, mbit_per_s, packets, idle_seconds, listening, error, below_since_utc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_id, stream) DO UPDATE SET taken_utc = excluded.taken_utc, mbit_per_s = excluded.mbit_per_s, packets = excluded.packets,
                 idle_seconds = excluded.idle_seconds, listening = excluded.listening, error = excluded.error, below_since_utc = excluded.below_since_utc""",
            (source_id, stream, taken, reading["mbit_per_s"], reading["packets"], reading["idle_seconds"], 1 if report["listening"] else 0, report["error"], below_since))
        if report["listening"]:
            conn.execute(
                """INSERT INTO site_c_feed_minutes (minute_utc, stream, source_id, mbit_sum, samples) VALUES (?, ?, ?, ?, 1)
                   ON CONFLICT(minute_utc, stream, source_id) DO UPDATE SET mbit_sum = mbit_sum + excluded.mbit_sum, samples = samples + 1""",
                (now.replace(second=0, microsecond=0).isoformat(), stream, source_id, reading["mbit_per_s"]))
            conn.execute(
                "INSERT OR REPLACE INTO site_c_feed_recent (taken_utc, stream, source_id, mbit_per_s, packets, window_seconds) VALUES (?, ?, ?, ?, ?, ?)",
                (taken, stream, source_id, reading["mbit_per_s"], reading["packets"], report["window_seconds"]))
    conn.execute("DELETE FROM site_c_feed_recent WHERE taken_utc < ?", ((now - timedelta(seconds=RECENT_SECONDS)).isoformat(),))
    _ingest_seconds_locked(conn, source_id, report, now)


def _ingest_seconds_locked(conn, source_id: str, report: dict, now: datetime) -> None:
    """Files a PC's per-second samples (chart only: nothing here is read by the judgement) and removes what the chart no longer shows. A second sent again replaces itself."""
    cutoff = (now - timedelta(seconds=RECENT_SECONDS)).replace(microsecond=0).isoformat()
    if report["listening"]:
        for stream in STREAMS:
            for sample in report.get("seconds", {}).get(stream, ()):
                second = (now - timedelta(seconds=sample["age"])).replace(microsecond=0).isoformat()
                if second >= cutoff:
                    conn.execute("INSERT OR REPLACE INTO site_c_feed_seconds (second_utc, stream, source_id, mbit_per_s, packets, span_seconds) VALUES (?, ?, ?, ?, ?, ?)",
                                 (second, stream, source_id, sample["mbit_per_s"], sample["packets"], sample["span"]))
    conn.execute("DELETE FROM site_c_feed_seconds WHERE second_utc < ?", (cutoff,))


def _witnesses(conn, stream: str, now: datetime, settings: Settings) -> list:
    found = []
    for row in _rows(conn, "SELECT * FROM site_c_feed_latest WHERE stream = ? ORDER BY source_id", (stream,)):
        age = (now - _utc(row["taken_utc"])).total_seconds()
        if not row["listening"] or age > settings.stale_seconds:
            continue
        below = (now - _utc(row["below_since_utc"])).total_seconds() if row["below_since_utc"] else None
        found.append(Witness(row["source_id"], row["mbit_per_s"], row["packets"], row["idle_seconds"], max(age, 0.0), below))
    return found


def _stored_alert(conn, stream: str) -> dict:
    rows = _rows(conn, "SELECT level, kind, cause, changed_utc, clear_since_utc FROM site_c_feed_alerts WHERE stream = ?", (stream,))
    return rows[0] if rows else empty_alert()


def evaluate_locked(conn, now: datetime, settings: Optional[Settings] = None) -> list:
    """Judges both streams, stores any change of alarm state, and returns what the caller must DO after it releases its lock: one dict per alarm to raise or restore."""
    settings = settings or Settings()
    actions = []
    for stream in STREAMS:
        witnesses = _witnesses(conn, stream, now, settings)
        verdict = judge(witnesses, settings)
        stored = _stored_alert(conn, stream)
        row, action = next_step(stored, verdict, now, settings)
        if row != stored:
            conn.execute(
                """INSERT INTO site_c_feed_alerts (stream, level, kind, cause, changed_utc, clear_since_utc) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(stream) DO UPDATE SET level = excluded.level, kind = excluded.kind, cause = excluded.cause, changed_utc = excluded.changed_utc,
                     clear_since_utc = excluded.clear_since_utc""",
                (stream, row["level"], row["kind"], row["cause"], row["changed_utc"], row["clear_since_utc"]))
        if action:
            actions.append({"action": action, "stream": stream, "kind": row["kind"] or stored.get("kind"), "cause": row["cause"] or stored.get("cause", ""),
                            "witnesses": [{"source": w.source_id, "state": classify(w, settings), "mbit_per_s": round(w.mbit_per_s, 2), "idle_seconds": int(w.idle_seconds),
                                           "age_seconds": int(w.age_seconds)} for w in witnesses]})
    return actions


def _history(conn, stream: str, now: datetime) -> list:
    """24 hours as 10-minute points: for each minute the best PC's average rate, then the LOWEST minute of each 10 (a dip must stay visible on a small line)."""
    since = (now - timedelta(hours=HISTORY_HOURS)).replace(second=0, microsecond=0).isoformat()
    best = {}
    for row in _rows(conn, "SELECT minute_utc, mbit_sum, samples FROM site_c_feed_minutes WHERE stream = ? AND minute_utc >= ?", (stream, since)):
        average = row["mbit_sum"] / row["samples"]
        best[row["minute_utc"]] = max(best.get(row["minute_utc"], 0.0), average)
    buckets = {}
    for minute, average in best.items():
        moment = _utc(minute)
        start = moment.replace(minute=moment.minute // HISTORY_BUCKET_MINUTES * HISTORY_BUCKET_MINUTES).isoformat()
        buckets[start] = min(buckets.get(start, average), average)
    return [{"t": start, "mbit": round(value, 2)} for start, value in sorted(buckets.items())]


def _live(conn, stream: str, now: datetime) -> list:
    """The last ten minutes as one point per ten seconds, oldest first. When two PCs report in the same step the better reading is drawn (the stream is judged lost only
    when EVERY PC says so, and a dip must not be invented by the weaker listener)."""
    since = (now - timedelta(seconds=LIVE_SECONDS)).isoformat()
    best = {}
    for row in _rows(conn, "SELECT taken_utc, mbit_per_s FROM site_c_feed_recent WHERE stream = ? AND taken_utc >= ?", (stream, since)):
        step = int(_utc(row["taken_utc"]).timestamp()) // LIVE_STEP_SECONDS * LIVE_STEP_SECONDS
        best[step] = max(best.get(step, 0.0), row["mbit_per_s"])
    return [{"t": datetime.fromtimestamp(step, tz=timezone.utc).isoformat(), "mbit": round(value, 2)} for step, value in sorted(best.items())]


def _fine(conn, stream: str, now: datetime) -> dict:
    """The last ten minutes as one number a second: {"step_seconds": 1, "start": first second, "mbit": [..]} with None for a second nobody reported (a compact list, because it is
    sent to every open page every few seconds). When two PCs report one second the better reading is drawn, as in the ten-second line."""
    since = (now - timedelta(seconds=LIVE_SECONDS)).replace(microsecond=0).isoformat()
    best = {}
    for row in _rows(conn, "SELECT second_utc, mbit_per_s FROM site_c_feed_seconds WHERE stream = ? AND second_utc >= ?", (stream, since)):
        second = int(_utc(row["second_utc"]).timestamp())
        best[second] = max(best.get(second, 0.0), row["mbit_per_s"])
    if not best:
        return {"step_seconds": FINE_STEP_SECONDS, "start": None, "mbit": []}
    first, last = min(best), max(best)
    return {"step_seconds": FINE_STEP_SECONDS, "start": datetime.fromtimestamp(first, tz=timezone.utc).isoformat(),
            "mbit": [round(best[second], 2) if second in best else None for second in range(first, last + 1, FINE_STEP_SECONDS)]}


def _packet_details(conn, stream: str) -> dict:
    """Each PC's NEWEST reading of this stream with the window it was counted over: {source: row}. Empty for a reading stored before this table existed."""
    rows = _rows(conn, """SELECT source_id, packets, window_seconds FROM site_c_feed_recent AS r WHERE stream = ? AND taken_utc =
                          (SELECT MAX(taken_utc) FROM site_c_feed_recent WHERE stream = r.stream AND source_id = r.source_id)""", (stream,))
    return {row["source_id"]: row for row in rows}


def snapshot(conn, now: datetime, settings: Optional[Settings] = None) -> dict:
    """What /status carries and the dashboard panel draws. It judges live, so the panel is current between heartbeats; a stored critical alarm stays on show until it has
    really cleared. The only address in it is the encoder's multicast group (shown on the panel at the owner's request, 2026-11-08); no encoder or PC address appears: the
    page can be opened from outside the studio."""
    settings = settings or Settings()
    streams = []
    latest = _rows(conn, "SELECT source_id, stream, listening, error, taken_utc FROM site_c_feed_latest ORDER BY source_id")
    for stream in DISPLAY_ORDER:
        witnesses = _witnesses(conn, stream, now, settings)
        verdict = judge(witnesses, settings)
        stored = _stored_alert(conn, stream)
        critical = stored["level"] == "critical"
        level = "critical" if critical else verdict.level
        if critical:
            word = "LOST" if stored["kind"] == "lost" else f"BELOW {settings.floor_mbit:g} MBIT/S"
            cause = stored["cause"]
        else:
            word, cause = verdict.word, verdict.cause
        entries = [{"source": w.source_id, "state": classify(w, settings), "mbit_per_s": round(w.mbit_per_s, 2), "idle_seconds": int(w.idle_seconds),
                    "age_seconds": int(w.age_seconds)} for w in witnesses]
        details = _packet_details(conn, stream)
        for entry in entries:
            detail = details.get(entry["source"])
            if detail and detail["window_seconds"] > 0:
                entry.update(packets=detail["packets"], window_seconds=round(detail["window_seconds"], 1), packets_per_s=round(detail["packets"] / detail["window_seconds"]))
        entries += [{"source": row["source_id"], "state": "cannot_listen", "error": row["error"], "age_seconds": int((now - _utc(row["taken_utc"])).total_seconds())}
                    for row in latest if row["stream"] == stream and not row["listening"]]
        role, label = STREAM_ROLES[stream]
        streams.append({"stream": stream, "role": role, "label": label, "address": f"udp://@{settings.group}:{stream}", "live": _live(conn, stream, now), "fine": _fine(conn, stream, now), "level": level, "word": word, "cause": cause, "mbit_per_s": round(max((w.mbit_per_s for w in witnesses), default=0.0), 2),
                        "heard_by": list(verdict.heard_by), "witnesses": entries, "history": _history(conn, stream, now),
                        "alarm": {"level": stored["level"], "kind": stored["kind"], "cause": stored["cause"], "since": stored["changed_utc"],
                                  "clearing": critical and verdict.any_fine}})
    return {"reporting": bool(latest), "floor_mbit": settings.floor_mbit, "lost_after_seconds": settings.lost_after_seconds, "floor_seconds": settings.floor_seconds,
            "group": settings.group, "expected_mbit": settings.expected_mbit, "live_seconds": LIVE_SECONDS, "live_step_seconds": LIVE_STEP_SECONDS, "streams": streams}


def prune_locked(conn, now: datetime, keep_days: int = KEEP_DAYS) -> int:
    """Removes history older than keep_days (called once a day). Returns how many rows went."""
    cutoff = (now - timedelta(days=keep_days)).isoformat()
    return conn.execute("DELETE FROM site_c_feed_minutes WHERE minute_utc < ?", (cutoff,)).rowcount
