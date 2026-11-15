# File:         standby.py
# Purpose:      The pure decision logic of the standby listener: is Core UP or DOWN, may this listener deliver alerts, and what do the takeover and stand-down messages say.
# Inputs:       Probe results (did GET /health on Core work) and studios' reports (did my post to Core work), each with a timezone-aware time; the settings.
# Outputs:      UP/DOWN state with a Transition the moment it changes, a yes/no delivery gate, a status snapshot, and Notice objects (email + push text).
# Dependencies: Python standard library only. No network, no clock of its own, no database: main.py feeds it and acts on what it says.
"""Why the standby decides the way it does (plain words).

Taking over alerting is a big step: if the standby cried wolf, you would stop trusting it. So it needs TWO kinds of evidence that Core is gone:

1. its own probe of Core's public /health fails several times in a row, AND
2. no studio has recently said "my post to Core worked" (each studio's copy to the standby carries that flag).

If only the probe path is broken (a Cloudflare hiccup) the studios still reach Core and veto the takeover. If only one studio has trouble, the probe stays
healthy and nothing happens. Coming back is slower and harder than going down (a minimum time down, successful probes, nobody still failing), so it cannot flap.

A studio's "Core took my post" is self-reported, so it is never trusted alone (security review of the first version): a veto needs a STRICT MAJORITY of the
studios that reported, and no veto, in either direction, lasts longer than `veto_max_seconds`. A lying or stuck studio can therefore only delay a takeover
(or keep the standby loud), never hide an outage or silence it for good.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple, Optional
from urllib.parse import urlsplit

logger = logging.getLogger("standby")

UP = "UP"
DOWN = "DOWN"
ROLES = ("primary", "standby")
MAX_SOURCE_CHARS = 64            # the same bound the listener puts on a source name
MAX_ERROR_CHARS = 160            # what is kept of a probe error: it comes from the network, so it is cut and cleaned before it reaches a message
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class WatchConfig:
    """All timings in one place; the defaults are the ones agreed in the spec."""

    down_after_failures: int = 3          # failed probes in a row (about 90 s at one probe per 30 s)
    veto_true_seconds: int = 90           # a studio saying "Core accepted my post" this recently vetoes the takeover
    up_after_successes: int = 2           # good probes in a row before standing down
    veto_false_seconds: int = 60          # a studio saying "I cannot reach Core" this recently vetoes the stand-down
    veto_max_seconds: int = 600           # no veto holds longer than this: once the probes have failed (or succeeded) for this long, the probes win
    min_down_seconds: int = 120           # never stand down sooner than this after taking over
    max_sources: int = 64                 # studios remembered (the table is bounded)
    proven_max_age_seconds: int = 7 * 24 * 3600   # the probe counts as "able to see Core" if one probe succeeded within this long (a restart keeps the proof)

    def __post_init__(self):
        for name in ("down_after_failures", "up_after_successes", "veto_true_seconds", "veto_false_seconds", "veto_max_seconds", "max_sources"):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"{name} must be at least 1")
        if int(self.min_down_seconds) < 0:
            raise ValueError("min_down_seconds cannot be negative")
        if int(self.proven_max_age_seconds) < 60:
            raise ValueError("proven_max_age_seconds must be at least 60")


class Transition(NamedTuple):
    kind: str            # "down" (the standby takes over) or "up" (it stands down)
    at: datetime
    reason: str
    previous_since: Optional[datetime] = None      # when the belief it just left had begun (for "was down for ...")


class Notice(NamedTuple):
    title: str                       # email heading and subject text, one line
    summary: str                     # one plain sentence
    rows: list                       # (label, value) pairs for the email table
    push_title: str
    push_body: str


def _require_aware(moment, what: str) -> None:
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError(f"{what} must be a timezone-aware datetime")


def _clean(text, limit: int) -> str:
    """One line, no control characters, cut to `limit`. Anything from the network goes through this before it is put in a message."""
    return _WHITESPACE.sub(" ", "".join(ch if ch.isprintable() or ch.isspace() else " " for ch in str(text or ""))).strip()[:limit]


class CoreWatch:
    """The UP/DOWN state of Core as the standby believes it. Feed it with record_probe and record_report; each returns a Transition when the belief just changed."""

    def __init__(self, config: WatchConfig, now: datetime, proven_at: Optional[datetime] = None):
        _require_aware(now, "now")
        self._cfg = config
        self._state = UP
        self._since = now
        self._failures = 0
        self._successes = 0
        # When a probe last SUCCEEDED (restored from the database after a restart). Until one has, the probe has not proven that it can see Core at all, and failing is
        # not evidence that Core is gone: it may be blocked (Cloudflare answered this probe with HTTP 403 on the first real day) or misaddressed. The way IN to DOWN needs it.
        self._last_ok_at: Optional[datetime] = proven_at
        self._blocked_logged = False
        self._failing_since: Optional[datetime] = None      # when the latest streak of failed probes began (only read while failures are counted)
        self._ok_since: Optional[datetime] = None           # when the latest streak of good probes began (only read while successes are counted)
        self._last_error = ""
        self._reports: dict = {}        # studio -> (primary_ok, when): only the LATEST report of each studio counts

    @property
    def state(self) -> str:
        return self._state

    @property
    def since(self) -> datetime:
        return self._since

    def delivery_allowed(self) -> bool:
        """True only while the standby has taken over. While Core is believed up, the standby tracks everything and says nothing."""
        return self._state == DOWN

    @property
    def last_ok_at(self) -> Optional[datetime]:
        return self._last_ok_at

    def probe_proven(self, now: datetime) -> bool:
        """True when a probe has succeeded recently enough to show that the probe path can see Core at all."""
        return self._last_ok_at is not None and (now - self._last_ok_at).total_seconds() <= self._cfg.proven_max_age_seconds

    def restore_proven(self, when) -> None:
        """Takes a remembered time of the last good probe (from the database after a restart). Never moves the proof backwards; anything that is not a time is ignored."""
        if isinstance(when, datetime) and when.tzinfo is not None and (self._last_ok_at is None or when > self._last_ok_at):
            self._last_ok_at = when

    def record_probe(self, ok, now: datetime, error: str = "") -> Optional[Transition]:
        if not isinstance(ok, bool):
            raise ValueError("a probe result must be True or False")
        _require_aware(now, "now")
        if ok:
            self._last_ok_at = now
            self._blocked_logged = False
            if self._successes == 0:
                self._ok_since = now
            self._successes += 1
            self._failures = 0
        else:
            if self._failures == 0:
                self._failing_since = now
            self._failures += 1
            self._successes = 0
            self._last_error = _clean(error, MAX_ERROR_CHARS)
        return self._evaluate(now)

    def record_report(self, source, primary_ok, now: datetime) -> Optional[Transition]:
        """A studio says whether its latest post to Core was accepted. Bad input is logged and ignored, never raised: a malformed flag must not break a heartbeat."""
        _require_aware(now, "now")
        if not isinstance(source, str) or not source.strip() or len(source) > MAX_SOURCE_CHARS or not isinstance(primary_ok, bool):
            logger.warning("[standby.py::record_report] data_issue | reason=bad_report source_type=%s flag_type=%s action=ignored", type(source).__name__, type(primary_ok).__name__)
            return None
        if source not in self._reports and len(self._reports) >= self._cfg.max_sources:
            oldest = min(self._reports, key=lambda name: self._reports[name][1])
            del self._reports[oldest]
        self._reports[source] = (primary_ok, now)
        return self._evaluate(now)

    def _count_fresh(self, value: bool, window_seconds: int, now: datetime) -> int:
        """How many studios' latest report said `value` within the window? (A report dated in the future of `now` counts as fresh.)"""
        return sum(1 for flag, when in self._reports.values() if flag is value and (now - when).total_seconds() <= window_seconds)

    def _fresh_report(self, value: bool, window_seconds: int, now: datetime) -> bool:
        return self._count_fresh(value, window_seconds, now) > 0

    def _evaluate(self, now: datetime) -> Optional[Transition]:
        cfg = self._cfg
        if self._state == UP:
            if self._failures < cfg.down_after_failures:
                return None
            if not self.probe_proven(now):
                if not self._blocked_logged:
                    self._blocked_logged = True
                    logger.warning("[standby.py::_evaluate] decision | action=takeover_blocked reason=the probe has not succeeded in the last %d s, so failing is not evidence about Core "
                                   "last_error=%s", cfg.proven_max_age_seconds, self._last_error or "none")
                return None
            vouching = self._count_fresh(True, cfg.veto_true_seconds, now)
            doubting = self._count_fresh(False, cfg.veto_true_seconds, now)
            vetoed = vouching > doubting                                  # a strict majority of the studios that reported must vouch for Core
            capped = self._failing_since is not None and (now - self._failing_since).total_seconds() >= cfg.veto_max_seconds
            if vetoed and not capped:
                return None
            before, self._state, self._since = self._since, DOWN, now
            reason = f"{self._failures} failed probes in a row ({self._last_error or 'no error text'})" + (
                f"; {vouching} studio(s) still vouch for Core but the probes have failed for {cfg.veto_max_seconds} s or more" if vetoed else
                f"; vouching studios {vouching}, doubting {doubting}")
            logger.info("[standby.py::_evaluate] decision | action=takeover failures=%d vouching=%d doubting=%d capped=%s", self._failures, vouching, doubting, vetoed and capped)
            return Transition("down", now, reason, before)
        waited = (now - self._since).total_seconds()
        capped = self._ok_since is not None and (now - self._ok_since).total_seconds() >= cfg.veto_max_seconds
        if waited >= cfg.min_down_seconds and self._successes >= cfg.up_after_successes and (capped or not self._fresh_report(False, cfg.veto_false_seconds, now)):
            before, self._state, self._since = self._since, UP, now
            logger.info("[standby.py::_evaluate] decision | action=stand_down successes=%d down_seconds=%d", self._successes, int(waited))
            return Transition("up", now, f"{self._successes} good probes in a row after {int(waited)} s down", before)
        return None

    def snapshot(self, now: datetime) -> dict:
        """What /status shows: state, counts, ages. No address, no key."""
        _require_aware(now, "now")
        return {
            "state": self._state,
            "since": self._since.isoformat(),
            "failed_probes_in_a_row": self._failures,
            "successful_probes_in_a_row": self._successes,
            "last_error": self._last_error,
            "probe_proven": self.probe_proven(now),
            "last_ok_at": self._last_ok_at.isoformat() if self._last_ok_at else None,
            "takeover_blocked": self._state == UP and self._failures >= self._cfg.down_after_failures and not self.probe_proven(now),
            "studios": {name: {"primary_ok": flag, "age_seconds": round((now - when).total_seconds(), 1)} for name, (flag, when) in sorted(self._reports.items())},
        }


# ---- What the standby says ---------------------------------------------------------------------------------------------------------------------
def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s"
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


def _studio_rows(studios) -> str:
    pairs = [f"{_clean(name, MAX_SOURCE_CHARS)} {_clean(state, 40)}" for name, state in (studios or [])]
    return "; ".join(pairs) if pairs else "no studio has reported to the standby yet"


def takeover_notice(snapshot: dict, studios, public_host: str) -> Notice:
    """The one message sent when the standby takes over. `studios` is [(name, state)] as the standby itself sees them, so a studio already in alarm is not lost."""
    since = str(snapshot.get("since", ""))
    error = _clean(snapshot.get("last_error", ""), MAX_ERROR_CHARS) or "no error text"
    testimony = "; ".join(f"{_clean(name, MAX_SOURCE_CHARS)}: {'reaches Core' if report.get('primary_ok') else 'cannot reach Core'}"
                          for name, report in sorted((snapshot.get("studios") or {}).items())) or "no studio sent a report"
    host = _clean(public_host, 100)
    rows = [
        ("Core watchdog", f"DOWN since {since}"),
        ("Why", f"{snapshot.get('failed_probes_in_a_row', 0)} failed checks in a row; last error: {error}"),
        ("What the studios say", testimony),
    ]
    if any(report.get("primary_ok") for report in (snapshot.get("studios") or {}).values()):
        rows.append(("Studios disagree", "Some studios still say they reach Core, but its public health page has not answered for a long time, so the standby is "
                                         "alerting anyway. Core may be up but unreachable from outside, or a studio's report may be wrong."))
    rows += [
        ("Studios as the standby sees them", _studio_rows(studios)),
        ("What this means", "The standby is now sending the studio alerts. Core's own dashboard and alerts may not work until it is back."),
        ("Standby dashboard", f"https://{host}/"),
    ]
    summary = "The Core watchdog has stopped answering. The standby listener has taken over watching the studios."
    return Notice("Core watchdog is DOWN", summary, rows, "Watchdog: Core is DOWN", _clean(f"Standby has taken over. Last error: {error}. {_studio_rows(studios)}", 280))


def standdown_notice(down_since: datetime, now: datetime, studios, public_host: str) -> Notice:
    """The one message sent when Core is answering again and the standby goes quiet."""
    gone = _duration((now - down_since).total_seconds())
    host = _clean(public_host, 100)
    rows = [
        ("Core watchdog", "answering again"),
        ("Was down for", gone),
        ("Studios as the standby sees them", _studio_rows(studios)),
        ("What this means", "The standby has stood down and is quiet again. Core sends the alerts."),
        ("Standby dashboard", f"https://{host}/"),
    ]
    return Notice("Core watchdog is back", f"Core is answering again after {gone}. The standby has stood down.", rows,
                  "Watchdog: Core is back", _clean(f"Core answered again after {gone}. Standby stood down.", 280))


# ---- Settings ----------------------------------------------------------------------------------------------------------------------------------
def parse_role(setting) -> str:
    """WATCHDOG_ROLE. Empty means `primary`, so Core behaves exactly as before. Anything else unknown stops the start (a typo must not silently pick a role)."""
    value = str(setting or "").strip().lower()
    if not value:
        return "primary"
    if value not in ROLES:
        raise ValueError(f"WATCHDOG_ROLE must be one of {ROLES}, not {value[:20]!r}")
    return value


def parse_probe_url(setting) -> str:
    """WATCHDOG_PRIMARY_URL: the address of Core's /health. Empty is allowed (no probing). Only a plain http(s) address with a host and no embedded login."""
    text = str(setting or "").strip()
    if not text:
        return ""
    if any(ch.isspace() or ord(ch) < 32 for ch in text):
        raise ValueError("WATCHDOG_PRIMARY_URL must not contain spaces or control characters")
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("WATCHDOG_PRIMARY_URL must be an http(s) address with a host and no login in it")
    return text


# ---- "Standby alive": is the standby READY, not just running? --------------------------------------------------------------------------------------
# A backup nobody monitors fails silently: the process can be up while its keys are stale, a studio has stopped sending copies, the disk is full, the mail
# account is blocked or the probe loop is stuck. The standby therefore judges itself every minute and pings an outside check (Healthchecks.io "Standby alive")
# only while this list is EMPTY; on any problem it reports the reasons instead, and the outside service emails. If the standby, its machine or its network
# is gone altogether, the pings simply stop and the same service emails after its grace period. Pure: the caller hands in what it measured.
MAX_SUMMARY_CHARS = 900          # what a ping body carries; outside services keep only a little
_COPY_NAME = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,31}$")


@dataclass(frozen=True)
class HealthConfig:
    expect_copies: tuple = ()               # the studios that send this standby a copy of every post; empty = do not judge the copies
    copy_max_age_seconds: int = 300         # a studio's latest copy may be this old
    probe_max_age_seconds: int = 180        # the Core probe loop must have completed a probe this recently
    min_free_mb: int = 200                  # free disk under the database
    refusals_warn: int = 3                  # refused posts (wrong key) within the window that turn a stale copy into a key-mismatch hint
    refusals_window_seconds: int = 300
    startup_grace_seconds: int = 120        # after a start the studios need a moment to find the standby again


@dataclass(frozen=True)
class HealthInputs:
    now: datetime
    started_at: datetime
    last_probe_at: Optional[datetime]       # when the Core probe loop last completed a probe (None: not yet)
    db_ok: bool
    free_mb: Optional[float]                # None when it could not be measured
    key_error: str                          # the keys file watcher's latest problem ("" when the file in force is the file on disk)
    keyed_sources: tuple                    # the studios that have a sender key loaded
    copy_seen: dict                         # studio -> when its latest accepted post arrived (None: never since the standby started)
    refusals_recent: int                    # posts refused for a wrong key within config.refusals_window_seconds
    smtp_ok: Optional[bool]                 # None: email is not configured
    probe_proven: bool = True               # False: no probe of Core has succeeded recently, so the standby could not tell a dead Core from a blocked probe
    probe_error: str = ""                   # the latest probe error (what a blocked probe looks like: "HTTP 403")


def _minutes(seconds: float) -> str:
    count = int(seconds // 60)
    return f"{count} minute" + ("" if count == 1 else "s")


def assess_health(inputs: HealthInputs, config: HealthConfig) -> list:
    """The reasons the standby is NOT ready, in plain English, in a stable order; an empty list means everything is in order. Never contains a key."""
    _require_aware(inputs.now, "now")
    _require_aware(inputs.started_at, "started_at")
    problems: list = []
    if not inputs.db_ok:
        problems.append("the database did not answer")
    if inputs.free_mb is not None and inputs.free_mb < config.min_free_mb:
        problems.append(f"only {int(inputs.free_mb)} MB of disk space is free (the minimum is {config.min_free_mb} MB)")
    if inputs.key_error:
        problems.append("the sender keys file has a problem: " + _clean(inputs.key_error, MAX_ERROR_CHARS))
    unkeyed = [name for name in config.expect_copies if name not in inputs.keyed_sources]
    for name in unkeyed:
        problems.append(f"{name} has no sender key loaded, so its copies cannot be accepted")
    if (inputs.now - inputs.started_at).total_seconds() >= config.startup_grace_seconds:
        if inputs.last_probe_at is None:
            problems.append("no probe of Core has completed since the standby started")
        else:
            age = (inputs.now - inputs.last_probe_at).total_seconds()
            if age >= config.probe_max_age_seconds:
                problems.append(f"Core has not been probed for {_minutes(age)}")
        if not inputs.probe_proven:
            problems.append("the standby cannot prove that it can read Core's health page" + (f" (latest error: {_clean(inputs.probe_error, MAX_ERROR_CHARS)})" if inputs.probe_error else "")
                            + ", so it will NOT take over if Core fails: fix the probe")
        silent = False
        for name in config.expect_copies:
            if name in unkeyed:
                continue                                    # already reported above
            seen = inputs.copy_seen.get(name)
            if seen is None:
                problems.append(f"{name}: no copy received since the standby started")
                silent = True
                continue
            age = (inputs.now - seen).total_seconds()
            if age >= config.copy_max_age_seconds:
                problems.append(f"{name}: no copy received for {_minutes(age)}")
                silent = True
        if silent and inputs.refusals_recent >= config.refusals_warn:
            problems.append(f"{inputs.refusals_recent} posts were refused in the last {config.refusals_window_seconds // 60} minutes: a studio's key probably no longer "
                            "matches the standby (run ops/standby_sync.py reconcile)")
    if inputs.smtp_ok is False:
        problems.append("the email login does not work, so alert emails could not be sent")
    return problems


def summarize_problems(problems) -> str:
    """The problems as one short line of plain ASCII (control characters removed), for a ping body or a log line; "" when there are none."""
    text = "; ".join(_clean(problem, 300) for problem in problems if problem)
    return text.encode("ascii", "replace").decode("ascii")[:MAX_SUMMARY_CHARS]


def parse_expect_copies(setting) -> tuple:
    """WATCHDOG_STANDBY_EXPECT_COPIES: the studios whose copies the standby should keep receiving, e.g. `MAIN,BACKUP-SERVER`. Empty means none are judged.
    Strict: a name must look like a studio name (upper case, digits, - and _), so a typo or a stray list separator stops the start."""
    names: list = []
    for part in str(setting or "").split(","):
        name = part.strip()
        if not name:
            continue
        if not _COPY_NAME.fullmatch(name):
            raise ValueError(f"WATCHDOG_STANDBY_EXPECT_COPIES has a name that is not a studio name: {name[:20]!r}")
        if name not in names:
            names.append(name)
    return tuple(names)
