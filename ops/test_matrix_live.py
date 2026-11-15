# File:         test_matrix_live.py
# Purpose:      End-to-end check of the AV matrix feature with REAL parts: a stand-in matrix on a real UDP socket, a Core-shaped listener (real HTTP, real FastAPI) that talks to it, and a
#               standby-shaped listener started through the launcher with its staff door. Proves the bytes on the wire, a switch confirmed by a read-back, the "key only" rule on a real
#               server, two presses at once, a silent matrix, and the access rule of 2026-11-10: the super user from any address, and a registered office PC (the staff door) with no sign-in. Never touches production: its own ports, databases and keys.
# Inputs:       optional argv[1] = listener folder (default: the repo's core-listener), argv[2] = first port (default 8491; the standby uses +1 and its door +2).
# Outputs:      PASS/FAIL lines; exit code 1 if anything is wrong.
# Dependencies: python + fastapi + uvicorn + the listener source, and ops/matrix_fixtures.py (the stand-in matrix).
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
LISTENER = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core-listener")
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8491
sys.argv = sys.argv[:1]                       # matrix_fixtures and the app modules must not see this script's arguments
import matrix_fixtures as fx  # noqa: E402

SHARED = "live-test-shared-key-0123456789abcdef"
CONTROL = "live-test-control-key-0123456789abcdef"
KEYS = {"MAIN": "live-test-main-key-0123456789abcdef", "BACKUP-SERVER": "live-test-backup-key-0123456789abcdef",
        "SITE-A": "live-test-site_a-key-0123456789abcdef", "SITE-B": "live-test-site_b-key-0123456789abcdef"}
problems = []
servers = []


def check(label, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + label + ("" if ok or not detail else f"   ({detail})"))
    if not ok:
        problems.append(label)


def start(command, port, db, extra_env):
    env = {**os.environ, "WATCHDOG_SHARED_TOKEN": SHARED, "WATCHDOG_DB_PATH": db, "WATCHDOG_ENABLE_DOCS": "false", "WATCHDOG_CONTROL_KEY": CONTROL,
           "WATCHDOG_EXPECTED_INSTANCES": '{"MAIN":["usa","caribbean"]}', "WATCHDOG_LISTENER_STARTUP_GRACE_SECONDS": "600", **extra_env}
    process = subprocess.Popen(command, cwd=LISTENER, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    servers.append(process)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/offline.html", timeout=1).read()
            return base, process
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    raise RuntimeError("the throwaway listener did not start on port " + str(port))


def call(base, method, path, headers=None, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=15) as reply:
            return reply.status, reply.read().decode("utf-8", "replace"), {k.lower(): v for k, v in reply.headers.items()}
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace"), {k.lower(): v for k, v in error.headers.items()}


def json_of(text):
    try:
        return json.loads(text)
    except ValueError:
        return {}


def stop(process):
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()


def route_datagrams(standin):
    """The ASCII route datagrams the stand-in received (everything that is not the 20-byte binary read packet)."""
    return [data.decode("ascii") for data in standin.received if len(data) != 20]


def main():
    work = tempfile.mkdtemp()
    key = {"X-Watchdog-Control": CONTROL}
    standin = fx.StandInMatrix(fx.LIVE_ROUTING)

    # ---- Core-shaped listener with the matrix attached ------------------------------------------------------------------------------------------------
    core, core_process = start([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT)], PORT, os.path.join(work, "h.db"),
                                   {"WATCHDOG_MATRIX_HOST": "127.0.0.1", "WATCHDOG_MATRIX_PORT": str(standin.port)})
    check("the matrix status needs the control key", call(core, "GET", "/control/matrix")[0] == 401)
    check("a wrong key is refused", call(core, "GET", "/control/matrix", {"X-Watchdog-Control": "x" * 40})[0] == 401)
    code, text, _ = call(core, "GET", "/control/matrix", key)
    status = json_of(text)
    check("with the key the real server reads the real socket: the eight outputs come back", code == 200 and status.get("routing") == list(fx.LIVE_ROUTING) and status.get("reachable") is True, text[:160])
    check("no preset is active for the routing the real matrix showed", status.get("active") is None)
    reads = [data for data in standin.received if len(data) == 20]
    check("the question on the wire is exactly the manufacturer's read-status packet", bool(reads) and reads[0].hex(" ") == "a5 6c 14 00 82 01 01 00 00 00 00 00 00 00 00 00 53 fc 01 ae", reads[0].hex(" ") if reads else "none")
    code, text, _ = call(core, "GET", "/control/matrix?fresh=1", key)
    check("?fresh=1 reaches the real server's query string", code == 200 and json_of(text).get("routing") == list(fx.LIVE_ROUTING), text[:100])

    code, text, _ = call(core, "POST", "/control/matrix/switch", key, {"preset": "main", "operator": "live test", "via": "control-key"})
    result = json_of(text)
    check("MAIN PC is confirmed by a read-back from the real matrix", code == 200 and result.get("state") == "done" and result.get("after") == list(fx.MAIN_ROUTING), text[:200])
    check("only the one output that was wrong was sent, as the guide's ASCII form", route_datagrams(standin) == ["2v5."], str(route_datagrams(standin)))
    check("the stand-in's own routing changed only output 5", standin.routing == list(fx.MAIN_ROUTING), str(standin.routing))
    code, text, _ = call(core, "POST", "/control/matrix/switch", key, {"preset": "main"})
    check("pressing MAIN PC again says it is already in place and sends nothing", json_of(text).get("state") == "already" and route_datagrams(standin) == ["2v5."], text[:160])
    code, text, _ = call(core, "POST", "/control/matrix/switch", key, {"preset": "backup", "operator": "live test"})
    check("BACKUP PC is confirmed and leaves outputs 3, 4, 7 and 8 as they were", json_of(text).get("state") == "done" and standin.routing == [7, 7, 2, 3, 7, 5, 7, 7], str(standin.routing))
    code, text, _ = call(core, "GET", "/control/matrix", key)
    status = json_of(text)
    check("the status now names BACKUP PC as active and carries the last successful switch", status.get("active") == "backup" and (status.get("last_success") or {}).get("preset") == "backup", text[:200])

    code, text, _ = call(core, "POST", "/control/matrix/switch", key, {"preset": "brightsign"})
    check("the emergency preset without its confirmation is refused and sends nothing", code == 400 and route_datagrams(standin).count("1v1,2,3,4,5,6.") == 0, text[:120])
    code, text, _ = call(core, "POST", "/control/matrix/switch", key, {"preset": "5All."})
    check("a command string is not a preset and is refused", code == 400 and not any("All" in d for d in route_datagrams(standin)), text[:120])

    code, text, headers = call(core, "POST", "/control/session", {}, {"key": CONTROL})
    cookie = headers.get("set-cookie", "").split(";")[0]
    check("a super user's session cookie exists (the old key sign-in, no password set)", code == 200 and cookie.startswith("watchdog_control="), text[:100])
    code, text, _ = call(core, "POST", "/control/matrix/switch", {"Cookie": cookie, "Sec-Fetch-Site": "same-origin"}, {"preset": "main"})
    check("that cookie alone can never switch the matrix on Core", code == 401 and standin.routing == [7, 7, 2, 3, 7, 5, 7, 7], f"{code} {text[:100]}")
    # the super user on CORE'S OWN address (rule of 2026-11-10): Core reads and switches the matrix itself, through the browser endpoints
    code, text, _ = call(core, "GET", "/control/panel/matrix")
    check("on Core's own address, with no session, the browser endpoint does not exist", code == 404, f"{code} {text[:80]}")
    code, text, _ = call(core, "POST", "/control/panel/matrix", {"Sec-Fetch-Site": "same-origin"}, {"preset": "main"})
    check("and a press with no session is refused the same way and sends nothing", code == 404 and standin.routing == [7, 7, 2, 3, 7, 5, 7, 7], f"{code} {text[:80]}")
    code, text, _ = call(core, "GET", "/control/panel/matrix", {"Cookie": cookie})
    seen = json_of(text)
    check("the super user's session reads the matrix directly on Core's own address", code == 200 and seen.get("routing") == [7, 7, 2, 3, 7, 5, 7, 7] and seen.get("core_reached") is True, f"{code} {text[:120]}")
    code, text, _ = call(core, "POST", "/control/panel/matrix", {"Cookie": cookie}, {"preset": "main"})
    check("a press that is not from the page itself is refused and sends nothing", code == 403 and standin.routing == [7, 7, 2, 3, 7, 5, 7, 7], f"{code} {text[:80]}")
    code, text, _ = call(core, "POST", "/control/panel/matrix", {"Cookie": cookie, "Sec-Fetch-Site": "same-origin"}, {"preset": "main"})
    check("the super user's press from the page switches the real matrix and is confirmed by a read-back", code == 200 and json_of(text).get("state") == "done" and standin.routing == list(fx.MAIN_ROUTING), f"{code} {text[:160]}")
    code, text, _ = call(core, "GET", "/control/matrix", key)
    newest = (json_of(text).get("recent") or [{}])[0]
    check("the audit row says the press came by Core's own site", newest.get("via") == "core-site" and newest.get("preset") == "main" and newest.get("result") == "done", str(newest)[:160])
    code, text, _ = call(core, "POST", "/control/panel/matrix", {"Cookie": cookie, "Sec-Fetch-Site": "same-origin"}, {"preset": "brightsign"})
    check("the emergency preset through the browser endpoint also needs its confirmation", code == 400 and standin.routing == list(fx.MAIN_ROUTING), f"{code} {text[:80]}")

    # two presses at once: one wins, the other is told it is busy (or finds the work already done); the matrix is never left half way
    standin.routing[:] = fx.LIVE_ROUTING
    outcomes = []
    gate = threading.Barrier(2)

    def press(name):
        gate.wait()
        outcomes.append(call(core, "POST", "/control/matrix/switch", key, {"preset": name, "operator": "race"}))
    threads = [threading.Thread(target=press, args=("main",)), threading.Thread(target=press, args=("backup",))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    codes = sorted(code for code, _, _ in outcomes)
    final = tuple(standin.routing)
    check("two presses at once: one is refused as busy or both finish in turn, never a crash", codes in ([200, 200], [200, 409]), str(codes))
    check("and outputs 1, 2, 5 and 6 end as one whole preset, never a mixture", (final[0], final[1], final[4], final[5]) in ((2, 2, 2, 3), (7, 7, 7, 5)), str(final))

    # a silent matrix: the next two questions (one read, two tries) get no answer
    standin.replies[:] = [None, None]
    code, text, _ = call(core, "GET", "/control/matrix?fresh=1", key)
    silent = json_of(text)
    check("a silent matrix shows as not reached, with no routing and no active preset", code == 200 and silent.get("reachable") is False and silent.get("routing") is None and silent.get("active") is None, text[:160])
    code, text, _ = call(core, "GET", "/control/matrix?fresh=1", key)
    check("and it is read normally again as soon as it answers", json_of(text).get("reachable") is True)

    # ---- a standby-shaped listener started through the launcher, with its staff door ----------------------------------------------------------------------
    door_port = PORT + 2
    standby, standby_process = start([sys.executable, "-m", "app.serve", "--host", "127.0.0.1", "--port", str(PORT + 1)], PORT + 1, os.path.join(work, "s.db"),
                                     {"WATCHDOG_ROLE": "standby", "WATCHDOG_SENDER_AUTH": "per-sender", **{f"WATCHDOG_SENDER_KEY_{k.replace('-', '_')}": v for k, v in KEYS.items()},
                                      "WATCHDOG_PRIMARY_URL": f"http://127.0.0.1:{PORT}/health", "WATCHDOG_STAFF_DOOR_PORT": str(door_port), "WATCHDOG_STAFF_DOOR_HOST": "127.0.0.1"})
    door = f"http://127.0.0.1:{door_port}"
    code, text, headers = call(standby, "POST", "/control/session", {}, {"key": CONTROL})
    cookie = headers.get("set-cookie", "").split(";")[0]
    same_origin = {"Cookie": cookie, "Sec-Fetch-Site": "same-origin"}
    check("the standby has a super user session", code == 200 and cookie.startswith("watchdog_control="), text[:100])
    code, text, _ = call(standby, "GET", "/control/panel/matrix")
    check("on the standby's PUBLIC address with no session the matrix endpoint does not exist", code == 404, f"{code} {text[:80]}")
    code, text, _ = call(standby, "POST", "/control/panel/matrix", {"Sec-Fetch-Site": "same-origin"}, {"preset": "main"})
    check("and a press there with no session is refused the same way", code == 404, f"{code} {text[:80]}")
    code, text, _ = call(standby, "GET", "/control/panel/matrix", {"Cookie": cookie})
    check("the super user on the standby's public address IS served (rule of 2026-11-10), and the standby says Core was not reached here", code == 200 and json_of(text).get("core_reached") is False, f"{code} {text[:100]}")
    code, text, _ = call(standby, "POST", "/control/panel/matrix", same_origin, {"preset": "main"})
    check("and a press from the page there is relayed, saying it may or may not have switched when Core cannot be reached", code == 200 and json_of(text).get("state") == "core_unreachable", f"{code} {text[:100]}")
    code, text, _ = call(door, "GET", "/control/panel/matrix")
    check("an office PC (the staff door) with NO sign-in is served too", code == 200 and json_of(text).get("core_reached") is False, f"{code} {text[:100]}")
    code, text, _ = call(door, "POST", "/control/panel/matrix", {"Sec-Fetch-Site": "same-origin"}, {"preset": "main"})
    check("and its press from the page is relayed with no sign-in", code == 200 and json_of(text).get("state") == "core_unreachable", f"{code} {text[:100]}")
    code, text, _ = call(door, "POST", "/control/panel/matrix", {}, {"preset": "main"})
    check("but an office PC press that is not from the page itself is refused (the certificate is shown to any page)", code == 403, f"{code} {text[:80]}")
    code, text, _ = call(door, "POST", "/control/panel/matrix", {"Sec-Fetch-Site": "same-origin"}, {"preset": "brightsign"})
    check("and the emergency preset asks for its confirmation on the door too", code == 400, f"{code} {text[:80]}")
    code, text, _ = call(door, "GET", "/control/panel/matrix", {"Cookie": cookie})
    answer = json_of(text)
    check("on the staff door with the super session it answers, and says Core was not reached (the peer is plain http here, never trusted with the key)", code == 200 and answer.get("core_reached") is False and "routing" not in answer, f"{code} {text[:120]}")
    code, text, _ = call(door, "POST", "/control/panel/matrix", {"Cookie": cookie}, {"preset": "main"})
    check("a press on the door that is not from the page itself is refused", code == 403, f"{code} {text[:80]}")
    code, text, _ = call(door, "POST", "/control/panel/matrix", same_origin, {"preset": "main"})
    check("a press from the page, with Core unreachable, says it may or may not have switched", code == 200 and json_of(text).get("state") == "core_unreachable" and "may or may not" in json_of(text).get("message", ""), f"{code} {text[:160]}")
    code, text, _ = call(door, "POST", "/control/panel/matrix", same_origin, {"preset": "main", "operator": "someone else"})
    check("the browser cannot choose the operator", code == 400, f"{code} {text[:80]}")
    code, text, _ = call(door, "GET", "/control/panel/state", {"Cookie": cookie})
    check("the panel data on the door says the card may be shown", json_of(text).get("matrix") == {"available": True}, text[:60])
    code, text, _ = call(standby, "GET", "/control/panel/state", {"Cookie": cookie})
    check("the same panel data on the public address says so too (the super user may use it from anywhere)", json_of(text).get("matrix") == {"available": True}, text[:60])
    code, text, _ = call(door, "GET", "/control/panel", {"Cookie": cookie})
    check("the panel page itself carries the card, hidden until the data says so", code == 200 and 'id="matrix" hidden' in text and "192.168" not in text, f"{code}")
    # ---- the DOOR PASS (his choice, 2026-11-10): the standby hands it out on the staff door only, and Core accepts it from the same PC on the watchdog address ----------------
    code, text, headers = call(door, "GET", "/control/panel/matrix")
    pass_header = headers.get("set-cookie", "")
    check("the staff door hands an office PC a pass with the right attributes", code == 200 and pass_header.startswith("watchdog_office=") and all(a in pass_header for a in
          ("Domain=corp.example", "Path=/control/panel/matrix", "HttpOnly", "SameSite=Strict", "Max-Age=259200")), pass_header[:60])
    code, text, headers = call(standby, "GET", "/control/panel/matrix", {"Cookie": cookie})
    check("the standby's public address never hands one out, even to the super user", code == 200 and headers.get("set-cookie", "") == "", f"{code}")
    code, text, headers = call(door, "GET", "/control/login")
    check("the door hands a pass on ANY page, the sign-in page included, with nobody signed in", headers.get("set-cookie", "").startswith("watchdog_office="), f"{code}")
    code, text, headers = call(door, "POST", "/control/panel/matrix", {"Sec-Fetch-Site": "same-origin"}, {"preset": "nope"})
    check("and a press answer on the door carries one too", headers.get("set-cookie", "").startswith("watchdog_office="), f"{code}")
    code, text, headers = call(standby, "GET", "/control/login")
    check("while the standby's public address hands out nothing on any page", "watchdog_office" not in headers.get("set-cookie", ""), f"{code}")
    pass_cookie = pass_header.split(";")[0]
    standin.routing[:] = fx.LIVE_ROUTING
    before = len(route_datagrams(standin))
    code, text, _ = call(core, "GET", "/control/panel/matrix?fresh=1", {"Cookie": pass_cookie})          # fresh: a plain read may reuse a reading under two seconds old
    seen = json_of(text)
    check("Core accepts that pass on its own address and reads the real matrix for it", code == 200 and seen.get("routing") == list(fx.LIVE_ROUTING) and seen.get("core_reached") is True, f"{code} {text[:100]}")
    code, text, _ = call(core, "GET", "/control/panel/matrix", {"Cookie": "watchdog_office=p1.1.2"})
    check("a made-up pass is refused (404)", code == 404, f"{code}")
    code, text, _ = call(core, "POST", "/control/panel/matrix", {"Cookie": pass_cookie}, {"preset": "main"})
    check("a press by pass that is not from the page itself is refused and sends nothing", code == 403 and len(route_datagrams(standin)) == before, f"{code}")
    code, text, _ = call(core, "POST", "/control/panel/matrix", {"Cookie": pass_cookie, "Sec-Fetch-Site": "same-origin"}, {"preset": "main"})
    check("a press by pass from the page switches the real matrix, confirmed by a read-back", code == 200 and json_of(text).get("state") == "done" and standin.routing == list(fx.MAIN_ROUTING), f"{code} {text[:140]}")
    code, text, _ = call(core, "GET", "/control/matrix", key)
    newest = (json_of(text).get("recent") or [{}])[0]
    check("the audit row names the office PC and the way in office-pass", newest.get("operator") == "office PC" and newest.get("via") == "office-pass" and newest.get("preset") == "main", str(newest)[:150])
    code, text, _ = call(core, "GET", "/control/panel/state", {"Cookie": pass_cookie})
    check("the pass is not a login session anywhere else", code == 401, f"{code}")
    stop(core_process)
    stop(standby_process)
    standin.close()


if __name__ == "__main__":
    try:
        main()
    finally:
        for server in servers:
            if server.poll() is None:
                stop(server)
    print(("\nFAILED: " + "; ".join(problems)) if problems else "\nALL PASS")
    sys.exit(1 if problems else 0)
