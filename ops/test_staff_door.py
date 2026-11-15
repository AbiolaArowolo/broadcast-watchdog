# File:         test_staff_door.py
# Purpose:      Tests of the STAFF DOOR: staff may sign in, and use the staff pages, only through a second port that the listener opens next to its main one (8421 beside 8420),
#               which Core publishes on its own loopback and reaches only through Tailscale. The super user is never bound. The launcher that opens the second port
#               (app/serve.py) must accept exactly the flags the old uvicorn command line had, and must never stop the listener from starting.
# Inputs:       None. Real throwaway listeners on 127.0.0.1 with their own databases, ports and keys (nothing leaves this machine).
# Outputs:      Printed unittest results; exit code 1 if anything fails.
# Dependencies: core-listener/app/serve.py, main.py, docker-compose.yml and Dockerfile in the same folder; python + fastapi + uvicorn.
"""Run: python ops/test_staff_door.py

What is proven here, in plain words: the launcher reads the same flags production always used; it opens the second port only when asked and only when that port is usable and not the
main one; a busy or bad staff port can never stop the listener from starting; the application learns which door a request used from the socket itself (a client cannot write it, a
Host header cannot fake it); a staff sign-in through the main port is refused before any password is looked at, and through the staff door it works; a staff cookie is worth nothing
on the main port; the super user signs in on both; and a listener started without the launcher (the standby on the VPS) accepts no staff sign-in at all.
"""
import http.client
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

OPS = Path(__file__).resolve().parent
LISTENER = OPS.parent / "core-listener"
sys.path.insert(0, str(LISTENER))
from app import control_login  # noqa: E402

spec = importlib.util.spec_from_file_location("staff_serve", LISTENER / "app" / "serve.py")
serve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(serve)

CONTROL_KEY = "door-test-control-key-0123456789abcdef-abcdef"
SUPER_USER, SUPER_PASSWORD = "super@door.test", "door test super passphrase 42"
STAFF_PASSWORD = "a long enough pass 1"


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def compose_command():
    text = (LISTENER / "docker-compose.yml").read_text(encoding="utf-8")
    return json.loads(re.search(r'^\s*command: (\["python".*\])\s*$', text, re.M).group(1))


class DoorPlanTests(unittest.TestCase):
    """Which second port to open, if any: only a usable port that is not the main one."""

    def plan(self, value, main=8420):
        return serve.door_plan({} if value is None else {serve.DOOR_ENV: value}, main)

    def test_nothing_asked_means_no_door_and_no_complaint(self):
        self.assertEqual(self.plan(None), (None, ""))
        self.assertEqual(self.plan(""), (None, ""))
        self.assertEqual(self.plan("   "), (None, ""))

    def test_a_usable_port_is_the_door(self):
        self.assertEqual(self.plan("8421"), (8421, ""))
        self.assertEqual(self.plan(" 8421 "), (8421, ""))
        self.assertEqual(self.plan("1024")[0], 1024)
        self.assertEqual(self.plan("65535")[0], 65535)

    def test_anything_else_is_no_door_with_a_reason(self):
        for bad in ("0", "80", "443", "1023", "65536", "-1", "abc", "8421.5", "8421;", "0x2105", "８４２１"):
            port, note = self.plan(bad)
            self.assertIsNone(port, bad)
            self.assertTrue(note, bad)

    def test_the_door_can_never_be_the_main_port(self):
        port, note = self.plan("8420")
        self.assertIsNone(port)
        self.assertIn("main", note)
        self.assertEqual(self.plan("9000", main=9000)[0], None)


class ArgumentTests(unittest.TestCase):
    def test_it_reads_exactly_the_flags_production_uses(self):
        command = compose_command()
        self.assertEqual(command[:3], ["python", "-m", "app.serve"])
        args = serve.parse_arguments(command[3:])
        self.assertEqual((args.host, args.port, args.timeout_graceful_shutdown, args.no_proxy_headers), ("0.0.0.0", 8420, 3, True))

    def test_the_dockerfile_default_is_the_same_launcher_with_the_proxy_flag(self):
        text = (LISTENER / "Dockerfile").read_text(encoding="utf-8")
        command = json.loads(re.search(r"^CMD (\[.*\])\s*$", text, re.M).group(1))
        self.assertEqual(command[:3], ["python", "-m", "app.serve"])
        self.assertTrue(serve.parse_arguments(command[3:]).no_proxy_headers)

    def test_a_flag_it_does_not_know_stops_it_loudly_rather_than_being_ignored(self):
        with self.assertRaises(SystemExit):
            serve.parse_arguments(["--host", "0.0.0.0", "--workers", "4"])

    def test_without_the_proxy_flag_proxy_headers_would_be_believed_so_the_flag_must_stay(self):
        self.assertFalse(serve.parse_arguments(["--port", "8420"]).no_proxy_headers)
        self.assertIn("--no-proxy-headers", compose_command())


class DeploymentFilesTests(unittest.TestCase):
    def test_compose_publishes_the_door_on_cores_own_loopback_only(self):
        text = (LISTENER / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn('"127.0.0.1:8421:8421"', text)
        self.assertNotIn('"8421:8421"', text)                         # never on every interface
        self.assertIn("WATCHDOG_STAFF_DOOR_PORT: ${WATCHDOG_STAFF_DOOR_PORT:-8421}", text)
        self.assertIn('"8420:8420"', text)                            # the senders' port is exactly as before

    def test_the_door_listens_on_the_whole_container_network_unless_a_test_says_otherwise(self):
        # A door bound to the container's own loopback could never be reached through Docker's published port: staff would be locked out with nothing visibly wrong.
        self.assertEqual(serve.DOOR_HOST_DEFAULT, "0.0.0.0")
        self.assertIn("os.environ.get(DOOR_HOST_ENV, DOOR_HOST_DEFAULT)", (LISTENER / "app" / "serve.py").read_text(encoding="utf-8"))
        self.assertNotIn("WATCHDOG_STAFF_DOOR_HOST", (LISTENER / "docker-compose.yml").read_text(encoding="utf-8"))

    def test_the_launcher_listens_on_loopback_unless_told_and_its_one_bind_all_literal_is_the_annotated_door_default(self):
        # CI's bandit (medium and above) rejects an unexplained "bind to every address" literal; and a default of every address would be a trap for anyone who starts it by hand.
        self.assertEqual(serve.parse_arguments([]).host, "127.0.0.1")
        source = (LISTENER / "app" / "serve.py").read_text(encoding="utf-8")
        self.assertEqual(source.count('"0.0.0.0"'), 1)
        self.assertEqual(source.count("# nosec B104"), 1)
        line = next(line for line in source.splitlines() if '"0.0.0.0"' in line)
        self.assertTrue(line.startswith("DOOR_HOST_DEFAULT") and "# nosec B104 --" in line)
        for flag in ("--host", "0.0.0.0"):                                            # production still names its address explicitly
            self.assertIn(flag, (LISTENER / "docker-compose.yml").read_text(encoding="utf-8"))

    def test_the_launcher_is_part_of_the_image(self):
        self.assertTrue((LISTENER / "app" / "serve.py").is_file())
        self.assertIn("COPY app ./app", (LISTENER / "Dockerfile").read_text(encoding="utf-8"))


class LiveDoorTests(unittest.TestCase):
    """The real launcher, the real listener, two real sockets."""
    processes = []

    @classmethod
    def tearDownClass(cls):
        for process in cls.processes:
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()

    @classmethod
    def start(cls, main_port, door_port=None, door_value=None, with_launcher=True, extra_env=None):
        work = tempfile.mkdtemp(prefix="staff-door-")
        env = {**os.environ, "WATCHDOG_SHARED_TOKEN": "door-test-shared-token-0123456789abcdef-abcdef", "WATCHDOG_DB_PATH": os.path.join(work, "t.db"),
               "WATCHDOG_ENABLE_DOCS": "false", "WATCHDOG_CONTROL_KEY": CONTROL_KEY, "WATCHDOG_EXPECTED_INSTANCES": '{"MAIN":["usa","caribbean"]}',
               "WATCHDOG_LISTENER_STARTUP_GRACE_SECONDS": "0", "WATCHDOG_SUPER_USER": SUPER_USER,
               "WATCHDOG_SUPER_PASSWORD_HASH": control_login.hash_password(SUPER_PASSWORD, n=16), "WATCHDOG_STAFF_DOOR_HOST": "127.0.0.1"}
        env.pop(serve.DOOR_ENV, None)
        env.pop(serve.BOUND_ENV, None)
        if door_value is not None:
            env[serve.DOOR_ENV] = door_value
        elif door_port:
            env[serve.DOOR_ENV] = str(door_port)
        env.update(extra_env or {})
        if with_launcher:
            command = [sys.executable, "-m", "app.serve", "--host", "127.0.0.1", "--port", str(main_port), "--no-proxy-headers"]
        else:
            command = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(main_port), "--no-proxy-headers"]
        process = subprocess.Popen(command, cwd=LISTENER, env=env, stdout=open(os.path.join(work, "out.log"), "w"), stderr=subprocess.STDOUT)
        cls.processes.append(process)
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError("the listener stopped at start: " + open(os.path.join(work, "out.log")).read()[-800:])
            try:
                if cls.call(main_port, "GET", "/offline.html")[0] == 200:
                    return process
            except OSError:
                time.sleep(0.3)
        raise RuntimeError("the listener did not start")

    @staticmethod
    def call(port, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        sent = {"Sec-Fetch-Site": "same-origin", **(headers or {})}
        data = None
        if body is not None:
            data, sent["Content-Type"] = json.dumps(body).encode(), "application/json"
        connection.request(method, path, body=data, headers=sent)
        reply = connection.getresponse()
        raw = reply.read().decode("utf-8", "replace")
        info = {key.lower(): value for key, value in reply.getheaders()}
        connection.close()
        try:
            return reply.status, info, json.loads(raw)
        except ValueError:
            return reply.status, info, raw

    def add_ada(self, main):
        status, _, data = self.call(main, "POST", "/control/panel/staff", {"action": "add_person", "username": "ada", "display": "Ada", "password": STAFF_PASSWORD}, {"X-Watchdog-Control": CONTROL_KEY})
        self.assertEqual(status, 200, data)

    def sign_in(self, port, user="ada", password=STAFF_PASSWORD, headers=None):
        return self.call(port, "POST", "/control/session", {"user": user, "password": password}, headers)

    def test_both_doors_answer_and_the_staff_one_is_the_door(self):
        main, door = free_port(), free_port()
        self.start(main, door)
        self.assertEqual(self.call(main, "GET", "/health")[0], 200)
        self.assertEqual(self.call(door, "GET", "/health")[0], 200)

        self.add_ada(main)
        status, _, data = self.sign_in(main)
        self.assertEqual(status, 403, data)                                          # the main port: refused before any password is looked at
        self.assertIn("office PC", json.dumps(data))
        for _ in range(7):                                                           # refusals at the main port are not password guesses ...
            self.assertEqual(self.sign_in(main)[0], 403)
        status, headers, data = self.sign_in(door)
        self.assertEqual(status, 200, data)                                          # ... so they never lock the staff door
        cookie = headers["set-cookie"].split(";")[0]
        self.assertTrue(cookie.startswith("watchdog_staff="))

        self.assertEqual(self.call(door, "GET", "/staff/state", headers={"Cookie": cookie})[0], 200)
        self.assertEqual(self.call(door, "GET", "/staff", headers={"Cookie": cookie})[0], 200)
        self.assertEqual(self.call(main, "GET", "/staff/state", headers={"Cookie": cookie})[0], 401)          # the same cookie is worth nothing on the main port
        status, headers, _ = self.call(main, "GET", "/staff", headers={"Cookie": cookie})
        self.assertEqual((status, headers.get("location")), (302, "/control/login"))
        status, _, _ = self.call(main, "POST", "/staff/act", {"action": "start_shift"}, {"Cookie": cookie})
        self.assertEqual(status, 401)
        status, _, data = self.call(door, "POST", "/staff/act", {"action": "start_shift"}, {"Cookie": cookie})
        self.assertEqual((status, data.get("me", {}).get("on_shift")), (200, True))

    def test_a_client_cannot_make_the_main_port_look_like_the_door(self):
        main, door = free_port(), free_port()
        self.start(main, door)
        self.add_ada(main)
        for fake in (f"127.0.0.1:{door}", f"localhost:{door}", "staff-door", f"127.0.0.1:{main}"):
            status, _, _ = self.sign_in(main, headers={"Host": fake, "X-Forwarded-Port": str(door), "X-Forwarded-Host": fake, "Forwarded": f"host={fake}"})
            self.assertEqual(status, 403, fake)

    def test_forwarding_headers_cannot_change_who_is_asking(self):
        # --no-proxy-headers must really be in force. Otherwise uvicorn believes X-Forwarded-For from a loopback peer and every wrong guess looks like a new visitor, so the
        # staff lockout would never close.
        main, door = free_port(), free_port()
        self.start(main, door)
        self.add_ada(main)
        codes = [self.sign_in(door, password="wrong password number 1", headers={"X-Forwarded-For": f"198.51.100.{n}"})[0] for n in range(6)]
        self.assertEqual(codes, [401, 401, 401, 401, 401, 429])

    def test_the_super_user_signs_in_on_both_doors(self):
        main, door = free_port(), free_port()
        self.start(main, door)
        for port in (main, door):
            status, headers, data = self.sign_in(port, SUPER_USER, SUPER_PASSWORD)
            self.assertEqual(status, 200, (port, data))
            self.assertIn("watchdog_control=", headers["set-cookie"])

    def test_a_busy_door_port_never_stops_the_listener_and_staff_stay_locked_out(self):
        main, door = free_port(), free_port()
        blocker = socket.socket()
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):                                  # Windows lets sockets share a port unless this is set
            blocker.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        blocker.bind(("127.0.0.1", door))
        blocker.listen(1)
        self.addCleanup(blocker.close)
        process = self.start(main, door)
        self.assertIsNone(process.poll())
        self.assertEqual(self.call(main, "GET", "/health")[0], 200)
        self.add_ada(main)
        self.assertEqual(self.sign_in(main)[0], 403)

    def test_a_door_the_launcher_could_not_open_is_not_believed_to_exist(self):
        # The listener believes in a door only when the launcher says it opened one, so a stray setting alone must change nothing.
        main = free_port()
        self.start(main, with_launcher=False, door_value="8421")
        self.add_ada(main)
        self.assertEqual(self.sign_in(main)[0], 403)                                 # the standby on the VPS starts like this: no staff sign-in at all

    def test_a_door_claimed_from_outside_the_launcher_is_never_believed(self):
        main = free_port()
        self.start(main, extra_env={serve.BOUND_ENV: str(main)})                    # someone set the launcher's own word by hand: the launcher clears it before it starts
        self.add_ada(main)
        self.assertEqual(self.sign_in(main)[0], 403)

    def test_a_door_that_is_the_main_port_is_ignored(self):
        main = free_port()
        self.start(main, door_value=str(main))
        self.add_ada(main)
        self.assertEqual(self.sign_in(main)[0], 403)                                 # not treated as a door: every request would otherwise count as one

    def test_a_listener_asked_for_no_door_runs_exactly_as_before(self):
        main = free_port()
        self.start(main)
        self.assertEqual(self.call(main, "GET", "/health")[0], 200)
        self.assertEqual(self.call(main, "GET", "/status", headers={"X-Watchdog-Token": "door-test-shared-token-0123456789abcdef-abcdef"})[0], 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
