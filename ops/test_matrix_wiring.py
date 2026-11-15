# File:         test_matrix_wiring.py
# Purpose:      Tests of how core-listener/app/main.py USES matrix.py: the access rules (the staff door, the super user, the control key, the same-origin press), the relay from the
#               standby to Core, the endpoints on Core, the audit log, the e-mail on a failure, the separate rate limit and what the public dashboard can never see.
# Inputs:       None (a throwaway database, fake mail, a fake matrix, a fake other node, made-up requests). Nothing leaves this machine.
# Outputs:      Printed unittest results; exit code 1 if anything fails.
# Dependencies: core-listener/app/main.py (loaded with the same FastAPI stand-in as ops/test_listener_units.py), ops/test_control_wiring.py, ops/test_staff_wiring.py (helpers only)
#               and ops/matrix_fixtures.py.
"""Run: python ops/test_matrix_wiring.py

What is proven here, in plain words (the unpublished design notes section 3, as changed on 2026-11-10): a browser may use the matrix in exactly two ways. (1) The signed-in super user, from ANY
address: Core's own address (Core switches directly) or any address of the standby (the standby relays). (2) A registered OFFICE PC: a request that arrived on the standby's staff door
(the PC showed its certificate), with NO sign-in, or a browser holding the DOOR PASS the standby hands such a PC (valid 12 hours, so the same PC is recognised on the watchdog
address too). Everyone else, a staff session included, gets "not found" and learns nothing. Core's own key endpoints answer only the internal key,
never a browser cookie; a request can name only a preset and never carries a command; the emergency preset needs its confirmation everywhere; every press is logged on Core with who
and by which way in, a failure mails the super user, and nothing about it reaches the public dashboard's data.
"""
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import matrix_fixtures as fx  # noqa: E402
import test_control_wiring as tcw  # noqa: E402
import test_staff_wiring as tsw  # noqa: E402

listener, access, control, matrix = tcw.listener, tcw.access, tcw.control, fx.matrix
KEY, WHO, run, FakeRequest = tcw.KEY, tcw.WHO, tcw.run, tcw.FakeRequest
DOOR, MAIN_DOOR = tsw.DOOR, tsw.MAIN_DOOR
through = tsw.through
FIXED = ("MATRIX_SWITCHER", "_matrix_rate", "STAFF_DOOR_PORT", "SUPER_USER", "_peer_request", "IS_STANDBY", "NODE", "send_plain_mail", "MATRIX_MAIL_WAIT_SECONDS",
         "OFFICE_PASS_ON", "STAFF_URL", "DASHBOARD_URL", "request_scheme", "FORWARDED_TRUST")


def request(payload=None, cookie=None, key=None, host="203.0.113.5", same_site=True, door=DOOR, raw=None):
    headers = {"content-type": "application/json"}
    if same_site:
        headers["sec-fetch-site"] = "same-origin"
    if cookie:
        headers["cookie"] = cookie
    if key:
        headers["x-watchdog-control"] = key
    body = raw if raw is not None else (b"" if payload is None else json.dumps(payload).encode())
    return through(FakeRequest(headers, body, host=host), door)


class Base(tcw.Wiring):
    """A listener with a fake matrix attached, and a way to look at what is in the log."""

    def setUp(self):
        super().setUp()
        self.saved_matrix = {name: getattr(listener, name) for name in FIXED}
        listener.STAFF_DOOR_PORT, listener.SUPER_USER = DOOR, WHO
        self.fake = fx.FakeMatrix(fx.LIVE_ROUTING)
        listener.MATRIX_SWITCHER = matrix.Switcher(self.fake, sleep=lambda seconds: None)
        listener._matrix_rate = listener.VisitorRate(60)
        listener._control_rate._seen.clear()
        self.peer_calls = []

    def tearDown(self):
        for name, value in self.saved_matrix.items():
            setattr(listener, name, value)
        super().tearDown()

    def super_cookie(self):
        return f"{listener.CONTROL_COOKIE}={access.issue_session(KEY, datetime.now(timezone.utc), 3600, listener._session_generation())}"

    def log_rows(self):
        with listener.db() as conn:
            return matrix.recent(conn, 50)

    def make_standby(self, peer):
        """This listener is now the standby on the VPS: no matrix of its own, and `peer` stands in for Core."""
        listener.IS_STANDBY, listener.NODE, listener.MATRIX_SWITCHER = True, control.VPS, None

        def fake_peer(method, path, payload=None, timeout=10):
            self.peer_calls.append((method, path, payload, timeout))
            return peer(method, path, payload)
        listener._peer_request = fake_peer


class CoreEndpointTests(Base):
    """The endpoints that touch the matrix. They answer only the internal key."""

    def test_the_status_answers_the_key_and_nothing_else(self):
        for label, kwargs in (("no credential", {}), ("wrong key", {"key": "x" * 40}), ("a super user's browser cookie alone", {"cookie": self.super_cookie()})):
            with self.subTest(label):
                reply = run(listener.matrix_status(request(**kwargs, door=MAIN_DOOR)))
                self.assertEqual(reply.status_code, 401)
        self.assertEqual((self.fake.reads, self.fake.sent), (0, []))

    def test_the_status_with_the_key_shows_the_routing_the_presets_and_the_log(self):
        reply = run(listener.matrix_status(request(key=KEY, door=MAIN_DOOR)))
        data = reply.content
        self.assertEqual(reply.status_code, 200)
        self.assertEqual((data["enabled"], data["reachable"], data["routing"], data["active"]), (True, True, list(fx.LIVE_ROUTING), None))
        self.assertEqual([item["key"] for item in data["presets"]], ["main", "backup", "brightsign"])
        self.assertEqual((data["last_success"], data["recent"]), (None, []))
        self.assertEqual(len(data["outputs"]), 8)

    def test_a_locked_or_broken_log_never_takes_the_status_down(self):
        with mock.patch.object(matrix, "last_success", side_effect=RuntimeError("database is locked")):
            reply = run(listener.matrix_status(request(key=KEY, door=MAIN_DOOR)))
        self.assertEqual(reply.status_code, 200)
        self.assertEqual((reply.content["routing"], reply.content["last_success"], reply.content["recent"]), (list(fx.LIVE_ROUTING), None, []))

    def test_a_plain_status_may_reuse_a_reading_under_two_seconds_but_fresh_always_reads_again(self):
        run(listener.matrix_status(request(key=KEY, door=MAIN_DOOR)))
        self.fake.routing[:] = fx.MAIN_ROUTING                                         # someone else changes the matrix with the manufacturer's Control Panel
        cached = run(listener.matrix_status(request(key=KEY, door=MAIN_DOOR))).content
        self.assertEqual(cached["routing"], list(fx.LIVE_ROUTING))
        req = request(key=KEY, door=MAIN_DOOR)
        req.query_params = {"fresh": "1"}
        self.assertEqual(run(listener.matrix_status(req)).content["routing"], list(fx.MAIN_ROUTING))

    def test_a_node_without_a_matrix_answers_a_stranger_exactly_like_a_node_with_one(self):
        # Without the key the answer is 401 whether or not a matrix is attached, so nobody can learn from outside which node has one; only the key holder is told it is absent (404).
        listener.MATRIX_SWITCHER = None
        for label, kwargs, expected in (("with the key", {"key": KEY}, 404), ("without", {}, 401), ("with a cookie", {"cookie": self.super_cookie()}, 401)):
            with self.subTest(label):
                self.assertEqual(run(listener.matrix_status(request(**kwargs, door=MAIN_DOOR))).status_code, expected)
                self.assertEqual(run(listener.matrix_switch(request({"preset": "main"}, **kwargs, door=MAIN_DOOR))).status_code, expected)

    def test_with_the_control_phase_off_the_matrix_door_does_not_exist(self):
        listener.CONTROL_KEY = ""
        self.assertEqual(run(listener.matrix_status(request(key=KEY, door=MAIN_DOOR))).status_code, 404)
        self.assertEqual(run(listener.matrix_switch(request({"preset": "main"}, key=KEY, door=MAIN_DOOR))).status_code, 404)

    def test_a_press_with_the_key_switches_confirms_and_is_logged_with_who_and_how(self):
        reply = run(listener.matrix_switch(request({"preset": "main", "operator": "owner@mail.example", "via": "staff-door"}, key=KEY, door=MAIN_DOOR, same_site=False)))
        self.assertEqual(reply.status_code, 200)
        self.assertEqual((reply.content["ok"], reply.content["state"]), (True, "done"))
        self.assertEqual(self.fake.sent, ["2v5."])
        self.assertEqual(reply.content["status"]["active"], "main")
        row = self.log_rows()[0]
        self.assertEqual((row["operator"], row["preset"], row["result"], row["via"]), ("owner@mail.example", "main", "done", "staff-door"))
        self.assertEqual((row["detail"]["before"], row["detail"]["after"], row["detail"]["sent"]), (list(fx.LIVE_ROUTING), list(fx.MAIN_ROUTING), ["2v5."]))
        self.assertEqual(row["detail"]["from"], "peer:203.0.113.5")      # who really asked, as the server saw it: the operator and via fields are only claims by a key holder
        status = run(listener.matrix_status(request(key=KEY, door=MAIN_DOOR))).content
        self.assertEqual((status["last_success"]["operator"], status["last_success"]["preset"]), ("owner@mail.example", "main"))
        # the answer to the press carries the same last-success line, so the card never says "none recorded" right after a success
        self.assertEqual((reply.content["status"]["last_success"]["operator"], reply.content["status"]["last_success"]["preset"]), ("owner@mail.example", "main"))
        self.assertEqual(len(reply.content["status"]["recent"]), 1)

    def test_a_super_users_browser_cookie_can_never_switch_on_core(self):
        reply = run(listener.matrix_switch(request({"preset": "main"}, cookie=self.super_cookie(), door=MAIN_DOOR)))
        self.assertEqual(reply.status_code, 401)
        self.assertEqual((self.fake.sent, self.fake.reads, self.log_rows()), ([], 0, []))

    def test_a_request_can_only_name_a_preset_never_carry_a_command(self):
        bad = ({"preset": "2v1,2,5."}, {"preset": "All"}, {"preset": "5All."}, {"preset": "MAIN"}, {"preset": ""}, {"preset": ["main"]}, {"preset": None}, {}, {"preset": "main", "command": "2v1."},
               {"preset": "main", "outputs": [1]}, {"preset": "main", "confirm": "yes"}, {"preset": "main", "operator": 5}, {"preset": "main", "operator": "x" * 81})
        for payload in bad:
            with self.subTest(payload=payload):
                reply = run(listener.matrix_switch(request(payload, key=KEY, door=MAIN_DOOR, same_site=False)))
                self.assertEqual(reply.status_code, 400, reply.content)
        self.assertEqual((self.fake.sent, self.fake.reads, self.log_rows()), ([], 0, []))

    def test_a_body_that_is_not_json_is_refused(self):
        for raw in (b"not json", b"[]", b'"main"', b""):
            with self.subTest(raw=raw):
                self.assertEqual(run(listener.matrix_switch(request(key=KEY, door=MAIN_DOOR, raw=raw))).status_code, 400)
        self.assertEqual(self.fake.sent, [])

    def test_the_emergency_preset_needs_its_confirmation_and_main_and_backup_do_not(self):
        refused = run(listener.matrix_switch(request({"preset": "brightsign"}, key=KEY, door=MAIN_DOOR, same_site=False)))
        self.assertEqual(refused.status_code, 400)
        self.assertIn("confirm", refused.content["error"])
        self.assertEqual(self.fake.sent, [])
        for wrong in (False, "true", 1, None):
            self.assertEqual(run(listener.matrix_switch(request({"preset": "brightsign", "confirm": wrong}, key=KEY, door=MAIN_DOOR, same_site=False))).status_code, 400)
        self.assertEqual((self.fake.sent, self.log_rows()), ([], []))
        accepted = run(listener.matrix_switch(request({"preset": "brightsign", "confirm": True}, key=KEY, door=MAIN_DOOR, same_site=False)))
        self.assertEqual((accepted.status_code, accepted.content["state"]), (200, "done"))
        self.assertEqual(self.fake.sent, ["1v1,2,3,4,5,6."])
        self.assertEqual(accepted.content["status"]["active"], "brightsign")
        self.assertEqual(run(listener.matrix_switch(request({"preset": "main"}, key=KEY, door=MAIN_DOOR, same_site=False))).status_code, 200)    # no confirmation needed

    def test_a_preset_whose_outputs_are_not_set_is_refused_with_a_conflict_and_sends_nothing(self):
        empty = matrix.Preset("brightsign", "BRIGHTSIGN PLAYER", "EMERGENCY OPERATION", (), needs_confirm=True)
        with mock.patch.dict(matrix.PRESETS, {"brightsign": empty}):
            reply = run(listener.matrix_switch(request({"preset": "brightsign", "confirm": True}, key=KEY, door=MAIN_DOOR, same_site=False)))
        self.assertEqual((reply.status_code, reply.content["state"]), (409, "not_configured"))
        self.assertEqual((self.fake.sent, self.fake.reads, self.log_rows()), ([], 0, []))

    def test_a_press_while_another_runs_is_a_conflict_sends_nothing_and_is_not_logged(self):
        self.assertTrue(listener.MATRIX_SWITCHER._lock.acquire(blocking=False))
        try:
            reply = run(listener.matrix_switch(request({"preset": "main"}, key=KEY, door=MAIN_DOOR, same_site=False)))
        finally:
            listener.MATRIX_SWITCHER._lock.release()
        self.assertEqual((reply.status_code, reply.content["state"]), (409, "busy"))
        self.assertEqual((self.fake.sent, self.log_rows()), ([], []))

    def test_the_operator_name_is_cleaned_defaulted_and_the_way_in_is_one_of_the_known_words(self):
        run(listener.matrix_switch(request({"preset": "main", "operator": "bad\r\nname"}, key=KEY, door=MAIN_DOOR, same_site=False)))
        self.assertEqual(self.log_rows()[0]["operator"], "badname")
        self.fake.routing[:] = fx.LIVE_ROUTING
        run(listener.matrix_switch(request({"preset": "backup", "via": "somewhere else"}, key=KEY, door=MAIN_DOOR, same_site=False)))
        newest = self.log_rows()[0]
        self.assertEqual((newest["operator"], newest["via"]), ("control key", "control-key"))
        # The two ways the standby relays a press are accepted as claims by the key holder. "core-site" is NOT one: only Core itself, having seen a super session, may write it.
        for word, expected in (("staff-door", "staff-door"), ("standby-site", "standby-site"), ("office-pc", "office-pc"), ("office-pass", "office-pass"), ("core-site", "control-key")):
            self.fake.routing[:] = fx.LIVE_ROUTING
            run(listener.matrix_switch(request({"preset": "main", "via": word}, key=KEY, door=MAIN_DOOR, same_site=False)))
            self.assertEqual(self.log_rows()[0]["via"], expected, word)

    def test_every_kind_of_result_is_logged_but_only_failures_are_mailed_to_the_super_user(self):
        run(listener.matrix_switch(request({"preset": "main"}, key=KEY, door=MAIN_DOOR, same_site=False)))            # done
        run(listener.matrix_switch(request({"preset": "main"}, key=KEY, door=MAIN_DOOR, same_site=False)))            # already
        self.fake.offline = True
        run(listener.matrix_switch(request({"preset": "backup"}, key=KEY, door=MAIN_DOOR, same_site=False)))          # offline
        self.assertEqual([row["result"] for row in self.log_rows()], ["offline", "already", "done"])
        self.assertEqual(tcw.FakeSmtp.sent, [])
        self.fake.offline = False
        self.fake.ignore = lambda text: True
        reply = run(listener.matrix_switch(request({"preset": "backup"}, key=KEY, door=MAIN_DOOR, same_site=False)))  # failed
        self.assertEqual(reply.content["state"], "failed")
        self.assertEqual(self.log_rows()[0]["result"], "failed")
        self.assertEqual(len(tcw.FakeSmtp.sent), 1)
        self.assertEqual(tcw.FakeSmtp.sent[0][0], ["super@x.test"])
        self.assertIn("did not confirm", tcw.FakeSmtp.sent[0][1])

    def test_an_unverified_switch_also_mails_and_a_broken_mail_server_never_changes_the_answer(self):
        self.fake.fail_reads_after_send = True
        tcw.FakeSmtp.down = True
        reply = run(listener.matrix_switch(request({"preset": "main"}, key=KEY, door=MAIN_DOOR, same_site=False)))
        self.assertEqual((reply.status_code, reply.content["state"]), (200, "unverified"))
        self.assertEqual(self.log_rows()[0]["result"], "unverified")

    def test_a_press_whose_caller_goes_away_mid_switch_is_still_finished_and_logged(self):
        # The browser or the tunnel can drop the request while the datagrams are out. The matrix does not stop, so the log must not skip the press.
        import asyncio
        import threading
        entered, release = threading.Event(), threading.Event()
        self.fake.on_send = lambda text: (entered.set(), release.wait(5))

        async def scenario():
            task = asyncio.ensure_future(listener.matrix_switch(request({"preset": "main", "operator": "owner@mail.example"}, key=KEY, door=MAIN_DOOR, same_site=False)))
            await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)
            task.cancel()                                                          # the caller is gone
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            for _ in range(100):
                await asyncio.sleep(0.05)
                if self.log_rows():
                    break
        run(scenario())
        rows = self.log_rows()
        self.assertEqual([(row["preset"], row["result"], row["operator"]) for row in rows], [("main", "done", "owner@mail.example")])

    def test_a_slow_mail_server_cannot_hold_back_the_answer_to_a_failed_press(self):
        import time
        sent = []
        listener.MATRIX_MAIL_WAIT_SECONDS = 0.2
        listener.send_plain_mail = lambda subject, body, to: (time.sleep(1.5), sent.append(subject))
        self.fake.ignore = lambda text: True
        async def timed():                                                # measured inside the loop: asyncio.run itself waits for the mail thread when it shuts down
            began = time.monotonic()
            return await listener.matrix_switch(request({"preset": "main"}, key=KEY, door=MAIN_DOOR, same_site=False)), time.monotonic() - began
        reply, took = run(timed())
        self.assertLess(took, 1.0)
        self.assertEqual((reply.status_code, reply.content["state"]), (200, "failed"))
        for _ in range(60):
            if sent:
                break
            time.sleep(0.05)
        self.assertEqual(len(sent), 1)                                    # the mail still goes out in its own thread

    def test_the_peer_call_keeps_the_body_of_an_error_answer_so_busy_reaches_the_page(self):
        import io
        import urllib.error
        listener.PRIMARY_URL = "https://core.test/health"
        error = urllib.error.HTTPError("https://core.test/control/matrix/switch", 409, "Conflict", {}, io.BytesIO(b'{"ok": false, "state": "busy", "message": "A switch is already running."}'))

        class Opener:
            def open(self, request, timeout=None):
                raise error
        with mock.patch.object(listener.urllib.request, "build_opener", lambda *handlers: Opener()):
            code, data = listener._peer_request("POST", "/control/matrix/switch", {"preset": "main"}, 25)
        self.assertEqual((code, data.get("state")), (409, "busy"))
        broken = urllib.error.HTTPError("https://core.test/x", 502, "Bad Gateway", {}, io.BytesIO(b"<html>not json</html>"))
        with mock.patch.object(listener.urllib.request, "build_opener", lambda *handlers: type("O", (), {"open": lambda self, request, timeout=None: (_ for _ in ()).throw(broken)})()):
            self.assertEqual(listener._peer_request("GET", "/control/matrix"), (502, {}))

    def test_nothing_about_a_press_reaches_the_public_dashboards_data(self):
        run(listener.matrix_switch(request({"preset": "main", "operator": "owner@mail.example"}, key=KEY, door=MAIN_DOOR, same_site=False)))
        with listener.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE source_id = 'MATRIX' OR detail LIKE '%owner%'").fetchone()[0], 0)
            self.assertNotIn("owner", json.dumps(listener._recent_events_snapshot(conn, 50)))

    def test_the_matrix_has_its_own_rate_limit_and_polling_does_not_use_up_the_panels(self):
        listener._matrix_rate = listener.VisitorRate(5)
        replies = [run(listener.matrix_status(request(key=KEY, door=MAIN_DOOR))).status_code for _ in range(7)]
        self.assertEqual(replies, [200] * 5 + [429] * 2)
        self.assertTrue(listener._control_rate.allow("203.0.113.5", 1.0))             # the panel's own allowance was not spent


class CoreBrowserTests(Base):
    """The super user signed in on CORE'S OWN address (changed 2026-11-10): Core reads and switches the matrix itself, with no relay. Core has no staff door, so nobody else may."""

    def setUp(self):
        super().setUp()
        listener.IS_STANDBY, listener.NODE = False, control.CORE

        def never(method, path, payload=None, timeout=10):
            self.peer_calls.append((method, path, payload, timeout))
            raise AssertionError("Core must never ask another node about its own matrix")
        listener._peer_request = never

    def get(self, **kwargs):
        return run(listener.matrix_panel_status(request(**kwargs)))

    def post(self, payload, **kwargs):
        return run(listener.matrix_panel_switch(request(payload, **kwargs)))

    def test_the_super_user_reads_the_matrix_directly_on_any_port(self):
        for door in (MAIN_DOOR, None, DOOR):                      # Core has no staff door, so even a request on that port number is just a request
            with self.subTest(door=door):
                reply = self.get(cookie=self.super_cookie(), door=door)
                self.assertEqual(reply.status_code, 200)
                data = reply.content
                self.assertEqual((data["enabled"], data["core_reached"], data["reachable"], data["routing"]), (True, True, True, list(fx.LIVE_ROUTING)))
                self.assertEqual([item["key"] for item in data["presets"]], ["main", "backup", "brightsign"])
        self.assertEqual(self.peer_calls, [])
        self.assertGreaterEqual(self.fake.reads, 1)

    def test_a_forced_refresh_reads_the_matrix_again(self):
        self.get(cookie=self.super_cookie())
        self.fake.routing[:] = fx.MAIN_ROUTING                                         # someone else changes the matrix with the manufacturer's Control Panel
        self.assertEqual(self.get(cookie=self.super_cookie()).content["routing"], list(fx.LIVE_ROUTING))      # a plain read may reuse a reading under two seconds old
        req = request(cookie=self.super_cookie())
        req.query_params = {"fresh": "1"}
        self.assertEqual(run(listener.matrix_panel_status(req)).content["routing"], list(fx.MAIN_ROUTING))

    def test_everyone_else_gets_not_found_and_nothing_is_read_or_sent(self):
        everyone_else = (("no session", {}), ("a staff session", {"cookie": f"{listener.STAFF_COOKIE}=anything-a-staff-session-could-hold"}),
                         ("a forged super session", {"cookie": f"{listener.CONTROL_COOKIE}=forged.value"}), ("a wrong key", {"key": "x" * 40}))
        for label, kwargs in everyone_else:
            for door in (MAIN_DOOR, DOOR, None):                   # no office-PC rule on Core, even on the door's port number
                with self.subTest(label=label, door=door):
                    self.assertEqual(self.get(**kwargs, door=door).status_code, 404)
                    self.assertEqual(self.post({"preset": "main"}, **kwargs, door=door).status_code, 404)
        self.assertEqual((self.fake.reads, self.fake.sent, self.log_rows(), self.peer_calls), (0, [], [], []))

    def test_a_node_without_a_matrix_says_not_found_even_to_the_super_user(self):
        listener.MATRIX_SWITCHER = None
        self.assertEqual(self.get(cookie=self.super_cookie()).status_code, 404)
        self.assertEqual(self.post({"preset": "main"}, cookie=self.super_cookie()).status_code, 404)

    def test_a_press_switches_confirms_and_is_logged_with_the_super_users_name_and_the_site_as_the_way_in(self):
        reply = self.post({"preset": "main"}, cookie=self.super_cookie())
        self.assertEqual((reply.status_code, reply.content["ok"], reply.content["state"]), (200, True, "done"))
        self.assertEqual(self.fake.sent, ["2v5."])
        self.assertEqual(reply.content["status"]["active"], "main")
        row = self.log_rows()[0]
        self.assertEqual((row["operator"], row["preset"], row["result"], row["via"]), (WHO, "main", "done", "core-site"))
        self.assertEqual(row["detail"]["from"], "peer:203.0.113.5")                    # who really asked, as the server saw it
        self.assertEqual((reply.content["status"]["last_success"]["operator"], reply.content["status"]["last_success"]["preset"]), (WHO, "main"))
        self.assertEqual(self.peer_calls, [])

    def test_a_press_must_come_from_the_page_itself(self):
        self.assertEqual(self.post({"preset": "main"}, cookie=self.super_cookie(), same_site=False).status_code, 403)
        self.assertEqual((self.fake.sent, self.log_rows()), ([], []))
        self.assertEqual(self.post({"preset": "main"}, key=KEY, same_site=False).status_code, 200)     # the PC tool carries the key and no ambient credential

    def test_the_browser_cannot_choose_the_operator_the_way_in_or_a_command(self):
        for payload in ({"preset": "main", "operator": "someone else"}, {"preset": "main", "via": "control-key"}, {"preset": "2v1."}, {"preset": "main", "command": "5All."}, {}, {"preset": "nope"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.post(payload, cookie=self.super_cookie()).status_code, 400)
        self.assertEqual((self.fake.sent, self.fake.reads, self.log_rows()), ([], 0, []))

    def test_the_emergency_preset_needs_its_confirmation_and_main_and_backup_do_not(self):
        for wrong in ({"preset": "brightsign"}, {"preset": "brightsign", "confirm": "true"}, {"preset": "brightsign", "confirm": False}):
            self.assertEqual(self.post(wrong, cookie=self.super_cookie()).status_code, 400)
        self.assertEqual((self.fake.sent, self.log_rows()), ([], []))
        accepted = self.post({"preset": "brightsign", "confirm": True}, cookie=self.super_cookie())
        self.assertEqual((accepted.status_code, accepted.content["state"]), (200, "done"))
        self.assertEqual(self.fake.sent, ["1v1,2,3,4,5,6."])
        self.assertEqual(self.post({"preset": "main"}, cookie=self.super_cookie()).status_code, 200)

    def test_a_press_while_another_runs_is_a_conflict_sends_nothing_and_is_not_logged(self):
        self.assertTrue(listener.MATRIX_SWITCHER._lock.acquire(blocking=False))
        try:
            reply = self.post({"preset": "main"}, cookie=self.super_cookie())
        finally:
            listener.MATRIX_SWITCHER._lock.release()
        self.assertEqual((reply.status_code, reply.content["state"]), (409, "busy"))
        self.assertEqual((self.fake.sent, self.log_rows()), ([], []))

    def test_a_failed_switch_is_logged_with_the_way_in_and_mails_the_super_user(self):
        self.fake.ignore = lambda text: True
        reply = self.post({"preset": "backup"}, cookie=self.super_cookie())
        self.assertEqual(reply.content["state"], "failed")
        newest = self.log_rows()[0]
        self.assertEqual((newest["result"], newest["via"], newest["operator"]), ("failed", "core-site", WHO))
        self.assertEqual(len(tcw.FakeSmtp.sent), 1)

    def test_the_browser_endpoints_have_the_matrix_rate_limit(self):
        listener._matrix_rate = listener.VisitorRate(3)
        codes = [self.get(cookie=self.super_cookie()).status_code for _ in range(5)]
        self.assertEqual(codes, [200, 200, 200, 429, 429])


class StandbyBrowserTests(Base):
    """The endpoints the browser uses on the standby, which relay to Core. Allowed: the super user on any address, and a registered office PC (the staff door) with no sign-in."""

    CORE_STATUS = {"enabled": True, "reachable": True, "switching": False, "routing": list(fx.LIVE_ROUTING), "active": None, "outputs": [], "presets": [], "last_success": None, "recent": [], "error": None}

    def setUp(self):
        super().setUp()
        self.make_standby(lambda method, path, payload: (200, dict(self.CORE_STATUS)) if method == "GET" else (200, {"ok": True, "state": "done", "message": "MAIN PC is active."}))

    def get(self, **kwargs):
        return run(listener.matrix_panel_status(request(**kwargs)))

    def post(self, payload, **kwargs):
        return run(listener.matrix_panel_switch(request(payload, **kwargs)))

    def test_the_super_user_on_the_staff_door_gets_cores_status_through_the_relay(self):
        reply = self.get(cookie=self.super_cookie())
        self.assertEqual(reply.status_code, 200)
        self.assertEqual((reply.content["core_reached"], reply.content["routing"]), (True, list(fx.LIVE_ROUTING)))
        self.assertEqual(self.peer_calls[0][:2], ("GET", "/control/matrix"))

    def test_a_forced_refresh_asks_core_to_read_the_matrix_again(self):
        req = request(cookie=self.super_cookie())
        req.query_params = {"fresh": "1"}
        run(listener.matrix_panel_status(req))
        self.assertEqual(self.peer_calls[0][1], "/control/matrix?fresh=1")

    def test_the_super_user_is_relayed_on_every_address_of_the_standby_not_only_the_door(self):
        for door in (DOOR, MAIN_DOOR, None):
            with self.subTest(door=door):
                self.peer_calls.clear()
                self.assertEqual(self.get(cookie=self.super_cookie(), door=door).status_code, 200)
                self.assertEqual(self.post({"preset": "main"}, cookie=self.super_cookie(), door=door).status_code, 200)
                way = "staff-door" if door == DOOR else "standby-site"
                self.assertEqual(self.peer_calls[-1][2], {"preset": "main", "confirm": False, "operator": WHO, "via": way})

    def test_an_office_pc_on_the_door_needs_no_sign_in_at_all_and_is_logged_as_the_office_pc(self):
        reply = self.get()                                                             # the door, no cookie of any kind: the certificate was the proof
        self.assertEqual((reply.status_code, reply.content["core_reached"]), (200, True))
        self.assertEqual(self.peer_calls[0][:2], ("GET", "/control/matrix"))
        press = self.post({"preset": "main"})
        self.assertEqual((press.status_code, press.content["state"]), (200, "done"))
        self.assertEqual(self.peer_calls[-1][2], {"preset": "main", "confirm": False, "operator": "office PC", "via": "office-pc"})

    def test_a_staff_member_on_an_office_pc_may_press_and_is_named_in_the_log(self):
        async def ada(request):
            return "Ada Lovelace"
        with mock.patch.object(listener, "_staff_display_of", ada):                    # the session check itself is proven in test_staff_wiring.py
            press = self.post({"preset": "main"}, cookie=f"{listener.STAFF_COOKIE}=whatever-the-door-verifies")
        self.assertEqual(press.status_code, 200)
        self.assertEqual(self.peer_calls[-1][2], {"preset": "main", "confirm": False, "operator": "Ada Lovelace", "via": "office-pc"})
        self.peer_calls.clear()
        self.post({"preset": "main"}, cookie=f"{listener.STAFF_COOKIE}=a-cookie-the-door-does-not-verify")     # an unverified staff cookie is no session: still an office PC press, nobody named
        self.assertEqual(self.peer_calls[-1][2]["operator"], "office PC")

    def test_a_staff_name_is_cleaned_and_cut_so_that_core_never_refuses_a_press_over_it(self):
        # Core refuses an operator name of 0 or more than 80 characters, and a line break in a name would break a line of the log or of the mail.
        async def messy(request):
            return "Ada\r\nLovelace" + "x" * 200

        async def blank(request):
            return "  \r\n  "
        with mock.patch.object(listener, "_staff_display_of", messy):
            self.post({"preset": "main"}, cookie=f"{listener.STAFF_COOKIE}=whatever-the-door-verifies")
        operator = self.peer_calls[-1][2]["operator"]
        self.assertEqual((len(operator), "\r" in operator, "\n" in operator, operator.startswith("AdaLovelace")), (80, False, False, True))
        with mock.patch.object(listener, "_staff_display_of", blank):
            self.post({"preset": "main"}, cookie=f"{listener.STAFF_COOKIE}=whatever-the-door-verifies")
        self.assertEqual(self.peer_calls[-1][2]["operator"], "office PC")               # a name that cleans down to nothing is no name

    def test_the_super_session_wins_over_the_door_alone_so_the_super_user_is_named(self):
        self.post({"preset": "main"}, cookie=self.super_cookie())
        self.assertEqual((self.peer_calls[-1][2]["operator"], self.peer_calls[-1][2]["via"]), (WHO, "staff-door"))

    def test_off_the_door_without_a_super_session_everyone_gets_not_found_and_core_is_not_asked(self):
        everyone_else = (("no session", {}), ("a staff account on a PC that is not an office PC", {"cookie": f"{listener.STAFF_COOKIE}=anything-a-staff-session-could-hold"}),
                         ("a forged super session", {"cookie": f"{listener.CONTROL_COOKIE}=forged.value"}), ("a wrong key", {"key": "x" * 40}))
        for label, kwargs in everyone_else:
            for door in (MAIN_DOOR, None):
                with self.subTest(label=label, door=door):
                    self.assertEqual(self.get(**kwargs, door=door).status_code, 404)
                    self.assertEqual(self.post({"preset": "main"}, **kwargs, door=door).status_code, 404)
        self.assertEqual(self.peer_calls, [])

    def test_core_has_no_office_pc_rule_even_on_a_request_that_carries_the_doors_port_number(self):
        listener.IS_STANDBY, listener.NODE = False, control.CORE
        listener.MATRIX_SWITCHER = matrix.Switcher(self.fake, sleep=lambda seconds: None)
        for door in (DOOR, MAIN_DOOR):
            with self.subTest(door=door):
                self.assertEqual(self.get(door=door).status_code, 404)
                self.assertEqual(self.post({"preset": "main"}, door=door).status_code, 404)
        self.assertEqual((self.fake.reads, self.fake.sent, self.peer_calls), (0, [], []))

    def test_a_press_must_come_from_the_page_itself_the_certificate_is_an_ambient_credential_too(self):
        self.assertEqual(self.post({"preset": "main"}, cookie=self.super_cookie(), same_site=False).status_code, 403)
        self.assertEqual(self.post({"preset": "main"}, same_site=False).status_code, 403)       # an office PC's browser shows its certificate to any page, so a forged cross-site press must fail
        self.assertEqual(self.peer_calls, [])
        ok = self.post({"preset": "main"}, key=KEY, same_site=False)                    # the PC tool carries the key and no ambient credential
        self.assertEqual(ok.status_code, 200)

    def test_a_press_is_relayed_with_the_super_users_name_and_the_door_as_the_way_in(self):
        reply = self.post({"preset": "main"}, cookie=self.super_cookie())
        self.assertEqual((reply.status_code, reply.content["state"]), (200, "done"))
        method, path, payload, timeout = self.peer_calls[0]
        self.assertEqual((method, path), ("POST", "/control/matrix/switch"))
        self.assertEqual(payload, {"preset": "main", "confirm": False, "operator": WHO, "via": "staff-door"})
        self.assertGreaterEqual(timeout, 20)                                              # a switch can take about twelve seconds in the worst case

    def test_the_browser_cannot_choose_the_operator_the_way_in_or_a_command(self):
        for payload in ({"preset": "main", "operator": "someone else"}, {"preset": "main", "via": "control-key"}, {"preset": "2v1."}, {"preset": "main", "command": "5All."}, {}, {"preset": "nope"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.post(payload, cookie=self.super_cookie()).status_code, 400)
        self.assertEqual(self.peer_calls, [])

    def test_the_emergency_confirmation_is_checked_here_before_core_is_asked(self):
        self.assertEqual(self.post({"preset": "brightsign"}, cookie=self.super_cookie()).status_code, 400)
        self.assertEqual(self.post({"preset": "brightsign", "confirm": "true"}, cookie=self.super_cookie()).status_code, 400)
        self.assertEqual(self.post({"preset": "brightsign"}).status_code, 400)                  # an office PC with no sign-in is asked the same question
        self.assertEqual(self.peer_calls, [])
        self.post({"preset": "brightsign", "confirm": True}, cookie=self.super_cookie())
        self.assertEqual(self.peer_calls[0][2]["confirm"], True)

    def test_when_core_cannot_be_reached_the_page_is_told_so_and_nothing_is_assumed(self):
        self.make_standby(lambda method, path, payload: (None, {}))
        status = self.get(cookie=self.super_cookie())
        self.assertEqual((status.status_code, status.content["core_reached"]), (200, False))
        self.assertNotIn("routing", status.content)
        press = self.post({"preset": "main"}, cookie=self.super_cookie())
        self.assertEqual((press.status_code, press.content["ok"], press.content["state"]), (200, False, "core_unreachable"))
        self.assertIn("may or may not have switched", press.content["message"])           # a timeout after the datagrams left is possible, so the page must not say nothing happened

    def test_cores_busy_and_not_set_answers_pass_through_and_a_refusal_is_explained(self):
        self.make_standby(lambda method, path, payload: (409, {"ok": False, "state": "busy", "message": "A switch is already running."}))
        busy = self.post({"preset": "main"}, cookie=self.super_cookie())
        self.assertEqual((busy.status_code, busy.content["state"]), (409, "busy"))
        self.make_standby(lambda method, path, payload: (401, {}))
        refused = self.post({"preset": "main"}, cookie=self.super_cookie())
        self.assertEqual((refused.status_code, refused.content["ok"], refused.content["state"]), (200, False, "core_refused"))
        self.make_standby(lambda method, path, payload: (404, {}))
        absent = self.get(cookie=self.super_cookie())
        self.assertEqual((absent.status_code, absent.content["core_reached"], absent.content.get("configured")), (200, True, False))

    def test_the_standby_has_the_same_separate_rate_limit(self):
        listener._matrix_rate = listener.VisitorRate(3)
        codes = [self.get(cookie=self.super_cookie()).status_code for _ in range(5)]
        self.assertEqual(codes, [200, 200, 200, 429, 429])


class OfficePassBase(Base):
    def now(self):
        return datetime.now(timezone.utc)

    def token(self, key=None, seconds=3600, ago=0):
        return access.issue_pass(key or KEY, self.now() - timedelta(seconds=ago), seconds)

    def pass_cookie(self, **kwargs):
        return f"{listener.OFFICE_PASS_COOKIE}={self.token(**kwargs)}"

    def get(self, **kwargs):
        return run(listener.matrix_panel_status(request(**kwargs)))

    def post(self, payload, **kwargs):
        return run(listener.matrix_panel_switch(request(payload, **kwargs)))


class OfficePassDomainTests(unittest.TestCase):
    """The pass is shared between the staff address and the watchdog address through their common parent domain, worked out from the two addresses the listener already knows."""

    def test_the_parent_domain_is_the_part_both_addresses_share(self):
        cases = (("https://staff.corp.example/staff", "https://watchdog.corp.example/", "corp.example"),
                 ("https://staff.corp.example:8443/staff", "https://WATCHDOG.corp.example/", "corp.example"),
                 ("https://a.b.corp.example/", "https://c.corp.example/", "corp.example"),
                 ("https://staff.example.org/staff", "https://watchdog.corp.example/", ""),            # two unrelated domains share nothing
                 ("https://staff.example.com/staff", "https://watchdog.corp.example/", ""),            # sharing only ".com" is sharing nothing: no cookie may be set for a public ending
                 ("https://10.0.0.5/staff", "https://10.0.0.6/", ""),                                    # addresses of machines, never a cookie domain
                 ("https://10.0.0.5/staff", "https://11.0.0.5/", ""),                                    # even when the two addresses end in the same numbers
                 ("https://sta_ff.corp.example/staff", "https://watchdog.corp.example/", ""),        # a host name that is not a plain name fails closed
                 ("https://localhost/staff", "https://localhost/", ""),
                 ("", "https://watchdog.corp.example/", ""))
        for staff_url, dashboard_url, expected in cases:
            with self.subTest(staff=staff_url, dashboard=dashboard_url):
                self.assertEqual(listener._office_pass_domain(staff_url, dashboard_url), expected)

    def test_the_defaults_give_clarixtech_com(self):
        self.assertEqual(listener._office_pass_domain(), "corp.example")


class OfficePassIssueTests(OfficePassBase):
    """The STANDBY hands a pass to every browser that arrives on the staff door (so it showed a listed certificate), on ANY page the door serves, signed in or not: an office PC that only ever
    uses the Staff page must still be recognised on the watchdog address (his report, 2026-11-10). Nothing else ever gets one."""

    def setUp(self):
        super().setUp()
        self.make_standby(lambda method, path, payload: (None, {}))

    def serve(self, path="/staff", server=("127.0.0.1", DOOR), inner_headers=(), status=200, scheme="https", extra=()):
        """The messages an answer would send, run through the issuer exactly as the server runs it."""
        sent = []

        async def inner(scope, receive, send):
            await send({"type": "http.response.start", "status": status, "headers": list(inner_headers)})
            await send({"type": "http.response.body", "body": b"ok"})

        async def send(message):
            sent.append(message)
        scope = {"type": "http", "method": "GET", "path": path, "server": server, "client": ("127.0.0.1", 5555), "scheme": scheme, "headers": list(extra)}
        run(listener.OfficePassIssuer(inner)(scope, None, send))
        return sent

    def cookies(self, sent):
        return [value.decode("latin-1") for name, value in sent[0]["headers"] if name == b"set-cookie" and value.startswith(listener.OFFICE_PASS_COOKIE.encode())]

    def test_every_page_the_door_serves_carries_a_pass_signed_in_or_not(self):
        for path in ("/", "/staff", "/control/login", "/control/panel/matrix", "/stream", "/health", "/anything-at-all"):
            with self.subTest(path=path):
                found = self.cookies(self.serve(path))
                self.assertEqual(len(found), 1)
                header = found[0]
                for attribute in ("Domain=corp.example", "Path=/control/panel/matrix", "HttpOnly", "SameSite=Strict", "Max-Age=259200", "Secure"):
                    self.assertIn(attribute, header)
                token = header.split(";")[0].split("=", 1)[1]
                self.assertTrue(access.verify_pass(KEY, token, self.now()))
                self.assertTrue(access.verify_pass(KEY, token, self.now() + timedelta(hours=71)))
                self.assertFalse(access.verify_pass(KEY, token, self.now() + timedelta(hours=73)))

    def test_a_refused_or_redirected_page_carries_it_too_the_pass_proves_the_certificate_not_the_page(self):
        self.assertEqual(len(self.cookies(self.serve("/nothing-here", status=404))), 1)
        self.assertEqual(len(self.cookies(self.serve("/staff", status=302))), 1)

    def test_the_answers_own_headers_and_other_cookies_are_kept_and_the_body_is_untouched(self):
        sent = self.serve("/staff", inner_headers=[(b"content-type", b"text/html"), (b"set-cookie", b"watchdog_staff=abc; HttpOnly")])
        names = [name for name, value in sent[0]["headers"]]
        self.assertEqual(names.count(b"set-cookie"), 2)
        self.assertIn((b"content-type", b"text/html"), sent[0]["headers"])
        self.assertIn((b"set-cookie", b"watchdog_staff=abc; HttpOnly"), sent[0]["headers"])
        self.assertEqual((sent[0]["status"], sent[1]), (200, {"type": "http.response.body", "body": b"ok"}))

    def test_nothing_off_the_door_ever_gets_a_pass(self):
        for server in (("127.0.0.1", MAIN_DOOR), None, ("127.0.0.1", 8000), ("127.0.0.1", "8421"), ("127.0.0.1", True)):
            with self.subTest(server=server):
                self.assertEqual(self.cookies(self.serve(server=server)), [])
        # not even a super session, a pass or the key earns a pass off the door: only the certificate at the door does
        for extra in ([(b"cookie", self.super_cookie().encode())], [(b"cookie", self.pass_cookie().encode())], [(b"x-watchdog-control", KEY.encode())]):
            self.assertEqual(self.cookies(self.serve(server=("127.0.0.1", MAIN_DOOR), extra=extra)), [])

    def test_core_has_no_staff_door_so_it_issues_nothing(self):
        listener.IS_STANDBY, listener.NODE = False, control.CORE
        self.assertEqual(self.cookies(self.serve()), [])

    def test_only_http_traffic_is_touched(self):
        reached = []

        async def inner(scope, receive, send):
            reached.append(scope["type"])
        for kind in ("lifespan", "websocket"):
            run(listener.OfficePassIssuer(inner)({"type": kind, "server": ("127.0.0.1", DOOR)}, None, None))
        self.assertEqual(reached, ["lifespan", "websocket"])

    def test_the_off_switch_stops_the_pass_being_issued(self):
        listener.OFFICE_PASS_ON = False
        sent = self.serve()
        self.assertEqual((sent[0]["status"], self.cookies(sent)), (200, []))               # the door itself still works

    def test_without_a_shared_parent_domain_no_pass_is_issued(self):
        listener.STAFF_URL = "https://staff.example.org/staff"
        self.assertEqual(self.cookies(self.serve()), [])

    def test_without_a_control_key_no_pass_is_issued(self):
        listener.CONTROL_KEY = ""
        self.assertEqual(self.cookies(self.serve()), [])

    def test_the_pass_is_not_marked_secure_unless_the_scheme_says_so(self):
        original = listener.request_scheme
        listener.request_scheme = lambda req: "http"
        try:
            self.assertNotIn("Secure", self.cookies(self.serve())[0])
        finally:
            listener.request_scheme = original

    def test_behind_caddy_the_scheme_comes_from_its_forwarded_header_so_the_pass_is_secure_in_production(self):
        # On the VPS the listener itself only sees plain http from Caddy on the loopback; Caddy says "https" in X-Forwarded-Proto and only that trusted proxy may say so.
        listener.FORWARDED_TRUST = listener.parse_forwarded_trust("127.0.0.1")
        trusted = self.cookies(self.serve(scheme="http", extra=[(b"x-forwarded-proto", b"https")]))
        self.assertIn("Secure", trusted[0])
        untrusted_peer = [(b"x-forwarded-proto", b"https")]
        sent = []

        async def inner(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})

        async def send(message):
            sent.append(message)
        scope = {"type": "http", "method": "GET", "path": "/", "server": ("127.0.0.1", DOOR), "client": ("10.9.9.9", 5555), "scheme": "http", "headers": untrusted_peer}
        run(listener.OfficePassIssuer(inner)(scope, None, send))
        self.assertNotIn("Secure", self.cookies(sent)[0])                                    # a peer that is not the trusted proxy cannot claim the connection was encrypted

    def test_the_matrix_answers_no_longer_carry_their_own_copy(self):
        self.make_standby(lambda method, path, payload: (200, {"enabled": True, "reachable": True, "routing": list(fx.LIVE_ROUTING), "presets": [], "last_success": None, "recent": []}))
        self.assertIsNone(self.get().headers)                                              # one place issues the pass: the issuer, not every endpoint


class OfficePassOnCoreTests(OfficePassBase):
    """The WATCHDOG address (Core itself): a valid pass makes the browser an office PC there, with the same limits as on the door."""

    def setUp(self):
        super().setUp()
        listener.IS_STANDBY, listener.NODE = False, control.CORE

        def never(method, path, payload=None, timeout=10):
            self.peer_calls.append((method, path, payload, timeout))
            raise AssertionError("Core must never ask another node about its own matrix")
        listener._peer_request = never

    def test_a_valid_pass_reads_the_matrix_directly(self):
        reply = self.get(cookie=self.pass_cookie(), door=MAIN_DOOR)
        self.assertEqual(reply.status_code, 200)
        self.assertEqual((reply.content["core_reached"], reply.content["routing"]), (True, list(fx.LIVE_ROUTING)))
        self.assertEqual(self.peer_calls, [])

    def test_a_valid_pass_presses_and_is_logged_as_an_office_pc_by_pass(self):
        reply = self.post({"preset": "main"}, cookie=self.pass_cookie(), door=MAIN_DOOR)
        self.assertEqual((reply.status_code, reply.content["state"]), (200, "done"))
        self.assertEqual(self.fake.sent, ["2v5."])
        row = self.log_rows()[0]
        self.assertEqual((row["operator"], row["via"], row["result"]), ("office PC", "office-pass", "done"))

    def test_every_bad_pass_gets_not_found_and_nothing_is_read_or_sent(self):
        bad = (("expired", self.pass_cookie(ago=7200)), ("signed with another key", self.pass_cookie(key="z" * 40)),
               ("a login session in the pass cookie", f"{listener.OFFICE_PASS_COOKIE}={access.issue_session(KEY, self.now(), 3600, listener._session_generation())}"),
               ("rubbish", f"{listener.OFFICE_PASS_COOKIE}=not.a.pass"), ("empty", f"{listener.OFFICE_PASS_COOKIE}="),
               ("a pass under another cookie name", f"watchdog_other={self.token()}"))
        for label, cookie in bad:
            for door in (MAIN_DOOR, None):
                with self.subTest(label=label, door=door):
                    self.assertEqual(self.get(cookie=cookie, door=door).status_code, 404)
                    self.assertEqual(self.post({"preset": "main"}, cookie=cookie, door=door).status_code, 404)
        self.assertEqual((self.fake.reads, self.fake.sent, self.log_rows(), self.peer_calls), (0, [], [], []))

    def test_a_pass_is_not_a_login_session_anywhere_else(self):
        reply = run(listener.control_panel_state(request(cookie=self.pass_cookie(), door=MAIN_DOOR)))
        self.assertEqual(reply.status_code, 401)

    def test_a_press_by_pass_still_must_come_from_the_page_itself(self):
        self.assertEqual(self.post({"preset": "main"}, cookie=self.pass_cookie(), same_site=False).status_code, 403)    # a cookie is an ambient credential too
        self.assertEqual((self.fake.sent, self.log_rows()), ([], []))

    def test_the_emergency_question_and_the_name_only_rule_still_hold(self):
        for payload in ({"preset": "brightsign"}, {"preset": "main", "operator": "someone else"}, {"preset": "main", "via": "control-key"}, {"preset": "2v1."}):
            with self.subTest(payload=payload):
                self.assertEqual(self.post(payload, cookie=self.pass_cookie()).status_code, 400)
        self.assertEqual(self.fake.sent, [])

    def test_the_off_switch_makes_every_pass_worthless(self):
        listener.OFFICE_PASS_ON = False
        self.assertEqual(self.get(cookie=self.pass_cookie()).status_code, 404)
        self.assertEqual(self.post({"preset": "main"}, cookie=self.pass_cookie()).status_code, 404)

    def test_without_a_shared_parent_domain_a_pass_is_not_accepted_either(self):
        listener.DASHBOARD_URL = "https://watchdog.example.org/"
        self.assertEqual(self.get(cookie=self.pass_cookie()).status_code, 404)

    def test_a_node_without_a_matrix_says_not_found_even_to_a_pass(self):
        listener.MATRIX_SWITCHER = None
        self.assertEqual(self.get(cookie=self.pass_cookie()).status_code, 404)


class OfficePassOnStandbyPublicTests(OfficePassBase):
    """The standby's own public address accepts a pass too and relays the press, with its own way-in word."""

    def setUp(self):
        super().setUp()
        self.make_standby(lambda method, path, payload: (200, {"enabled": True, "reachable": True, "routing": list(fx.LIVE_ROUTING), "presets": [], "last_success": None, "recent": []}) if method == "GET" else (200, {"ok": True, "state": "done"}))

    def test_a_valid_pass_is_relayed_with_the_way_in_office_pass(self):
        self.assertEqual(self.get(cookie=self.pass_cookie(), door=MAIN_DOOR).status_code, 200)
        reply = self.post({"preset": "main"}, cookie=self.pass_cookie(), door=MAIN_DOOR)
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(self.peer_calls[-1][2], {"preset": "main", "confirm": False, "operator": "office PC", "via": "office-pass"})

    def test_a_bad_pass_is_not_found_and_core_is_not_asked(self):
        for cookie in (self.pass_cookie(ago=7200), self.pass_cookie(key="z" * 40), f"{listener.OFFICE_PASS_COOKIE}=junk"):
            self.assertEqual(self.get(cookie=cookie, door=MAIN_DOOR).status_code, 404)
            self.assertEqual(self.post({"preset": "main"}, cookie=cookie, door=MAIN_DOOR).status_code, 404)
        self.assertEqual(self.peer_calls, [])

    def test_the_door_still_wins_over_a_pass_so_a_signed_in_staff_member_is_named(self):
        async def ada(request):
            return "Ada Lovelace"
        with mock.patch.object(listener, "_staff_display_of", ada):
            self.post({"preset": "main"}, cookie=f"{listener.STAFF_COOKIE}=x; {self.pass_cookie()}")
        self.assertEqual((self.peer_calls[-1][2]["operator"], self.peer_calls[-1][2]["via"]), ("Ada Lovelace", "office-pc"))


class PanelStateTests(Base):
    """The panel's own data says whether the card may be shown; the server refuses the card's endpoints either way."""

    def state(self, door):
        return run(listener.control_panel_state(request(cookie=self.super_cookie(), door=door))).content

    def test_the_card_is_offered_to_the_super_user_wherever_something_can_answer(self):
        self.make_standby(lambda method, path, payload: (None, {}))
        for door in (DOOR, MAIN_DOOR, None):                                          # any address of the standby: it relays to Core
            self.assertEqual(self.state(door)["matrix"], {"available": True}, door)
        listener.IS_STANDBY, listener.NODE = False, control.CORE
        listener.MATRIX_SWITCHER = matrix.Switcher(self.fake, sleep=lambda seconds: None)
        self.assertEqual(self.state(MAIN_DOOR)["matrix"], {"available": True})        # Core with a matrix attached answers directly
        listener.MATRIX_SWITCHER = None
        self.assertEqual(self.state(MAIN_DOOR)["matrix"], {"available": False})       # Core with no matrix has nothing to show


class SettingsWiringTests(unittest.TestCase):
    def test_the_listener_builds_its_matrix_only_from_the_settings_and_a_bad_one_leaves_it_off(self):
        source = (Path(__file__).resolve().parent.parent / "core-listener" / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("matrix.settings_from_environ(os.environ)", source)
        self.assertNotIn("request.query_params.get(\"host\"", source)

    def test_the_pass_issuer_is_registered_between_the_gate_and_the_https_policy(self):
        # Added later means outermost: the gate first (innermost), then the issuer (so even the gate's own answers carry the pass), then the HTTPS policy (so every answer gets the security headers).
        source = (Path(__file__).resolve().parent.parent / "core-listener" / "app" / "main.py").read_text(encoding="utf-8")
        gate, issuer, policy = (source.index(f"app.add_middleware({name})") for name in ("ControlGate", "OfficePassIssuer", "HttpsPolicy"))
        self.assertLess(gate, issuer)
        self.assertLess(issuer, policy)
        self.assertEqual(source.count("app.add_middleware(OfficePassIssuer)"), 1)

    def test_the_log_table_is_created_with_the_others(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            saved = listener.DB_PATH
            listener.DB_PATH = Path(folder) / "t.db"
            try:
                listener.init_db()
                with listener.db() as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM matrix_log").fetchone()[0], 0)
            finally:
                listener.DB_PATH = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
