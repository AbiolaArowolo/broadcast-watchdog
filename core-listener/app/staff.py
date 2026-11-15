# File:         staff.py
# Purpose:      The pure rules of the staff section: accounts (at most 10), shifts (several people at once, only Start and Finish), failures (red alarms grouped), who may assign
#               and finish what, the row log both nodes copy, and the staff sign-in cookie.
# Inputs:       Rows (plain dicts), the picture replayed from them, an actor (the super user or one staff person), and a timezone-aware time handed in by the caller.
# Outputs:      New rows to append, views for the pages, yes/no answers, and cookie tokens. Never a changed row: rows are only ever added.
# Dependencies: Python standard library only. No network, no clock of its own, no database: main.py feeds it and acts on what it says (same shape as control.py).
"""Why the staff section is built this way (plain words).

Everything a person does is ONE ROW added to a log. Nothing is ever updated or deleted. The "picture" (who exists, who is on shift, who holds which failure) is worked out
by replaying all the rows in time order. Two nodes that only ever add rows can only differ by rows one of them is missing, so keeping them alike is just "copy the rows the
other lacks": safe to repeat, in any order, with no conflict to settle. Editing the same record on both nodes at once is where two-node designs go wrong; this avoids it.

His rules (2026-11-01) that this file pins:
  - up to 10 people, never the super user; only the super user adds, resets and disables them;
  - a shift is ONLY Start and Finish and can take 48 hours: no timer, no sign-out, no expired cookie ends one (a shift over 48 hours is merely marked "long");
  - several people can be on shift together, and a failure can be assigned to several of them;
  - only the super user assigns, only to people on shift (the one exception since 2026-11-08: the system's automatic assignment of a critical alert, below); a person marks Done only a failure assigned to them;
  - the display names of people on shift are public; usernames, hashes and assignments are not.
His rules (2026-11-04) for "I am on it": a person on shift says they have seen an open failure. It is recorded for every person who presses, once each, with the time; it never assigns
the failure and never lets the person mark it Done; the super user does not press it (he has Assign and Done). It exists so the weekly report can say WHO responded and HOW LONG after the red.
His rule (2026-11-08) for critical alerts: the system gives a new failure to EVERYONE on shift by itself (by="system", the same row his Assign makes), and a failure nobody holds goes to whoever
starts a shift next while it is under 24 hours old (auto_assign_row, waiting_failures). It never overwrites an assignment he made, and his Assign, Unassign and Done still work as before.
public_alerts is what the dashboard strip shows every visitor (display names only). the unpublished design notes part 16.

# LEARN: functional core, imperative shell. Every rule is a function of made-up inputs, so it is tested in milliseconds without a server, a clock or a database.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional

MAX_ACTIVE_STAFF = 10
SESSION_SECONDS = 48 * 3600                # a staff sign-in lasts a long shift; his own lasts one hour
MIN_SESSION_SECONDS, MAX_SESSION_SECONDS = 60, 96 * 3600
LONG_SHIFT_SECONDS = 48 * 3600             # a shift older than this is marked, never ended
FAILURE_GAP_SECONDS = 600                  # reds of one PLAYOUT closer than this are one failure (0 makes every red e-mail its own item); different playouts are never joined
OPEN_SHOWN_SECONDS = 7 * 24 * 3600
DONE_SHOWN_SECONDS = 24 * 3600
LIST_CAP = 100
MAX_NOTE_CHARS = 200
MAX_ASSIGNEES = 10
MAX_ROW_DATA_CHARS = 2000
FUTURE_ALLOWED = timedelta(days=1)
ACTIVE_FAILURE_SECONDS = 24 * 3600         # an open failure this young is still an ALARM: it is handed to a late starter, shown on the dashboard strip and sounds the Staff page siren
AUTO = "system"                            # the `by` of an assignment made by the system itself; a reserved name, so no person can ever be called this
MAX_WAITING = 20                           # at most this many failures are handed out by one shift start
ALERTS_SHOWN = 6                           # the dashboard strip lists at most this many open failures and counts the rest

# A node drops any row whose kind is not in this list (row_problem), so a NEW kind must be deployed on the standby VPS first, then Core (the unpublished design notes part 15).
KINDS = ("person_added", "person_password", "person_disabled", "person_deleted", "shift_started", "shift_finished",
         "failure_opened", "failure_extended", "failure_assigned", "failure_unassigned", "failure_done", "failure_acknowledged")

_USERNAME = re.compile(r"[a-z0-9][a-z0-9.-]{1,22}[a-z0-9]")
_RESERVED = frozenset({"admin", "administrator", "super", "superuser", "root", "system", "watchdog"})
_HASH = re.compile(r"scrypt:[0-9]{1,7}:[0-9]{1,2}:[0-9]{1,2}:[A-Za-z0-9_-]{8,64}:[A-Za-z0-9_-]{40,64}")
_ROW_ID = re.compile(r"[0-9]{8}T[0-9]{6}\.[0-9]{6}-([A-Z]{1,8})-[0-9a-f]{6,16}")
_NODE = re.compile(r"[A-Z]{1,8}")
_FID = re.compile(r"[0-9A-Za-z._-]{1,80}")
_STUDIO = re.compile(r"[0-9A-Za-z._-]{1,40}")
_HEX = re.compile(r"[0-9a-f]+")
_TAG = re.compile(r"[0-9a-f]{16}")
_MAC = re.compile(r"[0-9a-f]{64}")
_DIGITS = re.compile(r"[0-9]{1,12}")
_VERSION = "s1"


class StaffError(ValueError):
    """The request is not allowed or not valid. The message is plain words that are safe to show the person."""


class Actor(NamedTuple):
    role: str          # "super" (the one account that switches access ON and OFF) or "staff"
    name: str          # the username for staff, "" for the super user


SUPER = Actor("super", "")


# ---- Names and text -----------------------------------------------------------------------------------------------------------------------------
def username_problem(name) -> Optional[str]:
    """None when this is an acceptable username (3 to 24 lowercase letters, digits, dots or dashes, not starting or ending with a dot or dash), else why not."""
    if not isinstance(name, str) or not _USERNAME.fullmatch(name):
        return "A username is 3 to 24 lowercase letters, digits, dots or dashes, starting and ending with a letter or digit."
    if name in _RESERVED:
        return "That username is reserved."
    return None


def display_problem(text) -> Optional[str]:
    """None when this is an acceptable display name (what everyone on the dashboard sees), else why not."""
    if not isinstance(text, str) or not 1 <= len(text) <= 30:
        return "A display name is 1 to 30 characters."
    if text != text.strip() or "  " in text or not text[0].isalnum() or any(not (c.isalnum() or c in " .'-") for c in text):
        return "A display name uses letters, digits, spaces, dots, apostrophes and dashes only, with no spaces at either end."
    return None


MIN_STAFF_PASSWORD_CHARS, MAX_STAFF_PASSWORD_CHARS = 4, 128


def staff_password_problem(password) -> Optional[str]:
    """None when this is an acceptable STAFF password, else one plain sentence that never contains it. His rule (2026-11-02): staff passwords are SIMPLE on purpose: letters and
    digits, even a 4-digit number, and no strength rule is enforced (no mixed characters, no ban on repeats or on the person's own name). Only what could not work or could do harm is
    refused: nothing, fewer than 4 characters, more than 128 (the slow hash would be abused), a control character, or only spaces. What keeps a short password from being guessed is
    the slow hash, the staff lockout, and now that staff sign in only on the office PCs' own door. His own password keeps its own rule (control_login.py)."""
    if not isinstance(password, str) or not password:
        return "enter a password"
    if len(password) < MIN_STAFF_PASSWORD_CHARS:
        return f"the password must be at least {MIN_STAFF_PASSWORD_CHARS} characters long"
    if len(password) > MAX_STAFF_PASSWORD_CHARS:
        return f"the password must be at most {MAX_STAFF_PASSWORD_CHARS} characters long"
    if any(ord(c) < 32 or ord(c) == 127 for c in password):
        return "the password cannot contain a line break, a tab or another control character"
    if not password.strip():
        return "the password cannot be only spaces"
    return None


def hash_problem(value) -> Optional[str]:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        return "That is not a password hash made by the sign-in module."
    return None


def _clean(value, low: int, high: int) -> bool:
    return isinstance(value, str) and low <= len(value) <= high and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def pw_tag(stored_hash: str) -> str:
    """A short fingerprint of a stored password hash. A session carries it, so a password reset ends that person's sessions at once, and it reveals nothing about the hash."""
    return hashlib.sha256(("clarix-staff-tag|" + str(stored_hash)).encode("utf-8")).hexdigest()[:16]


# ---- Rows ---------------------------------------------------------------------------------------------------------------------------------------
def _utc(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc)


def _aware(moment) -> bool:
    return isinstance(moment, datetime) and moment.tzinfo is not None


def make_row(kind: str, person: str, data: dict, at: datetime, node: str, nonce: str) -> dict:
    """One row. The id starts with the time (so ids sort in time order), then the node that wrote it and a short random part (so two nodes never write the same id)."""
    when = _utc(at)
    return {"id": f"{when:%Y%m%dT%H%M%S.%f}-{node}-{nonce}", "at": when.isoformat(), "node": node, "kind": kind, "person": person, "data": data}


def _data_problem(kind: str, person, data: dict) -> Optional[str]:
    """The shape of a row's data for its kind: exactly these keys, these types, these sizes."""
    def keys(*wanted):
        return set(data) == set(wanted)

    def names(value):
        return isinstance(value, list) and 1 <= len(value) <= MAX_ASSIGNEES and all(isinstance(x, str) and not username_problem(x) for x in value)

    def by_ok(value):
        return value == "super" or (isinstance(value, str) and bool(_USERNAME.fullmatch(value)))

    if kind == "person_added":
        ok = keys("display", "hash") and not username_problem(person) and not display_problem(data["display"]) and not hash_problem(data["hash"])
    elif kind == "person_password":
        ok = keys("hash") and bool(_USERNAME.fullmatch(person)) and not hash_problem(data["hash"])
    elif kind in ("person_disabled", "person_deleted"):
        ok = keys() and bool(_USERNAME.fullmatch(person))
    elif kind == "shift_started":
        ok = keys() and bool(_USERNAME.fullmatch(person))
    elif kind == "shift_finished":
        ok = keys("by") and bool(_USERNAME.fullmatch(person)) and by_ok(data["by"])
    elif kind in ("failure_opened", "failure_extended"):
        ok = (keys("fid", "studio", "cause", "display") and person == "" and bool(_FID.fullmatch(str(data["fid"]))) and isinstance(data["studio"], str) and bool(_STUDIO.fullmatch(data["studio"]))
              and _clean(data["cause"], 1, 80) and _clean(data["display"], 1, 80))
    elif kind == "failure_assigned":
        ok = keys("fid", "people", "by") and person == "" and bool(_FID.fullmatch(str(data["fid"]))) and names(data["people"]) and by_ok(data["by"])
    elif kind == "failure_unassigned":
        ok = keys("fid", "username") and person == "" and bool(_FID.fullmatch(str(data["fid"]))) and isinstance(data["username"], str) and not username_problem(data["username"])
    elif kind == "failure_done":
        ok = keys("fid", "note", "by") and person == "" and bool(_FID.fullmatch(str(data["fid"]))) and _clean(data["note"], 0, MAX_NOTE_CHARS) and by_ok(data["by"])
    elif kind == "failure_acknowledged":                           # `by` is a staff username, never "super" (a reserved name, so username_problem refuses it)
        ok = (keys("fid", "by") and person == "" and isinstance(data["fid"], str) and bool(_FID.fullmatch(data["fid"])) and isinstance(data["by"], str)
              and not username_problem(data["by"]))
    else:
        ok = False
    return None if ok else f"the data of a {kind} row is not in the expected shape"


def row_problem(row, now: Optional[datetime] = None) -> Optional[str]:
    """None when a row (made here or sent by the other node) is exactly the known shape, else why not. Everything that comes from outside passes through this first."""
    if not isinstance(row, dict) or set(row) != {"id", "at", "node", "kind", "person", "data"}:
        return "a row has exactly the keys id, at, node, kind, person, data"
    if row["kind"] not in KINDS:
        return "unknown kind of row"
    if not isinstance(row["node"], str) or not _NODE.fullmatch(row["node"]):
        return "bad node name"
    if not isinstance(row["person"], str) or not isinstance(row["data"], dict):
        return "bad person or data"
    if not isinstance(row["id"], str) or not isinstance(row["at"], str):
        return "bad id or time"
    try:
        when = datetime.fromisoformat(row["at"])
    except ValueError:
        return "bad time"
    if not _aware(when):
        return "the time must carry its zone"
    matched = _ROW_ID.fullmatch(row["id"])
    if not matched or matched.group(1) != row["node"] or not row["id"].startswith(f"{_utc(when):%Y%m%dT%H%M%S.%f}-"):
        return "the id does not match the time and node"
    if now is not None and _aware(now) and _utc(when) > _utc(now) + FUTURE_ALLOWED:
        return "the row is dated in the future"
    if len(json.dumps(row["data"], ensure_ascii=False, sort_keys=True)) > MAX_ROW_DATA_CHARS:
        return "the data is too large"
    return _data_problem(row["kind"], row["person"], row["data"])


# ---- The picture, replayed from the rows ---------------------------------------------------------------------------------------------------------
def _accounts(ordered: list) -> tuple:
    """(people, former, since): who exists after every add and delete row, in time order. A name can be deleted and made again, so this is a walk through time and not a lookup.
    `former` holds the display name of everyone who was ever deleted (so history can still say who did what). `since` maps a name to the id of the row that made its CURRENT
    account, but ONLY for an account that replaced a deleted one: rows older than that belong to the old account and must not touch the new one. For a name that was never deleted
    there is no such line, so a row the other node dated a moment before the account's own row (clocks differ a little) still lands, exactly as before."""
    people, former, since = {}, {}, {}
    for row in ordered:
        kind = row.get("kind")
        if kind not in ("person_added", "person_deleted"):
            continue
        try:
            who, data, at, row_id = row["person"], row["data"], row["at"], row["id"]
            if kind == "person_added":
                if who not in people:
                    people[who] = {"display": data["display"], "hash": data["hash"], "tag": pw_tag(data["hash"]), "active": True, "added_at": at}
                    if who in former:
                        since[who] = row_id
            elif who in people:
                former[who] = people.pop(who)["display"]
                since.pop(who, None)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return people, former, since


def replay(rows) -> dict:
    """{"people", "shifts", "failures", "former"} worked out from every row. Order and repeats do not matter: rows are applied by id (which starts with the time), and a person or a
    failure is created before anything that refers to it, so a row that arrives late still lands. A row that makes no sense (a finish with no start, an assignment to nothing)
    is skipped. There is no clock here on purpose: nothing about time can end a shift."""
    unique = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            unique.setdefault(row["id"], row)
    ordered = [unique[key] for key in sorted(unique)]
    people, former, since = _accounts(ordered)
    shifts, failures = {}, {}
    creators = [r for r in ordered if r.get("kind") == "failure_opened"]
    others = [r for r in ordered if r.get("kind") not in ("person_added", "failure_opened")]
    for row in creators + others:
        try:
            if row["kind"] in ("person_password", "person_disabled", "shift_started") and row["id"] < since.get(row["person"], ""):
                continue                                           # it belongs to an account of that name that has since been deleted
            _apply(row, people, shifts, failures)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return {"people": people, "shifts": shifts, "failures": failures, "former": former}


def _apply(row: dict, people: dict, shifts: dict, failures: dict) -> None:
    kind, who, data, at = row["kind"], row["person"], row["data"], row["at"]
    if kind == "person_password":
        if who in people:
            people[who]["hash"], people[who]["tag"] = data["hash"], pw_tag(data["hash"])
    elif kind == "person_disabled":
        if who in people:
            people[who]["active"] = False
            shifts.pop(who, None)
    elif kind == "person_deleted":                                 # the account itself is handled in _accounts; here is what it leaves behind
        shifts.pop(who, None)
        for failure in failures.values():
            if failure["done"] is None and who in failure["people"]:
                failure["people"].remove(who)
    elif kind == "shift_started":
        if who in people and people[who]["active"] and who not in shifts:
            shifts[who] = at
    elif kind == "shift_finished":
        shifts.pop(who, None)
    elif kind == "failure_opened":
        if data["fid"] not in failures:
            failures[data["fid"]] = {"fid": data["fid"], "studio": data["studio"], "cause": data["cause"], "latest_cause": data["cause"], "display": data["display"],
                                     "first_at": at, "opened_at": at, "last_at": at, "alarms": 1, "people": [], "acknowledged": [], "done": None}
    elif kind == "failure_extended":
        failure = failures.get(data["fid"])
        if failure is not None and failure["done"] is None:
            failure["alarms"] += 1
            failure["latest_cause"] = data["cause"]
            failure["first_at"], failure["last_at"] = min(failure["first_at"], at), max(failure["last_at"], at)
    elif kind == "failure_assigned":
        failure = failures.get(data["fid"])
        if failure is not None and failure["done"] is None:
            for name in data["people"]:
                if name in people and name not in failure["people"]:
                    failure["people"].append(name)
    elif kind == "failure_unassigned":
        failure = failures.get(data["fid"])
        if failure is not None and data["username"] in failure["people"]:
            failure["people"].remove(data["username"])
    elif kind == "failure_done":
        failure = failures.get(data["fid"])
        if failure is not None and failure["done"] is None:
            failure["done"] = {"by": data["by"], "at": at, "note": data["note"]}
    elif kind == "failure_acknowledged":
        # Kept even if the failure is already Done in this picture: the press was made on the other node a moment before it heard of Done, and WHEN a person responded is a fact. Rows are
        # applied in time order, so a person's earliest press is the one that lands and a second row from the same person (both nodes pressed) changes nothing.
        failure = failures.get(data["fid"])
        if failure is not None and all(seen["by"] != data["by"] for seen in failure["acknowledged"]):
            failure["acknowledged"].append({"by": data["by"], "at": at})


# ---- Doing something: the rules for each action --------------------------------------------------------------------------------------------------
def _require_super(actor) -> None:
    if actor != SUPER:
        raise StaffError("Only the super user can do that.")


def _require_staff(state: dict, actor) -> str:
    """The username of an actor who is an active staff person, else StaffError."""
    if not isinstance(actor, Actor) or actor.role != "staff" or not isinstance(actor.name, str):
        raise StaffError("Sign in as a staff member to do that.")
    person = state["people"].get(actor.name)
    if person is None or not person["active"]:
        raise StaffError("That account is not active.")
    return actor.name


def _text(value, label: str, low: int, high: int) -> str:
    if not _clean(value, low, high):
        raise StaffError(f"{label} is not valid.")
    return value


def _active_person(state: dict, username) -> dict:
    if not isinstance(username, str) or username not in state["people"] or not state["people"][username]["active"]:
        raise StaffError("There is no active person with that username.")
    return state["people"][username]


def _failure(state: dict, fid, open_only: bool = True) -> dict:
    failure = state["failures"].get(fid) if isinstance(fid, str) else None
    if failure is None:
        raise StaffError("There is no such failure.")
    if open_only and failure["done"] is not None:
        raise StaffError("That failure is already finished.")
    return failure


def plan(state: dict, actor, action, args, now: datetime, node: str, nonce: str) -> dict:
    """The ONE row an action adds, or StaffError (nothing is added). `state` is replay()'s picture; `actor` is SUPER or Actor("staff", username)."""
    if not isinstance(args, dict) or not isinstance(action, str) or not isinstance(actor, Actor):
        raise StaffError("That request is not understood.")
    make = lambda kind, who, data: make_row(kind, who, data, now, node, nonce)       # noqa: E731 -- one-line helper
    if action == "add_person":
        _require_super(actor)
        username, display, stored = args.get("username"), args.get("display"), args.get("password_hash")
        for problem in (username_problem(username), display_problem(display), hash_problem(stored)):
            if problem:
                raise StaffError(problem)
        if username in state["people"]:
            raise StaffError("That username is already taken.")
        if sum(1 for p in state["people"].values() if p["active"]) >= MAX_ACTIVE_STAFF:
            raise StaffError(f"There are already {MAX_ACTIVE_STAFF} active staff. Disable someone first.")
        return make("person_added", username, {"display": display, "hash": stored})
    if action == "reset_password":
        _require_super(actor)
        username, stored = args.get("username"), args.get("password_hash")
        _active_person(state, username)
        if hash_problem(stored):
            raise StaffError(hash_problem(stored))
        return make("person_password", username, {"hash": stored})
    if action == "change_password":
        name = _require_staff(state, actor)
        if hash_problem(args.get("password_hash")):
            raise StaffError(hash_problem(args.get("password_hash")))
        return make("person_password", name, {"hash": args["password_hash"]})
    if action == "disable_person":
        _require_super(actor)
        username = args.get("username")
        _active_person(state, username)
        return make("person_disabled", username, {})
    if action == "delete_person":                           # his request (2026-11-02): a disabled name stayed taken for ever; a delete frees the name and the slot, history stays in the log
        _require_super(actor)
        username = args.get("username")
        if not isinstance(username, str) or username not in state["people"]:
            raise StaffError("There is no person with that username.")
        return make("person_deleted", username, {})
    if action == "start_shift":
        if actor == SUPER:                                  # his absolute power (2026-11-02): he can start a shift for an active person, so a broken tailnet never stops him assigning failures
            name = args.get("username")
            _active_person(state, name)
            if name in state["shifts"]:
                raise StaffError("That person is already on shift.")
            return make("shift_started", name, {})          # the same row a person's own start makes
        name = _require_staff(state, actor)
        if args.get("username") not in (None, name):
            raise StaffError("You can only start your own shift.")
        if name in state["shifts"]:
            raise StaffError("You are already on shift.")
        return make("shift_started", name, {})
    if action == "finish_shift":
        target = args.get("username")
        if actor == SUPER:
            by = "super"
        else:
            by = _require_staff(state, actor)
            target = by if target is None else target
            if target != by:
                raise StaffError("You can only finish your own shift.")
        if not isinstance(target, str) or target not in state["shifts"]:
            raise StaffError("That person is not on shift.")
        return make("shift_finished", target, {"by": by})
    if action == "assign":
        _require_super(actor)
        failure = _failure(state, args.get("fid"))
        people = args.get("people")
        if not isinstance(people, list) or not 1 <= len(people) <= MAX_ASSIGNEES:
            raise StaffError(f"Choose one to {MAX_ASSIGNEES} people.")
        for name in people:
            _active_person(state, name)
            if name not in state["shifts"]:
                raise StaffError("Only people who are on shift can be assigned.")
        return make("failure_assigned", "", {"fid": failure["fid"], "people": list(dict.fromkeys(people)), "by": "super"})
    if action == "unassign":
        _require_super(actor)
        failure = _failure(state, args.get("fid"))
        username = args.get("username")
        if not isinstance(username, str) or username not in failure["people"]:
            raise StaffError("That person does not hold this failure.")
        return make("failure_unassigned", "", {"fid": failure["fid"], "username": username})
    if action == "done":
        failure = _failure(state, args.get("fid"))
        raw = args.get("note")
        note = "" if raw is None else (raw.strip() if isinstance(raw, str) else None)
        if note is None or not _clean(note, 0, MAX_NOTE_CHARS):
            raise StaffError(f"A note is at most {MAX_NOTE_CHARS} characters on one line.")
        if actor == SUPER:
            by = "super"
        else:
            by = _require_staff(state, actor)
            if by not in failure["people"]:
                raise StaffError("Only a person this failure is assigned to can finish it.")
        return make("failure_done", "", {"fid": failure["fid"], "note": note, "by": by})
    if action == "acknowledge":                                  # "I am on it": it says "seen", not "owned" (it assigns nothing and does not allow Done)
        if actor == SUPER:
            raise StaffError("Only a staff member says they are on it. You have Assign and Done.")
        name = _require_staff(state, actor)
        if name not in state["shifts"]:
            raise StaffError("Start your shift first, then say you are on it.")
        failure = _failure(state, args.get("fid"))
        if any(seen["by"] == name for seen in failure["acknowledged"]):
            raise StaffError("You already said you are on this one.")
        return make("failure_acknowledged", "", {"fid": failure["fid"], "by": name})
    raise StaffError("That action is not known.")


def failure_row(state: dict, studio, cause, display, when: datetime, node: str, nonce: str, gap: float = FAILURE_GAP_SECONDS) -> dict:
    """The row that records one red alarm: it joins the open failure of the same key when this alarm is within `gap` seconds of that failure's last alarm, otherwise it opens a new one.
    The key (the argument and row field are still called `studio`, so rows keep the shape both nodes already agree on) is the PLAYOUT's id, e.g. MAIN-USA: since 2026-11-01 a failure
    is counted per playout, even for two playouts on one PC.
    A failure someone marked Done is never joined: an alarm after Done is a new failure."""
    if not isinstance(studio, str) or not _STUDIO.fullmatch(studio):
        raise StaffError("bad studio name")
    if not _clean(cause, 1, 80) or not _clean(display, 1, 80) or not _aware(when):
        raise StaffError("bad failure details")
    openings = [f for f in state["failures"].values() if f["studio"] == studio and f["done"] is None]
    latest = max(openings, key=lambda f: f["last_at"]) if openings else None
    if latest is not None and abs((_utc(when) - datetime.fromisoformat(latest["last_at"])).total_seconds()) <= gap:
        return make_row("failure_extended", "", {"fid": latest["fid"], "studio": studio, "cause": cause, "display": display}, when, node, nonce)
    fid = f"{_utc(when):%Y%m%dT%H%M%S}-{studio}"
    return make_row("failure_opened", "", {"fid": fid, "studio": studio, "cause": cause, "display": display}, when, node, nonce)


# ---- Automatic assignment (asked 2026-11-08) ----------------------------------------------------------------------------------------------------
# His words: "whenever there is a critical alert, automatically assign it to anyone on shift". It is the same `failure_assigned` row the super user's Assign makes (so Done, "I am on it",
# the panel and the Monday report need no change), marked by=AUTO so the history can tell the system from the super user. His rule that only HE assigns still holds for everything else.
def _recent(first_at: str, now: datetime) -> bool:
    return (_utc(now) - datetime.fromisoformat(first_at)).total_seconds() <= ACTIVE_FAILURE_SECONDS


def on_shift_now(state: dict, now: datetime) -> list:
    """The usernames on shift, in the order they started, leaving out a shift nobody ended for over 48 hours: that one is marked 'long' because it was probably forgotten, and giving
    an alarm to someone who went home two days ago would only hide that nobody is watching."""
    return [name for name, since in _on_shift(state) if not _is_long(since, now)]


def nobody_on_shift(state: dict, now: datetime) -> bool:
    return not on_shift_now(state, now)


def waiting_failures(state: dict, now: datetime) -> list:
    """The ids of the failures that are open, recent and held by NOBODY, oldest first and capped: what a person who has just started a shift is handed. A failure older than a day is
    never handed out by itself (the old open ones from before this rule would otherwise all land on the first person in the morning); he can still assign those by hand."""
    waiting = [f for f in state["failures"].values() if f["done"] is None and not f["people"] and _recent(f["first_at"], now)]
    waiting.sort(key=lambda f: (f["first_at"], f["fid"]))
    return [f["fid"] for f in waiting[:MAX_WAITING]]


def auto_assign_row(state: dict, fid, now: datetime, node: str, nonce: str) -> Optional[dict]:
    """The row that gives failure `fid` to everyone on shift, or None when there is nothing to do: no such failure, already finished, already held by somebody (a choice of the super user's
    is never overwritten), too old, or nobody on shift. One rule for the three moments it is used: a failure opens, another alarm joins it, a person starts a shift."""
    failure = state["failures"].get(fid) if isinstance(fid, str) else None
    if failure is None or failure["done"] is not None or failure["people"] or not _recent(failure["first_at"], now):
        return None
    people = on_shift_now(state, now)
    if not people:
        return None
    return make_row("failure_assigned", "", {"fid": failure["fid"], "people": people, "by": AUTO}, now, node, nonce)


# ---- What the pages show -------------------------------------------------------------------------------------------------------------------------
def _is_long(since: str, now: datetime) -> bool:
    return (_utc(now) - datetime.fromisoformat(since)).total_seconds() > LONG_SHIFT_SECONDS


def _on_shift(state: dict) -> list:
    return sorted(state["shifts"].items(), key=lambda item: (item[1], item[0]))


def public_roster(state: dict) -> list:
    """The display names of everyone on shift, in the order they started. This is the only thing the public dashboard shows."""
    return [state["people"][name]["display"] for name, _ in _on_shift(state)]


def _who(state: dict, name: str) -> str:
    if name == "super":
        return "super"
    person = state["people"].get(name)
    if person:
        return person["display"]
    return state.get("former", {}).get(name, name)             # someone who was deleted is still named in what they did


def _seen(failure: dict) -> list:
    """Who said "I am on it", in the order they pressed: {"by" (a username), "at", "after_s"}. after_s is the seconds from the failure's first red e-mail to the press, or None when the press
    is dated BEFORE the red (the two servers' clocks disagree): that is not measured, it is never shown as zero or as a negative time."""
    opened = datetime.fromisoformat(failure["opened_at"])
    seen = []
    for item in failure["acknowledged"]:
        gap = (datetime.fromisoformat(item["at"]) - opened).total_seconds()
        seen.append({"by": item["by"], "at": item["at"], "after_s": gap if gap >= 0 else None})
    return seen


def _failure_lists(state: dict, now: datetime) -> tuple:
    """(open failures, finished-in-the-last-day failures) as plain dicts. An open one carries its holders (and those who pressed "I am on it") as USERNAMES; the caller turns them into
    what its page shows."""
    current = _utc(now)
    open_items, done_items = [], []
    for failure in state["failures"].values():
        if failure["done"] is None:
            if (current - datetime.fromisoformat(failure["first_at"])).total_seconds() <= OPEN_SHOWN_SECONDS:
                open_items.append(failure)
        elif (current - datetime.fromisoformat(failure["done"]["at"])).total_seconds() <= DONE_SHOWN_SECONDS:
            done_items.append(failure)
    open_items.sort(key=lambda f: f["last_at"], reverse=True)
    done_items.sort(key=lambda f: f["done"]["at"], reverse=True)
    shown_open = [{"fid": f["fid"], "studio": f["studio"], "cause": f["cause"], "latest_cause": f["latest_cause"], "display": f["display"], "first_at": f["first_at"],
                   "last_at": f["last_at"], "alarms": f["alarms"], "people": list(f["people"]), "seen": _seen(f)} for f in open_items[:LIST_CAP]]
    shown_done = [{"fid": f["fid"], "studio": f["studio"], "display": f["display"], "done_by": _who(state, f["done"]["by"]), "done_at": f["done"]["at"], "note": f["done"]["note"]}
                  for f in done_items[:LIST_CAP]]
    return shown_open, shown_done


def staff_view(state: dict, username: str, now: datetime) -> dict:
    """What one signed-in staff person sees: themselves, who is on shift, the open failures (marked when they hold one), and what was finished in the last day."""
    person = state["people"].get(username)
    since = state["shifts"].get(username)
    me = {"username": username, "display": person["display"] if person else username, "on_shift": since is not None, "since": since,
          "long": bool(since) and _is_long(since, now)}
    roster = [{"display": state["people"][name]["display"], "since": since_at, "long": _is_long(since_at, now)} for name, since_at in _on_shift(state)]
    failures, done = _failure_lists(state, now)
    for item in failures:
        item["mine"] = username in item["people"]
        item["active"] = _recent(item["first_at"], now)                           # recent enough to be an ALARM (strip and siren); older open ones are only listed
        item["people"] = [_who(state, name) for name in item["people"]]
        item["seen_by_me"] = any(seen["by"] == username for seen in item["seen"])
        item["seen"] = [{"display": _who(state, seen["by"]), "at": seen["at"], "after_s": seen["after_s"]} for seen in item["seen"]]
    return {"me": me, "roster": roster, "failures": failures, "done": done}


def public_alerts(state: dict, now: datetime) -> dict:
    """What the dashboard strip shows every visitor about the open, recent failures: the playout, how long it has been open, and the DISPLAY NAMES of who holds it and who said they are on
    it. Display names are already public (the 'On shift' chip); no username, no password hash, no note and no cause text is here. Newest first, at most ALERTS_SHOWN, the rest counted."""
    current = _utc(now)
    live = [f for f in state["failures"].values() if f["done"] is None and _recent(f["first_at"], now)]
    live.sort(key=lambda f: f["last_at"], reverse=True)
    items = [{"playout": f["display"], "since": f["first_at"], "age_s": max(0, int((current - datetime.fromisoformat(f["first_at"])).total_seconds())),
              "assigned": [_who(state, name) for name in f["people"]], "on_it": [_who(state, seen["by"]) for seen in f["acknowledged"]]} for f in live[:ALERTS_SHOWN]]
    return {"items": items, "more": max(0, len(live) - ALERTS_SHOWN)}


def panel_view(state: dict, now: datetime) -> dict:
    """What the super user's panel shows: the people (with their state), who is on shift, the open failures with who holds them, and what was finished in the last day."""
    people = []
    for name, info in sorted(state["people"].items()):
        since = state["shifts"].get(name)
        people.append({"username": name, "display": info["display"], "active": info["active"], "on_shift": since is not None, "since": since, "long": bool(since) and _is_long(since, now)})
    roster = [{"username": name, "display": state["people"][name]["display"], "since": since_at, "long": _is_long(since_at, now)} for name, since_at in _on_shift(state)]
    failures, done = _failure_lists(state, now)
    for item in failures:
        item["people"] = [{"username": name, "display": _who(state, name)} for name in item["people"]]
        item["seen"] = [{"username": seen["by"], "display": _who(state, seen["by"]), "at": seen["at"], "after_s": seen["after_s"]} for seen in item["seen"]]
    return {"slots": {"active": sum(1 for p in state["people"].values() if p["active"]), "max": MAX_ACTIVE_STAFF}, "people": people, "roster": roster, "failures": failures, "done": done}


# ---- The staff sign-in cookie --------------------------------------------------------------------------------------------------------------------
def _signing_key(secret: str) -> bytes:
    """Made from the control key under a label of its own, so a staff signature can never be confused with the super user's (his uses another label)."""
    return hmac.new(secret.encode("utf-8"), b"clarix-staff-session-v1", hashlib.sha256).digest()


def _sign(secret: str, expiry: int, hex_name: str, tag: str) -> str:
    return hmac.new(_signing_key(secret), f"{_VERSION}.{expiry}.{hex_name}.{tag}".encode("ascii"), hashlib.sha256).hexdigest()


def issue_session(secret: str, username: str, tag: str, now: datetime, ttl: int = SESSION_SECONDS) -> str:
    """`s1.<expiry>.<name in hex>.<password tag>.<signature>`: letters and digits only, so it is safe in a cookie. It carries the name (so the page can say who you are) and
    when it ends; the signature covers both, and the password tag ties it to the password that was current, so a reset ends it."""
    if not isinstance(secret, str) or not secret.strip() or username_problem(username) or not isinstance(tag, str) or not _TAG.fullmatch(tag) or not _aware(now):
        raise ValueError("a staff session needs a key, a valid username, a password tag and an aware time")
    if not isinstance(ttl, int) or not MIN_SESSION_SECONDS <= ttl <= MAX_SESSION_SECONDS:
        raise ValueError("a staff session lasts between a minute and four days")
    expiry = int(now.timestamp()) + ttl
    hex_name = username.encode("utf-8").hex()
    return f"{_VERSION}.{expiry}.{hex_name}.{tag}.{_sign(secret, expiry, hex_name, tag)}"


def verify_session(secret, token, now, state: dict) -> Optional[str]:
    """The username a valid, unexpired token belongs to, or None. Also None when the account was disabled or its password changed since the token was made. Never raises."""
    try:
        if not isinstance(secret, str) or not secret.strip() or not isinstance(token, str) or not _aware(now):
            return None
        parts = token.split(".")
        if len(parts) != 5 or parts[0] != _VERSION or not _DIGITS.fullmatch(parts[1]) or not _HEX.fullmatch(parts[2]) or len(parts[2]) % 2 or len(parts[2]) > 48:
            return None
        if not _TAG.fullmatch(parts[3]) or not _MAC.fullmatch(parts[4]):
            return None
        if not hmac.compare_digest(parts[4].encode("ascii"), _sign(secret, int(parts[1]), parts[2], parts[3]).encode("ascii")):
            return None
        if int(now.timestamp()) >= int(parts[1]):
            return None
        name = bytes.fromhex(parts[2]).decode("utf-8")
        person = state["people"].get(name)
        if person is None or not person["active"] or not hmac.compare_digest(person["tag"].encode("ascii"), parts[3].encode("ascii")):
            return None
        return name
    except (ValueError, UnicodeError, KeyError, TypeError, AttributeError):
        return None
