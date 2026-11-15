# File:         control.py
# Purpose:      The pure decisions of the control phase: is access ON or OFF, which node is preferred, may THIS node deliver alerts now, who is told, and the two mails.
# Inputs:       The stored state (plain text values), the command being applied, the other node's health, and a timezone-aware time handed in by the caller.
# Outputs:      A new immutable state with its effects, yes/no answers, a delivery route, and Message objects (subject + body).
# Dependencies: Python standard library only. No network, no clock of its own, no database: main.py feeds it and acts on what it says (same shape as standby.py).
"""Why the control phase decides the way it does (plain words).

Two things the super user asked for, and one promise that holds them together:

1. ACCESS: switching it OFF stops users seeing the dashboard and silences alerts to users, while everything underneath (ingest, keys, config) keeps running.
2. PREFERRED NODE: Core (default) or the VPS. The preferred node alerts. The other node watches it and takes over BY ITSELF if it is down.
3. The promise: a choice is a preference, never a mute. No setting leaves both nodes quiet while one of them is up. (OFF is the one deliberate exception.)

The hard part is a command that reaches only one node, so the two disagree about who is preferred. Rule: when they disagree, BOTH alert. A double alert is
annoying; silence is dangerous in a monitoring system.

# LEARN: functional core, imperative shell. This file never touches the network, a clock or a database, so every rule can be tested with made-up inputs in
# milliseconds. main.py is the thin shell that reads the world, asks this file what to do, and does it.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Mapping, NamedTuple, Optional

CORE = "CORE"
VPS = "VPS"
NODES = (CORE, VPS)
_LABELS = {CORE: "Core", VPS: "the VPS"}

OFFLINE = "offline"              # the pending mail kinds
RESTORED = "restored"
_NOTICE_KINDS = (OFFLINE, RESTORED)


def _require_aware(moment, what: str) -> None:
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError(f"{what} must be a timezone-aware datetime")


def _require_node(node) -> str:
    if node not in NODES:
        raise ValueError(f"node must be one of {NODES}")
    return node


@dataclass(frozen=True)
class ControlState:
    """Everything one node remembers about the control phase. Frozen: a change makes a NEW state, so a half-applied change cannot exist."""

    access_on: bool = True
    preferred: str = CORE
    access_changed_at: Optional[datetime] = None
    preferred_changed_at: Optional[datetime] = None
    off_at: Optional[datetime] = None            # when the latest OFF began; kept after ON so the "back" mail can say how long it was off
    pending_notice: Optional[str] = None         # a mail still to be sent: "offline", "restored" or None


class Change(NamedTuple):
    state: ControlState
    effects: tuple               # ("access_off",), ("access_on",), ("preferred_changed",) or () when nothing changed


class PeerView(NamedTuple):
    """What this node knows about the OTHER node."""

    down: bool                           # the other node is believed down (the proven probe rule in standby.CoreWatch)
    preferred: Optional[str] = None      # who the other node says is preferred; None when it does not say (an older version, or it could not be read)


class Route(NamedTuple):
    users: bool            # the normal recipients (e-mail list and shared phone-push topic)
    super_user: bool       # the super user's own e-mail and private phone-push topic


class Message(NamedTuple):
    subject: str
    body: str


class Loaded(NamedTuple):
    state: ControlState
    problems: tuple


# ---- Parsing (strict: a typo must stop, not pick something) ----------------------------------------------------------------------------------------
def parse_preferred(text) -> str:
    value = str(text).strip().upper() if isinstance(text, str) else ""
    if value not in NODES:
        raise ValueError(f"preferred node must be one of {NODES}")
    return value


def parse_access(text) -> bool:
    value = text.strip().lower() if isinstance(text, str) else ""
    if value not in ("on", "off"):
        raise ValueError("access must be 'on' or 'off'")
    return value == "on"


# ---- Who alerts -------------------------------------------------------------------------------------------------------------------------------------
def may_alert(node: str, state: ControlState, peer: PeerView) -> bool:
    """May `node` deliver alerts to people right now?

    - The preferred node always may.
    - The other node may only if the preferred node is down (automatic fallback), or if the two disagree about who is preferred (fail loud).
    - An unknown peer preference is NOT a disagreement: an older peer does not report one, and a disagreement can only come from a command on new code.
    """
    _require_node(node)
    if node == state.preferred:
        return True
    if peer.down:
        return True
    return peer.preferred is not None and peer.preferred != state.preferred


def route_delivery(state: ControlState) -> Route:
    """Who is told about an alert (a studio alarm or a node-health message). OFF silences users and keeps the super user informed."""
    return Route(users=True, super_user=False) if state.access_on else Route(users=False, super_user=True)


# ---- Changing the state -----------------------------------------------------------------------------------------------------------------------------
def set_access(state: ControlState, on: bool, now: datetime, notify: bool = True) -> Change:
    """Switch access ON or OFF. Repeating the same command changes nothing and queues no second mail (that is what makes the offline mail ONE mail).

    `notify` is False for all but one of the nodes the command is sent to: both nodes record the change, but only the one told to notify queues the mail,
    so the support mailbox gets ONE mail, not one from each node."""
    _require_aware(now, "now")
    if bool(on) == state.access_on:
        return Change(state, ())
    if not on:
        return Change(replace(state, access_on=False, access_changed_at=now, off_at=now, pending_notice=OFFLINE if notify else None), ("access_off",))
    # Coming back ON before the "offline" mail went out: users were never told, so telling them it is back would be noise.
    pending = RESTORED if notify and state.pending_notice != OFFLINE else None
    return Change(replace(state, access_on=True, access_changed_at=now, pending_notice=pending), ("access_on",))


def set_preferred(state: ControlState, node: str, now: datetime) -> Change:
    _require_aware(now, "now")
    _require_node(node)
    if node == state.preferred:
        return Change(state, ())
    return Change(replace(state, preferred=node, preferred_changed_at=now), ("preferred_changed",))


def notice_sent(state: ControlState, kind: str) -> ControlState:
    """The mail of this kind went out. Clears it only if it is the one that was pending (a newer one stays queued)."""
    return replace(state, pending_notice=None) if kind in _NOTICE_KINDS and state.pending_notice == kind else state


# ---- Storing (plain text, so a human can read it with sqlite3) --------------------------------------------------------------------------------------
def to_store(state: ControlState) -> dict:
    def moment(value):
        return value.isoformat() if value else ""
    return {
        "control_access": "on" if state.access_on else "off",
        "control_preferred": state.preferred,
        "control_access_at": moment(state.access_changed_at),
        "control_preferred_at": moment(state.preferred_changed_at),
        "control_off_at": moment(state.off_at),
        "control_pending": state.pending_notice or "",
    }


def from_store(rows: Mapping[str, str]) -> Loaded:
    """Reads the stored values. Nothing stored = a fresh node, no problem. A value that is there but unreadable falls back FIELD BY FIELD to ON / CORE /
    nothing pending (a monitoring system that goes silent because its settings are damaged is worse than one that alerts), and each fallback is reported.

    # LEARN: "unknown is its own answer". The fallback is chosen on purpose and always comes with a problem text; it is never allowed to look like a normal read.
    """
    problems: list = []
    state = ControlState()

    raw = rows.get("control_access")
    if raw:
        try:
            state = replace(state, access_on=parse_access(raw))
        except ValueError:
            problems.append("control_access holds a value that cannot be read, so access is treated as ON")

    raw = rows.get("control_preferred")
    if raw:
        try:
            state = replace(state, preferred=parse_preferred(raw))
        except ValueError:
            problems.append("control_preferred holds a value that cannot be read, so Core is treated as preferred")

    for key, field in (("control_access_at", "access_changed_at"), ("control_preferred_at", "preferred_changed_at"), ("control_off_at", "off_at")):
        raw = rows.get(key)
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(raw)
            _require_aware(parsed, key)
            state = replace(state, **{field: parsed})
        except ValueError:
            problems.append(f"{key} holds a time that cannot be read, so it is ignored")

    raw = rows.get("control_pending")
    if raw:
        if raw in _NOTICE_KINDS:
            state = replace(state, pending_notice=raw)
        else:
            problems.append("control_pending holds a value that cannot be read, so no mail is treated as pending")
    return Loaded(state, tuple(problems))


def snapshot(state: ControlState, problems=()) -> dict:
    """What /status and /control show: state and times, never a key."""
    def moment(value):
        return value.isoformat() if value else None
    return {
        "access": "on" if state.access_on else "off",
        "preferred": state.preferred,
        "off_since": moment(state.off_at) if not state.access_on else None,
        "access_changed_at": moment(state.access_changed_at),
        "preferred_changed_at": moment(state.preferred_changed_at),
        "pending_notice": state.pending_notice,
        "problems": list(problems),
    }


# ---- What is said -----------------------------------------------------------------------------------------------------------------------------------
def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s"
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


def _utc_clock(moment: datetime) -> str:
    _require_aware(moment, "time")
    return moment.astimezone(timezone.utc).strftime("%H:%M UTC")


def offline_message(off_at: datetime) -> Message:
    """The ONE mail when access goes OFF (to the support mailbox). Wording agreed in the unpublished design notes."""
    return Message("[Watchdog] Service is OFFLINE",
                   f"The Clarix Broadcast Watchdog service was switched off at {_utc_clock(off_at)}. Users cannot view monitoring, and no alert e-mails or phone push "
                   "messages will be sent until it is switched back on. Contact ClarixTech to restore it.")


def restored_message(off_at: Optional[datetime], on_at: datetime) -> Message:
    """The ONE mail when access is switched back ON. Says honestly that alerts raised while it was off were not sent."""
    after = f" after {_duration((on_at - off_at).total_seconds())}" if off_at is not None else ""
    return Message("[Watchdog] Service is back",
                   f"The service was switched back on at {_utc_clock(on_at)}{after}. Monitoring access and alerts are active again. "
                   "Alerts raised while it was off were not sent.")


class Text(NamedTuple):
    title: str         # the end of an alert subject, one line
    summary: str       # one plain sentence


def peer_down_text(down: str, taker: str, error: str = "") -> Text:
    """Said when the preferred node stopped answering and the other node has taken over alerting (sent as a normal alert e-mail)."""
    _require_node(down), _require_node(taker)
    detail = f" Last error: {' '.join(str(error).split())[:120]}." if error else ""
    return Text(f"{down} watchdog is DOWN", f"{down} has stopped answering. {taker} has taken over alerting.{detail}")


def peer_up_text(back: str, taker: str, gone_seconds: float) -> Text:
    """Said when the preferred node answers again and the other node has stood down."""
    _require_node(back), _require_node(taker)
    return Text(f"{back} watchdog is back", f"{back} is answering again after {_duration(gone_seconds)}. {taker} has stood down.")


def preference_message(node: str, now: datetime) -> Message:
    """Mailed to the super user when the preferred node is changed (every change is announced)."""
    _require_node(node)
    other = CORE if node == VPS else VPS
    name, rest = _LABELS[node], _LABELS[other]
    return Message(f"[Watchdog] Preferred node is now {node}",
                   f"At {_utc_clock(now)} the preferred node was set to {node}. {name[0].upper() + name[1:]} alerts now. "
                   f"{rest[0].upper() + rest[1:]} takes over by itself if {name} goes down.")
