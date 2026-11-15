# File:         test_control_wiring.py
# Purpose:      Tests of how core-listener/app/main.py USES the control phase: who may alert (preferred node, fallback), where alerts go while access is OFF, the
#               stored state, the one-mail rule, the OFF gate in front of every request, and the control door's handlers.
# Inputs:       None (a throwaway database, fake mail and push, made-up requests). Nothing leaves this machine.
# Outputs:      Printed unittest results; exit code 1 if anything fails.
# Dependencies: core-listener/app/main.py (loaded with the same FastAPI stand-in as ops/test_listener_units.py) and control.py / control_access.py.
"""Run: python ops/test_control_wiring.py

What is proven here, in plain words: with the control phase off the listener behaves exactly as before; with it on, the preferred node alerts and the other one
steps in only when it is down or the two disagree; while access is OFF alerts reach only the super user and every request but the system's own is refused
(the super user excepted); a change is stored before it takes effect and survives a restart; the offline and back mails are sent once each, retried if the mail
server is down, and never twice; a damaged control key can never lock a node.
"""
import asyncio
import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))
import test_listener_units as base  # noqa: E402,F401 -- installs the FastAPI stand-in the listener is loaded with

KEY = "control-key-for-tests-0123456789abcdef"
os.environ.update({"WATCHDOG_CONTROL_KEY": KEY, "WATCHDOG_PEER_URL": "http://127.0.0.1:9/health"})
sys.modules.pop("app.main", None)
listener = importlib.import_module("app.main")
control = importlib.import_module("app.control")
access = importlib.import_module("app.control_access")
standby = importlib.import_module("app.standby")
login = importlib.import_module("app.control_login")

T0 = datetime(2026, 10, 25, 14, 30, 0, tzinfo=timezone.utc)
H, V = control.CORE, control.VPS
SETTINGS = ("SUPER_USER", "SUPER_PASSWORD_HASH", "_peer_request", "_login_throttle", "_login_slots", "_control_rate", "SMTP_HOST", "SMTP_USER", "SMTP_PASS", "ALERT_EMAIL_FROM", "ALERT_EMAIL_TO", "SUPER_EMAIL_TO", "CONTROL_NOTICE_TO", "PUSH_URL", "SUPER_PUSH_URL",
            "IS_STANDBY", "NODE", "PRIMARY_URL", "CONTROL_KEY", "WATCH", "DB_PATH", "ROLE")


def run(coroutine):
    return asyncio.run(coroutine)


class FakeSmtp:
    sent: list = []
    down = False

    def __init__(self, host, port, timeout=None):
        if FakeSmtp.down:
            raise OSError("mail server down")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        pass

    def sendmail(self, sender, recipients, text):
        FakeSmtp.sent.append((list(recipients), text))


class FakeRequest:
    def __init__(self, headers=None, body=b"", host="203.0.113.5", scheme="https"):
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}
        self._body = body
        self.client = types.SimpleNamespace(host=host)
        self.scope = {"scheme": scheme}

    async def stream(self):
        yield self._body

    async def is_disconnected(self):
        return False


class Wiring(unittest.TestCase):
    """A fresh Core-shaped listener with fake mail and push for every test."""

    def setUp(self):
        self.saved = {name: getattr(listener, name) for name in SETTINGS}
        self.saved_smtp, self.saved_urlopen = listener.smtplib.SMTP, listener.urllib.request.urlopen
        self.tmp = tempfile.TemporaryDirectory()
        listener.DB_PATH = Path(self.tmp.name) / "t.db"
        listener.init_db()
        listener.SMTP_HOST, listener.SMTP_USER, listener.SMTP_PASS, listener.ALERT_EMAIL_FROM = "smtp.test", "user", "pass", "from@x.test"
        listener.ALERT_EMAIL_TO, listener.SUPER_EMAIL_TO, listener.CONTROL_NOTICE_TO = "users@x.test", "super@x.test", "support@x.test"
        listener.PUSH_URL, listener.SUPER_PUSH_URL = "http://127.0.0.1:1/shared", "http://127.0.0.1:1/super"
        listener.CONTROL_KEY, listener.IS_STANDBY, listener.NODE, listener.PRIMARY_URL, listener.ROLE = KEY, False, H, "http://127.0.0.1:9/health", "primary"
        listener.WATCH = standby.CoreWatch(listener.WATCH_CONFIG, T0, proven_at=T0)
        listener.CONTROL["state"], listener.CONTROL["problems"] = control.ControlState(), ()
        listener.CONTROL["session_gen"] = ""                                 # no earlier test may leave a session generation behind
        listener.PEER["preferred"] = None
        listener._notice_warned["done"] = False
        listener._control_rate._seen.clear()
        FakeSmtp.sent, FakeSmtp.down = [], False
        self.pushes = []
        listener.smtplib.SMTP = FakeSmtp
        listener.urllib.request.urlopen = self.fake_urlopen

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(listener, name, value)
        listener.smtplib.SMTP, listener.urllib.request.urlopen = self.saved_smtp, self.saved_urlopen
        self.tmp.cleanup()

    def fake_urlopen(self, request, timeout=None):
        self.pushes.append(request.full_url)
        return _Ctx()

    def as_standby(self):
        listener.IS_STANDBY, listener.NODE, listener.ROLE = True, V, "standby"

    def prefer(self, node):
        listener.CONTROL["state"] = control.set_preferred(listener.CONTROL["state"], node, T0).state

    def switch_off(self, notify=True):
        return run(listener.apply_control_command({"access": "off"}, notify, T0))

    def peer_down(self):
        for i in range(3):
            listener.WATCH.record_probe(False, T0 + timedelta(seconds=30 * i), "refused")
        self.assertEqual(listener.WATCH.state, standby.DOWN)


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DeliveryGateTests(Wiring):
    def test_core_preferred_always_alerts(self):
        self.assertTrue(listener._delivery_allowed())
        self.peer_down()
        self.assertTrue(listener._delivery_allowed())

    def test_core_steps_aside_for_a_preferred_vps_until_the_vps_is_down(self):
        self.prefer(V)
        self.assertFalse(listener._delivery_allowed())
        self.peer_down()
        self.assertTrue(listener._delivery_allowed())

    def test_core_with_no_address_for_the_vps_never_steps_aside(self):
        self.prefer(V)
        listener.PRIMARY_URL = ""
        self.assertTrue(listener._delivery_allowed())

    def test_the_vps_is_quiet_while_core_is_up_and_takes_over_when_it_is_down(self):
        self.as_standby()
        self.assertFalse(listener._delivery_allowed())
        self.peer_down()
        self.assertTrue(listener._delivery_allowed())

    def test_the_vps_alerts_when_it_is_the_preferred_node(self):
        self.as_standby()
        self.prefer(V)
        self.assertTrue(listener._delivery_allowed())

    def test_the_vps_alerts_when_core_says_a_different_node_is_preferred(self):
        self.as_standby()
        listener.PEER["preferred"] = V                    # core thinks the VPS is preferred, the VPS thinks Core is
        self.assertTrue(listener._delivery_allowed())

    def test_the_vps_stays_quiet_when_core_agrees_or_does_not_say(self):
        self.as_standby()
        for said in (H, None):
            listener.PEER["preferred"] = said
            self.assertFalse(listener._delivery_allowed())

    def test_without_a_control_key_everything_is_as_it_was_whatever_the_stored_state_says(self):
        listener.CONTROL_KEY = ""
        self.prefer(V)
        self.assertTrue(listener._delivery_allowed())      # core
        self.as_standby()
        self.assertFalse(listener._delivery_allowed())     # a standby while core is up
        self.peer_down()
        self.assertTrue(listener._delivery_allowed())


class RoutingTests(Wiring):
    def mail(self):
        listener.send_alert_email("MAIN", "critical", "MAIN is down", [("Reason", "no heartbeat")])
        return [recipients for recipients, _ in FakeSmtp.sent]

    def push(self):
        self.pushes.clear()
        listener.send_push_notice("MAIN down", "MAIN is down")
        return list(self.pushes)

    def test_while_access_is_on_alerts_go_to_the_normal_recipients(self):
        self.assertEqual(self.mail(), [["users@x.test"]])
        self.assertEqual(self.push(), ["http://127.0.0.1:1/shared"])

    def test_while_access_is_off_alerts_go_to_the_super_user_only(self):
        self.switch_off()
        FakeSmtp.sent.clear()
        self.assertEqual(self.mail(), [["super@x.test"]])
        self.assertEqual(self.push(), ["http://127.0.0.1:1/super"])

    def test_while_access_is_off_and_he_has_no_address_nothing_is_sent(self):
        self.switch_off()
        FakeSmtp.sent.clear()
        listener.SUPER_EMAIL_TO, listener.SUPER_PUSH_URL = "", ""
        self.assertEqual(self.mail(), [])
        self.assertEqual(self.push(), [])

    def test_a_forced_notice_while_access_is_off_also_goes_to_the_super_user_only(self):
        self.switch_off()
        FakeSmtp.sent.clear()
        listener.send_alert_email("CORE", "critical", "down", [], "Core watchdog is DOWN", force=True)
        self.assertEqual([r for r, _ in FakeSmtp.sent], [["super@x.test"]])

    def test_a_standby_still_holds_back_while_core_is_up(self):
        self.as_standby()
        self.assertEqual(self.mail(), [])
        self.assertEqual(self.push(), [])

    def test_without_a_control_key_alerts_go_to_the_normal_recipients_even_if_the_state_says_off(self):
        self.switch_off()
        FakeSmtp.sent.clear()
        listener.CONTROL_KEY = ""
        self.assertEqual(self.mail(), [["users@x.test"]])


class CommandTests(Wiring):
    def stored(self):
        return control.from_store({key: listener._read_state(key) for key in listener.CONTROL_STORE_KEYS if listener._read_state(key) is not None}).state

    def test_switching_off_is_stored_recorded_and_survives_a_restart(self):
        self.assertEqual(self.switch_off(), ["access_off"])
        self.assertFalse(listener.CONTROL["state"].access_on)
        self.assertEqual(self.stored(), listener.CONTROL["state"])
        listener.CONTROL["state"] = control.ControlState()                  # a restart forgets memory ...
        run(listener._load_control())                                       # ... and reads the database
        self.assertFalse(listener.CONTROL["state"].access_on)
        with listener.db() as conn:
            kinds = [row["event_type"] for row in conn.execute("SELECT event_type FROM events WHERE source_id = 'CONTROL'")]
        self.assertEqual(kinds, ["access_off"])

    def test_repeating_a_command_changes_and_records_nothing(self):
        self.switch_off()
        self.assertEqual(self.switch_off(), [])
        with listener.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM events WHERE source_id = 'CONTROL'").fetchone()["n"], 1)

    def test_a_bad_value_changes_nothing(self):
        for payload in ({"access": "maybe"}, {"preferred": "BOTH"}, {"access": None}, {"access": "off", "preferred": "x"}):
            with self.assertRaises(ValueError, msg=str(payload)):
                run(listener.apply_control_command(payload, True, T0))
        self.assertEqual(listener.CONTROL["state"], control.ControlState())
        self.assertIsNone(listener._read_state("control_access"))

    def test_a_node_that_cannot_watch_the_other_refuses_to_step_aside(self):
        listener.PRIMARY_URL = ""
        with self.assertRaises(PermissionError):
            run(listener.apply_control_command({"preferred": "VPS"}, True, T0))
        self.assertEqual(listener.CONTROL["state"].preferred, H)

    def test_choosing_the_preferred_node_is_stored(self):
        self.assertEqual(run(listener.apply_control_command({"preferred": "vps"}, True, T0)), ["preferred_changed"])
        self.assertEqual(self.stored().preferred, V)

    def test_if_the_database_refuses_nothing_takes_effect(self):
        original = listener._write_states

        def refuse(values):
            raise RuntimeError("disk full")
        listener._write_states = refuse
        try:
            with self.assertRaises(RuntimeError):
                run(listener.apply_control_command({"access": "off"}, True, T0))
        finally:
            listener._write_states = original
        self.assertTrue(listener.CONTROL["state"].access_on)

    def test_a_damaged_stored_value_falls_back_and_is_reported(self):
        listener._write_state("control_access", "maybe")
        run(listener._load_control())
        self.assertTrue(listener.CONTROL["state"].access_on)
        self.assertEqual(len(listener.CONTROL["problems"]), 1)
        self.assertEqual(len(listener._control_status(T0, True)["problems"]), 1)
        self.assertEqual(listener._control_status(T0)["problems"], [])          # the reasons are for the super user only

    def test_a_damaged_control_key_means_no_lockout_even_if_the_stored_state_is_off(self):
        self.switch_off()
        listener.CONTROL_KEY = ""
        self.assertTrue(listener._access_on())


class NoticeTests(Wiring):
    def sent_subjects(self):
        return [next(line for line in text.splitlines() if line.lower().startswith("subject:")) for _, text in FakeSmtp.sent]

    def test_the_offline_mail_goes_once_to_support_only(self):
        self.switch_off()
        for _ in range(3):
            run(listener.control_notice_step())
        self.assertEqual([recipients for recipients, _ in FakeSmtp.sent], [["support@x.test"]])
        self.assertIn("Service is OFFLINE", self.sent_subjects()[0])
        self.assertIsNone(listener.CONTROL["state"].pending_notice)
        self.assertEqual(self.stored_pending(), "")

    def stored_pending(self):
        return listener._read_state("control_pending")

    def test_the_back_mail_follows_once_and_says_how_long(self):
        self.switch_off()
        run(listener.control_notice_step())
        FakeSmtp.sent.clear()
        run(listener.apply_control_command({"access": "on"}, True, T0 + timedelta(minutes=130)))
        for _ in range(2):
            run(listener.control_notice_step())
        self.assertEqual(len(FakeSmtp.sent), 1)
        self.assertIn("Service is back", self.sent_subjects()[0])

    def test_a_mail_server_outage_delays_the_mail_and_never_loses_or_doubles_it(self):
        self.switch_off()
        FakeSmtp.down = True
        self.assertIsNone(run(listener.control_notice_step()))
        self.assertEqual(listener.CONTROL["state"].pending_notice, "offline")
        FakeSmtp.down = False
        self.assertEqual(run(listener.control_notice_step()), "offline")
        self.assertIsNone(run(listener.control_notice_step()))
        self.assertEqual(len(FakeSmtp.sent), 1)

    def test_a_queued_mail_survives_a_restart(self):
        self.switch_off()
        listener.CONTROL["state"] = control.ControlState()
        run(listener._load_control())
        self.assertEqual(run(listener.control_notice_step()), "offline")

    def test_a_node_told_not_to_notify_sends_nothing(self):
        self.switch_off(notify=False)
        self.assertIsNone(run(listener.control_notice_step()))
        self.assertEqual(FakeSmtp.sent, [])

    def test_without_a_notice_address_the_mail_stays_queued(self):
        self.switch_off()
        listener.CONTROL_NOTICE_TO = ""
        self.assertIsNone(run(listener.control_notice_step()))
        self.assertEqual(listener.CONTROL["state"].pending_notice, "offline")

    def test_the_super_user_is_told_of_every_change(self):
        effects = run(listener.apply_control_command({"access": "off", "preferred": "VPS"}, True, T0))
        subjects = [message.subject for message in listener._super_user_messages(effects, listener.CONTROL["state"])]
        self.assertEqual(subjects, ["[Watchdog] Service is OFFLINE", "[Watchdog] Preferred node is now VPS"])


class GateTests(Wiring):
    async def through(self, method, path, headers=()):
        reached, sent = [], []

        async def inner(scope, receive, send):
            reached.append(path)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async def send(message):
            sent.append(message)
        scope = {"type": "http", "method": method, "path": path, "headers": [(k.encode(), v.encode()) for k, v in headers]}
        await listener.ControlGate(inner)(scope, None, send)
        return sent[0]["status"], sent[1]["body"], bool(reached)

    def go(self, method, path, headers=()):
        return run(self.through(method, path, headers))

    def test_while_access_is_on_nothing_is_touched(self):
        for method, path in (("GET", "/"), ("GET", "/status"), ("POST", "/heartbeat"), ("GET", "/anything")):
            self.assertEqual(self.go(method, path)[0], 200)

    def test_while_off_a_user_gets_the_branded_page_or_a_503(self):
        self.switch_off()
        status, body, reached = self.go("GET", "/")
        self.assertEqual((status, reached), (503, False))
        self.assertIn(b"Service temporarily unavailable", body)
        status, body, reached = self.go("GET", "/status")
        self.assertEqual((status, reached), (503, False))
        self.assertEqual(json.loads(body)["error"], "service temporarily unavailable")
        self.assertEqual(self.go("GET", "/stream")[0], 503)
        self.assertEqual(self.go("GET", "/never-heard-of-it")[0], 503)

    def test_while_off_the_system_keeps_working(self):
        self.switch_off()
        for method, path in (("POST", "/heartbeat"), ("POST", "/event"), ("GET", "/health"), ("POST", "/alerts/reset"), ("GET", "/control"), ("GET", "/favicon.ico")):
            self.assertEqual(self.go(method, path), (200, b"ok", True), path)

    def test_the_super_user_gets_through_with_the_key_or_a_session(self):
        self.switch_off()
        self.assertEqual(self.go("GET", "/status", [("x-watchdog-control", KEY)])[0], 200)
        token = access.issue_session(KEY, datetime.now(timezone.utc), 3600, listener._session_generation())
        self.assertEqual(self.go("GET", "/status", [("cookie", f"a=b; {listener.CONTROL_COOKIE}={token}")])[0], 200)

    def test_a_wrong_key_a_forged_or_expired_session_does_not(self):
        self.switch_off()
        self.assertEqual(self.go("GET", "/status", [("x-watchdog-control", KEY + "x")])[0], 503)
        self.assertEqual(self.go("GET", "/status", [("cookie", f"{listener.CONTROL_COOKIE}=v1.9999999999.{'0' * 64}")])[0], 503)
        old = access.issue_session(KEY, datetime.now(timezone.utc) - timedelta(hours=3), 3600, listener._session_generation())
        self.assertEqual(self.go("GET", "/status", [("cookie", f"{listener.CONTROL_COOKIE}={old}")])[0], 503)
        other = access.issue_session("another-control-key-0123456789abcdef", datetime.now(timezone.utc), 3600)
        self.assertEqual(self.go("GET", "/status", [("cookie", f"{listener.CONTROL_COOKIE}={other}")])[0], 503)

    def test_a_node_without_a_working_key_never_locks_anyone_out(self):
        self.switch_off()
        listener.CONTROL_KEY = ""
        self.assertEqual(self.go("GET", "/status")[0], 200)

    def test_anything_that_is_not_http_passes_through(self):
        self.switch_off()
        reached = []

        async def inner(scope, receive, send):
            reached.append(scope["type"])
        run(listener.ControlGate(inner)({"type": "lifespan"}, None, None))
        self.assertEqual(reached, ["lifespan"])


class HandlerTests(Wiring):
    def body(self, **fields):
        return json.dumps(fields).encode()

    def get(self, headers):
        return run(listener.control_get(FakeRequest(headers)))

    def post(self, headers, **fields):
        return run(listener.control_post(FakeRequest(headers, self.body(**fields))))

    def test_without_a_key_the_door_does_not_exist_or_stays_shut(self):
        listener.CONTROL_KEY = ""
        self.assertEqual(self.get({"x-watchdog-control": KEY}).status_code, 404)
        listener.CONTROL_KEY = KEY
        for headers in ({}, {"x-watchdog-control": "wrong"}, {"x-watchdog-token": "test-token-123"}):
            self.assertEqual(self.get(headers).status_code, 401)
            self.assertEqual(self.post(headers, access="off").status_code, 401)
        self.assertTrue(listener.CONTROL["state"].access_on)

    def test_the_admin_key_is_not_the_control_key(self):
        self.assertEqual(self.get({"x-watchdog-token": base.TEST_TOKEN}).status_code, 401)

    def test_the_super_user_reads_the_state_and_no_key_is_in_it(self):
        reply = self.get({"x-watchdog-control": KEY})
        self.assertEqual(reply.status_code, 200)
        content = reply.content
        self.assertEqual((content["access"], content["preferred"], content["node"], content["alerting_now"]), ("on", H, H, True))
        self.assertNotIn(KEY, json.dumps(content))

    def test_the_state_carries_the_node_clock_and_when_each_studio_was_last_heard(self):
        listener._write_state("unrelated", "x")
        with listener.db() as conn:
            conn.execute("INSERT INTO sources (source_id, last_seen_utc, last_payload_json, state, state_changed_utc, expected_interval_seconds) "
                         "VALUES ('MAIN', '2026-10-25T14:29:50+00:00', '{}', 'healthy', '2026-10-25T14:00:00+00:00', 10)")
        content = self.get({"x-watchdog-control": KEY}).content
        self.assertEqual(content["studios"], {"MAIN": "2026-10-25T14:29:50+00:00"})
        self.assertTrue(datetime.fromisoformat(content["server_time_utc"]).tzinfo is not None)

    def test_the_super_user_switches_access_off_and_on(self):
        reply = self.post({"x-watchdog-control": KEY}, access="off", notify=False)
        self.assertEqual((reply.status_code, reply.content["access"], reply.content["effects"]), (200, "off", ["access_off"]))
        self.assertFalse(listener.CONTROL["state"].access_on)
        reply = self.post({"x-watchdog-control": KEY}, access="on", notify=False)
        self.assertEqual((reply.status_code, reply.content["access"]), (200, "on"))

    def test_the_super_user_chooses_the_preferred_node(self):
        reply = self.post({"x-watchdog-control": KEY}, preferred="VPS")
        self.assertEqual((reply.status_code, reply.content["preferred"], reply.content["alerting_now"]), (200, V, False))

    def test_bad_requests_are_refused_with_a_reason(self):
        headers = {"x-watchdog-control": KEY}
        self.assertEqual(self.post(headers).status_code, 400)                                      # nothing to do
        self.assertEqual(self.post(headers, access="maybe").status_code, 400)
        self.assertEqual(self.post(headers, access="off", extra=1).status_code, 400)               # an unknown field
        self.assertEqual(self.post(headers, access="off", notify="yes").status_code, 400)
        self.assertEqual(run(listener.control_post(FakeRequest(headers, b"[1]"))).status_code, 400)
        self.assertTrue(listener.CONTROL["state"].access_on)

    def test_stepping_aside_without_a_peer_address_is_a_409(self):
        listener.PRIMARY_URL = ""
        self.assertEqual(self.post({"x-watchdog-control": KEY}, preferred="VPS").status_code, 409)

    def test_the_session_is_issued_for_the_right_key_only(self):
        wrong = run(listener.control_session(FakeRequest({}, self.body(key="nope"))))
        self.assertEqual(wrong.status_code, 401)
        self.assertNotIn("Set-Cookie", wrong.headers or {})
        right = run(listener.control_session(FakeRequest({}, self.body(key=KEY))))
        self.assertEqual(right.status_code, 200)
        cookie = right.headers["Set-Cookie"]
        for part in ("HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=3600", "Secure"):
            self.assertIn(part, cookie)
        token = cookie.split(";")[0].split("=", 1)[1]
        self.assertTrue(access.verify_session(KEY, token, datetime.now(timezone.utc), listener._session_generation()))
        self.assertNotIn(KEY, cookie)

    def test_guessing_is_slowed_down(self):
        statuses = [self.get({"x-watchdog-control": "guess"}).status_code for _ in range(32)]
        self.assertEqual(statuses[:30], [401] * 30)
        self.assertEqual(statuses[30:], [429, 429])

    def test_health_names_the_preferred_node_only_when_the_phase_is_on(self):
        self.assertEqual(run(listener.health()).content["preferred"], H)
        listener.CONTROL_KEY = ""
        self.assertNotIn("preferred", run(listener.health()).content)

    def test_health_says_whether_access_is_on_so_an_open_page_can_follow_it(self):
        self.assertEqual(run(listener.health()).content["access"], "on")
        self.switch_off()
        self.assertEqual(run(listener.health()).content["access"], "off")
        listener.CONTROL_KEY = ""
        self.assertNotIn("access", run(listener.health()).content)

    def test_the_status_block_never_shows_a_key_and_the_reasons_only_to_the_super_user(self):
        block = listener._control_status(T0)
        self.assertEqual(set(block), {"enabled", "node", "access", "preferred", "off_since", "access_changed_at", "preferred_changed_at", "pending_notice", "problems",
                                      "alerting_now", "peer", "login"})
        self.assertNotIn(KEY, json.dumps(block))
        listener.CONTROL_KEY = ""
        self.assertEqual(listener._control_status(T0), {"enabled": False, "node": H})

    def test_the_banner_appears_only_while_access_is_off_and_never_says_which_node_is_preferred(self):
        page = "<html><body class='x'><p>hi</p></body></html>"
        self.assertEqual(listener._with_banner(page), page)
        self.prefer(V)
        self.assertEqual(listener._with_banner(page), page)                  # choosing the VPS is not something a visitor is shown
        self.switch_off()
        banner = listener._with_banner(page)
        self.assertIn("ACCESS OFF since 14:30 UTC", banner)
        self.assertIn('id="control-banner"', banner)                        # the page finds it by this name and updates or removes it live
        self.assertNotIn("MANUAL", banner)
        self.assertNotIn("preferred", banner)
        self.assertTrue(banner.startswith("<html><body class='x'><div id=\"control-banner\" role=\"status\""))
        listener.CONTROL_KEY = ""
        self.assertEqual(listener._with_banner(page), page)

    def test_the_off_screen_is_put_in_its_place_when_the_phase_is_on_and_removed_when_it_is_not(self):
        page = "<html><body><!--off-screen--><p>live</p></body></html>"
        shown = listener._with_off_screen(page)
        self.assertIn('id="off-screen"', shown)
        self.assertNotIn("<!--off-screen-->", shown)
        self.assertIn("support@corp.example", shown)
        listener.CONTROL_KEY = ""
        self.assertEqual(listener._with_off_screen(page), "<html><body><p>live</p></body></html>")

    def test_the_real_pages_get_the_off_screen_exactly_once(self):
        for render in (listener.render_dashboard_html, listener.render_topology_html):
            page = listener._with_off_screen(render("k" * 64))
            self.assertEqual(page.count('id="off-screen"'), 1)
            self.assertNotIn("<!--off-screen-->", page)


class BrowserSafetyTests(Wiring):
    """A change made with the browser's cookie must come from this site's own page (a security review asked for this second lock on top of SameSite=Strict)."""

    def cookie(self):
        return f"{listener.CONTROL_COOKIE}={access.issue_session(KEY, datetime.now(timezone.utc), 3600, listener._session_generation())}"

    def post(self, headers):
        return run(listener.control_post(FakeRequest(headers, json.dumps({"access": "off", "notify": False}).encode()))).status_code

    def test_a_cookie_request_with_a_json_body_from_the_same_origin_is_accepted(self):
        self.assertEqual(self.post({"cookie": self.cookie(), "content-type": "application/json", "sec-fetch-site": "same-origin"}), 200)

    def test_a_cookie_request_is_accepted_when_the_origin_names_this_host(self):
        listener.CONTROL["state"] = control.ControlState()
        headers = {"cookie": self.cookie(), "content-type": "application/json", "origin": "https://watchdog.x.test", "host": "watchdog.x.test"}
        self.assertEqual(self.post(headers), 200)

    def test_a_cookie_request_from_another_site_is_refused_and_changes_nothing(self):
        for extra in ({"sec-fetch-site": "cross-site"}, {"sec-fetch-site": "same-site"}, {"origin": "https://evil.test", "host": "watchdog.x.test"}, {}):
            headers = {"cookie": self.cookie(), "content-type": "application/json", **extra}
            self.assertEqual(self.post(headers), 403, str(extra))
        self.assertTrue(listener.CONTROL["state"].access_on)

    def test_a_cookie_request_that_is_not_json_is_refused(self):
        for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
            self.assertEqual(self.post({"cookie": self.cookie(), "content-type": content_type, "sec-fetch-site": "same-origin"}), 403, content_type)

    def test_the_pc_tool_with_the_key_header_is_not_asked_for_an_origin(self):
        self.assertEqual(self.post({"x-watchdog-control": KEY}), 200)


class StreamTests(Wiring):
    """An open live stream must not outlive access: a viewer connected before OFF is cut off at the next push (a security review found this gap)."""

    def watch(self, headers, switch_off):
        listener.STREAM_PUSH_SECONDS = 0
        request = FakeRequest(headers)

        async def scenario():
            stream = (await listener.stream(request, token=listener.VIEW_TOKEN)).body_iterator
            first = await stream.__anext__()
            switch_off()
            try:
                for _ in range(3):                                     # the viewer gets one last "access is off" message, then the stream ends
                    await asyncio.wait_for(stream.__anext__(), 5)
                return first, False
            except StopAsyncIteration:
                return first, True
        try:
            return run(scenario())
        finally:
            listener.STREAM_PUSH_SECONDS = 5

    def off(self):
        listener.CONTROL["state"] = control.set_access(listener.CONTROL["state"], False, T0).state

    def test_a_user_stream_ends_when_access_goes_off(self):
        first, ended = self.watch({}, self.off)
        self.assertIn('"control"', first)
        self.assertTrue(ended)

    def test_the_user_is_told_why_in_one_last_message_before_the_stream_ends(self):
        listener.STREAM_PUSH_SECONDS = 0
        request = FakeRequest({})

        async def scenario():
            stream = (await listener.stream(request, token=listener.VIEW_TOKEN)).body_iterator
            await stream.__anext__()
            self.off()
            last = await asyncio.wait_for(stream.__anext__(), 5)
            try:
                await asyncio.wait_for(stream.__anext__(), 5)
                return last, False
            except StopAsyncIteration:
                return last, True
        try:
            last, ended = run(scenario())
        finally:
            listener.STREAM_PUSH_SECONDS = 5
        self.assertEqual(last, 'event: access\ndata: {"access": "off"}\n\n')
        self.assertTrue(ended)

    def test_the_super_users_stream_stays_open(self):
        _, ended = self.watch({"x-watchdog-control": KEY}, self.off)
        self.assertFalse(ended)

    def test_a_stream_stays_open_while_access_stays_on(self):
        _, ended = self.watch({}, lambda: None)
        self.assertFalse(ended)


PASSWORD = "correct horse battery staple 42"
WHO = "owner@mail.example"


class LoginTests(Wiring):
    """The super user logs in with a name and a password. Wrong tries are refused alike and locked out; the old key login works only until a password is set."""

    def setUp(self):
        super().setUp()
        listener.SUPER_USER = WHO
        listener.SUPER_PASSWORD_HASH = login.hash_password(PASSWORD, n=16)
        listener._login_throttle = login.LoginThrottle()
        listener._control_rate._seen.clear()

    def attempt(self, user=WHO, password=PASSWORD, host="203.0.113.5", **extra):
        body = json.dumps({"user": user, "password": password, **extra}).encode()
        return run(listener.control_session(FakeRequest({}, body, host=host)))

    def test_the_right_name_and_password_get_a_signed_http_only_session(self):
        reply = self.attempt()
        self.assertEqual(reply.status_code, 200)
        cookie = reply.headers["Set-Cookie"]
        for part in ("HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=3600"):
            self.assertIn(part, cookie)
        token = cookie.split(";")[0].split("=", 1)[1]
        self.assertTrue(access.verify_session(KEY, token, datetime.now(timezone.utc), listener._session_generation()))
        for secret in (PASSWORD, KEY, listener.SUPER_PASSWORD_HASH):
            self.assertNotIn(secret, cookie + json.dumps(reply.content))

    def test_a_wrong_password_and_a_wrong_name_are_refused_in_exactly_the_same_words(self):
        wrong_password, wrong_name = self.attempt(password="not the password at all 1"), self.attempt(user="someone@else.test")
        self.assertEqual((wrong_password.status_code, wrong_name.status_code), (401, 401))
        self.assertEqual(wrong_password.content, wrong_name.content)
        self.assertNotIn("Set-Cookie", wrong_password.headers or {})
        self.assertIn("not accepted", wrong_password.content["error"])

    def test_five_wrong_tries_lock_that_address_even_for_the_right_password(self):
        for _ in range(5):
            self.assertEqual(self.attempt(password="wrong password number 1").status_code, 401)
        locked = self.attempt()
        self.assertEqual(locked.status_code, 429)
        self.assertGreaterEqual(int(locked.headers["Retry-After"]), 1)
        self.assertNotIn("Set-Cookie", locked.headers)

    def test_another_address_can_still_log_in(self):
        for _ in range(5):
            self.attempt(password="wrong password number 1")
        self.assertEqual(self.attempt(host="198.51.100.9").status_code, 200)

    def test_a_good_login_forgives_earlier_wrong_tries_from_that_address(self):
        for _ in range(4):
            self.attempt(password="wrong password number 1")
        self.assertEqual(self.attempt().status_code, 200)
        for _ in range(4):
            self.assertEqual(self.attempt(password="wrong password number 1").status_code, 401)

    def test_once_a_password_is_set_the_browser_no_longer_accepts_the_key(self):
        reply = run(listener.control_session(FakeRequest({}, json.dumps({"key": KEY}).encode())))
        self.assertEqual(reply.status_code, 401)
        self.assertEqual(run(listener.control_get(FakeRequest({"x-watchdog-control": KEY}))).status_code, 200)       # the key still works in the header, for the PC tool

    def test_password_checks_run_at_most_two_at_a_time(self):
        self.assertEqual(listener._login_slots._value, 2)           # 16 MiB each: bounded so a burst of guesses cannot use up the standby's memory

    def test_the_login_page_matches_what_is_set_up(self):
        self.assertIn('autocomplete="current-password"', listener._login_page_html())
        listener.SUPER_PASSWORD_HASH = ""
        self.assertNotIn('autocomplete="current-password"', listener._login_page_html())      # not set up yet: the old key page, so nobody is locked out of the migration

    def test_the_login_route_refuses_when_the_control_phase_is_off(self):
        listener.CONTROL_KEY = ""
        self.assertEqual(run(listener.control_session(FakeRequest({}, json.dumps({"user": WHO, "password": PASSWORD}).encode()))).status_code, 404)


class DashboardLinkTests(Wiring):
    """The little Sign in on the dashboards: 'Sign in' for everyone, 'Control panel' once signed in, nothing where the control phase is off."""
    PAGE = "<html><body class='x'><p>live status</p></body></html>"

    def signed_in(self):
        token = access.issue_session(KEY, datetime.now(timezone.utc), 3600, listener._session_generation())
        return FakeRequest({"cookie": f"{listener.CONTROL_COOKIE}={token}"})

    def test_everyone_sees_a_quiet_sign_in_link_that_leads_to_the_sign_in_page(self):
        page = listener._with_super_link(self.PAGE, FakeRequest({}))
        self.assertIn('href="/control/login"', page)
        self.assertIn(">Sign in</a>", page)
        self.assertNotIn("/control/panel", page)
        self.assertEqual(page.count("wd-super\" href"), 1)
        self.assertTrue(page.endswith("</body></html>"))
        self.assertLess(page.index("live status"), page.index("Sign in"))
        self.assertIn("<p>live status</p>", page)                        # the page itself is untouched

    def test_once_signed_in_the_link_becomes_the_control_panel(self):
        page = listener._with_super_link(self.PAGE, self.signed_in())
        self.assertIn('href="/control/panel"', page)
        self.assertIn(">Control panel</a>", page)
        self.assertNotIn(">Sign in</a>", page)

    def test_a_wrong_or_old_session_still_shows_sign_in(self):
        for cookie in (f"{listener.CONTROL_COOKIE}=v1.9999999999." + "0" * 64, f"{listener.CONTROL_COOKIE}=garbage", "other=1"):
            page = listener._with_super_link(self.PAGE, FakeRequest({"cookie": cookie}))
            self.assertIn(">Sign in</a>", page)

    def test_nothing_is_added_where_the_control_phase_is_off(self):
        listener.CONTROL_KEY = ""
        self.assertEqual(listener._with_super_link(self.PAGE, FakeRequest({})), self.PAGE)

    TOOLBAR_PAGE = "<html><body><div class='toolbar'><button>a</button><!--staff-link--><span class='spacer'></span></div><p>live status</p></body></html>"

    def test_the_link_sits_in_the_toolbar_where_the_buttons_are_and_nowhere_else(self):
        page = listener._with_super_link(self.TOOLBAR_PAGE, FakeRequest({}))
        self.assertIn('<a class="staff-link" href="/control/login">Sign in</a>', page)
        self.assertNotIn("<!--staff-link-->", page)
        self.assertNotIn("wd-super", page)                                # no floating pill as well
        self.assertEqual(page.count("/control/login"), 1)
        self.assertLess(page.index("staff-link"), page.index("live status"))

    def test_signed_in_the_toolbar_link_is_the_control_panel(self):
        page = listener._with_super_link(self.TOOLBAR_PAGE, self.signed_in())
        self.assertIn('<a class="staff-link" href="/control/panel">Control panel</a>', page)
        self.assertNotIn(">Sign in</a>", page)

    def test_where_the_control_phase_is_off_the_place_is_emptied_and_nothing_is_offered(self):
        listener.CONTROL_KEY = ""
        page = listener._with_super_link(self.TOOLBAR_PAGE, FakeRequest({}))
        self.assertNotIn("staff-link", page)
        self.assertNotIn("/control", page)

    def test_the_real_dashboard_and_topology_pages_have_exactly_one_place_for_it(self):
        for render in (listener.render_dashboard_html, listener.render_topology_html):
            raw = render("k" * 64)
            self.assertEqual(raw.count(listener.STAFF_LINK_MARKER), 1)
            page = listener._with_super_link(raw, FakeRequest({}))
            self.assertIn('<a class="staff-link" href="/control/login">Sign in</a>', page)
            self.assertNotIn("wd-super", page)
            self.assertNotIn(listener.STAFF_LINK_MARKER, page)
            self.assertIn(".toolbar a.staff-link", page)                  # it is styled like the toolbar's buttons, with a 44 px touch target

    def test_a_page_without_a_body_end_still_gets_the_link_at_its_end(self):
        page = listener._with_super_link("<p>no body tag</p>", FakeRequest({}))
        self.assertTrue(page.startswith("<p>no body tag</p>"))
        self.assertIn(">Sign in</a>", page)

    def test_it_sits_beside_the_off_banner_without_disturbing_it(self):
        self.switch_off()
        page = listener._with_super_link(listener._with_banner(self.PAGE), FakeRequest({}))
        self.assertIn("ACCESS OFF since", page)
        self.assertIn(">Sign in</a>", page)

    def test_both_dashboard_handlers_take_the_request_so_they_can_tell_who_is_signed_in(self):
        import inspect
        for handler in (listener.dashboard, listener.topology):
            self.assertIn("request", inspect.signature(handler).parameters)

    def test_the_link_is_keyboard_reachable_and_visibly_focused(self):
        page = listener._with_super_link(self.PAGE, FakeRequest({}))
        self.assertIn("focus-visible", page)
        self.assertNotIn("tabindex", page)                               # a plain link is focusable by itself


class SessionRevocationTests(Wiring):
    """A copied cookie must stop working when the super user logs out, when his password changes and across a restart; a stranger cannot log him out."""

    def setUp(self):
        super().setUp()
        listener.SUPER_USER = WHO
        listener.SUPER_PASSWORD_HASH = login.hash_password(PASSWORD, n=16)
        listener._login_throttle = login.LoginThrottle()
        listener._control_rate._seen.clear()

    def sign_in(self):
        reply = run(listener.control_session(FakeRequest({}, json.dumps({"user": WHO, "password": PASSWORD}).encode())))
        self.assertEqual(reply.status_code, 200)
        return reply.headers["Set-Cookie"].split(";")[0]

    def works(self, cookie):
        return run(listener.control_panel_state(FakeRequest({"cookie": cookie}))).status_code == 200

    def log_out(self, cookie):
        return run(listener.control_logout(FakeRequest({"cookie": cookie} if cookie else {})))

    def test_logging_out_ends_every_session_on_this_node_including_a_copied_one(self):
        mine, copied = self.sign_in(), self.sign_in()
        self.assertTrue(self.works(mine) and self.works(copied))
        reply = self.log_out(mine)
        self.assertEqual(reply.status_code, 200)
        self.assertIn("Max-Age=0", reply.headers["Set-Cookie"])
        self.assertFalse(self.works(mine))
        self.assertFalse(self.works(copied))

    def test_signing_in_again_after_a_log_out_works(self):
        self.log_out(self.sign_in())
        self.assertTrue(self.works(self.sign_in()))

    def test_logging_out_without_a_session_ends_nothing(self):
        mine = self.sign_in()
        self.log_out("")
        self.log_out("watchdog_control=v1.9999999999." + "0" * 64)
        self.assertTrue(self.works(mine))

    def test_a_new_password_ends_every_session(self):
        mine = self.sign_in()
        listener.SUPER_PASSWORD_HASH = login.hash_password("a different long passphrase 77", n=16)
        self.assertFalse(self.works(mine))

    def test_the_end_of_the_sessions_survives_a_restart(self):
        mine = self.sign_in()
        self.log_out(mine)
        listener.CONTROL["session_gen"] = ""                   # a restart forgets memory ...
        run(listener._load_control())                          # ... and reads the database
        self.assertFalse(self.works(mine))
        self.assertTrue(self.works(self.sign_in()))

    def test_if_the_database_refuses_the_other_sessions_are_not_claimed_ended(self):
        mine, copied = self.sign_in(), self.sign_in()
        original = listener._write_state

        def refuse(key, value):
            raise RuntimeError("disk full")
        listener._write_state = refuse
        try:
            reply = self.log_out(mine)
        finally:
            listener._write_state = original
        self.assertEqual(reply.status_code, 500)
        self.assertFalse(reply.content["ok"])
        self.assertIn("Max-Age=0", reply.headers["Set-Cookie"])                # this browser is logged out regardless
        self.assertTrue(self.works(copied))                                      # and the page does not pretend the others were ended

    def test_the_generation_is_a_hash_that_shows_neither_the_password_hash_nor_the_stored_value(self):
        listener.CONTROL["session_gen"] = "cafebabe" * 4
        shown = listener._session_generation()
        for secret in (listener.SUPER_PASSWORD_HASH, "cafebabe" * 4, KEY):
            self.assertNotIn(secret, shown)
        self.assertRegex(shown, r"^[0-9a-f]{32}$")


class ConcurrentGuessingTests(Wiring):
    """A burst of simultaneous guesses must not all run before the lockout closes (a security review found they could)."""

    def setUp(self):
        super().setUp()
        listener.SUPER_USER = WHO
        listener.SUPER_PASSWORD_HASH = login.hash_password(PASSWORD, n=16)
        listener._login_throttle = login.LoginThrottle()
        listener._control_rate = listener.VisitorRate(1000)
        listener._login_slots = asyncio.Semaphore(2)               # a fresh one per test: a semaphore that has been waited on belongs to the event loop it was waited on in

    def test_twelve_simultaneous_wrong_guesses_run_at_most_six_checks_and_the_rest_are_locked_out(self):
        real, calls = listener.control_login.check_login, []

        def counting(*args):
            calls.append(1)
            return real(*args)
        listener.control_login.check_login = counting
        body = json.dumps({"user": WHO, "password": "wrong password number 1"}).encode()

        async def burst():
            return await asyncio.gather(*[listener.control_session(FakeRequest({}, body)) for _ in range(12)])
        try:
            replies = run(burst())
        finally:
            listener.control_login.check_login = real
        statuses = sorted(reply.status_code for reply in replies)
        self.assertLessEqual(len(calls), 6, f"{len(calls)} passwords were checked")          # five tries plus at most one more already running when the lock closed
        self.assertEqual(statuses.count(429), 12 - statuses.count(401))
        self.assertGreaterEqual(statuses.count(429), 6)
        self.assertEqual(listener._login_throttle.locked_for("peer:203.0.113.5", __import__("time").monotonic()) > 0, True)

    def test_the_right_password_in_the_same_burst_is_refused_once_the_lock_has_closed(self):
        wrong = json.dumps({"user": WHO, "password": "wrong password number 1"}).encode()
        right = json.dumps({"user": WHO, "password": PASSWORD}).encode()

        async def burst():
            guesses = [listener.control_session(FakeRequest({}, wrong)) for _ in range(8)]
            return await asyncio.gather(*guesses, listener.control_session(FakeRequest({}, right)))
        replies = run(burst())
        self.assertEqual(replies[-1].status_code, 429)
        self.assertNotIn("Set-Cookie", replies[-1].headers or {})


class FakePeer:
    """The OTHER node's control door, as the panel sees it over https. It runs the real control rules for its own state."""

    def __init__(self, name):
        self.name, self.state, self.up, self.calls = name, control.ControlState(), True, []

    def view(self, local_state):
        alerting = control.may_alert(self.name, self.state, control.PeerView(down=False, preferred=local_state.preferred))
        return {"enabled": True, "node": self.name, "access": "on" if self.state.access_on else "off", "preferred": self.state.preferred, "alerting_now": alerting,
                "problems": [], "peer": {"watched": True, "believed": "UP", "preferred": local_state.preferred}, "server_time_utc": T0.isoformat(),
                "studios": {"MAIN": (T0 - timedelta(seconds=7)).isoformat()}}

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if not self.up:
            return None, {}
        if method == "GET":
            return 200, self.view(listener.CONTROL["state"])
        state, effects = self.state, []
        if "access" in payload:
            change = control.set_access(state, control.parse_access(payload["access"]), T0, payload.get("notify", True))
            state, effects = change.state, effects + list(change.effects)
        if "preferred" in payload:
            change = control.set_preferred(state, control.parse_preferred(payload["preferred"]), T0)
            state, effects = change.state, effects + list(change.effects)
        self.state = state
        return 200, {**self.view(listener.CONTROL["state"]), "effects": effects}


class PanelTests(Wiring):
    """The buttons: a press on the node you are logged in to changes BOTH nodes (the other one is asked with the internal key) and reads both back."""

    def setUp(self):
        super().setUp()
        listener.SUPER_USER = WHO
        listener.SUPER_PASSWORD_HASH = login.hash_password(PASSWORD, n=16)
        listener.PRIMARY_URL = "https://other-node.example.test/health"
        self.peer = FakePeer("VPS")
        listener._peer_request = self.peer.request

    def session_headers(self, **extra):
        token = access.issue_session(KEY, datetime.now(timezone.utc), 3600, listener._session_generation())
        return {"cookie": f"{listener.CONTROL_COOKIE}={token}", "content-type": "application/json", "sec-fetch-site": "same-origin", **extra}

    def act(self, action, headers=None):
        body = json.dumps({"action": action}).encode()
        return run(listener.control_panel_act(FakeRequest(self.session_headers() if headers is None else headers, body)))

    def state(self, headers=None):
        return run(listener.control_panel_state(FakeRequest(self.session_headers() if headers is None else headers)))

    def test_nothing_works_without_a_session(self):
        for headers in ({}, {"cookie": "watchdog_control=v1.9999999999." + "0" * 64}, {"x-watchdog-token": base.TEST_TOKEN}):
            self.assertEqual(self.state(headers).status_code, 401)
            self.assertEqual(self.act("off", headers).status_code, 401)
        self.assertTrue(listener.CONTROL["state"].access_on)
        self.assertEqual(self.peer.calls, [])

    def test_the_state_shows_both_nodes_and_never_a_key(self):
        reply = self.state()
        self.assertEqual(reply.status_code, 200)
        content = reply.content
        self.assertEqual((content["self"]["node"], content["peer"]["node"], content["peer"]["reachable"]), (H, V, True))
        for secret in (KEY, PASSWORD, listener.SUPER_PASSWORD_HASH):
            self.assertNotIn(secret, json.dumps(content))

    def test_the_state_says_plainly_when_the_other_node_cannot_be_reached(self):
        self.peer.up = False
        content = self.state().content
        self.assertEqual(content["peer"]["reachable"], False)
        self.assertEqual(content["self"]["node"], H)

    def test_off_sets_both_nodes_and_this_node_announces_it_once(self):
        reply = self.act("off")
        self.assertEqual(reply.status_code, 200, reply.content)
        self.assertTrue(reply.content["ok"])
        self.assertFalse(listener.CONTROL["state"].access_on)
        self.assertFalse(self.peer.state.access_on)
        self.assertEqual(listener.CONTROL["state"].pending_notice, "offline")
        self.assertIsNone(self.peer.state.pending_notice)
        self.assertEqual([c for c in self.peer.calls if c[0] == "POST"][0][2], {"access": "off", "notify": False})
        self.assertIn("OFF on both nodes", " ".join(reply.content["messages"]))
        self.assertEqual(sum("Service is OFFLINE" in text for _, text in FakeSmtp.sent), 1)           # the super user's own announcement

    def test_when_the_other_node_is_the_alerting_one_it_announces(self):
        self.as_standby()
        self.peer = FakePeer(H)
        listener._peer_request = self.peer.request
        self.act("off")
        self.assertEqual(self.peer.state.pending_notice, "offline")
        self.assertIsNone(listener.CONTROL["state"].pending_notice)

    def test_on_restores_both_nodes(self):
        self.act("off")
        reply = self.act("on")
        self.assertTrue(reply.content["ok"])
        self.assertTrue(listener.CONTROL["state"].access_on and self.peer.state.access_on)
        self.assertIn("ON on both nodes", " ".join(reply.content["messages"]))

    def test_pressing_the_same_button_twice_changes_and_announces_nothing_more(self):
        self.act("off")
        sent = len(FakeSmtp.sent)
        posts = len([c for c in self.peer.calls if c[0] == "POST"])
        reply = self.act("off")
        self.assertTrue(reply.content["ok"])
        self.assertEqual((len(FakeSmtp.sent), len([c for c in self.peer.calls if c[0] == "POST"])), (sent, posts))
        self.assertIn("already", " ".join(reply.content["messages"]))

    def test_use_the_vps_sets_the_vps_first_then_this_node(self):
        reply = self.act("use_vps")
        self.assertTrue(reply.content["ok"], reply.content)
        self.assertEqual((listener.CONTROL["state"].preferred, self.peer.state.preferred), (V, V))
        first_post = [c for c in self.peer.calls if c[0] == "POST"][0]
        self.assertEqual(first_post[2], {"preferred": "VPS", "notify": True})
        self.assertIn("preferred", " ".join(reply.content["messages"]))

    def test_use_the_vps_when_the_vps_cannot_be_reached_changes_nothing(self):
        self.peer.up = False
        reply = self.act("use_vps")
        self.assertFalse(reply.content["ok"])
        self.assertEqual(listener.CONTROL["state"].preferred, H)
        self.assertIn("nothing was changed", " ".join(reply.content["messages"]))

    def test_use_core_here_works_even_if_the_other_node_is_unreachable_and_says_so(self):
        listener.CONTROL["state"] = control.set_preferred(listener.CONTROL["state"], V, T0).state
        self.peer.up = False
        reply = self.act("use_core")
        self.assertFalse(reply.content["ok"])
        self.assertEqual(listener.CONTROL["state"].preferred, H)
        self.assertIn("not reached", " ".join(reply.content["messages"]))

    def test_off_with_the_other_node_unreachable_still_sets_this_one_and_says_so(self):
        self.peer.up = False
        reply = self.act("off")
        self.assertFalse(reply.content["ok"])
        self.assertFalse(listener.CONTROL["state"].access_on)
        self.assertIn("not reached", " ".join(reply.content["messages"]))

    def test_an_unknown_or_missing_action_is_refused(self):
        for body in (b'{"action": "explode"}', b'{}', b'{"action": 5}', b'[1]'):
            reply = run(listener.control_panel_act(FakeRequest(self.session_headers(), body)))
            self.assertEqual(reply.status_code, 400, body)
        self.assertTrue(listener.CONTROL["state"].access_on)

    def test_a_press_from_another_site_is_refused(self):
        headers = self.session_headers()
        headers["sec-fetch-site"] = "cross-site"
        self.assertEqual(self.act("off", headers).status_code, 403)
        self.assertTrue(listener.CONTROL["state"].access_on)

    def test_the_other_node_is_only_ever_asked_over_https(self):
        real_call = self.saved["_peer_request"]                    # the real function, not the fake this class installs
        listener.PRIMARY_URL = "http://other-node.example.test/health"
        self.assertEqual(listener._peer_base(), "")
        self.assertEqual(real_call("GET", "/control"), (None, {}))      # refused before any connection is made: the key never travels over plain http
        listener.PRIMARY_URL = ""
        self.assertEqual(listener._peer_base(), "")
        listener.PRIMARY_URL = "https://other-node.example.test/health"
        self.assertEqual(listener._peer_base(), "https://other-node.example.test")
        listener.CONTROL_KEY = ""
        self.assertEqual(real_call("GET", "/control"), (None, {}))      # and nothing is sent without a key to send

    def test_logout_ends_the_cookie(self):
        reply = run(listener.control_logout(FakeRequest(self.session_headers())))
        self.assertEqual(reply.status_code, 200)
        self.assertIn("Max-Age=0", reply.headers["Set-Cookie"])

    def test_the_panel_page_needs_a_session_and_otherwise_sends_you_to_the_login(self):
        sent_away = run(listener.control_panel(FakeRequest({})))
        self.assertEqual((sent_away.status_code, sent_away.headers["Location"]), (302, "/control/login"))
        page = run(listener.control_panel(FakeRequest(self.session_headers())))
        self.assertEqual(page.status_code, 200)
        self.assertIn("Switch access OFF", page.content)


class PeerProbeTests(Wiring):
    def probe(self, answer):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        listener.urllib.request.urlopen = self.saved_urlopen            # this one talks to a real (local) server; tearDown puts the fake back for the next test
        try:
            return listener.probe_primary(f"http://127.0.0.1:{server.server_port}/health", 3)
        finally:
            server.shutdown()
            server.server_close()

    def test_the_probe_reads_who_the_other_node_says_is_preferred(self):
        self.assertEqual(self.probe({"status": "ok", "preferred": "VPS"}), (True, ""))
        self.assertEqual(listener.PEER["preferred"], V)

    def test_an_older_peer_that_does_not_say_leaves_it_unknown(self):
        listener.PEER["preferred"] = H
        self.assertEqual(self.probe({"status": "ok"}), (True, ""))
        self.assertIsNone(listener.PEER["preferred"])

    def test_a_nonsense_preference_is_unknown_not_believed(self):
        self.assertEqual(self.probe({"status": "ok", "preferred": "<script>"}), (True, ""))
        self.assertIsNone(listener.PEER["preferred"])

    def test_a_failed_probe_forgets_what_the_peer_said(self):
        listener.PEER["preferred"] = V
        self.assertFalse(listener.probe_primary("http://127.0.0.1:9/health", 1)[0])
        self.assertIsNone(listener.PEER["preferred"])


class PeerTransitionTests(Wiring):
    def change(self, kind, seconds=0):
        return standby.Transition(kind, T0 + timedelta(seconds=seconds), "reason", T0)

    def test_core_says_so_once_when_the_preferred_vps_goes_down_and_when_it_is_back(self):
        self.prefer(V)
        run(listener._apply_transition(self.change("down")))
        run(listener._apply_transition(self.change("up", 300)))
        subjects = [text for _, text in FakeSmtp.sent]
        self.assertEqual(len(subjects), 2)
        self.assertIn("VPS watchdog is DOWN", subjects[0])
        self.assertIn("VPS watchdog is back", subjects[1])
        self.assertTrue(all(recipients == ["users@x.test"] for recipients, _ in FakeSmtp.sent))

    def test_core_stays_quiet_about_the_vps_while_core_is_the_preferred_node(self):
        run(listener._apply_transition(self.change("down")))
        self.assertEqual(FakeSmtp.sent, [])

    def test_while_access_is_off_the_fallback_message_goes_to_the_super_user_only(self):
        self.prefer(V)
        self.switch_off()
        FakeSmtp.sent.clear()
        run(listener._apply_transition(self.change("down")))
        self.assertEqual([recipients for recipients, _ in FakeSmtp.sent], [["super@x.test"]])


class HeartbeatIdleMarkTests(Wiring):
    """The real heartbeat route stores and carries the idle mark, so the clock survives from one heartbeat (and one restart) to the next."""

    def send(self, **fields):
        body = json.dumps({"hostname": "SITE-B", "expected_interval_seconds": 30, "player_running": True, **fields}).encode()
        reply = run(listener.heartbeat(FakeRequest({"x-watchdog-token": "t"}, body)))
        self.assertEqual(reply.status_code, 200, reply.content)

    def stored(self):
        with listener.db() as conn:
            row = conn.execute("SELECT last_payload_json FROM sources WHERE source_id = 'SITE-B'").fetchone()
        return json.loads(row["last_payload_json"])

    def test_an_idle_heartbeat_is_stored_with_its_start_and_the_start_is_kept(self):
        self.send(current_item=None)
        first = self.stored()["_idle_since"]
        self.send(current_item=None)
        self.assertEqual(self.stored()["_idle_since"], first)

    def test_an_item_removes_the_mark(self):
        self.send(current_item=None)
        self.send(current_item="SHOW.mp4")
        self.assertNotIn("_idle_since", self.stored())

    def test_other_senders_are_never_marked(self):
        body = json.dumps({"hostname": "SITE-A", "current_item": None, "player_running": True}).encode()
        run(listener.heartbeat(FakeRequest({"x-watchdog-token": "t"}, body)))
        with listener.db() as conn:
            row = conn.execute("SELECT last_payload_json FROM sources WHERE source_id = 'SITE-A'").fetchone()
        self.assertNotIn("_idle_since", json.loads(row["last_payload_json"]))


class HeartbeatAbsentMarkTests(Wiring):
    """The real heartbeat route keeps each missing player's clock, and raises the critical `instance_missing` event (and later the restoration) only after the grace."""

    def send(self, *feeds):
        instances = [{"feed": feed, "launcher_present": True, "engine_present": True, "engine_pid": 100} for feed in feeds]
        body = json.dumps({"hostname": "BACKUP-SERVER", "instance_count": len(instances), "instances": instances}).encode()
        reply = run(listener.heartbeat(FakeRequest({"x-watchdog-token": "t"}, body)))
        self.assertEqual(reply.status_code, 200, reply.content)

    def stored(self):
        with listener.db() as conn:
            row = conn.execute("SELECT last_payload_json, instance_alert FROM sources WHERE source_id = 'BACKUP-SERVER'").fetchone()
        return json.loads(row["last_payload_json"]), row["instance_alert"]

    def events(self):
        with listener.db() as conn:
            return [row["event_type"] for row in conn.execute(
                "SELECT event_type FROM events WHERE source_id = 'BACKUP-SERVER' AND event_type IN ('instance_missing', 'instances_restored') ORDER BY id")]

    def seed(self, seconds_missing, alert="ok"):
        mark = {"caribbean": (datetime.now(timezone.utc) - timedelta(seconds=seconds_missing)).isoformat()}
        with listener.db() as conn:
            conn.execute("INSERT INTO sources (source_id, last_seen_utc, last_payload_json, state, state_changed_utc, expected_interval_seconds, instance_alert)"
                         " VALUES ('BACKUP-SERVER', ?, ?, 'healthy', ?, 10, ?)", (T0.isoformat(), json.dumps({"hostname": "BACKUP-SERVER", "_absent": mark}), T0.isoformat(), alert))

    def test_a_missing_feed_gets_a_clock_that_is_kept_and_then_removed_and_nothing_is_raised(self):
        self.send("usa")
        first = self.stored()[0]["_absent"]["caribbean"]
        self.send("usa")
        self.assertEqual(self.stored()[0]["_absent"]["caribbean"], first)
        self.send("usa", "caribbean")
        self.assertNotIn("_absent", self.stored()[0])
        self.assertEqual(self.events(), [])                                        # a blip: no instance_missing, no instances_restored
        self.assertEqual(self.stored()[1], "ok")

    def test_after_the_grace_it_is_raised_once_and_its_return_is_announced(self):
        self.seed(60)
        self.send("usa")
        self.assertEqual(self.events(), ["instance_missing"])
        self.assertEqual(self.stored()[1], "missing")
        self.send("usa")
        self.assertEqual(self.events(), ["instance_missing"])                      # not raised again
        self.send("usa", "caribbean")
        self.assertEqual(self.events(), ["instance_missing", "instances_restored"])
        self.assertEqual(self.stored()[1], "ok")

    def test_inside_the_grace_a_machine_that_was_already_flagged_stays_flagged(self):
        self.seed(10, alert="missing")
        self.send("usa")
        self.assertEqual(self.stored()[1], "missing")                               # an unjudged reading leaves the stored state alone
        self.assertEqual(self.events(), [])


class NoAutomaticSwitchingTests(Wiring):
    """Access goes OFF when the super user switches it OFF and comes back ON when he switches it ON; nothing else (a timer, a node coming back, a restart, a mail retry) does it."""

    def change(self, kind, seconds=0):
        return standby.Transition(kind, T0 + timedelta(seconds=seconds), "reason", T0)

    def control_events(self):
        with listener.db() as conn:
            return [row["event_type"] for row in conn.execute("SELECT event_type FROM events WHERE source_id = 'CONTROL' ORDER BY id")]

    def go_through_a_rough_patch(self):
        run(listener._apply_transition(self.change("down")))
        run(listener._apply_transition(self.change("up", 300)))
        run(listener.control_notice_step())
        listener.CONTROL["state"] = control.ControlState()                          # a restart forgets memory ...
        run(listener._load_control())                                               # ... and reads the database

    def test_off_stays_off_through_a_peer_outage_a_mail_retry_and_a_restart(self):
        self.prefer(V)
        self.switch_off()
        self.go_through_a_rough_patch()
        self.assertFalse(listener.CONTROL["state"].access_on)
        self.assertEqual(self.control_events(), ["access_off"])                     # no access_on was ever recorded

    def test_off_stays_off_when_the_mail_server_is_down_and_comes_back(self):
        self.switch_off()
        FakeSmtp.down = True
        run(listener.control_notice_step())
        FakeSmtp.down = False
        run(listener.control_notice_step())
        self.assertFalse(listener.CONTROL["state"].access_on)
        self.assertEqual(self.control_events(), ["access_off"])

    def test_on_stays_on_through_the_same_rough_patch(self):
        self.prefer(V)
        self.go_through_a_rough_patch()
        self.assertTrue(listener.CONTROL["state"].access_on)
        self.assertEqual(self.control_events(), [])

    def test_the_only_place_that_switches_access_is_the_one_command_path(self):
        import inspect
        import re
        source = Path(listener.__file__).read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"control\.set_access\(", source)), 1)
        self.assertIn("control.set_access(", inspect.getsource(listener.apply_control_command))
        self.assertIsNone(re.search(r"access_on\s*=", source))                       # no other code writes the flag
        for word in ("auto_off", "auto_on", "switch_back", "off_for", "expires_at"):
            self.assertNotIn(word, source, word)


if __name__ == "__main__":
    unittest.main(verbosity=2)
