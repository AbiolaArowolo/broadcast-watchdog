# File:         test_staff_wiring.py
# Purpose:      Tests of how core-listener/app/main.py USES the staff rules: the shared sign-in, the staff cookie, the staff and panel endpoints, the red-alarm hook that opens failures,
#               the row log table, and the copying of rows between the two nodes.
# Inputs:       None (a throwaway database, a fake mail server, a fake other node, made-up requests). Nothing leaves this machine.
# Outputs:      Printed unittest results; exit code 1 if anything fails.
# Dependencies: core-listener/app/main.py (loaded the way ops/test_control_wiring.py loads it), staff.py, control_access.py, control_login.py.
"""Run: python ops/test_staff_wiring.py

What is proven here, in plain words: a staff person signs in on the same page as the super user and gets a cookie of their own that can never act as his; staff sign-ins have their
own lockout so guessing staff names cannot lock him out; staff pages and sign-in are shut while access is OFF but the nodes can still copy rows to each other; only the super user
adds, resets, disables and assigns; a person finishes only what is assigned to them; a red alarm e-mail opens or extends a failure and a broken staff table can never stop that e-mail;
nothing is recorded while access is OFF; and two nodes holding different rows end up with the same rows by copying what they lack, page by page.
"""
import asyncio
import importlib
import inspect
import json
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))
import test_control_wiring as tcw  # noqa: E402 -- loads the listener the same way the control tests do
import test_staff_logic as tsl  # noqa: E402 -- the row builders of the pure tests

listener, access, login, staff = tcw.listener, tcw.access, tcw.login, importlib.import_module("app.staff")
KEY, WHO, PASSWORD, run, FakeRequest = tcw.KEY, tcw.WHO, tcw.PASSWORD, tcw.run, tcw.FakeRequest
STAFF_PASSWORD = "a long enough pass 1"
DOOR, MAIN_DOOR = 8421, 8420          # the staff door and the main port: every request below arrives through the staff door unless a test says otherwise


def through(request, door):
    """The request as the server sees it when it arrived on this port (uvicorn puts the real local port of the socket in scope["server"]; None for no usable address)."""
    request.scope["server"] = ("127.0.0.1", door) if door is not None else None
    return request
OTHER_PASSWORD = "another long pass 22"


def jreq(payload, cookie=None, host="203.0.113.5", key=None, same_site=True, door=DOOR):
    headers = {"content-type": "application/json"}
    if same_site:
        headers["sec-fetch-site"] = "same-origin"
    if cookie:
        headers["cookie"] = cookie
    if key:
        headers["x-watchdog-control"] = key
    return through(FakeRequest(headers, json.dumps(payload).encode(), host=host), door)


def greq(cookie=None, key=None, host="203.0.113.5", door=DOOR):
    headers = {}
    if cookie:
        headers["cookie"] = cookie
    if key:
        headers["x-watchdog-control"] = key
    return through(FakeRequest(headers, b"", host=host), door)


class StaffWiring(tcw.Wiring):
    def setUp(self):
        super().setUp()
        self.saved_staff = {name: getattr(listener, name) for name in ("_staff_throttle", "_staff_address_throttle", "_peer_request", "_send_endpoint_critical_email", "STAFF_DOOR_PORT")}
        listener.STAFF_DOOR_PORT = DOOR
        listener.SUPER_USER = WHO
        listener.SUPER_PASSWORD_HASH = login.hash_password(PASSWORD, n=16)
        listener._login_throttle = login.LoginThrottle()
        listener._staff_throttle = listener._new_staff_person_throttle()            # the real ones, so every test below runs under the real rules
        listener._staff_address_throttle = listener._new_staff_address_throttle()
        listener._staff_cache["state"] = None
        listener._control_rate._seen.clear()
        self.pushed = []
        listener._peer_request = lambda method, path, payload=None: (None, {})
        self.red_mails = []
        listener._send_endpoint_critical_email = lambda endpoint: self.red_mails.append(endpoint["display_name"])
        self.real_hash = login.hash_password
        login.hash_password = lambda password, **kwargs: self.real_hash(password, n=16)          # the real cost (16 MiB each) is for real passwords, not for 55 tests
        listener._staff_rate._seen.clear()
        listener._STAFF_DUMMY["hash"] = ""

    def tearDown(self):
        login.hash_password = self.real_hash
        for name, value in self.saved_staff.items():
            setattr(listener, name, value)
        super().tearDown()

    # ---- helpers
    def super_cookie(self):
        return f"{listener.CONTROL_COOKIE}={access.issue_session(KEY, datetime.now(timezone.utc), 3600, listener._session_generation())}"

    def panel(self, payload, **kwargs):
        return run(listener.control_panel_staff_act(jreq(payload, cookie=self.super_cookie(), **kwargs)))

    def add(self, username, display=None, password=STAFF_PASSWORD):
        reply = self.panel({"action": "add_person", "username": username, "display": display or username.title(), "password": password})
        self.assertEqual(reply.status_code, 200, reply.content)
        return reply

    def sign_in(self, username="ada", password=STAFF_PASSWORD, host="203.0.113.5", door=DOOR):
        return run(listener.control_session(jreq({"user": username, "password": password}, host=host, same_site=False, door=door)))

    def cookie_of(self, reply):
        return reply.headers["Set-Cookie"].split(";")[0]

    def staff_cookie(self, username="ada", password=STAFF_PASSWORD):
        reply = self.sign_in(username, password)
        self.assertEqual(reply.status_code, 200, reply.content)
        return self.cookie_of(reply)

    def act(self, cookie, payload, **kwargs):
        return run(listener.staff_act(jreq(payload, cookie=cookie, **kwargs)))

    def start_shift(self, username="ada"):
        return self.act(self.staff_cookie(username), {"action": "start_shift"})

    def endpoint(self, studio="SITE-A", cause="Silence > 60s"):
        return {"endpoint_id": studio, "display_name": studio, "source_id": studio, "root_cause": cause}

    def real_playout(self, endpoint_id, cause="Process Missing"):
        """One of the six playouts exactly as the listener defines it (so these tests follow the real list), going red for `cause`."""
        found = [e for e in listener.PLAYOUT_ENDPOINTS if e["endpoint_id"] == endpoint_id]
        self.assertEqual(len(found), 1, endpoint_id)
        return {**found[0], "root_cause": cause}

    def state(self):
        return run(listener._staff_state())

    def failures(self):
        return self.state()["failures"]

    def fake_peer(self, rows):
        calls = []

        def fake(method, path, payload=None):
            calls.append((method, path))
            route, _, query = path.partition("?")
            params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
            if route == "/control/staff/summary":
                return 200, {"count": len(rows), "digest": listener._staff_digest(rows)}
            if route == "/control/staff/rows" and method == "GET":
                after, limit = params.get("after", ""), int(params.get("limit", "40"))
                keyed = sorted(rows, key=lambda r: r["id"] if isinstance(r, dict) and isinstance(r.get("id"), str) else "")
                return 200, {"rows": [r for r in keyed if (r["id"] if isinstance(r, dict) and isinstance(r.get("id"), str) else "") > after][:limit]}
            if route == "/control/staff/rows" and method == "POST":
                rows.extend(payload["rows"])
                return 200, {"added": len(payload["rows"]), "rejected": 0}
            return 404, {}

        listener._peer_request = fake
        return calls


class SignInTests(StaffWiring):
    def test_a_staff_person_signs_in_on_the_same_page_and_gets_their_own_cookie(self):
        self.add("ada")
        reply = self.sign_in()
        self.assertEqual((reply.status_code, reply.content.get("ok"), reply.content.get("next")), (200, True, "/staff"))
        cookie = reply.headers["Set-Cookie"]
        for part in (f"{listener.STAFF_COOKIE}=", "HttpOnly", "SameSite=Strict", "Path=/", f"Max-Age={staff.SESSION_SECONDS}"):
            self.assertIn(part, cookie)
        self.assertNotIn(listener.CONTROL_COOKIE, cookie)
        for secret in (STAFF_PASSWORD, KEY, listener.SUPER_PASSWORD_HASH):
            self.assertNotIn(secret, cookie + json.dumps(reply.content))

    def test_the_super_user_still_signs_in_as_before(self):
        self.add("ada")
        reply = run(listener.control_session(jreq({"user": WHO, "password": PASSWORD}, same_site=False)))
        self.assertEqual(reply.status_code, 200)
        self.assertIn(f"{listener.CONTROL_COOKIE}=", reply.headers["Set-Cookie"])
        self.assertNotIn("next", reply.content)

    def test_the_name_is_not_case_sensitive_and_spaces_around_it_are_ignored(self):
        self.add("ada")
        self.assertEqual(self.sign_in(username=" ADA ").status_code, 200)

    def test_a_wrong_password_and_an_unknown_name_are_refused_in_exactly_the_same_words(self):
        self.add("ada")
        wrong, unknown = self.sign_in(password="not the password 99"), self.sign_in(username="ghost")
        self.assertEqual((wrong.status_code, unknown.status_code), (401, 401))
        self.assertEqual(wrong.content, unknown.content)
        self.assertNotIn("Set-Cookie", wrong.headers or {})

    def test_a_disabled_person_cannot_sign_in_and_says_the_same_words(self):
        self.add("ada")
        self.panel({"action": "disable_person", "username": "ada"})
        disabled, wrong = self.sign_in(), self.sign_in(password="not the password 99", host="198.51.100.9")
        self.assertEqual(disabled.status_code, 401)
        self.assertEqual(disabled.content, wrong.content)

    def test_five_wrong_tries_lock_that_person_and_the_super_user_is_not_locked_with_it(self):
        self.add("ada")
        for _ in range(5):
            self.assertEqual(self.sign_in(password="wrong password number 1").status_code, 401)
        self.assertEqual(self.sign_in().status_code, 429)
        reply = run(listener.control_session(jreq({"user": WHO, "password": PASSWORD}, same_site=False)))
        self.assertEqual(reply.status_code, 200)

    def test_staff_guesses_from_everywhere_cannot_lock_the_super_user_out(self):
        self.add("ada")
        for index in range(30):
            self.sign_in(password="wrong password number 1", host=f"198.51.100.{index}")
        self.assertEqual(self.sign_in(host="203.0.113.77").status_code, 429)        # the guessed-at person is locked, from wherever they try
        reply = run(listener.control_session(jreq({"user": WHO, "password": PASSWORD}, host="203.0.113.78", same_site=False)))
        self.assertEqual(reply.status_code, 200)

    def test_the_super_users_wrong_tries_do_not_lock_staff_either(self):
        self.add("ada")
        for index in range(30):
            run(listener.control_session(jreq({"user": WHO, "password": "wrong password number 1"}, host=f"198.51.100.{index}", same_site=False)))
        self.assertEqual(self.sign_in(host="203.0.113.77").status_code, 200)

    def test_staff_cannot_sign_in_while_access_is_off(self):
        self.add("ada")
        self.switch_off(notify=False)
        reply = self.sign_in()
        self.assertEqual(reply.status_code, 403)
        self.assertNotIn("Set-Cookie", reply.headers or {})

    def test_junk_in_the_name_or_password_never_signs_anyone_in(self):
        self.add("ada")
        for user, password in ((None, None), (5, 5), ([], {}), ("", ""), ("ada", ""), ("ada", None), ("ada", "x" * 1000), ("ADA", STAFF_PASSWORD[:-1])):
            reply = run(listener.control_session(jreq({"user": user, "password": password}, same_site=False)))
            self.assertIn(reply.status_code, (401, 429), (user, password))
            self.assertNotIn("Set-Cookie", reply.headers or {})

    def test_a_node_without_the_control_phase_has_no_staff_door(self):
        self.add("ada")
        listener.CONTROL_KEY = ""
        self.assertEqual(self.sign_in().status_code, 404)


class FrozenClock:
    """`time` as the listener sees it, with monotonic() frozen until a test moves it, so a two-minute lock is checked in no time at all.

    It starts at a ROUND number on purpose (not at the machine's real uptime): "lock time + 120 s, minus now" is exactly 120 only when the sum is exact, and for a clock that starts between 8 and 128
    seconds (a fresh CI runner) about one start in five rounds to 120.00000000000001, which the listener rounds UP to 121. That made this test fail on Linux CI (PR 22) while it passed on a PC.
    # LEARN: floating point cannot hold every decimal exactly; a test that compares an exact number must control the inputs that feed the arithmetic."""

    def __init__(self, real):
        self._real, self.now = real, 1000.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds

    def __getattr__(self, name):
        return getattr(self._real, name)


class LockoutTests(StaffWiring):
    """His report (2026-11-02): a person could not get back in after a password change. A wrong try used to count against the ADDRESS, five of them locked it for fifteen minutes, and the
    two office PCs share one address, so one person's slips (an old password typed from habit) locked every person out, the right password included. Now a lock belongs to the NAME,
    lasts two minutes and says so; only a very busy address is stopped as a whole, and the super user's reset of a password unlocks the person at once."""

    def setUp(self):
        super().setUp()
        self.real_time = listener.time
        self.clock = FrozenClock(self.real_time)
        listener.time = self.clock
        self.add("ada")
        self.add("kemi")

    def tearDown(self):
        listener.time = self.real_time
        super().tearDown()

    def wrong(self, username="ada", times=5, host="203.0.113.5"):
        for _ in range(times):
            self.assertEqual(self.sign_in(username, "wrong password number 1", host=host).status_code, 401)

    def test_wrong_tries_lock_that_person_not_the_whole_office(self):
        self.wrong("ada", 5)
        self.assertEqual(self.sign_in("ada").status_code, 429)                              # even the right password: that is what a lock is
        self.assertEqual(self.sign_in("kemi").status_code, 200)                            # same address, another person: free

    def test_the_lock_is_two_minutes_and_lifts_by_itself(self):
        self.wrong("ada", 5)
        self.clock.advance(119)
        self.assertEqual(self.sign_in("ada").status_code, 429)
        self.clock.advance(2)
        self.assertEqual(self.sign_in("ada").status_code, 200)

    def test_the_refusal_says_how_long_and_says_the_same_for_a_name_that_does_not_exist(self):
        self.wrong("ada", 5)
        self.wrong("ghost", 5)
        known, unknown = self.sign_in("ada"), self.sign_in("ghost")
        for reply in (known, unknown):
            self.assertEqual(reply.status_code, 429)
            self.assertEqual(reply.headers["Retry-After"], "120")
        self.assertEqual(known.content, unknown.content)                                    # a lock must not tell a stranger which names exist
        self.assertEqual(known.content["error"], "Too many wrong tries for this name. Try again in 2 minutes.")
        self.clock.advance(60)
        self.assertEqual(self.sign_in("ada").content["error"], "Too many wrong tries for this name. Try again in 60 seconds.")

    def test_a_correct_sign_in_forgets_the_wrong_tries_before_it(self):
        self.wrong("ada", 4)
        self.assertEqual(self.sign_in("ada").status_code, 200)
        self.wrong("ada", 4)
        self.assertEqual(self.sign_in("ada").status_code, 200)

    def test_wrong_tries_older_than_ten_minutes_no_longer_count(self):
        self.wrong("ada", 4)
        self.clock.advance(601)
        self.wrong("ada", 4)
        self.assertEqual(self.sign_in("ada").status_code, 200)

    def test_a_wrong_current_password_when_changing_counts_for_that_person_only(self):
        cookie = self.staff_cookie("ada")
        for _ in range(5):
            self.assertEqual(self.act(cookie, {"action": "change_password", "current": "wrong password number 1", "new": "brand new pass"}).status_code, 401)
        locked = self.act(cookie, {"action": "change_password", "current": STAFF_PASSWORD, "new": "brand new pass"})
        self.assertEqual(locked.status_code, 429)
        self.assertIn("Try again in", locked.content["error"])
        self.assertEqual(self.sign_in("kemi").status_code, 200)

    def test_one_address_trying_many_names_is_still_stopped(self):
        for index in range(40):
            if index % 20 == 0:
                self.clock.advance(61)                                                      # the control door has its own 30-a-minute pace limit; stay clear of that other guard
            self.assertEqual(self.sign_in(f"person{index:02d}", "wrong password number 1", host="198.51.100.7").status_code, 401)
        self.assertEqual(self.sign_in("ada", host="198.51.100.7").status_code, 429)
        self.assertEqual(self.sign_in("ada", host="198.51.100.8").status_code, 200)

    def test_an_address_is_not_stopped_by_a_handful_of_slips_from_different_people(self):
        for name in ("ada", "kemi", "ada", "kemi", "ada", "kemi", "ada", "kemi"):
            self.assertEqual(self.sign_in(name, "wrong password number 1").status_code, 401)
        self.assertEqual(self.sign_in("kemi").status_code, 200)

    def test_the_super_users_reset_of_a_password_unlocks_that_person_at_once(self):
        self.wrong("ada", 5)
        self.assertEqual(self.sign_in("ada").status_code, 429)
        self.assertEqual(self.panel({"action": "reset_password", "username": "ada", "password": OTHER_PASSWORD}).status_code, 200)
        self.assertEqual(self.sign_in("ada", OTHER_PASSWORD).status_code, 200)

    def test_making_or_deleting_an_account_clears_a_lock_left_behind_by_the_name(self):
        self.wrong("newbie", 5)
        self.add("newbie")
        self.assertEqual(self.sign_in("newbie").status_code, 200)
        self.wrong("kemi", 5)
        self.assertEqual(self.panel({"action": "delete_person", "username": "kemi"}).status_code, 200)
        self.add("kemi")
        self.assertEqual(self.sign_in("kemi").status_code, 200)

    def test_the_super_users_own_lock_is_unchanged_and_apart(self):
        for _ in range(5):
            run(listener.control_session(jreq({"user": WHO, "password": "wrong password number 1"}, same_site=False)))
        self.assertEqual(run(listener.control_session(jreq({"user": WHO, "password": PASSWORD}, same_site=False))).status_code, 429)
        self.assertEqual(self.sign_in("ada").status_code, 200)


class DeleteWiringTests(StaffWiring):
    """His request (2026-11-02): the super user can delete an account from the panel, and it is gone for real: no cookie, no sign-in, the name free to use again."""

    def test_the_panel_deletes_a_person_and_everything_of_theirs_stops_at_once(self):
        self.add("ada")
        cookie = self.staff_cookie("ada")
        self.assertEqual(self.act(cookie, {"action": "start_shift"}).status_code, 200)
        reply = self.panel({"action": "delete_person", "username": "ada"})
        self.assertEqual(reply.status_code, 200)
        self.assertEqual([p["username"] for p in reply.content["people"]], [])
        self.assertEqual(reply.content["roster"], [])
        self.assertEqual(self.act(cookie, {"action": "finish_shift"}).status_code, 401)           # the cookie is dead
        self.assertEqual(self.sign_in().status_code, 401)                                           # and so is the password, in the same words as any wrong one
        self.assertEqual(listener._staff_public(), {"on_shift": []})

    def test_the_name_can_be_used_again_and_the_new_account_starts_clean(self):
        self.add("ada", "Old Ada")
        old_cookie = self.staff_cookie("ada")
        self.panel({"action": "delete_person", "username": "ada"})
        self.add("ada", "New Ada", password=OTHER_PASSWORD)
        self.assertEqual(self.sign_in("ada", OTHER_PASSWORD).status_code, 200)
        self.assertEqual(self.sign_in("ada", STAFF_PASSWORD).status_code, 401)                      # the old account's password is gone
        self.assertEqual(self.act(old_cookie, {"action": "start_shift"}).status_code, 401)
        self.assertEqual(self.state()["people"]["ada"]["display"], "New Ada")

    def test_a_disabled_person_can_be_deleted_and_their_name_freed(self):
        self.add("ada")
        self.panel({"action": "disable_person", "username": "ada"})
        self.assertEqual(self.panel({"action": "delete_person", "username": "ada"}).status_code, 200)
        self.add("ada")

    def test_deleting_someone_who_is_not_there_is_refused_plainly_and_adds_nothing(self):
        for username in ("ghost", "", None, 7):
            reply = self.panel({"action": "delete_person", "username": username})
            self.assertEqual(reply.status_code, 400, username)
            self.assertIn("error", reply.content)
        self.assertEqual(run(listener._staff_summary())["count"], 0)

    def test_only_the_super_user_can_and_staff_have_no_such_button(self):
        self.add("ada")
        self.add("kemi")
        cookie = self.staff_cookie("ada")
        self.assertEqual(self.act(cookie, {"action": "delete_person", "username": "kemi"}).status_code, 400)
        self.assertEqual(run(listener.control_panel_staff_act(jreq({"action": "delete_person", "username": "kemi"}, cookie=cookie))).status_code, 401)
        self.assertEqual(run(listener.control_panel_staff_act(jreq({"action": "delete_person", "username": "kemi"}, cookie=self.super_cookie(), same_site=False))).status_code, 403)
        self.assertIn("kemi", self.state()["people"])
        self.assertIn("delete_person", listener.STAFF_PANEL_ACTIONS)

    def test_a_delete_travels_to_the_other_node_like_every_other_row(self):
        rows = []
        calls = self.fake_peer(rows)
        self.add("ada")
        self.panel({"action": "delete_person", "username": "ada"})
        self.assertEqual([row["kind"] for row in rows], ["person_added", "person_deleted"])
        self.assertIn(("POST", "/control/staff/rows"), calls)

    def test_the_super_user_can_still_delete_while_access_is_off_like_every_other_panel_action(self):
        self.add("ada")
        self.switch_off(notify=False)
        self.assertEqual(self.panel({"action": "delete_person", "username": "ada"}).status_code, 200)
        self.assertNotIn("ada", self.state()["people"])


class TwoKindsOfRefusalTests(StaffWiring):
    """His report (2026-11-02): "change of password didn't work". The staff page tells a signed-out session from a refused password change by the SERVER'S WORDS, because both are a 401. These tests pin
    the words: only a request with no valid session says exactly "sign in first"; a signed-in person's wrong current password says something else, and nothing is changed by it."""

    def test_only_a_missing_session_says_sign_in_first(self):
        self.add("ada")
        anonymous = self.act(None, {"action": "start_shift"})
        self.assertEqual((anonymous.status_code, anonymous.content["error"]), (401, "sign in first"))
        stale = self.act("watchdog_staff=not.a.real.cookie", {"action": "start_shift"})
        self.assertEqual((stale.status_code, stale.content["error"]), (401, "sign in first"))
        self.assertEqual(run(listener.staff_state(greq())).content["error"], "sign in first")

    def test_a_wrong_current_password_is_a_401_that_does_not_say_sign_in_first_and_changes_nothing(self):
        self.add("ada")
        cookie = self.staff_cookie("ada")
        refused = self.act(cookie, {"action": "change_password", "current": "not my password 99", "new": "a brand new one"})
        self.assertEqual(refused.status_code, 401)
        self.assertEqual(refused.content["error"], "Your current password was not accepted.")
        self.assertNotEqual(refused.content["error"], "sign in first")
        self.assertEqual(self.sign_in("ada").status_code, 200)                                      # the old password still works
        self.assertEqual(self.sign_in("ada", "a brand new one").status_code, 401)                   # the new one was never saved
        self.assertEqual(run(listener.staff_state(greq(cookie=cookie))).status_code, 200)           # and the cookie of the person who made the slip is still good

    def test_a_good_change_works_end_to_end_and_the_old_password_stops(self):
        self.add("ada")
        cookie = self.staff_cookie("ada")
        done = self.act(cookie, {"action": "change_password", "current": STAFF_PASSWORD, "new": "a brand new one"})
        self.assertEqual(done.status_code, 200)
        self.assertIn(f"{listener.STAFF_COOKIE}=", done.headers["Set-Cookie"])
        new_cookie = self.cookie_of(done)
        self.assertEqual(run(listener.staff_state(greq(cookie=new_cookie))).status_code, 200)       # signed in on with the new cookie at once
        self.assertEqual(run(listener.staff_state(greq(cookie=cookie))).status_code, 401)           # the old cookie ended with the old password
        self.assertEqual(self.sign_in("ada", "a brand new one").status_code, 200)
        self.assertEqual(self.sign_in("ada", STAFF_PASSWORD).status_code, 401)

    def test_a_too_short_new_password_is_a_400_with_words_and_changes_nothing(self):
        self.add("ada")
        cookie = self.staff_cookie("ada")
        refused = self.act(cookie, {"action": "change_password", "current": STAFF_PASSWORD, "new": "abc"})
        self.assertEqual(refused.status_code, 400)
        self.assertIn("error", refused.content)
        self.assertEqual(self.sign_in("ada").status_code, 200)


class AppSignInLinkTests(StaffWiring):
    """His request (2026-11-02): staff should be able to sign in from the app. Staff sign-in stays on the office PCs' staff address (his certificate rule), so the app offers a Staff sign in
    link to that address next to its own Sign in, and its sign-in page carries it too."""
    PAGE = "<nav>" + "<!--staff-link-->" + "</nav>"
    DEFAULT = "https://staff.corp.example/staff"

    def test_a_visitor_who_is_not_signed_in_is_offered_the_staff_address(self):
        shown = listener._with_super_link(self.PAGE, greq())
        self.assertIn('<a class="staff-link" href="/control/login">Sign in</a>', shown)
        self.assertIn(f'<a class="staff-link" href="{self.DEFAULT}">Staff sign in</a>', shown)
        self.assertLess(shown.index("/control/login"), shown.index(self.DEFAULT))

    def test_nobody_who_is_already_signed_in_is_offered_it(self):
        for shown in (listener._with_super_link(self.PAGE, greq(cookie=self.super_cookie()), None), listener._with_super_link(self.PAGE, greq(), "Ada")):
            self.assertNotIn("Staff sign in", shown)
            self.assertNotIn(self.DEFAULT, shown)

    def test_nothing_is_offered_where_the_control_phase_is_off(self):
        listener.CONTROL_KEY = ""
        shown = listener._with_super_link(self.PAGE, greq())
        self.assertNotIn("Staff sign in", shown)
        self.assertNotIn("staff.corp.example", shown)

    def test_the_address_comes_from_the_operator_and_only_a_plain_https_address_is_ever_used(self):
        self.assertEqual(listener._staff_url_from({}), self.DEFAULT)
        self.assertEqual(listener._staff_url_from({"WATCHDOG_STAFF_URL": "https://staff.example.org/staff"}), "https://staff.example.org/staff")
        for bad in ("javascript:alert(1)", "http://staff.example.org/staff", "https://a b/staff", 'https://x"onmouseover="y', "https://x/<script>", "", "   ", "//staff.example.org", "https://"):
            self.assertEqual(listener._staff_url_from({"WATCHDOG_STAFF_URL": bad}), self.DEFAULT, bad)

    def test_the_sign_in_page_carries_the_same_address_and_the_server_made_it_not_the_page(self):
        page = listener._login_page_html()
        self.assertIn(f'<a href="{self.DEFAULT}">', page)
        self.assertNotIn("__STAFF", page)
        self.assertNotIn("https://", access.login_page_password())                                    # the template itself names no address


class CookieIsNotSuperTests(StaffWiring):
    def setUp(self):
        super().setUp()
        self.add("ada")
        self.cookie = self.staff_cookie()

    def test_the_staff_cookie_is_not_a_super_session(self):
        self.assertFalse(listener._control_caller_is_super(greq(cookie=self.cookie)))
        token = self.cookie.split("=", 1)[1]
        now = datetime.now(timezone.utc)
        self.assertFalse(access.verify_session(KEY, token, now, listener._session_generation()))
        forged = f"{listener.CONTROL_COOKIE}={token}"
        self.assertFalse(listener._control_caller_is_super(greq(cookie=forged)))

    def test_every_super_door_refuses_the_staff_cookie(self):
        for call in (lambda: listener.control_get(greq(cookie=self.cookie)), lambda: listener.control_panel_state(greq(cookie=self.cookie)),
                     lambda: listener.control_panel_staff(greq(cookie=self.cookie)),
                     lambda: listener.control_post(jreq({"access": "off"}, cookie=self.cookie)),
                     lambda: listener.control_panel_act(jreq({"action": "off"}, cookie=self.cookie)),
                     lambda: listener.control_panel_staff_act(jreq({"action": "add_person", "username": "bob", "display": "Bob", "password": STAFF_PASSWORD}, cookie=self.cookie))):
            self.assertEqual(run(call()).status_code, 401)
        self.assertTrue(listener._access_on())
        self.assertNotIn("bob", self.state()["people"])

    def test_the_super_cookie_is_not_a_staff_session(self):
        reply = run(listener.staff_state(greq(cookie=self.super_cookie())))
        self.assertEqual(reply.status_code, 401)

    def test_a_cookie_ends_when_the_person_is_disabled_or_the_password_is_reset(self):
        self.assertEqual(run(listener.staff_state(greq(cookie=self.cookie))).status_code, 200)
        self.panel({"action": "reset_password", "username": "ada", "password": OTHER_PASSWORD})
        self.assertEqual(run(listener.staff_state(greq(cookie=self.cookie))).status_code, 401)
        fresh = self.staff_cookie(password=OTHER_PASSWORD)
        self.assertEqual(run(listener.staff_state(greq(cookie=fresh))).status_code, 200)
        self.panel({"action": "disable_person", "username": "ada"})
        self.assertEqual(run(listener.staff_state(greq(cookie=fresh))).status_code, 401)

    def test_a_cookie_signed_with_another_key_is_refused(self):
        token = staff.issue_session("another-control-key-0123456789abcdef", "ada", self.state()["people"]["ada"]["tag"], datetime.now(timezone.utc))
        self.assertEqual(run(listener.staff_state(greq(cookie=f"{listener.STAFF_COOKIE}={token}"))).status_code, 401)

    def test_a_staff_post_needs_a_json_body_from_this_site_itself(self):
        for kwargs in ({"same_site": False},):
            reply = self.act(self.cookie, {"action": "start_shift"}, **kwargs)
            self.assertEqual(reply.status_code, 403)
        self.assertEqual(self.state()["shifts"], {})

    def test_logout_clears_the_staff_cookie(self):
        reply = run(listener.staff_logout(jreq({}, cookie=self.cookie)))
        self.assertIn("Max-Age=0", reply.headers["Set-Cookie"])
        self.assertIn(listener.STAFF_COOKIE, reply.headers["Set-Cookie"])


class StaffDoorTests(StaffWiring):
    """His rule (2026-11-02): staff may sign in, and use the staff pages, only through the staff door, a second port that only the two office PCs can reach (over Tailscale).
    The super user is never bound. Every other test in this file arrives through the door, as the staff do."""

    def setUp(self):
        super().setUp()
        self.add("ada")

    def test_a_staff_sign_in_through_the_main_port_is_refused_before_any_password_is_looked_at(self):
        right, wrong = self.sign_in(door=MAIN_DOOR), self.sign_in(password="not the password 99", door=MAIN_DOOR)
        self.assertEqual((right.status_code, wrong.status_code), (403, 403))
        self.assertIn("office PC", right.content["error"])
        self.assertEqual(right.content, wrong.content)                                  # right or wrong password: the same words, so nothing is learned about the password
        self.assertNotIn("Set-Cookie", right.headers or {})

    def test_those_refusals_are_not_password_guesses_so_they_never_lock_the_staff_door(self):
        for _ in range(12):
            self.assertEqual(self.sign_in(password="wrong password number 1", door=MAIN_DOOR).status_code, 403)
        self.assertEqual(self.sign_in().status_code, 200)

    def test_a_staff_cookie_is_worth_nothing_on_the_main_port(self):
        cookie = self.staff_cookie()
        self.assertEqual(run(listener.staff_state(greq(cookie=cookie))).status_code, 200)                       # through the door it works
        self.assertEqual(run(listener.staff_state(greq(cookie=cookie, door=MAIN_DOOR))).status_code, 401)
        self.assertEqual(self.act(cookie, {"action": "start_shift"}, door=MAIN_DOOR).status_code, 401)
        self.assertEqual(self.state()["shifts"], {})
        page = run(listener.staff_page(greq(cookie=cookie, door=MAIN_DOOR)))
        self.assertEqual((page.status_code, page.headers["Location"]), (302, "/control/login"))
        self.assertEqual(run(listener.staff_page(greq(cookie=cookie))).status_code, 200)

    def test_the_dashboard_toolbar_shows_no_staff_name_off_the_door(self):
        cookie = self.staff_cookie()
        self.assertEqual(run(listener._staff_display_of(greq(cookie=cookie))), "Ada")
        self.assertIsNone(run(listener._staff_display_of(greq(cookie=cookie, door=MAIN_DOOR))))

    def test_the_super_user_is_never_bound(self):
        for door in (MAIN_DOOR, DOOR):
            reply = run(listener.control_session(jreq({"user": WHO, "password": PASSWORD}, same_site=False, door=door)))
            self.assertEqual(reply.status_code, 200, door)
        reply = run(listener.control_panel_staff_act(jreq({"action": "disable_person", "username": "ada"}, cookie=self.super_cookie(), door=MAIN_DOOR)))
        self.assertEqual(reply.status_code, 200)

    def test_a_node_with_no_staff_door_accepts_no_staff_sign_in_at_all(self):
        listener.STAFF_DOOR_PORT = 0                                                    # the standby on the VPS
        for door in (MAIN_DOOR, DOOR, None):
            self.assertEqual(self.sign_in(door=door).status_code, 403, door)
        self.assertEqual(run(listener.control_session(jreq({"user": WHO, "password": PASSWORD}, same_site=False, door=DOOR))).status_code, 200)

    def test_a_request_without_a_usable_local_address_is_not_the_door(self):
        self.assertEqual(self.sign_in(door=None).status_code, 403)
        request = jreq({"user": "ada", "password": STAFF_PASSWORD}, same_site=False)
        del request.scope["server"]
        self.assertEqual(run(listener.control_session(request)).status_code, 403)
        request = jreq({"user": "ada", "password": STAFF_PASSWORD}, same_site=False)
        request.scope["server"] = ("127.0.0.1", str(DOOR))                              # the right number as text is not the door either
        self.assertEqual(run(listener.control_session(request)).status_code, 403)

    def test_no_header_a_client_writes_can_make_a_request_look_like_the_door(self):
        fake = {"host": f"127.0.0.1:{DOOR}", "x-forwarded-port": str(DOOR), "x-forwarded-host": f"127.0.0.1:{DOOR}", "forwarded": f"host=127.0.0.1:{DOOR}", "x-staff-door": "1"}
        request = jreq({"user": "ada", "password": STAFF_PASSWORD}, same_site=False, door=MAIN_DOOR)
        request.headers.update(fake)
        self.assertEqual(run(listener.control_session(request)).status_code, 403)

    def test_the_listener_believes_in_a_door_only_when_the_launcher_says_it_opened_one(self):
        source = Path(listener.__file__).read_text(encoding="utf-8")
        self.assertIn("WATCHDOG_STAFF_DOOR_BOUND", source)
        self.assertNotIn("WATCHDOG_STAFF_DOOR_PORT", source)                           # the operator's request alone changes nothing here
        for good, expected in (("8421", 8421), (" 8421 ", 8421), ("1024", 1024), ("65535", 65535)):
            self.assertEqual(listener._door_port_from({"WATCHDOG_STAFF_DOOR_BOUND": good}), expected, good)
        for bad in (None, "", "abc", "0", "80", "1023", "65536", "-1", "8421.5", "８４２１"):          # the last is 8421 written in full-width digits
            self.assertEqual(listener._door_port_from({} if bad is None else {"WATCHDOG_STAFF_DOOR_BOUND": bad}), 0, bad)


class SuperStartsAShiftTests(StaffWiring):
    """He has absolute power (2026-11-02): from his panel he can also START a shift for someone, so a broken tailnet never stops him assigning failures."""

    def setUp(self):
        super().setUp()
        self.add("ada")

    def test_he_starts_a_shift_for_a_person_and_everyone_sees_them_on_it(self):
        reply = self.panel({"action": "start_shift", "username": "ada"})
        self.assertEqual(reply.status_code, 200, reply.content)
        self.assertIn("ada", self.state()["shifts"])
        self.assertEqual(listener.staff.public_roster(self.state()), ["Ada"])

    def test_he_cannot_start_it_for_a_stranger_a_disabled_person_or_someone_already_on_shift(self):
        for username in ("ghost", None, 5):
            self.assertEqual(self.panel({"action": "start_shift", "username": username}).status_code, 400, username)
        self.assertEqual(self.panel({"action": "start_shift", "username": "ada"}).status_code, 200)
        self.assertEqual(self.panel({"action": "start_shift", "username": "ada"}).status_code, 400)
        self.panel({"action": "disable_person", "username": "ada"})
        self.assertEqual(self.panel({"action": "start_shift", "username": "ada"}).status_code, 400)

    def test_a_staff_person_still_cannot_start_a_shift_for_somebody_else(self):
        self.add("bob")
        cookie = self.staff_cookie("ada")
        reply = self.act(cookie, {"action": "start_shift", "username": "bob"})          # the staff door drops any name that is sent: it can only ever act for the signed-in person
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(list(self.state()["shifts"]), ["ada"])                         # their own shift started, Bob's did not

    def test_the_panel_door_lists_start_shift_among_its_actions(self):
        self.assertIn("start_shift", listener.STAFF_PANEL_ACTIONS)


class GateTests(unittest.TestCase):
    def test_staff_pages_and_data_are_shut_while_access_is_off_and_the_row_copy_stays_open(self):
        for path in ("/staff", "/staff/state", "/staff/act", "/staff/logout"):
            for method in ("GET", "POST"):
                self.assertNotEqual(access.gate_request(method, path, False, False), access.ALLOW, (method, path))
        for path in ("/control/staff/summary", "/control/staff/rows"):
            self.assertEqual(access.gate_request("GET", path, False, False), access.ALLOW)
            self.assertEqual(access.gate_request("POST", path, False, False), access.ALLOW)


class PanelActionTests(StaffWiring):
    def test_only_the_super_user_adds_resets_and_disables_and_nothing_stores_a_password(self):
        reply = self.add("ada", "Ada O.")
        self.assertEqual([p["username"] for p in reply.content["people"]], ["ada"])
        row = [r for r in run(listener._staff_rows()) if r["kind"] == "person_added"][0]
        self.assertTrue(row["data"]["hash"].startswith("scrypt:"))
        self.assertNotIn(STAFF_PASSWORD, json.dumps(run(listener._staff_rows())))
        self.assertNotIn("scrypt", json.dumps(reply.content))

    def test_the_super_users_panel_view_lists_people_roster_failures_and_slots(self):
        self.add("ada")
        self.start_shift()
        run(listener._send_red_alarm(self.endpoint()))
        view = run(listener.control_panel_staff(greq(cookie=self.super_cookie()))).content
        self.assertEqual(view["slots"], {"active": 1, "max": 10})
        self.assertEqual([p["username"] for p in view["roster"]], ["ada"])
        self.assertEqual(len(view["failures"]), 1)
        self.assertNotIn("scrypt", json.dumps(view))

    def test_a_password_that_could_not_work_is_refused_and_nothing_is_added(self):
        for password in ("123", "ab", "", "    ", "x" * 129, "ab\ncd"):
            reply = self.panel({"action": "add_person", "username": "ada", "display": "Ada", "password": password})
            self.assertEqual(reply.status_code, 400, repr(password))
            self.assertNotIn(password or "\0", json.dumps(reply.content))
        self.assertEqual(self.state()["people"], {})

    def test_simple_passwords_are_accepted_a_four_digit_number_included(self):
        # His rule (2026-11-02): no strength rule for staff. A repeated character and the person's own name are allowed too.
        for username, password in (("ada", "1234"), ("bob", "bobbob"), ("chi", "aaaaaaaa"), ("dan", "abcd")):
            self.assertEqual(self.panel({"action": "add_person", "username": username + "x", "display": username.title(), "password": password}).status_code, 200, password)
        self.assertEqual(self.sign_in("adax", "1234").status_code, 200)                       # and the person really signs in with it
        reset = self.panel({"action": "reset_password", "username": "adax", "password": "7777"})
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(self.sign_in("adax", "7777", host="198.51.100.40").status_code, 200)
        self.assertEqual(self.panel({"action": "reset_password", "username": "adax", "password": "777"}).status_code, 400)

    def test_the_eleventh_person_is_refused_with_a_plain_message(self):
        for index in range(10):
            self.add(f"person{index}")
        reply = self.panel({"action": "add_person", "username": "person10", "display": "P", "password": STAFF_PASSWORD})
        self.assertEqual(reply.status_code, 400)
        self.assertIn("10", reply.content["error"])

    def test_unknown_actions_and_junk_bodies_change_nothing(self):
        self.add("ada")
        for payload in ({"action": "off"}, {"action": "nonsense"}, {}, {"action": None}, {"action": ["add_person"]}):
            self.assertEqual(self.panel(payload).status_code, 400, payload)
        self.assertEqual(self.panel({"action": "add_person", "username": "bob"}).status_code, 400)
        self.assertEqual(list(self.state()["people"]), ["ada"])

    def test_a_panel_press_needs_this_sites_own_page(self):
        reply = self.panel({"action": "add_person", "username": "ada", "display": "Ada", "password": STAFF_PASSWORD}, same_site=False)
        self.assertEqual(reply.status_code, 403)
        self.assertEqual(self.state()["people"], {})

    def test_assign_only_to_people_on_shift_and_several_at_once(self):
        self.add("ada")
        self.add("kemi")
        self.add("zara")
        self.start_shift("ada")
        self.start_shift("kemi")
        run(listener._send_red_alarm(self.endpoint()))
        fid = next(iter(self.failures()))
        off_shift = self.panel({"action": "assign", "fid": fid, "people": ["zara"]})
        self.assertEqual(off_shift.status_code, 400)
        both = self.panel({"action": "assign", "fid": fid, "people": ["ada", "kemi"]})
        self.assertEqual(both.status_code, 200)
        self.assertEqual(self.failures()[fid]["people"], ["ada", "kemi"])

    def test_the_super_user_can_finish_a_persons_shift_and_mark_a_failure_done(self):
        self.add("ada")
        self.start_shift()
        run(listener._send_red_alarm(self.endpoint()))
        fid = next(iter(self.failures()))
        self.assertEqual(self.panel({"action": "finish_shift", "username": "ada"}).status_code, 200)
        self.assertEqual(self.state()["shifts"], {})
        self.assertEqual(self.panel({"action": "done", "fid": fid, "note": "seen on the picture"}).status_code, 200)
        self.assertEqual(self.failures()[fid]["done"]["by"], "super")


class StaffActionTests(StaffWiring):
    def setUp(self):
        super().setUp()
        self.add("ada")
        self.add("kemi")
        self.cookie = self.staff_cookie()

    def test_start_and_finish_a_shift(self):
        reply = self.act(self.cookie, {"action": "start_shift"})
        self.assertEqual(reply.status_code, 200)
        self.assertTrue(reply.content["me"]["on_shift"])
        self.assertEqual(self.act(self.cookie, {"action": "start_shift"}).status_code, 400)
        self.assertFalse(self.act(self.cookie, {"action": "finish_shift"}).content["me"]["on_shift"])
        self.assertEqual(self.act(self.cookie, {"action": "finish_shift"}).status_code, 400)

    def test_a_person_finishes_only_what_is_assigned_to_them(self):
        self.start_shift("ada")
        self.start_shift("kemi")
        run(listener._send_red_alarm(self.endpoint()))
        fid = next(iter(self.failures()))
        # Since 2026-11-08 the system gives a new failure to everyone on shift (the unpublished design notes part 16), so here he takes it back from Ada and leaves it with Kemi alone.
        self.assertEqual(self.failures()[fid]["people"], ["ada", "kemi"])
        self.panel({"action": "unassign", "fid": fid, "username": "ada"})
        self.assertEqual(self.failures()[fid]["people"], ["kemi"])
        self.assertEqual(self.act(self.cookie, {"action": "done", "fid": fid, "note": "mine?"}).status_code, 400)
        theirs = self.staff_cookie("kemi")
        self.assertEqual(self.act(theirs, {"action": "done", "fid": fid, "note": "restarted"}).status_code, 200)
        self.assertEqual(self.failures()[fid]["done"]["by"], "kemi")

    def test_staff_cannot_use_the_super_users_actions_through_their_own_door(self):
        for payload in ({"action": "add_person", "username": "bob", "display": "Bob", "password": STAFF_PASSWORD}, {"action": "assign", "fid": "x", "people": ["ada"]},
                        {"action": "disable_person", "username": "kemi"}, {"action": "delete_person", "username": "kemi"}, {"action": "reset_password", "username": "kemi", "password": OTHER_PASSWORD}, {"action": "off"}):
            self.assertEqual(self.act(self.cookie, payload).status_code, 400, payload)
        self.assertEqual(sorted(self.state()["people"]), ["ada", "kemi"])
        self.assertTrue(self.state()["people"]["kemi"]["active"])

    def test_a_staff_person_cannot_do_a_super_action_even_with_every_argument_perfectly_valid(self):
        self.start_shift("ada")
        self.start_shift("kemi")
        run(listener._send_red_alarm(self.endpoint()))
        fid = next(iter(self.failures()))
        before = list(self.failures()[fid]["people"])             # the system gave it to both on shift (2026-11-08); what matters is that no staff press changes who holds it
        self.assertEqual(before, ["ada", "kemi"])
        good_hash = tsl.HASH
        for payload in ({"action": "add_person", "username": "bob", "display": "Bob", "password_hash": good_hash, "password": STAFF_PASSWORD},
                        {"action": "assign", "fid": fid, "people": ["ada", "kemi"]}, {"action": "reset_password", "username": "kemi", "password_hash": good_hash, "password": OTHER_PASSWORD},
                        {"action": "disable_person", "username": "kemi"}, {"action": "delete_person", "username": "kemi"}, {"action": "unassign", "fid": fid, "username": "ada"}):
            self.assertEqual(self.act(self.cookie, payload).status_code, 400, payload["action"])
        state = self.state()
        self.assertEqual(sorted(state["people"]), ["ada", "kemi"])
        self.assertTrue(state["people"]["kemi"]["active"])
        self.assertEqual(self.failures()[fid]["people"], before)

    def test_the_staff_view_is_theirs_and_carries_no_hash(self):
        self.start_shift("ada")
        view = run(listener.staff_state(greq(cookie=self.cookie))).content
        self.assertEqual(view["me"]["username"], "ada")
        self.assertEqual([r["display"] for r in view["roster"]], ["Ada"])
        self.assertNotIn("scrypt", json.dumps(view))

    def test_changing_your_own_password_needs_the_current_one_and_keeps_you_signed_in(self):
        wrong = self.act(self.cookie, {"action": "change_password", "current": "not my password 99", "new": OTHER_PASSWORD})
        self.assertEqual(wrong.status_code, 401)
        weak = self.act(self.cookie, {"action": "change_password", "current": STAFF_PASSWORD, "new": "abc"})                  # too short to work (fewer than 4)
        self.assertEqual(weak.status_code, 400)
        self.assertEqual(self.act(self.cookie, {"action": "change_password", "current": "not my password 99", "new": "abc"}).status_code, 401)   # a wrong current password is still told first
        good = self.act(self.cookie, {"action": "change_password", "current": STAFF_PASSWORD, "new": OTHER_PASSWORD})
        self.assertEqual(good.status_code, 200)
        self.assertEqual(run(listener.staff_state(greq(cookie=self.cookie))).status_code, 401)          # the old cookie ended
        new_cookie = self.cookie_of(good)
        self.assertEqual(run(listener.staff_state(greq(cookie=new_cookie))).status_code, 200)         # the new one is issued at once
        self.assertEqual(self.sign_in(password=OTHER_PASSWORD).status_code, 200)
        self.assertEqual(self.sign_in(password=STAFF_PASSWORD, host="198.51.100.9").status_code, 401)

    def test_a_simple_new_password_is_fine_when_changing_your_own(self):
        # His rule (2026-11-02): staff passwords are simple on purpose, a 4-digit number included.
        good = self.act(self.cookie, {"action": "change_password", "current": STAFF_PASSWORD, "new": "5678"})
        self.assertEqual(good.status_code, 200)
        self.assertEqual(self.sign_in(password="5678").status_code, 200)

    def test_wrong_current_passwords_count_toward_the_staff_lockout(self):
        for _ in range(5):
            self.act(self.cookie, {"action": "change_password", "current": "not my password 99", "new": OTHER_PASSWORD})
        self.assertEqual(self.act(self.cookie, {"action": "change_password", "current": STAFF_PASSWORD, "new": OTHER_PASSWORD}).status_code, 429)

    def test_junk_in_a_staff_post_never_raises(self):
        for payload in ({}, {"action": None}, {"action": 5}, {"action": "done"}, {"action": "done", "fid": [], "note": {}}, {"action": "change_password"}, {"action": "change_password", "current": 5, "new": 6}):
            self.assertIn(self.act(self.cookie, payload).status_code, (400, 401), payload)


class AcknowledgeWiringTests(StaffWiring):
    """The "I am on it" button through the real staff door (his spec of 2026-11-04, the unpublished design notes part 15)."""

    def setUp(self):
        super().setUp()
        self.add("ada")
        self.add("kemi")
        self.ada, self.kemi = self.staff_cookie("ada"), self.staff_cookie("kemi")

    def failure_while_on_shift(self, *names):
        for name in names:
            self.act(self.ada if name == "ada" else self.kemi, {"action": "start_shift"})
        run(listener._send_red_alarm(self.endpoint()))
        return next(iter(self.failures()))

    def kinds(self):
        return [row["kind"] for row in run(listener._staff_rows())]

    def test_a_person_on_shift_presses_and_the_failure_says_who_is_on_it(self):
        fid = self.failure_while_on_shift("ada")
        reply = self.act(self.ada, {"action": "acknowledge", "fid": fid})
        self.assertEqual(reply.status_code, 200)
        item = reply.content["failures"][0]
        self.assertEqual([seen["display"] for seen in item["seen"]], ["Ada"])
        self.assertTrue(item["seen_by_me"])
        self.assertEqual([seen["by"] for seen in self.failures()[fid]["acknowledged"]], ["ada"])
        self.assertEqual(self.kinds().count("failure_acknowledged"), 1)

    def test_a_person_who_is_not_on_shift_is_told_to_start_the_shift_and_nothing_is_stored(self):
        fid = self.failure_while_on_shift("kemi")
        reply = self.act(self.ada, {"action": "acknowledge", "fid": fid})
        self.assertEqual(reply.status_code, 400)
        self.assertIn("Start your shift", reply.content["error"])
        self.assertNotIn("failure_acknowledged", self.kinds())

    def test_pressing_twice_is_refused_in_words_and_stores_one_row(self):
        fid = self.failure_while_on_shift("ada")
        self.act(self.ada, {"action": "acknowledge", "fid": fid})
        again = self.act(self.ada, {"action": "acknowledge", "fid": fid})
        self.assertEqual(again.status_code, 400)
        self.assertIn("already", again.content["error"])
        self.assertEqual(self.kinds().count("failure_acknowledged"), 1)

    def test_two_people_pressing_are_both_kept(self):
        fid = self.failure_while_on_shift("ada", "kemi")
        self.act(self.ada, {"action": "acknowledge", "fid": fid})
        self.act(self.kemi, {"action": "acknowledge", "fid": fid})
        self.assertEqual([seen["by"] for seen in self.failures()[fid]["acknowledged"]], ["ada", "kemi"])

    def test_nobody_can_press_for_somebody_else_the_name_comes_from_the_cookie_only(self):
        fid = self.failure_while_on_shift("ada", "kemi")
        self.act(self.ada, {"action": "acknowledge", "fid": fid, "by": "kemi", "username": "kemi", "person": "kemi"})
        self.assertEqual([seen["by"] for seen in self.failures()[fid]["acknowledged"]], ["ada"])

    def test_no_session_no_press(self):
        fid = self.failure_while_on_shift("ada")
        self.assertEqual(self.act(None, {"action": "acknowledge", "fid": fid}).status_code, 401)
        self.assertEqual(self.act(self.super_cookie(), {"action": "acknowledge", "fid": fid}).status_code, 401)
        self.assertNotIn("failure_acknowledged", self.kinds())

    def test_the_super_users_panel_door_has_no_such_action(self):
        fid = self.failure_while_on_shift("ada")
        self.assertNotIn("acknowledge", listener.STAFF_PANEL_ACTIONS)
        self.assertEqual(self.panel({"action": "acknowledge", "fid": fid, "username": "ada"}).status_code, 400)
        self.assertNotIn("failure_acknowledged", self.kinds())

    def test_junk_in_the_button_never_raises_and_stores_nothing(self):
        self.failure_while_on_shift("ada")
        for fid in (None, [], {}, 5, "", "nope", "x" * 500):
            payload = {"action": "acknowledge", "fid": fid}
            self.assertEqual(self.act(self.ada, payload).status_code, 400, payload)
        self.assertEqual(self.act(self.ada, {"action": "acknowledge"}).status_code, 400)
        self.assertNotIn("failure_acknowledged", self.kinds())

    def test_the_press_is_pushed_to_the_other_node_like_every_other_row(self):
        fid = self.failure_while_on_shift("ada")
        sent = []
        listener._peer_request = lambda method, path, payload=None: (sent.append((method, path, payload)) or (200, {"added": 1, "rejected": 0}))
        self.act(self.ada, {"action": "acknowledge", "fid": fid})
        pushed = [row["kind"] for _, path, payload in sent if path == "/control/staff/rows" for row in payload["rows"]]
        self.assertEqual(pushed, ["failure_acknowledged"])

    def test_a_press_made_on_the_other_node_is_accepted_when_it_arrives(self):
        fid = self.failure_while_on_shift("ada")
        row = staff.make_row("failure_acknowledged", "", {"fid": fid, "by": "ada"}, datetime.now(timezone.utc), "VPS", "00000a")
        reply = run(listener.control_staff_ingest(jreq({"rows": [row]}, key=KEY, same_site=False)))
        self.assertEqual(reply.content, {"added": 1, "rejected": 0})
        self.assertEqual([seen["by"] for seen in self.failures()[fid]["acknowledged"]], ["ada"])

    def test_a_node_that_does_not_know_the_new_kind_yet_drops_it_and_takes_it_once_updated(self):
        # This is WHY the standby VPS is updated first and Core second (the unpublished design notes part 15): a node on the old code throws the row away, and the copy loop brings it back later.
        fid = self.failure_while_on_shift("ada")
        row = staff.make_row("failure_acknowledged", "", {"fid": fid, "by": "ada"}, datetime.now(timezone.utc), "VPS", "00000a")
        old_list = tuple(kind for kind in staff.KINDS if kind != "failure_acknowledged")
        with mock.patch.object(staff, "KINDS", old_list):
            reply = run(listener.control_staff_ingest(jreq({"rows": [row]}, key=KEY, same_site=False)))
        self.assertEqual(reply.content, {"added": 0, "rejected": 1})
        self.assertNotIn("failure_acknowledged", self.kinds())
        self.fake_peer(run(listener._staff_rows()) + [row])
        self.assertEqual(run(listener._staff_sync_once())["pulled"], 1)
        self.assertEqual(self.kinds().count("failure_acknowledged"), 1)


class FailureHookTests(StaffWiring):
    def test_a_red_alarm_sends_the_mail_and_opens_a_failure(self):
        run(listener._send_red_alarm(self.endpoint("SITE-A", "Silence > 60s")))
        self.assertEqual(self.red_mails, ["SITE-A"])
        failures = list(self.failures().values())
        self.assertEqual([(f["studio"], f["cause"]) for f in failures], [("SITE-A", "Silence > 60s")])

    def test_a_second_red_soon_after_joins_the_same_failure_and_another_studio_does_not(self):
        run(listener._send_red_alarm(self.endpoint("SITE-A")))
        run(listener._send_red_alarm(self.endpoint("SITE-A", "Off Air")))
        run(listener._send_red_alarm(self.endpoint("SITE-B")))
        by_studio = {f["studio"]: f["alarms"] for f in self.failures().values()}
        self.assertEqual(by_studio, {"SITE-A": 2, "SITE-B": 1})

    def test_two_playouts_of_one_studio_are_two_failures_not_one(self):
        # 2026-11-01: MAIN and BACKUP-SERVER have two playouts each and a failure is counted per playout, even on the same PC.
        run(listener._send_red_alarm(self.real_playout("MAIN-USA")))
        run(listener._send_red_alarm(self.real_playout("MAIN-Caribbean")))
        shown = sorted((f["studio"], f["display"], f["alarms"]) for f in self.failures().values())
        self.assertEqual(shown, [("MAIN-Caribbean", "MAIN - Caribbean", 1), ("MAIN-USA", "MAIN - USA", 1)])

    def test_the_same_playout_going_red_again_within_ten_minutes_is_one_failure(self):
        for cause in ("Process Missing", "Paused/Frozen > 60s", "Off Air"):
            run(listener._send_red_alarm(self.real_playout("BACKUP-Caribbean", cause)))
        self.assertEqual([(f["studio"], f["alarms"]) for f in self.failures().values()], [("BACKUP-Caribbean", 3)])

    def test_the_same_feed_on_main_and_on_backup_is_two_failures(self):
        run(listener._send_red_alarm(self.real_playout("MAIN-USA")))
        run(listener._send_red_alarm(self.real_playout("BACKUP-USA")))
        self.assertEqual(sorted(f["studio"] for f in self.failures().values()), ["BACKUP-USA", "MAIN-USA"])

    def test_a_node_that_may_not_alert_records_no_failure_but_the_one_that_may_does(self):
        # Found live on 2026-11-01: the standby heard the same MAIN alarm 0.14 s after Core and added a second alarm to Core's failure, though only Core sent the e-mail.
        # A failure is recorded by the node that is allowed to alert people (the rule that decides whether the e-mail goes out), so one event is one alarm.
        saved = listener._delivery_allowed
        try:
            listener._delivery_allowed = lambda: False
            run(listener._send_red_alarm(self.real_playout("MAIN-Caribbean")))
            self.assertEqual(self.failures(), {})
            self.assertEqual(self.red_mails, ["MAIN - Caribbean"])               # the mail function is still asked (it decides for itself that this node stays silent)
            listener._delivery_allowed = lambda: True
            run(listener._send_red_alarm(self.real_playout("MAIN-Caribbean")))
        finally:
            listener._delivery_allowed = saved
        self.assertEqual([(f["studio"], f["alarms"]) for f in self.failures().values()], [("MAIN-Caribbean", 1)])

    def test_every_real_playout_id_is_accepted_as_a_failure_key(self):
        for endpoint in listener.PLAYOUT_ENDPOINTS:
            row = staff.failure_row({"failures": {}}, endpoint["endpoint_id"], "Process Missing", endpoint["display_name"], datetime.now(timezone.utc), "CORE", "abcd1234")
            self.assertIsNone(staff.row_problem(row), endpoint["endpoint_id"])

    def test_an_endpoint_without_its_own_id_is_skipped_not_filed_under_its_studio(self):
        run(listener._send_red_alarm({"display_name": "MAIN - USA", "source_id": "MAIN", "root_cause": "Off Air"}))
        self.assertEqual(self.failures(), {})
        self.assertEqual(self.red_mails, ["MAIN - USA"])

    def test_the_mail_goes_out_before_the_failure_is_recorded(self):
        order = []

        async def note(endpoint):
            order.append("note")

        saved = listener._staff_note_failure
        listener._staff_note_failure = note
        listener._send_endpoint_critical_email = lambda endpoint: order.append("mail")
        try:
            run(listener._send_red_alarm(self.endpoint()))
        finally:
            listener._staff_note_failure = saved
        self.assertEqual(order, ["mail", "note"])

    def test_a_broken_staff_log_never_stops_the_red_mail(self):
        async def broken(*args, **kwargs):
            raise RuntimeError("the staff table is full")

        saved = listener._staff_write
        listener._staff_write = broken
        try:
            run(listener._send_red_alarm(self.endpoint("SITE-A")))
        finally:
            listener._staff_write = saved
        self.assertEqual(self.red_mails, ["SITE-A"])

    def test_a_failing_mail_still_records_the_failure_and_still_raises_as_before(self):
        def failing(endpoint):
            raise OSError("mail server down")

        listener._send_endpoint_critical_email = failing
        with self.assertRaises(OSError):
            run(listener._send_red_alarm(self.endpoint("MAIN")))
        self.assertEqual([f["studio"] for f in self.failures().values()], ["MAIN"])

    def test_nothing_is_recorded_while_access_is_off(self):
        self.switch_off(notify=False)
        run(listener._send_red_alarm(self.endpoint("SITE-A")))
        self.assertEqual(self.failures(), {})
        self.assertEqual(self.red_mails, ["SITE-A"])          # the alert path itself is untouched

    def test_nothing_is_recorded_where_the_control_phase_is_off(self):
        listener.CONTROL_KEY = ""
        run(listener._send_red_alarm(self.endpoint("SITE-A")))
        self.assertEqual(self.red_mails, ["SITE-A"])
        listener.CONTROL_KEY = KEY
        self.assertEqual(self.failures(), {})

    def test_bad_endpoint_details_are_skipped_without_breaking_the_mail(self):
        run(listener._send_red_alarm({"display_name": "", "endpoint_id": "bad playout!", "source_id": "MAIN", "root_cause": ""}))
        self.assertEqual(self.failures(), {})

    def test_every_red_mail_goes_through_the_one_wrapper(self):
        source = Path(listener.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("asyncio.to_thread(_send_endpoint_critical_email"), 1)
        self.assertGreaterEqual(source.count("await _send_red_alarm(endpoint)"), 3)


class PublicRosterTests(StaffWiring):
    def test_the_stream_frame_carries_display_names_of_people_on_shift_and_nothing_else(self):
        self.add("ada.o", "Ada O.")
        self.add("kemi", "Kemi")
        self.act(self.staff_cookie("ada.o"), {"action": "start_shift"})
        frame = listener._staff_public()
        self.assertEqual(frame, {"on_shift": ["Ada O."]})
        self.assertNotIn("ada.o", json.dumps(frame))
        self.assertIn("_staff_public()", inspect.getsource(listener.stream))

    def test_the_frame_is_empty_where_the_control_phase_is_off_and_while_access_is_off(self):
        self.add("ada")
        self.start_shift()
        self.switch_off(notify=False)
        listener.CONTROL_KEY = ""
        self.assertEqual(listener._staff_public(), {"on_shift": []})

    def test_the_toolbar_shows_the_signed_in_persons_name_escaped(self):
        page = "<nav>" + listener.STAFF_LINK_MARKER + "</nav>"
        shown = listener._with_super_link(page, greq(), "O'Brien")
        self.assertIn('href="/staff"', shown)
        self.assertIn("O&#x27;Brien", shown)
        self.assertNotIn("O'Brien", shown)
        self.assertIn("Sign in", listener._with_super_link(page, greq()))
        self.assertIn("Control panel", listener._with_super_link(page, greq(cookie=self.super_cookie()), "Ada"))

    def test_the_dashboard_page_greets_a_signed_in_staff_person(self):
        self.add("ada", "Ada")
        cookie = self.staff_cookie()
        saved = listener.HTMLResponse
        listener.HTMLResponse = lambda content, status_code=200, headers=None: types.SimpleNamespace(content=content, status_code=status_code, headers=headers)     # the harness's stand-in takes no headers
        try:
            page = run(listener.dashboard(greq(cookie=cookie))).content
            anonymous = run(listener.dashboard(greq())).content
        finally:
            listener.HTMLResponse = saved
        self.assertIn('href="/staff"', page)
        self.assertNotIn('href="/staff"', anonymous)


class RowCopyTests(StaffWiring):
    PAST = -60 * 24 * 3                # three days before the test clock: the listener refuses rows dated more than a day ahead

    def peer_rows(self, count=100):
        world = tsl.World(node="VPS")
        for index in range(count):
            world.red("SITE-A", self.PAST + index * 20)
        return world.rows

    def test_the_summary_and_the_row_pages_need_the_control_key_and_not_just_a_session(self):
        self.add("ada")
        for call in (listener.control_staff_summary, listener.control_staff_rows):
            self.assertEqual(run(call(greq())).status_code, 401)
            self.assertEqual(run(call(greq(cookie=self.super_cookie()))).status_code, 401)
            self.assertEqual(run(call(greq(key=KEY))).status_code, 200)

    def test_rows_come_in_pages_in_id_order_after_a_cursor(self):
        rows = self.peer_rows(100)
        run(listener._staff_append_rows(rows))
        first = run(listener.control_staff_rows(FakeRequest({"x-watchdog-control": KEY}, b"", host="203.0.113.5"))).content["rows"]
        self.assertEqual(len(first), listener.STAFF_PAGE_LIMIT)
        self.assertEqual([r["id"] for r in first], sorted(r["id"] for r in rows)[:listener.STAFF_PAGE_LIMIT])

    def test_a_page_is_never_longer_than_the_limit_whatever_is_asked_for(self):
        run(listener._staff_append_rows(self.peer_rows(100)))
        for params in ({"limit": "100000", "after": ""}, {"limit": "abc"}, {"limit": "-5"}, {}):
            request = greq(key=KEY)
            request.query_params = params
            size = len(run(listener.control_staff_rows(request)).content["rows"])
            self.assertTrue(1 <= size <= listener.STAFF_PAGE_LIMIT, (params, size))

    def test_ingest_adds_valid_rows_once_and_rejects_the_rest(self):
        rows = self.peer_rows(5)
        bad = [{**rows[0], "kind": "person_deleted", "id": rows[0]["id"] + "x"}, "text", {"id": "nope"}]
        reply = run(listener.control_staff_ingest(jreq({"rows": rows + bad}, key=KEY, same_site=False)))
        self.assertEqual((reply.status_code, reply.content), (200, {"added": 5, "rejected": 3}))
        again = run(listener.control_staff_ingest(jreq({"rows": rows}, key=KEY, same_site=False)))
        self.assertEqual(again.content, {"added": 0, "rejected": 0})
        self.assertEqual(len(run(listener._staff_rows())), 5)
        self.assertEqual(run(listener.control_staff_ingest(jreq({"rows": rows}, same_site=False))).status_code, 401)
        self.assertEqual(run(listener.control_staff_ingest(jreq({"rows": "x"}, key=KEY, same_site=False))).status_code, 400)
        self.assertEqual(run(listener.control_staff_ingest(jreq({"rows": rows * 20}, key=KEY, same_site=False))).status_code, 400)

    def test_ingest_refuses_a_row_dated_far_in_the_future(self):
        future = staff.make_row("shift_started", "ada", {}, datetime.now(timezone.utc) + timedelta(days=5), "VPS", "00000a")
        reply = run(listener.control_staff_ingest(jreq({"rows": [future]}, key=KEY, same_site=False)))
        self.assertEqual(reply.content, {"added": 0, "rejected": 1})

    def test_a_node_missing_many_rows_pulls_them_all_page_by_page_and_then_goes_quiet(self):
        rows = self.peer_rows(100)
        calls = self.fake_peer(rows)
        result = run(listener._staff_sync_once())
        self.assertEqual(result["pulled"], 100)
        self.assertEqual(listener._staff_digest(run(listener._staff_rows())), listener._staff_digest(rows))
        self.assertGreaterEqual(sum(1 for method, path in calls if path.startswith("/control/staff/rows")), 3)
        calls.clear()
        self.assertEqual(run(listener._staff_sync_once())["pulled"], 0)
        self.assertEqual([path for _, path in calls], ["/control/staff/summary"])

    def test_both_sides_missing_different_rows_end_with_the_same_rows(self):
        mine, theirs = self.peer_rows(30), self.peer_rows(30)
        shared_world = tsl.World(node="CORE")
        for index in range(20):
            shared_world.red("SITE-B", self.PAST + index * 20)
        run(listener._staff_append_rows(shared_world.rows[:12] + mine[:10]))
        peer = shared_world.rows[8:] + theirs[5:]
        self.fake_peer(peer)
        run(listener._staff_sync_once())
        local = run(listener._staff_rows())
        self.assertTrue({r["id"] for r in peer} <= {r["id"] for r in local})

    def test_an_unreachable_other_node_changes_nothing_and_does_not_raise(self):
        run(listener._staff_append_rows(self.peer_rows(3)))
        listener._peer_request = lambda method, path, payload=None: (None, {})
        self.assertIsNone(run(listener._staff_sync_once()))
        self.assertEqual(len(run(listener._staff_rows())), 3)

    def test_a_peer_that_sends_junk_rows_cannot_put_them_in(self):
        junk = [{"id": "x", "at": "y"}, "text", None]
        good = self.peer_rows(2)
        self.fake_peer(good + junk)
        run(listener._staff_sync_once())
        self.assertEqual({r["id"] for r in run(listener._staff_rows())}, {r["id"] for r in good})

    def test_a_write_is_pushed_to_the_other_node_and_a_failed_push_does_not_fail_the_write(self):
        sent = []
        listener._peer_request = lambda method, path, payload=None: (sent.append((method, path, payload)) or (200, {"added": 1, "rejected": 0}))
        self.add("ada")
        self.assertTrue(any(path == "/control/staff/rows" and method == "POST" for method, path, _ in sent))
        ids = {r["id"] for _, _, payload in sent for r in payload["rows"]}
        self.assertTrue(ids <= {r["id"] for r in run(listener._staff_rows())})
        listener._peer_request = lambda method, path, payload=None: (None, {})
        self.add("kemi")
        self.assertIn("kemi", self.state()["people"])

        def explodes(method, path, payload=None):
            raise RuntimeError("the other node's network fell over")

        listener._peer_request = explodes
        self.add("zara")                                  # a push that RAISES must not fail the write either
        self.assertIn("zara", self.state()["people"])

    def test_both_nodes_agree_on_who_can_sign_in_after_copying(self):
        self.add("ada")
        rows = run(listener._staff_rows())
        node_b_state = staff.replay(rows)
        self.assertTrue(staff.verify_session(KEY, staff.issue_session(KEY, "ada", node_b_state["people"]["ada"]["tag"], datetime.now(timezone.utc)), datetime.now(timezone.utc), node_b_state))


if __name__ == "__main__":
    unittest.main(verbosity=1)
