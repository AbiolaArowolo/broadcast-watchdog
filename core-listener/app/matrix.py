# File:         matrix.py
# Purpose:      Talks to the AVMATRIX MSS0811 8x8 SDI matrix switcher by UDP and switches it between the owner's three presets (MAIN PC, BACKUP PC, BRIGHTSIGN). It builds and reads
#               the manufacturer's packets, decides which preset is active from a READING (never from what was last asked), runs one switch at a time (send only the outputs that
#               are wrong, read back, resend, roll back, report), and keeps the small log table. The rules are in the unpublished design notes.
# Inputs:       A link to the matrix (UdpLink on Core, a fake in the tests) with two methods: send(datagram) and read_status() -> eight numbers; a clock and a sleep function that
#               the tests replace; a sqlite connection held under the caller's lock for the log.
# Outputs:      Outcome objects (JSON-ready) for a switch, status dictionaries for the page, log rows.
# Dependencies: Python standard library only. main.py owns the endpoints, the access rules, the e-mail and the database lock; this file never reads a request or a secret.
import ipaddress
import json
import logging
import re
import socket
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("matrix")

INPUTS = 8
OUTPUTS = 8
DEFAULT_PORT = 7000
TRIES = 3                      # one switch sends, reads back and resends at most this many times
SETTLE_SECONDS = 0.4           # how long the matrix is given to apply a datagram before it is read
READ_TIMEOUT_SECONDS = 1.0     # one read waits this long for the answer ...
READ_ATTEMPTS = 2              # ... and asks this many times before the matrix counts as not answering
STATUS_MAX_AGE_SECONDS = 2.0   # several open pages share one reading this old, so they cannot flood the matrix
LOG_KEEP_ROWS = 5000

_HEADER = b"\xa5\x6c"
_END = 0xAE
_READ_STATUS = 0x53
_DEVICE_TYPE = 0x82            # "matrix switcher" in the manufacturer's guide
_MIN_REPLY = 29                # header 16, command, status, eight outputs, checksum 2, end 1

# The owner's names for the inputs (2026-11-09). Inputs 4, 6 and 8 carry nothing he named and are shown by their number.
INPUT_NAMES = {1: "BrightSign Player", 2: "Main Server USA", 3: "Main Server Caribbean", 5: "Backup Server Caribbean", 7: "Backup Server USA"}


class MatrixError(Exception):
    """The matrix gave no usable answer: silence, a damaged packet, a wrong command or an error status."""


# ---- The packets -----------------------------------------------------------------------------------------------------------------------------------------
def _whole(value, low: int, high: int, what: str) -> int:
    if type(value) is not int or not low <= value <= high:        # a bool, a float or a string is refused, not converted
        raise ValueError(f"{what} must be a whole number from {low} to {high}")
    return value


def route_command(source, outputs) -> bytes:
    """The manufacturer's ASCII route form for ONE input and one or more outputs, e.g. b"2v1,2,5." (input 2 to outputs 1, 2 and 5). The matrix sends no answer to it.
    The `All` and `#` forms of the guide are deliberately not buildable here."""
    _whole(source, 1, INPUTS, "the input")
    if not isinstance(outputs, (tuple, list)) or not outputs:
        raise ValueError("at least one output is needed")
    for output in outputs:
        _whole(output, 1, OUTPUTS, "an output")
    if len(set(outputs)) != len(outputs):
        raise ValueError("an output is listed twice")
    return f"{source}v{','.join(str(output) for output in outputs)}.".encode("ascii")


def frame(device_type: int, command: int, data: bytes = b"") -> bytes:
    """One binary packet as the guide lays it out: header a5 6c, length (low byte first, the whole packet), device type, device id 01, interface 01 (LAN), nine reserved zero
    bytes, command, data, checksum (the sum of every byte before it, two bytes, low byte first), end ae."""
    body = bytes([0xA5, 0x6C, 0, 0, device_type, 0x01, 0x01]) + bytes(9) + bytes([command]) + bytes(data)
    body = body[:2] + (len(body) + 3).to_bytes(2, "little") + body[4:]
    return body + (sum(body) & 0xFFFF).to_bytes(2, "little") + bytes([_END])


def read_packet() -> bytes:
    """The one packet that asks the matrix for its status (command 0x53). It only reads."""
    return frame(_DEVICE_TYPE, _READ_STATUS)


def parse_status(reply) -> tuple:
    """The eight numbers (the input connected to output 1 to 8) from a read-status answer, or MatrixError saying exactly what is wrong with it. A number outside 1 to 8 is kept
    as it is (the page shows it as unknown); it is never guessed or dropped."""
    if not isinstance(reply, (bytes, bytearray)):
        raise MatrixError("the answer was not bytes")
    size = len(reply)
    if size < _MIN_REPLY:
        raise MatrixError(f"the answer is too short ({size} bytes)")
    if bytes(reply[:2]) != _HEADER or reply[-1] != _END:
        raise MatrixError("the answer does not start and end like a matrix packet")
    if int.from_bytes(reply[2:4], "little") != size:
        raise MatrixError("the answer's length field disagrees with the bytes that arrived")
    if (sum(reply[:-3]) & 0xFFFF) != int.from_bytes(reply[-3:-1], "little"):
        raise MatrixError("the answer's checksum is wrong")
    if reply[16] != _READ_STATUS:
        raise MatrixError("the answer belongs to another command")
    if reply[17] != 0x00:
        raise MatrixError("the matrix reported an error status")
    return tuple(reply[18:18 + OUTPUTS])


# ---- The presets -----------------------------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Preset:
    key: str
    title: str
    group: str
    routes: tuple                    # ((input, (output, ...)), ...) in the order the datagrams are sent; () means the owner has not given the outputs yet
    needs_confirm: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.routes)

    @property
    def outputs(self) -> tuple:
        return tuple(sorted(output for _source, outs in self.routes for output in outs))

    def wanted(self) -> dict:
        """{output: input} for every output this preset owns."""
        return {output: source for source, outs in self.routes for output in outs}


# The owner's three presets, exactly as he wrote them (2026-11-09: "follow this route ... STRICTLY"; BrightSign "12345" on 2026-11-09, changed by him to outputs 1 to 6 on 2026-11-10). MAIN and BACKUP set only outputs 1, 2, 5 and 6, so after an
# emergency outputs 3 and 4 stay on BrightSign until they are changed by hand: that is his order, not an oversight. A preset with no routes cannot be pressed (the guard stays).
PRESETS = {
    "main": Preset("main", "MAIN PC", "NORMAL OPERATION", ((2, (1, 2, 5)), (3, (6,)))),
    "backup": Preset("backup", "BACKUP PC", "BACKUP OPERATION", ((7, (1, 2, 5)), (5, (6,)))),
    "brightsign": Preset("brightsign", "BRIGHTSIGN PLAYER", "EMERGENCY OPERATION", ((1, (1, 2, 3, 4, 5, 6)),), needs_confirm=True),
}
PRESET_ORDER = ("main", "backup", "brightsign")


def differences(routing, preset: Preset) -> list:
    """The outputs of `preset` whose reading is not what the preset wants. Empty for an unconfigured preset or no reading."""
    if routing is None or not preset.configured:
        return []
    return sorted(output for output, source in preset.wanted().items() if routing[output - 1] != source)


def active_preset(routing) -> Optional[str]:
    """The key of the preset whose every output matches the READING, or None. Outputs the preset does not own never decide it. No reading means nothing is active."""
    if routing is None:
        return None
    for key in PRESET_ORDER:
        preset = PRESETS[key]
        if preset.configured and not differences(routing, preset):
            return key
    return None


def commands_for(preset: Preset, routing) -> list:
    """The datagrams that would bring `preset` into place from `routing`: one per input, naming ONLY the outputs that are wrong, in the preset's order. An output that already
    carries the wanted input is never sent again."""
    commands = []
    for source, outs in preset.routes:
        wrong = tuple(output for output in outs if routing[output - 1] != source)
        if wrong:
            commands.append(route_command(source, wrong))
    return commands


def output_rows(routing) -> list:
    """[{output, input, input_name}] for the page. A number outside 1 to 8 is shown as unknown (input None); an input nobody named is shown by its number."""
    if routing is None:
        return []
    rows = []
    for output, source in enumerate(routing, start=1):
        known = isinstance(source, int) and 1 <= source <= INPUTS
        rows.append({"output": output, "input": source if known else None, "input_name": (INPUT_NAMES.get(source) or f"Input {source}") if known else "unknown"})
    return rows


def _preset_view(preset: Preset, routing, switching: bool) -> dict:
    wrong = differences(routing, preset)
    return {"key": preset.key, "title": preset.title, "group": preset.group, "configured": preset.configured, "needs_confirm": preset.needs_confirm,
            "active": bool(routing is not None and not switching and preset.configured and not wrong), "differs": wrong,
            "routes": [{"input": source, "input_name": INPUT_NAMES.get(source, f"Input {source}"), "outputs": list(outs)} for source, outs in preset.routes]}


# ---- The link ----------------------------------------------------------------------------------------------------------------------------------------------
class UdpLink:
    """The real link: UDP datagrams to the matrix. A new socket for every exchange, so a late answer to an old question can never be taken for the answer to a new one."""

    def __init__(self, host: str, port: int = DEFAULT_PORT, timeout: float = READ_TIMEOUT_SECONDS, attempts: int = READ_ATTEMPTS):
        self.host, self.port, self.timeout, self.attempts = host, port, timeout, attempts

    @staticmethod
    def _new_socket() -> socket.socket:
        try:
            return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError as error:                       # no free socket (too many open files ...): a matrix error, never an escape
            raise MatrixError(f"no socket could be opened: {error}") from error

    def send(self, datagram: bytes) -> None:
        with self._new_socket() as sock:
            try:
                sock.sendto(datagram, (self.host, self.port))
            except OSError as error:
                raise MatrixError(f"the datagram could not be sent: {error}") from error

    def read_status(self) -> tuple:
        problem = "no answer"
        for _attempt in range(max(1, self.attempts)):
            try:
                return self._ask_once()
            except MatrixError as error:
                problem = str(error)
        raise MatrixError(problem)

    def _ask_once(self) -> tuple:
        problem = f"no answer within {self.timeout:g} s"
        with self._new_socket() as sock:
            try:
                sock.sendto(read_packet(), (self.host, self.port))
            except OSError as error:
                raise MatrixError(f"the question could not be sent: {error}") from error
            deadline = time.monotonic() + self.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MatrixError(problem)
                sock.settimeout(remaining)
                try:
                    data, source = sock.recvfrom(2048)
                except socket.timeout:
                    raise MatrixError(problem) from None
                except OSError as error:
                    raise MatrixError(f"the answer could not be read: {error}") from error
                if source[0] != self.host:
                    continue                                    # not from the matrix: ignored
                try:
                    return parse_status(data)
                except MatrixError as error:
                    problem = f"unusable answer: {error}"       # keep waiting: the real answer may still follow


# ---- One switch --------------------------------------------------------------------------------------------------------------------------------------------
@dataclass
class Outcome:
    ok: bool
    state: str                       # done, already, offline, failed, unverified, busy, not_configured
    message: str
    preset: str = ""
    before: Optional[tuple] = None
    after: Optional[tuple] = None
    failed_outputs: list = field(default_factory=list)       # [{output, wanted, got}] for outputs that did not take the new input
    restored: Optional[bool] = None                          # after a failure: are the preset's outputs back to what they were before the press
    unrestorable: list = field(default_factory=list)         # outputs that changed but whose earlier input was unreadable, so they could not be put back
    changed_elsewhere: list = field(default_factory=list)    # [{output, was, now}] outputs the preset does not own that changed meanwhile
    attempts: int = 0
    sent: list = field(default_factory=list)                 # the datagrams, as text, in order
    started_at: str = ""
    finished_at: str = ""

    def as_dict(self) -> dict:
        return {"ok": self.ok, "state": self.state, "message": self.message, "preset": self.preset,
                "before": list(self.before) if self.before is not None else None, "after": list(self.after) if self.after is not None else None,
                "failed_outputs": self.failed_outputs, "restored": self.restored, "unrestorable": self.unrestorable, "changed_elsewhere": self.changed_elsewhere,
                "attempts": self.attempts, "sent": self.sent, "started_at": self.started_at, "finished_at": self.finished_at}


def _list_outputs(numbers) -> str:
    numbers = list(numbers)
    return ("output " if len(numbers) == 1 else "outputs ") + ", ".join(str(number) for number in numbers)


def _elsewhere_note(items) -> str:
    """A sentence for the operator when an output the preset does not own changed during the switch (someone used the manufacturer's Control Panel meanwhile)."""
    if not items:
        return ""
    return " Note: " + "; ".join(f"output {item['output']} changed meanwhile (was input {item['was']}, now input {item['now']})" for item in items) + ", so someone else used the matrix."


class Switcher:
    """Switches the matrix, one operation at a time. `link` has send(datagram) and read_status(); the clock, the monotonic clock and the sleep are injected so a test can run a
    whole switch in no time. Nothing here is asynchronous: main.py runs these calls in a worker thread."""

    def __init__(self, link, clock=None, monotonic=None, sleep=None, settle: float = SETTLE_SECONDS, tries: int = TRIES, max_age: float = STATUS_MAX_AGE_SECONDS):
        self._link = link
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._settle, self._tries, self._max_age = settle, tries, max_age
        self._lock = threading.Lock()                  # held for the whole of one switch; a second press never waits for it
        self._cache_lock = threading.Lock()
        self._cache = None                             # (routing, monotonic time stored, iso time, monotonic time the read began) of the newest good reading

    # -- what the page shows --
    def status(self, fresh: bool = False) -> dict:
        """The matrix as the page shows it. While a switch runs it does NOT read the matrix (the answer would be a half-way state) and says it is switching. An unreachable matrix is
        shown as unreachable with no routing and no active preset, whatever was read before."""
        with self._cache_lock:
            cached = self._cache
        if self._lock.locked():
            return self._view(cached[0] if cached else None, cached[2] if cached else None, True if cached else None, True, None)
        if cached and not fresh and self._monotonic() - cached[1] < self._max_age:
            return self._view(cached[0], cached[2], True, False, None)
        began = self._monotonic()
        try:
            routing = self._link.read_status()
        except MatrixError as error:
            self._remember(None)
            logger.warning("[matrix.py::status] error | error=%s action=report_offline", error)
            return self._view(None, None, False, False, str(error))
        held, checked_at = self._remember(routing, began)
        return self._view(held, checked_at, True, False, None)

    def _view(self, routing, checked_at, reachable, switching: bool, error) -> dict:
        return {"reachable": reachable, "switching": switching, "routing": list(routing) if routing is not None else None, "checked_at": checked_at,
                "active": None if switching else active_preset(routing), "outputs": output_rows(routing),
                "presets": [_preset_view(PRESETS[key], routing, switching) for key in PRESET_ORDER], "error": error}

    def _remember(self, routing, began=None) -> tuple:
        """Keeps `routing` as the newest reading and returns (the routing now held, when it was taken). A reading whose read BEGAN before the held one began is older (a status read that
        overlapped a switch's read-back) and never replaces it. None clears the cache: an unreachable matrix is never shown with an old routing. The tuple comes from this call, not from
        a later look at the cache, so another thread clearing it at the same moment cannot break the caller."""
        with self._cache_lock:
            if routing is None:
                self._cache = None
                return None, None
            started = self._monotonic() if began is None else began
            if self._cache is None or started >= self._cache[3]:
                self._cache = (routing, self._monotonic(), self._clock().isoformat(), started)
            return self._cache[0], self._cache[2]

    def _read(self):
        """A fresh reading, or None when the matrix does not give a usable answer."""
        began = self._monotonic()
        try:
            routing = self._link.read_status()
        except MatrixError as error:
            logger.warning("[matrix.py::_read] error | error=%s", error)
            self._remember(None)
            return None
        self._remember(routing, began)
        return routing

    def _send(self, command: bytes) -> bool:
        """Sends one datagram. A send the system refuses is not fatal and not an escape: the read-back that follows decides whether anything arrived, and the next try resends."""
        try:
            self._link.send(command)
            return True
        except MatrixError as error:
            logger.warning("[matrix.py::_send] error | command=%s error=%s action=continue_to_read_back", command.decode("ascii"), error)
            return False

    # -- one switch --
    def switch(self, key) -> Outcome:
        """Brings one preset into place and returns what the matrix CONFIRMED. Raises ValueError for a name that is not a preset. A second call while one runs returns state busy at
        once and sends nothing."""
        if not isinstance(key, str) or key not in PRESETS:
            raise ValueError("unknown preset")
        preset = PRESETS[key]
        started = self._clock().isoformat()
        if not preset.configured:
            return Outcome(False, "not_configured", f"{preset.title} cannot be used yet: its outputs have not been set.", key, started_at=started, finished_at=started)
        if not self._lock.acquire(blocking=False):
            return Outcome(False, "busy", "A switch is already running. Wait for it to finish.", key, started_at=started, finished_at=started)
        try:
            logger.info("[matrix.py::switch] input_received | preset=%s", key)
            outcome = Outcome(False, "offline", "", key, started_at=started)
            try:
                self._run(preset, outcome)
            except Exception as error:  # noqa: BLE001 -- nothing may escape once datagrams may have left: the log, the mail and the page must still hear what was sent
                logger.error("[matrix.py::switch] error | preset=%s error=%r action=report_unverified", key, error)
                outcome.ok = False
                if outcome.sent:
                    outcome.state, outcome.after = "unverified", None
                    outcome.message = "An unexpected error stopped the switch after commands were sent, so the routing is not confirmed. Check the matrix."
                else:
                    outcome.state, outcome.message = "offline", "An unexpected error stopped the switch before anything was sent. Nothing was changed."
            outcome.finished_at = self._clock().isoformat()
            logger.info("[matrix.py::switch] output_returned | preset=%s state=%s attempts=%s", key, outcome.state, outcome.attempts)
            return outcome
        finally:
            self._lock.release()

    def _run(self, preset: Preset, outcome: Outcome) -> None:
        before = self._read()
        if before is None:
            outcome.message = "The matrix did not answer. Nothing was sent and nothing is assumed."
            return
        outcome.before = outcome.after = before
        wanted = preset.wanted()
        if not differences(before, preset):
            outcome.ok, outcome.state, outcome.message = True, "already", f"{preset.title} is already in place. Nothing was sent."
            return
        current = before
        for attempt in range(1, self._tries + 1):
            outcome.attempts = attempt
            for command in commands_for(preset, current):
                logger.info("[matrix.py::_run] decision | action=send command=%s attempt=%s", command.decode("ascii"), attempt)
                outcome.sent.append(command.decode("ascii"))
                self._send(command)
            self._sleep(self._settle)
            current = self._read()
            if current is None:
                outcome.state, outcome.after = "unverified", None
                outcome.message = "The commands were sent but the matrix stopped answering, so the routing is not confirmed. Check the matrix."
                return
            outcome.after = current
            if not differences(current, preset):
                break
        outcome.changed_elsewhere = [{"output": output, "was": before[output - 1], "now": current[output - 1]} for output in range(1, OUTPUTS + 1)
                                     if output not in wanted and current[output - 1] != before[output - 1]]
        if not differences(current, preset):
            outcome.ok, outcome.state = True, "done"
            outcome.message = f"{preset.title} is active: the matrix confirmed {_list_outputs(preset.outputs)}." + _elsewhere_note(outcome.changed_elsewhere)
            return
        outcome.state = "failed"
        outcome.failed_outputs = [{"output": output, "wanted": wanted[output], "got": current[output - 1]} for output in differences(current, preset)]
        self._roll_back(preset, before, current, outcome)

    def _roll_back(self, preset: Preset, before: tuple, current: tuple, outcome: Outcome) -> None:
        """Puts the preset's outputs back to what they were before the press, but only those that changed, and says whether the matrix confirmed it."""
        said = "; ".join(f"output {item['output']} shows input {item['got']}, wanted {item['wanted']}" for item in outcome.failed_outputs)
        changed = [output for output in preset.outputs if current[output - 1] != before[output - 1]]
        outcome.unrestorable = [output for output in changed if not 1 <= before[output - 1] <= INPUTS]
        by_input: dict = {}
        for output in changed:
            if output not in outcome.unrestorable:
                by_input.setdefault(before[output - 1], []).append(output)
        for source in sorted(by_input):
            command = route_command(source, tuple(by_input[source]))
            logger.warning("[matrix.py::_roll_back] decision | action=restore command=%s", command.decode("ascii"))
            outcome.sent.append(command.decode("ascii"))
            self._send(command)
        final = current
        if by_input:
            self._sleep(self._settle)
            final = self._read()
        outcome.after = final
        outcome.restored = bool(final is not None and not outcome.unrestorable and all(final[output - 1] == before[output - 1] for output in preset.outputs))
        if outcome.restored and not by_input:
            outcome.message = f"The matrix did not confirm {preset.title}: {said}. Nothing changed: the routing is as it was before."
        elif outcome.restored:
            outcome.message = f"The matrix did not confirm {preset.title}: {said}. The routing from before was put back and confirmed."
        else:
            left = [output for output in preset.outputs if final is None or final[output - 1] != before[output - 1]]
            outcome.message = f"The matrix did not confirm {preset.title}: {said}. The routing from before could NOT be put back ({_list_outputs(left)}). Check the matrix."


# ---- Settings --------------------------------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Settings:
    host: str
    port: int


def settings_from_environ(environ) -> tuple:
    """(Settings or None, [problems]). No host setting means the feature is off, which is not a problem. The host must be a literal IPv4 address on a private network (or the
    loopback address, for tests): a name is never looked up, so nothing a person types can point the controller somewhere else."""
    text = str(environ.get("WATCHDOG_MATRIX_HOST", "")).strip()
    if not text:
        return None, []
    try:
        address = ipaddress.IPv4Address(text)
    except ValueError:
        return None, ["WATCHDOG_MATRIX_HOST must be an IPv4 address such as 192.168.7.50 (a name is never looked up), so the matrix controller is off"]
    if not (address.is_private or address.is_loopback) or address.is_unspecified or address.is_link_local or address.is_reserved or address.is_multicast:
        return None, ["WATCHDOG_MATRIX_HOST must be an address on the studio network (private), so the matrix controller is off"]
    port_text = str(environ.get("WATCHDOG_MATRIX_PORT", str(DEFAULT_PORT))).strip() or str(DEFAULT_PORT)
    if not (port_text.isascii() and port_text.isdigit() and 1024 <= int(port_text) <= 65535):
        return None, ["WATCHDOG_MATRIX_PORT must be a whole number from 1024 to 65535, so the matrix controller is off"]
    return Settings(str(address), int(port_text)), []


# ---- The log table ---------------------------------------------------------------------------------------------------------------------------------------
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def clean_text(value, limit: int) -> str:
    """Text for the log with every control character removed and a length limit, so a name can never break a line of the log or an e-mail."""
    return _CONTROL.sub("", str(value))[:limit]


def init_table(conn: sqlite3.Connection) -> None:
    """The audit log of every press. It is NOT the events table, because that one is shown to the public dashboard and this one names the person and the routing."""
    conn.execute("CREATE TABLE IF NOT EXISTS matrix_log (id INTEGER PRIMARY KEY AUTOINCREMENT, at_utc TEXT NOT NULL, operator TEXT NOT NULL, preset TEXT NOT NULL, "
                 "result TEXT NOT NULL, via TEXT NOT NULL, detail TEXT NOT NULL)")


def insert_log(conn: sqlite3.Connection, at: str, operator: str, preset: str, result: str, via: str, detail: dict) -> None:
    conn.execute("INSERT INTO matrix_log (at_utc, operator, preset, result, via, detail) VALUES (?, ?, ?, ?, ?, ?)",
                 (clean_text(at, 40), clean_text(operator, 80) or "unknown", clean_text(preset, 20), clean_text(result, 20), clean_text(via, 20), json.dumps(detail, sort_keys=True)))


def recent(conn: sqlite3.Connection, limit: int) -> list:
    rows = conn.execute("SELECT at_utc, operator, preset, result, via, detail FROM matrix_log ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [{"at": row[0], "operator": row[1], "preset": row[2], "result": row[3], "via": row[4], "detail": json.loads(row[5])} for row in rows]


def last_success(conn: sqlite3.Connection) -> Optional[dict]:
    """The newest press that really changed the routing and was confirmed (result done). A failure, an offline matrix and an already-in-place press are not successes."""
    row = conn.execute("SELECT at_utc, operator, preset FROM matrix_log WHERE result = 'done' ORDER BY id DESC LIMIT 1").fetchone()
    return None if row is None else {"at": row[0], "operator": row[1], "preset": row[2]}


def prune(conn: sqlite3.Connection, keep: int = LOG_KEEP_ROWS) -> int:
    return conn.execute("DELETE FROM matrix_log WHERE id NOT IN (SELECT id FROM matrix_log ORDER BY id DESC LIMIT ?)", (int(keep),)).rowcount
