# File:         test_control_live.py
# Purpose:      End-to-end check, over real HTTP against throwaway LOCAL listeners, of the control phase: the OFF gate in front of a real server, the super user's two ways in
#               (key header, signed session cookie), the state surviving a restart, the preferred node reported on /health, and Core and a VPS-shaped listener reading each other.
#               Never touches production: its own databases, ports and keys.
# Inputs:       optional argv[1] = listener folder (default: the repo's core-listener), argv[2] = first port (default 8481; the second listener uses +1).
# Outputs:      PASS/FAIL lines; exit code 1 if anything is wrong.
# Dependencies: python + fastapi + uvicorn + the listener source.
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

LISTENER = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core-listener")
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8481
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


def start(port, db, extra_env):
    env = {**os.environ, "WATCHDOG_SHARED_TOKEN": SHARED, "WATCHDOG_DB_PATH": db, "WATCHDOG_ENABLE_DOCS": "false", "WATCHDOG_CONTROL_KEY": CONTROL,
           "WATCHDOG_EXPECTED_INSTANCES": '{"MAIN":["usa","caribbean"]}', "WATCHDOG_LISTENER_STARTUP_GRACE_SECONDS": "600", **extra_env}
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)], cwd=LISTENER, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        with urllib.request.urlopen(request, timeout=8) as reply:
            return reply.status, reply.read().decode("utf-8", "replace"), {k.lower(): v for k, v in reply.headers.items()}
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace"), {k.lower(): v for k, v in error.headers.items()}


def json_of(text):
    try:
        return json.loads(text)
    except ValueError:
        return {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def call_raw(base, method, path, headers=None, body=None):
    """Like call(), but a redirect is reported as the answer instead of being followed."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=8) as reply:
            return reply.status, reply.read().decode("utf-8", "replace"), {k.lower(): v for k, v in reply.headers.items()}
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace"), {k.lower(): v for k, v in error.headers.items()}


def stop(process):
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()


def main():
    work = tempfile.mkdtemp()
    db = os.path.join(work, "core.db")
    superuser = {"X-Watchdog-Control": CONTROL}
    sender = {"X-Watchdog-Token": SHARED}
    base, process = start(PORT, db, {})

    # ---- ON (the default): nothing is different ---------------------------------------------------------------------------------------------------
    check("access ON: the dashboard page is served", call(base, "GET", "/")[0] == 200)
    code, text, _ = call(base, "GET", "/health")
    check("/health names the preferred node and Core is it", code == 200 and json_of(text).get("preferred") == "CORE", text[:80])
    check("the control door needs the control key", call(base, "GET", "/control")[0] == 401)
    check("the admin key is not the control key", call(base, "GET", "/control", sender)[0] == 401)
    code, text, _ = call(base, "GET", "/control", superuser)
    state = json_of(text)
    check("the super user reads the control state", code == 200 and state.get("access") == "on" and state.get("alerting_now") is True, text[:120])
    check("the key is never echoed back", CONTROL not in text)

    # ---- OFF: users see the page, the system keeps working -----------------------------------------------------------------------------------------
    code, text, _ = call(base, "POST", "/control", superuser, {"access": "off", "notify": False})
    check("the super user switches access OFF", code == 200 and json_of(text).get("access") == "off", text[:120])
    code, text, headers = call(base, "GET", "/")
    check("OFF: a user gets the branded page with a 503", code == 503 and "Service temporarily unavailable" in text and "no-store" in headers.get("cache-control", ""), str(code))
    check("OFF: the page carries the security headers", "default-src" in headers.get("content-security-policy", ""))
    code, text, _ = call(base, "GET", "/status", {"X-Watchdog-Token": SHARED})
    check("OFF: even a valid read key gets no data", code == 503 and json_of(text).get("error") == "service temporarily unavailable", text[:100])
    check("OFF: the live stream is refused", call(base, "GET", "/stream?token=x")[0] == 503)
    check("OFF: an unknown path is closed", call(base, "GET", "/nothing-here")[0] == 503)
    check("OFF: /health still answers (the other node and Docker depend on it)", call(base, "GET", "/health")[0] == 200)
    code, text, _ = call(base, "POST", "/heartbeat", {"X-Watchdog-Token": SHARED}, {"source_id": "MAIN", "hostname": "t", "instance_count": 2})
    check("OFF: studios can still post heartbeats (ingest is not interrupted)", code == 200, f"{code} {text[:100]}")
    check("OFF: a wrong sender key is still refused at the door", call(base, "POST", "/heartbeat", {"X-Watchdog-Token": "wrong"}, {"source_id": "MAIN"})[0] == 401)
    code, text, _ = call(base, "GET", "/status", {**superuser, **sender})
    check("OFF: the super user (control key plus read key) still reads the full status", code == 200 and json_of(text).get("control", {}).get("access") == "off", str(code))
    check("OFF: the status shows the heartbeat that arrived while it was off", any(s.get("source_id") == "MAIN" for s in json_of(text).get("sources", [])))
    check("OFF: his dashboard carries the OFF banner", "ACCESS OFF since" in call(base, "GET", "/", superuser)[1])

    # ---- the browser way in: type the key once, get a signed cookie -------------------------------------------------------------------------------
    check("the login page is reachable while OFF", call(base, "GET", "/control/login")[0] == 200)
    check("a wrong key gets no session", call(base, "POST", "/control/session", None, {"key": "wrong"})[0] == 401)
    code, text, headers = call(base, "POST", "/control/session", None, {"key": CONTROL})
    cookie = headers.get("set-cookie", "")
    check("the right key gets a signed HttpOnly SameSite=Strict cookie", code == 200 and "HttpOnly" in cookie and "SameSite=Strict" in cookie and CONTROL not in cookie, cookie[:60])
    session = cookie.split(";")[0]
    check("OFF: the session cookie opens the dashboard", call(base, "GET", "/", {"Cookie": session})[0] == 200)
    check("OFF: a forged cookie does not", call(base, "GET", "/", {"Cookie": "watchdog_control=v1.9999999999." + "0" * 64})[0] == 503)

    # ---- it survives a restart ----------------------------------------------------------------------------------------------------------------------
    stop(process)
    base, process = start(PORT, db, {})
    check("after a restart access is STILL off", call(base, "GET", "/")[0] == 503)
    code, text, _ = call(base, "GET", "/control", superuser)
    check("the stored state is read back", code == 200 and json_of(text).get("access") == "off", text[:100])

    # ---- ON again ----------------------------------------------------------------------------------------------------------------------------------------
    code, text, _ = call(base, "POST", "/control", superuser, {"access": "on", "notify": False})
    check("the super user switches access back ON", code == 200 and json_of(text).get("access") == "on")
    check("ON: users get the dashboard again", call(base, "GET", "/")[0] == 200)
    check("ON: the status needs a key as before", call(base, "GET", "/status")[0] == 401)

    # ---- the password sign-in and the panel (a listener with a name and a password set up) -------------------------------------------------------------------
    sys.path.insert(0, LISTENER)
    from app import control_login
    password, who = "correct horse battery staple 42", "owner@mail.example"
    login_base, login_process = start(PORT + 2, os.path.join(work, "login.db"), {"WATCHDOG_SUPER_USER": who, "WATCHDOG_SUPER_PASSWORD_HASH": control_login.hash_password(password, n=16)})
    check("the sign-in page asks for a name and a password", 'autocomplete="current-password"' in call(login_base, "GET", "/control/login")[1])
    check("a wrong password is refused with the same words as a wrong name", call(login_base, "POST", "/control/session", None, {"user": who, "password": "wrong password 1234"})[1]
          == call(login_base, "POST", "/control/session", None, {"user": "x@y.test", "password": password})[1])
    check("the old key no longer opens the browser session once a password is set", call(login_base, "POST", "/control/session", None, {"key": CONTROL})[0] == 401)
    code, text, headers = call(login_base, "POST", "/control/session", None, {"user": who, "password": password})
    cookie = headers.get("set-cookie", "")
    check("the right name and password get a signed HttpOnly SameSite=Strict cookie", code == 200 and "HttpOnly" in cookie and "SameSite=Strict" in cookie and password not in cookie + text, str(code))
    session = {"Cookie": cookie.split(";")[0]}
    press = {**session, "Sec-Fetch-Site": "same-origin"}
    check("the dashboard carries a small Sign in link for everyone", 'href="/control/login"' in call(login_base, "GET", "/")[1] and ">Sign in</a>" in call(login_base, "GET", "/")[1])
    check("and once signed in it becomes the Control panel link", 'href="/control/panel"' in call(login_base, "GET", "/", session)[1])
    code, text, headers = call_raw(login_base, "GET", "/control/panel")
    check("the panel page sends a visitor without a session to the sign-in", code == 302 and headers.get("location") == "/control/login", str(code))
    code, text, _ = call_raw(login_base, "GET", "/control/panel", session)
    check("the panel page opens with the session and has the buttons", code == 200 and "Switch access OFF" in text and "Use the VPS" in text, str(code))
    code, text, _ = call(login_base, "GET", "/control/panel/state", session)
    state = json_of(text)
    check("the panel state shows this node and says plainly the other is not reached", code == 200 and state.get("self", {}).get("node") == "CORE" and state.get("peer", {}).get("reachable") is False, text[:140])
    check("the panel state needs the session", call(login_base, "GET", "/control/panel/state")[0] == 401)
    check("a button press from another site is refused", call(login_base, "POST", "/control/panel/act", {**session, "Sec-Fetch-Site": "cross-site"}, {"action": "off"})[0] == 403)
    code, text, _ = call(login_base, "POST", "/control/panel/act", press, {"action": "off"})
    answer = json_of(text)
    check("the OFF button sets this node and says plainly the other was not reached", code == 200 and answer.get("ok") is False and "not reached" in " ".join(answer.get("messages", [])), text[:160])
    check("OFF: a visitor now gets the branded page", call(login_base, "GET", "/")[0] == 503)
    check("OFF: and that page offers staff a way in", "Staff sign in" in call(login_base, "GET", "/")[1])
    check("OFF: the panel still opens for the signed-in super user", call_raw(login_base, "GET", "/control/panel", session)[0] == 200)
    code, text, _ = call(login_base, "POST", "/control/panel/act", press, {"action": "on"})
    check("the ON button restores access", code == 200 and call(login_base, "GET", "/")[0] == 200 and json_of(call(login_base, "GET", "/control", superuser)[1]).get("access") == "on", text[:120])
    check("an unknown action is refused", call(login_base, "POST", "/control/panel/act", press, {"action": "explode"})[0] == 400)
    copied = dict(session)                                                      # a copy of the cookie, as a thief would have it
    check("log out empties the cookie", "Max-Age=0" in call(login_base, "POST", "/control/logout", session)[2].get("set-cookie", ""))
    check("and a copied cookie stops working at once", call(login_base, "GET", "/control/panel/state", copied)[0] == 401)
    check("a stranger's log out request ends nothing", call(login_base, "POST", "/control/logout")[0] == 200)
    for _ in range(5):
        call(login_base, "POST", "/control/session", None, {"user": who, "password": "wrong password 1234"})
    code, text, headers = call(login_base, "POST", "/control/session", None, {"user": who, "password": password})
    check("after five wrong tries even the right password is locked out for a while", code == 429 and int(headers.get("retry-after", "0")) > 0, str(code))
    stop(login_process)

    # ---- preferred node ----------------------------------------------------------------------------------------------------------------------------
    code, text, _ = call(base, "POST", "/control", superuser, {"preferred": "VPS"})
    check("a node with no address for the other node refuses to step aside", code == 409, f"{code} {text[:100]}")
    stop(process)

    # ---- two listeners that watch each other: Core-shaped and VPS-shaped ----------------------------------------------------------------------------
    core_db, vps_db = os.path.join(work, "h2.db"), os.path.join(work, "v2.db")
    vps_base, vps_process = start(PORT + 1, vps_db, {"WATCHDOG_ROLE": "standby", "WATCHDOG_SENDER_AUTH": "per-sender", **{f"WATCHDOG_SENDER_KEY_{k.replace('-', '_')}": v for k, v in KEYS.items()},
                                                      "WATCHDOG_PRIMARY_URL": f"http://127.0.0.1:{PORT}/health", "WATCHDOG_PRIMARY_PROBE_SECONDS": "5"})
    core_base, core_process = start(PORT, core_db, {"WATCHDOG_PEER_URL": f"http://127.0.0.1:{PORT + 1}/health", "WATCHDOG_PRIMARY_PROBE_SECONDS": "5"})
    code, text, _ = call(vps_base, "GET", "/control", superuser)
    check("the VPS-shaped listener starts as a non-preferred node that is not alerting", code == 200 and json_of(text).get("node") == "VPS" and json_of(text).get("alerting_now") is False, text[:140])
    code, text, _ = call(core_base, "POST", "/control", superuser, {"preferred": "VPS"})
    check("Core (with an address for the VPS) accepts 'the VPS is preferred'", code == 200 and json_of(text).get("preferred") == "VPS", f"{code} {text[:140]}")
    code, text, _ = call(vps_base, "POST", "/control", superuser, {"preferred": "VPS"})
    check("the same command reaches the VPS, which now alerts", code == 200 and json_of(text).get("preferred") == "VPS" and json_of(text).get("alerting_now") is True, text[:140])
    deadline, seen = time.time() + 40, None
    while time.time() < deadline:
        code, text, _ = call(core_base, "GET", "/control", superuser)
        seen = json_of(text).get("peer", {})
        if seen.get("preferred") == "VPS":
            break
        time.sleep(1)
    check("Core learns from the VPS's /health who is preferred", seen and seen.get("preferred") == "VPS", str(seen))
    code, text, _ = call(core_base, "GET", "/control", superuser)
    check("once the two agree, Core (not preferred, VPS up) stays quiet", json_of(text).get("alerting_now") is False, text[:140])
    stop(vps_process)
    deadline, believed = time.time() + 60, None
    while time.time() < deadline:
        code, text, _ = call(core_base, "GET", "/control", superuser)
        body = json_of(text)
        believed = (body.get("peer", {}).get("believed"), body.get("alerting_now"))
        if believed == ("DOWN", True):
            break
        time.sleep(2)
    check("with the VPS stopped, Core (non-preferred) takes over by itself", believed == ("DOWN", True), str(believed))
    stop(core_process)

    for server in servers:
        if server.poll() is None:
            stop(server)
    print()
    print("ALL PASS" if not problems else f"{len(problems)} FAILED: " + "; ".join(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        for server in servers:
            if server.poll() is None:
                server.kill()
