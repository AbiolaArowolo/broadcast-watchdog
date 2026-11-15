# File:         control_login.py
# Purpose:      The super user's login: how a password is stored (a salted scrypt hash), how it is checked, which passwords are too weak, and how wrong tries are locked out.
# Inputs:       A password and a stored hash, a configured name and a given one, an address and a time.
# Outputs:      A stored-hash text, yes/no answers, a reason text, and the seconds an address must still wait.
# Dependencies: Python standard library only. No network, no clock of its own, no database (same shape as control.py and control_access.py).
"""Why the login is built the way it is (plain words).

A person remembers a password; a machine remembers a key. A password is weaker (people pick guessable ones, and an attacker can try over and over), so this file does three things:

1. It never keeps the password. It keeps a SALTED SCRYPT HASH: a one-way scramble that is deliberately slow and memory-hungry, so each guess costs an attacker real time. The salt makes two
   identical passwords look different, so one stolen hash cannot be used to find the password for the other node.
2. It refuses weak passwords (at least 8 characters, not one repeated character, not containing the name).
3. It LOCKS OUT guessing: five wrong tries from one address lock that address for 15 minutes, and 30 wrong tries from anywhere lock the login for everyone for 15 minutes.

The stored text uses only letters, digits, colon, underscore and hyphen. That matters: a dollar sign in a settings value would be read as a variable by docker compose and an equals sign by
other tools, so the hash is written in a form that survives every file it passes through.

# LEARN: slow on purpose. A normal hash (SHA-256) is built to be fast, which is exactly wrong for passwords: an attacker can try billions a second. scrypt is built to be slow and to need
# memory, so the same attacker gets a few per second per core.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
from collections import OrderedDict, deque
from typing import Optional

SCHEME = "scrypt"
DEFAULT_N = 2 ** 14            # 16 MiB of memory per check at r=8: gentle enough for the 256 MB standby, slow enough to hurt a guesser
R, P = 8, 1
MAX_N = 2 ** 17                # a stored hash may not ask for more than this (a damaged or planted value must not be able to exhaust the server)
MAX_R = 16
MIN_N = 2 ** 4
SALT_BYTES = 16
HASH_BYTES = 32
MIN_PASSWORD_CHARS = 8         # the super user's own choice (2026-10-26); the slow hash and the lockout below are what keep a short password from being guessed
MAX_PASSWORD_CHARS = 200       # hashing cost grows with length; nobody needs more
_MAXMEM = 96 * 1024 * 1024


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def hash_password(password: str, *, salt: Optional[bytes] = None, n: int = DEFAULT_N) -> str:
    """`scrypt:<n>:<r>:<p>:<salt>:<hash>` with the salt and hash in url-safe base64 without padding. The password itself is never part of it."""
    if not isinstance(password, str) or not password:
        raise ValueError("a password is needed")
    if not MIN_N <= n <= MAX_N:
        raise ValueError("unusable scrypt cost")
    salt = salt if salt is not None else secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=R, p=P, dklen=HASH_BYTES, maxmem=_MAXMEM)
    return f"{SCHEME}:{n}:{R}:{P}:{_b64(salt)}:{_b64(digest)}"


def verify_password(stored, password) -> bool:
    """True only when `password` is the one `stored` was made from. Everything else, a damaged value, a value asking for absurd cost, a non-text input, is False and never raises."""
    if not isinstance(stored, str) or not isinstance(password, str) or not password or len(password) > MAX_PASSWORD_CHARS * 2:
        return False
    parts = stored.split(":")
    if len(parts) != 6 or parts[0] != SCHEME:
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, expected = _unb64(parts[4]), _unb64(parts[5])
    except (ValueError, binascii.Error):
        return False
    if not (MIN_N <= n <= MAX_N and n & (n - 1) == 0 and 1 <= r <= MAX_R and 1 <= p <= 4) or len(salt) < 8 or len(expected) != HASH_BYTES:
        return False
    try:
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected), maxmem=_MAXMEM)
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


def password_problem(password, username: str = "") -> Optional[str]:
    """None when the password is acceptable, else one plain sentence that never contains the password."""
    if not isinstance(password, str) or not password:
        return "enter a password"
    if len(password) < MIN_PASSWORD_CHARS:
        return f"the password must be at least {MIN_PASSWORD_CHARS} characters long"
    if len(password) > MAX_PASSWORD_CHARS:
        return f"the password must be at most {MAX_PASSWORD_CHARS} characters long"
    if len(set(password)) < 5:
        return "the password is too simple: use a longer phrase with different characters"
    name = str(username or "").split("@")[0].strip().lower()
    if len(name) >= 4 and name in password.lower():
        return "the password must not contain your name"
    return None


def check_login(configured_user, stored_hash, given_user, given_password) -> bool:
    """True only for the configured name AND its password. The password is always checked, even when the name is wrong, so a wrong name and a wrong password take the same time."""
    if not configured_user or not stored_hash:
        return False
    wanted = str(configured_user).strip().lower().encode("utf-8")
    offered = str(given_user or "").strip().lower().encode("utf-8")
    name_ok = hmac.compare_digest(wanted, offered)
    password_ok = verify_password(stored_hash, given_password)
    return bool(name_ok and password_ok)


class LoginThrottle:
    """Wrong-try lockout, per address and for everyone. Times are plain seconds handed in by the caller (a monotonic clock in the listener, made-up numbers in tests)."""

    def __init__(self, per_visitor: int = 5, global_limit: int = 30, window_seconds: float = 900.0, lock_seconds: float = 900.0, max_visitors: int = 1000):
        self._per_visitor, self._global_limit = per_visitor, global_limit
        self._window, self._lock = window_seconds, lock_seconds
        self._max = max_visitors
        self._failures: "OrderedDict[str, deque]" = OrderedDict()
        self._locked_until: dict = {}
        self._all: deque = deque()
        self._global_locked_until = 0.0

    def tracked(self) -> int:
        return len(self._failures)

    def _prune(self, visitor: str, now: float) -> deque:
        history = self._failures.get(visitor)
        if history is None:
            return deque()
        while history and now - history[0] > self._window:
            history.popleft()
        return history

    def locked_for(self, visitor: str, now: float) -> int:
        """Whole seconds this address must still wait (0 = free to try). A global lock applies to everyone."""
        remaining = max(self._global_locked_until - now, self._locked_until.get(visitor, 0.0) - now, 0.0)
        return 0 if remaining <= 0 else max(1, int(-(-remaining // 1)))

    def record_failure(self, visitor: str, now: float) -> None:
        history = self._prune(visitor, now)
        history.append(now)
        self._failures[visitor] = history
        self._failures.move_to_end(visitor)
        while len(self._failures) > self._max:
            dropped, _ = self._failures.popitem(last=False)
            self._locked_until.pop(dropped, None)
        if len(history) >= self._per_visitor:
            self._locked_until[visitor] = now + self._lock
            self._failures[visitor] = deque()                  # the lock replaces the count; after it ends the address starts fresh
        self._all.append(now)
        while self._all and now - self._all[0] > self._window:
            self._all.popleft()
        if len(self._all) >= self._global_limit:
            self._global_locked_until = now + self._lock
            self._all.clear()

    def record_success(self, visitor: str) -> None:
        self._failures.pop(visitor, None)
        self._locked_until.pop(visitor, None)
