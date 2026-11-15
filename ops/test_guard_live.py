# File:         test_guard_live.py
# Purpose:      End-to-end check, over real HTTP against a throwaway LOCAL listener, of the three protections added after an earlier security review:
#               (1) per-sender keys bound to the source name, (2) the route diagnostics in /status, (3) the read-side limits (per-visitor stream cap,
#               /status rate limit, lower /status row limit). Never touches production: its own database, port and keys.
# Inputs:       optional argv[1] = listener folder (default: the repo's core-listener), argv[2] = first port (default 8471; the second listener uses +1).
# Outputs:      PASS/FAIL lines; exit code 1 if anything is wrong.
# Dependencies: python + fastapi + uvicorn + the listener source.
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

LISTENER = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core-listener")
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8471
SHARED = "live-test-shared-key-0123456789abcdef"
KEYS = {"MAIN": "live-test-main-key-0123456789abcdef", "BACKUP-SERVER": "live-test-backup-key-0123456789abcdef",
        "SITE-A": "live-test-site_a-key-0123456789abcdef", "SITE-B": "live-test-site_b-key-0123456789abcdef"}
problems = []
servers = []


def check(label, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + label + ("" if ok or not detail else f"   ({detail})"))
    if not ok:
        problems.append(label)


def production_uvicorn_flags():
    """The flags production starts uvicorn with (everything after the app name except host and port), read from docker-compose.yml, so this test
    can never run the listener differently from how it really runs."""
    compose = open(os.path.join(LISTENER, "docker-compose.yml"), encoding="utf-8").read()
    command = json.loads(re.search(r'^\s*command: (\["python", "-m", "app.serve".*\])\s*$', compose, re.M).group(1))
    flags, skip = [], False
    for index, item in enumerate(command[3:]):                       # after "python", "-m", "app.serve"
        if skip:
            skip = False
        elif item in ("--host", "--port"):
            skip = True
        else:
            flags.append(item)
    return flags


def start(port, extra_env):
    work = tempfile.mkdtemp()
    env = {**os.environ, "WATCHDOG_SHARED_TOKEN": SHARED, "WATCHDOG_DB_PATH": os.path.join(work, "t.db"), "WATCHDOG_ENABLE_DOCS": "false",
           "WATCHDOG_EXPECTED_INSTANCES": '{"MAIN":["usa","caribbean"],"BACKUP-SERVER":["usa","caribbean"]}',
           "WATCHDOG_LISTENER_STARTUP_GRACE_SECONDS": "600", "WATCHDOG_MAX_STREAMS_PER_VISITOR": "3", "WATCHDOG_MAX_STATUS_PER_MINUTE": "10", **extra_env}
    process = subprocess.Popen([sys.executable, "-m", "app.serve", "--host", "127.0.0.1", "--port", str(port), *production_uvicorn_flags()], cwd=LISTENER, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    servers.append(process)
    base = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            urllib.request.urlopen(base + "/offline.html", timeout=1).read()
            return base
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    raise RuntimeError("the throwaway listener did not start on port " + str(port))


def call(base, method, path, key=None, body=None, headers=None):
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    if key is not None:
        request_headers["X-Watchdog-Token"] = key
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as reply:
            return reply.status, reply.read().decode(), dict(reply.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(), dict(error.headers)


def visitor(n):
    """Headers that make a request look like it came through Cloudflare from visitor number n (so each check gets its own allowance)."""
    return {"CF-Connecting-IP": f"198.51.100.{n}", "CF-Ray": f"live-{n}"}


def status(base, n, **extra):
    code, text, headers = call(base, "GET", "/status?limit=1", SHARED, headers={**visitor(n), **extra})
    return code, (json.loads(text) if code == 200 else None), headers


try:
    base = start(PORT, {f"WATCHDOG_SENDER_KEY_{name.replace('-', '_')}": key for name, key in KEYS.items() if name in ("MAIN", "SITE-A")})

    print("--- per-sender keys, mode both")
    code, _, _ = call(base, "POST", "/heartbeat", KEYS["MAIN"], {"hostname": "MAIN"})
    check("MAIN's own key posts as MAIN", code == 200, code)
    code, text, _ = call(base, "POST", "/heartbeat", KEYS["MAIN"], {"hostname": "SITE-A"})
    check("MAIN's key posting as SITE-A is refused with 403", code == 403, code)
    check("the refusal does not say whose key it was", "MAIN" not in text, text)
    code, _, _ = call(base, "POST", "/event", KEYS["MAIN"], {"hostname": "SITE-A", "event_type": "forged"})
    check("MAIN's key posting an EVENT as SITE-A is refused with 403", code == 403, code)
    code, _, _ = call(base, "POST", "/event", KEYS["MAIN"], {"source_id": "SITE-B", "event_type": "forged"})
    check("... also when the name is given as source_id", code == 403, code)
    code, body, _ = call(base, "GET", "/status?limit=50", SHARED, headers=visitor(1))
    events = json.loads(body)["recent_events"]
    check("nothing the refused posts carried was stored", not any(e["event_type"] == "forged" for e in events))
    reported = [s["source_id"] for s in json.loads(body)["sources"] if s.get("last_seen_utc")]          # /status always lists the four known names
    check("the forged name never became a source that has reported", reported == ["MAIN"], reported)
    code, _, _ = call(base, "POST", "/heartbeat", SHARED, {"hostname": "SITE-A"})
    check("the old shared key still posts for anyone in `both` mode", code == 200, code)
    code, _, _ = call(base, "POST", "/heartbeat", "a-key-nobody-was-ever-given-0123456789", {"hostname": "MAIN"})
    check("an unknown key is refused with 401", code == 401, code)
    code, _, _ = call(base, "GET", "/status?limit=1", KEYS["MAIN"], headers=visitor(2))
    check("a sender's own key cannot read /status", code == 401, code)
    code, _, _ = call(base, "POST", "/alerts/reset", KEYS["MAIN"], {"endpoint_id": "MAIN-USA"})
    check("a sender's own key cannot reset an alarm", code == 401, code)
    code, _, _ = call(base, "POST", "/alerts/reset", SHARED, {"endpoint_id": "MAIN-USA"})
    check("the shared (admin) key can reset an alarm", code == 200, code)
    code, data, _ = status(base, 3)
    auth = data["sender_auth"]
    check("/status reports the auth picture (mode, keyed sources, who used which kind of key)",
          auth["mode"] == "both" and auth["keyed_sources"] == ["MAIN", "SITE-A"] and auth["used"]["MAIN"]["credential"] == "own"
          and auth["used"]["SITE-A"]["credential"] == "shared", auth)
    check("... and the refused attempts, by name", any(row["key_of"] == "MAIN" and row["claimed"] == "SITE-A" for row in auth["mismatches"]), auth["mismatches"])
    check("... and no key appears anywhere in /status", not any(secret in json.dumps(data) for secret in [SHARED, *KEYS.values()]))

    print("--- route diagnostics")
    call(base, "POST", "/heartbeat", KEYS["MAIN"], {"hostname": "MAIN"}, {"Host": "192.168.7.183:8420"})
    call(base, "POST", "/heartbeat", KEYS["SITE-A"], {"hostname": "SITE-A"}, {"Host": "100.64.0.10:8420"})
    call(base, "POST", "/heartbeat", SHARED, {"hostname": "SITE-B"}, {"Host": "watchdog.corp.example", "CF-Ray": "abc-JFK", "CF-Visitor": '{"scheme":"https"}'})
    call(base, "POST", "/heartbeat", SHARED, {"hostname": "BACKUP-SERVER"}, {"Host": "watchdog.corp.example", "CF-Ray": "abc-JFK", "CF-Visitor": '{"scheme":"http"}'})
    code, data, _ = status(base, 4)
    routes = data["routes"]
    check("MAIN dialling the LAN address is `lan`, cleartext", (routes["MAIN"]["kind"], routes["MAIN"]["protection"]) == ("lan", "cleartext"), routes.get("MAIN"))
    check("SITE-A dialling Core's Tailscale address is `tailscale`, encrypted", (routes["SITE-A"]["kind"], routes["SITE-A"]["protection"]) == ("tailscale", "encrypted"), routes.get("SITE-A"))
    check("SITE-B through Cloudflare over https is `cloudflare`, encrypted", (routes["SITE-B"]["kind"], routes["SITE-B"]["protection"]) == ("cloudflare", "encrypted"), routes.get("SITE-B"))
    check("BACKUP-SERVER through Cloudflare over plain http is cleartext and listed as exposed",
          routes["BACKUP-SERVER"]["protection"] == "cleartext" and data["exposed_senders"] == ["BACKUP-SERVER"], (routes.get("BACKUP-SERVER"), data["exposed_senders"]))
    check("the LAN and Tailscale senders are NOT listed as exposed", "MAIN" not in data["exposed_senders"] and "SITE-A" not in data["exposed_senders"])
    check("no address of any machine appears in the routes", not re.search(r"\d+\.\d+\.\d+\.\d+", json.dumps(routes)), routes)

    print("--- read-side limits (3 streams a visitor, 10 /status a minute here)")
    code, text, _ = call(base, "GET", "/status?limit=201", SHARED, headers=visitor(5))
    check("/status?limit=201 is refused (the most is 200)", code == 422, code)
    code, _, _ = call(base, "GET", "/status?limit=200", SHARED, headers=visitor(5))
    check("/status?limit=200 is allowed", code == 200, code)
    codes = [status(base, 6)[0] for _ in range(12)]
    check("the first 10 /status requests of a visitor are served and the rest get 429", codes[:10] == [200] * 10 and set(codes[10:]) == {429}, codes)
    refused = status(base, 6)
    retry = {name.lower(): value for name, value in refused[2].items()}.get("retry-after", "0")
    check("... with a Retry-After", int(retry) > 0, refused[2])
    check("... and another visitor is unaffected", status(base, 7)[0] == 200)
    spoof = [call(base, "GET", "/status?limit=1", SHARED, headers={"X-Forwarded-For": f"10.0.0.{i}"})[0] for i in range(12)]
    check("changing X-Forwarded-For does not buy a visitor a new allowance", 429 in spoof, spoof)

    print("--- the public dashboard asks search engines not to list it")
    code, text, _ = call(base, "GET", "/robots.txt")
    check("/robots.txt asks every crawler to stay out, with no key", code == 200 and text == "User-agent: *\nDisallow: /\n", (code, text))
    for path, key in (("/", None), ("/health", None), ("/offline.html", None), ("/robots.txt", None), ("/status?limit=1", SHARED), ("/status?limit=1", "wrong-key")):
        code, _, answer_headers = call(base, "GET", path, key, headers=visitor(200))
        tag = {name.lower(): value for name, value in answer_headers.items()}.get("x-robots-tag", "")
        check(f"GET {path} ({code}) carries X-Robots-Tag noindex", "noindex" in tag and "nofollow" in tag, tag)

    page = call(base, "GET", "/")[1]
    view = re.search(r'var token = "([0-9a-f]{64})";', page).group(1)

    def open_stream(n):
        connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
        connection.request("GET", "/stream?token=" + view, headers=visitor(n))
        return connection, connection.getresponse()

    held = [open_stream(8) for _ in range(3)]
    check("a visitor can hold its 3 streams", all(reply.status == 200 for _, reply in held), [reply.status for _, reply in held])
    fourth_connection, fourth = open_stream(8)
    check("its 4th stream is refused with 429", fourth.status == 429, fourth.status)
    fourth_connection.close()
    other_connection, other = open_stream(9)
    check("another visitor still gets a stream (the shared pool was not used up)", other.status == 200, other.status)
    first_frame = other.readline().decode() if other.status == 200 else ""          # readline() undoes the chunked framing the raw socket would show
    check("a stream delivers a frame", first_frame.startswith("data:"), first_frame[:40])
    other_connection.close()
    held[0][0].close()
    freed = False
    for _ in range(14):                                  # the listener notices a closed stream at its next push (every 5 s)
        time.sleep(1)
        connection, reply = open_stream(8)
        freed = reply.status == 200
        connection.close()
        if freed:
            break
    check("closing a stream frees the visitor's slot", freed)
    for connection, _ in held[1:]:
        connection.close()

    print("--- a full pool is shared fairly, and the private pool cannot be filled from the internet")
    fair_port = PORT + 3
    fair = start(fair_port, {"WATCHDOG_MAX_STREAMS": "4", "WATCHDOG_MAX_STREAMS_PER_VISITOR": "2", "WATCHDOG_MAX_PRIVATE_STREAMS": "2"})
    fair_view = re.search(r'var token = "([0-9a-f]{64})";', call(fair, "GET", "/")[1]).group(1)

    def open_on(headers):
        connection = http.client.HTTPConnection("127.0.0.1", fair_port, timeout=9)
        connection.request("GET", "/stream?token=" + fair_view, headers=headers)
        return connection, connection.getresponse()

    BLANK_LINE = bytes([10, 10])                                # two newlines end a server-sent message

    def read_frame(reply):
        """One whole server-sent message (up to the blank line that ends it), or b"" when the stream has ended. A frame may arrive in several network
        chunks, so reading one chunk and calling it a frame is wrong (the upgraded framework splits them differently)."""
        buffer = b""
        while BLANK_LINE not in buffer:
            chunk = reply.read1(65536)
            if chunk == b"":
                return b""
            buffer += chunk
        return buffer

    old = [open_on(visitor(11)) for _ in range(2)] + [open_on(visitor(12)) for _ in range(2)]
    check("two visitors fill the public pool of four (two streams each)", all(reply.status == 200 for _, reply in old), [reply.status for _, reply in old])
    for _, reply in old:
        read_frame(reply)                                         # take the first whole frame, so each stream is really running
    code, data, _ = status(fair, 13)
    check("/status says the public pool is full and names nobody", data["stream_pools"]["public"] == {"open": 4, "capacity": 4} and "198.51" not in json.dumps(data["stream_pools"]),
          data["stream_pools"])
    newcomer_connection, newcomer = open_on(visitor(13))
    check("a THIRD visitor arriving when the pool is full still gets a stream (it displaces one)", newcomer.status == 200, newcomer.status)
    ended = 0
    for connection, reply in old:
        try:
            frame = read_frame(reply)                             # the next whole frame comes within 5 s; an evicted stream ends instead
        except Exception:  # noqa: BLE001
            frame = b""
        ended += 1 if frame == b"" else 0
    check("exactly one of the earlier four streams was ended to make room", ended == 1, f"{ended} ended")
    code, data, _ = status(fair, 14)
    check("/status counts the eviction's result: the pool is still four, never more", data["stream_pools"]["public"]["open"] <= 4, data["stream_pools"])
    reconnect = open_on(visitor(11))
    check("a visitor who lost a stream and reconnects does not push the others out again", reconnect[1].status in (200, 503, 429), reconnect[1].status)
    lan_headers = {"Host": "192.168.7.183:8420"}
    lan = [open_on(lan_headers) for _ in range(2)]
    check("viewers who dial the LAN address get their own pool even while the internet pool is full", all(reply.status == 200 for _, reply in lan), [reply.status for _, reply in lan])
    third_lan = open_on(lan_headers)
    check("the private pool has its own per-visitor cap (a third from one place is refused with 429)", third_lan[1].status == 429, third_lan[1].status)
    spoof = open_on({**visitor(40), "Host": "192.168.7.183:8420"})
    check("a request carrying Cloudflare's headers cannot claim the private pool by writing a LAN Host header", spoof[1].status in (200, 503), spoof[1].status)
    code, data, _ = status(fair, 15)
    check("/status shows the private pool holding exactly the two LAN streams", data["stream_pools"]["private"] == {"open": 2, "capacity": 2}, data["stream_pools"])
    for connection in [pair[0] for pair in old + lan] + [newcomer_connection, reconnect[0], third_lan[0], spoof[0]]:
        connection.close()

    print("--- per-sender mode (the end state)")
    strict = start(PORT + 1, {f"WATCHDOG_SENDER_KEY_{name.replace('-', '_')}": key for name, key in KEYS.items()} | {"WATCHDOG_SENDER_AUTH": "per-sender"})
    code, _, _ = call(strict, "POST", "/heartbeat", SHARED, {"hostname": "MAIN"})
    check("the shared key can no longer post a heartbeat", code == 401, code)
    code, _, _ = call(strict, "POST", "/event", SHARED, {"hostname": "MAIN", "event_type": "x"})
    check("... or an event", code == 401, code)
    code, _, _ = call(strict, "GET", "/status?limit=1", SHARED)
    check("the shared key still reads /status (the deploy check and the weekly report use it)", code == 200, code)
    code, _, _ = call(strict, "POST", "/alerts/reset", SHARED, {"endpoint_id": "MAIN-USA"})
    check("the shared key still resets alarms (it is the admin key)", code == 200, code)
    for name, key in KEYS.items():
        code, _, _ = call(strict, "POST", "/heartbeat", key, {"hostname": name})
        check(f"{name}'s own key posts as {name}", code == 200, code)
    wrong = 0
    for stolen in KEYS:
        for victim in KEYS:
            if stolen != victim and call(strict, "POST", "/heartbeat", KEYS[stolen], {"hostname": victim})[0] != 403:
                wrong += 1
    check("a key taken from any one machine is refused as any OTHER machine (12 of 12 pairs)", wrong == 0, f"{wrong} pairs not refused")
    code, data, _ = call(strict, "GET", "/status?limit=1", SHARED)
    check("/status says per-sender", json.loads(data)["sender_auth"]["mode"] == "per-sender")

    print("--- Cloudflare's visitor header is believed only from the tunnel's network")
    untrusting = start(PORT + 2, {"WATCHDOG_TRUSTED_PROXY_CIDRS": "10.99.0.0/16"})          # this test's own loopback peer is NOT inside that network
    forged = [call(untrusting, "GET", "/status?limit=1", SHARED, headers=visitor(100 + i))[0] for i in range(12)]
    check("a direct client writing a different CF-Connecting-IP every time still shares ONE allowance (10 served, the rest 429)",
          forged[:10] == [200] * 10 and set(forged[10:]) == {429}, forged)
    trusted_run = [call(base, "GET", "/status?limit=1", SHARED, headers=visitor(150 + i))[0] for i in range(12)]
    check("... whereas from a trusted peer each Cloudflare visitor gets its own allowance", set(trusted_run) == {200}, trusted_run)

    print("--- a bad setting stops the listener instead of running looser")
    wrong = subprocess.run([sys.executable, "-c", "import app.main"], cwd=LISTENER, capture_output=True, text=True, timeout=60,
                           env={**os.environ, "WATCHDOG_SHARED_TOKEN": SHARED, "WATCHDOG_TRUSTED_PROXY_CIDRS": "172.21.0.0/99"})
    check("a mistyped WATCHDOG_TRUSTED_PROXY_CIDRS makes the listener refuse to start", wrong.returncode != 0 and "WATCHDOG_TRUSTED_PROXY_CIDRS" in wrong.stderr, wrong.stderr[-200:])
    bad = subprocess.run([sys.executable, "-c", "import app.main"], cwd=LISTENER, capture_output=True, text=True, timeout=60,
                         env={**os.environ, "WATCHDOG_SHARED_TOKEN": SHARED, "WATCHDOG_SENDER_AUTH": "per_sender"})
    check("a mistyped WATCHDOG_SENDER_AUTH makes the listener refuse to start", bad.returncode != 0 and "WATCHDOG_SENDER_AUTH" in bad.stderr, bad.stderr[-200:])
    check("... and the message never contains a key", SHARED not in bad.stderr)
    same = subprocess.run([sys.executable, "-c", "import app.main"], cwd=LISTENER, capture_output=True, text=True, timeout=60,
                          env={**os.environ, "WATCHDOG_SHARED_TOKEN": SHARED, "WATCHDOG_SENDER_KEY_MAIN": KEYS["MAIN"], "WATCHDOG_SENDER_KEY_SITE_A": KEYS["MAIN"]})
    check("one key given to two senders makes the listener refuse to start", same.returncode != 0 and "another sender also holds" in same.stderr, same.stderr[-200:])
finally:
    for process in servers:
        process.terminate()
    for process in servers:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

print()
print("ALL PASSED" if not problems else f"{len(problems)} FAILED: " + "; ".join(problems))
sys.exit(1 if problems else 0)
