"""Core's own vital signs and the dashboard's counters.

Two small, dependency-free jobs live here so main.py does not grow further:

* `SystemSampler` reads the Linux server's CPU load, memory, disk and uptime from /proc and keeps
  a short CPU history for the dashboard's sparkline. Inside Docker, /proc/stat, /proc/meminfo,
  /proc/loadavg and /proc/uptime still describe the WHOLE host (they are not namespaced), which is
  exactly what is wanted: Core is shared with other workloads, and "how busy is the server" is
  the question. On a machine without /proc (a Windows development PC) every reading is None and
  nothing raises.
* `fleet_counters` turns the event log into the numbers the dashboard counts up: critical alarms and
  events in the last 24 hours and the moment of the last critical alarm.

Nothing here writes to the database or sends anything.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Deque, Optional

SAMPLE_SECONDS = 5
HISTORY_POINTS = 90          # 90 samples x 5 s = 7.5 minutes of CPU history
HEARTBEAT_WINDOW_SECONDS = 60
MAX_TRACKED_HEARTBEATS = 20000   # a flood cannot grow the window without limit
# Where the kernel's counters are read from. Always /proc on the server; a test points it at a folder of made-up files.
PROC_ROOT = os.environ.get("WATCHDOG_PROC_ROOT", "/proc")


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="ascii", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def parse_cpu_times(stat_text: Optional[str]) -> Optional[tuple[int, int]]:
    """(idle, total) jiffies from the first line of /proc/stat. iowait counts as idle: a core waiting
    on the disk is not doing work. guest time is already inside user time, so it is not added twice."""
    if not stat_text:
        return None
    for line in stat_text.splitlines():
        if line.startswith("cpu "):
            try:
                fields = [int(part) for part in line.split()[1:9]]
            except ValueError:
                return None
            if len(fields) < 5:
                return None
            idle = fields[3] + fields[4]
            return idle, sum(fields)
    return None


def cpu_percent(previous: Optional[tuple[int, int]], current: Optional[tuple[int, int]]) -> Optional[float]:
    """Busy share between two readings, 0 to 100. None when there is nothing to compare."""
    if not previous or not current:
        return None
    total_delta = current[1] - previous[1]
    idle_delta = current[0] - previous[0]
    if total_delta <= 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * (total_delta - idle_delta) / total_delta)), 1)


def parse_loadavg(text: Optional[str]) -> Optional[tuple[float, float, float]]:
    if not text:
        return None
    parts = text.split()
    try:
        return float(parts[0]), float(parts[1]), float(parts[2])
    except (IndexError, ValueError):
        return None


def parse_meminfo(text: Optional[str]) -> Optional[dict]:
    """Memory in use as the kernel counts it: total minus MemAvailable (cache that can be dropped is not 'used')."""
    if not text:
        return None
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            try:
                values[key] = int(rest.split()[0])
            except (IndexError, ValueError):
                return None
    if "MemTotal" not in values or "MemAvailable" not in values or values["MemTotal"] <= 0:
        return None
    used = values["MemTotal"] - values["MemAvailable"]
    return {
        "mem_total_mb": round(values["MemTotal"] / 1024),
        "mem_used_percent": round(100.0 * used / values["MemTotal"], 1),
    }


def parse_uptime(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    try:
        return int(float(text.split()[0]))
    except (IndexError, ValueError):
        return None


def disk_usage(path: str = "/") -> Optional[dict]:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    if usage.total <= 0:
        return None
    return {
        "disk_total_gb": round(usage.total / 1e9),
        "disk_used_percent": round(100.0 * usage.used / usage.total, 1),
    }


class SystemSampler:
    """Takes one reading per `sample()` call and remembers the last few minutes of CPU."""

    def __init__(self) -> None:
        self._previous_cpu: Optional[tuple[int, int]] = None
        self._history: Deque[Optional[float]] = deque(maxlen=HISTORY_POINTS)
        self._latest: dict = {}

    def sample(self) -> dict:
        current_cpu = parse_cpu_times(_read_text(PROC_ROOT + "/stat"))
        if current_cpu is None and self._latest.get("available"):
            return self._latest      # one failed read must not blank a panel that was working: keep showing the last good reading
        percent = cpu_percent(self._previous_cpu, current_cpu)
        self._previous_cpu = current_cpu or self._previous_cpu
        if percent is not None:
            self._history.append(percent)
        load = parse_loadavg(_read_text(PROC_ROOT + "/loadavg"))
        reading = {
            "available": current_cpu is not None,
            "cpu_percent": percent if percent is not None else (self._history[-1] if self._history else None),
            "cores": os.cpu_count(),
            "load1": load[0] if load else None,
            "load5": load[1] if load else None,
            "load15": load[2] if load else None,
            "uptime_s": parse_uptime(_read_text(PROC_ROOT + "/uptime")),
            "sample_seconds": SAMPLE_SECONDS,
        }
        reading.update(parse_meminfo(_read_text(PROC_ROOT + "/meminfo")) or {"mem_total_mb": None, "mem_used_percent": None})
        reading.update(disk_usage("/") or {"disk_total_gb": None, "disk_used_percent": None})
        self._latest = reading
        return reading

    def snapshot(self) -> dict:
        """The latest reading plus the CPU history, in the shape the stream sends."""
        return {**self._latest, "cpu_history": list(self._history)}


class HeartbeatCounter:
    """Heartbeats received, counted two ways: since the listener started, and per minute over a sliding window."""

    def __init__(self) -> None:
        self.total = 0
        self._times: Deque[float] = deque(maxlen=MAX_TRACKED_HEARTBEATS)

    def record(self, now: Optional[float] = None) -> None:
        stamp = time.monotonic() if now is None else now
        self.total += 1
        self._times.append(stamp)
        self._trim(stamp)

    def _trim(self, now: float) -> None:
        while self._times and now - self._times[0] > HEARTBEAT_WINDOW_SECONDS:
            self._times.popleft()

    def per_minute(self, now: Optional[float] = None) -> int:
        stamp = time.monotonic() if now is None else now
        self._trim(stamp)
        return len(self._times)


def fleet_counters(conn: sqlite3.Connection, now: datetime, started_at: datetime, heartbeats: HeartbeatCounter) -> dict:
    """What the dashboard counts up. Events are counted over the last 24 hours; 'suppressed' rows are the
    detectors' own noise log (a pause seen but not alarmed), so they are left out of the event count."""
    since = (now - timedelta(hours=24)).isoformat()
    events_24h = conn.execute(
        "SELECT COUNT(*) FROM events WHERE timestamp_utc >= ? AND event_type != 'suppressed'", (since,)
    ).fetchone()[0]
    red_24h = conn.execute(
        "SELECT COUNT(*) FROM events WHERE timestamp_utc >= ? AND event_type = 'endpoint_critical'", (since,)
    ).fetchone()[0]
    last_red = conn.execute(
        "SELECT MAX(timestamp_utc) FROM events WHERE event_type = 'endpoint_critical'"
    ).fetchone()[0]
    # Counted forward by the browser from the moment each frame arrives, so a phone with a wrong clock still shows the right figure.
    since_last_red = None
    if last_red:
        try:
            moment = datetime.fromisoformat(last_red)
            moment = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
            since_last_red = max(0.0, round((now - moment).total_seconds(), 1))
        except ValueError:
            since_last_red = None
    return {
        "events_24h": events_24h,
        "red_alarms_24h": red_24h,
        "last_red_alarm_utc": last_red,
        "since_last_red_s": since_last_red,
        "listener_started_utc": started_at.astimezone(timezone.utc).isoformat(),
        "listener_uptime_s": max(0, int((now - started_at).total_seconds())),
        "heartbeats_total": heartbeats.total,
        "heartbeats_per_minute": heartbeats.per_minute(),
    }
