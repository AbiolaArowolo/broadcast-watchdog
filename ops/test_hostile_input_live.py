# File:         test_hostile_input_live.py
# Purpose:      End-to-end check that a malformed or hostile heartbeat cannot hurt the dashboard. Starts a throwaway LOCAL listener, sends
#               heartbeats carrying Infinity, NaN, an overflowing interval, a megabyte of text, a nested object and a non-object body, then
#               reads /status and the first /stream frame the way a browser does and requires STRICT JSON (a browser's JSON.parse rejects
#               NaN and Infinity, which would blind every viewer) and a small payload. Never touches production: its own database and token.
#               History: before the input hardening, one heartbeat saying Infinity made /status return 500 and every stream frame unparseable.
# Inputs:       optional argv[1] = listener folder (default: the repo's core-listener), argv[2] = port (default 8470).
# Outputs:      Printed findings; exit code 1 if anything is wrong.
# Dependencies: python + uvicorn + the listener source.
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

LISTENER = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core-listener")
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8470
BASE = f"http://127.0.0.1:{PORT}"
HEADERS = {"X-Watchdog-Token": "test-token", "Content-Type": "application/json"}
work = tempfile.mkdtemp()
env = {**os.environ, "WATCHDOG_SHARED_TOKEN": "test-token", "WATCHDOG_DB_PATH": os.path.join(work, "t.db"),
       "WATCHDOG_EXPECTED_INSTANCES": '{"MAIN":["usa","caribbean"],"BACKUP-SERVER":["usa","caribbean"]}',
       "WATCHDOG_ENABLE_DOCS": "false"}
server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT)], cwd=LISTENER, env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
problems = []


def refuse(constant):
    raise ValueError("not JSON: " + constant)


def post_raw(path, raw):
    request = urllib.request.Request(BASE + path, data=raw.encode(), headers=HEADERS, method="POST")
    try:
        return urllib.request.urlopen(request, timeout=10).status
    except urllib.error.HTTPError as error:
        return error.code


try:
    for _ in range(60):
        try:
            urllib.request.urlopen(BASE + "/offline.html", timeout=1).read()
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    big = "x" * 1_000_000
    bodies = {
        "Infinity length": '{"hostname":"SITE-A","expected_interval_seconds":30,"current_item":"A.mp4","current_item_duration_s":Infinity,"current_item_started_utc":"2026-10-12T10:00:00Z","timestamp":"2026-10-12T10:00:30Z"}',
        "NaN length and interval": '{"hostname":"SITE-B","expected_interval_seconds":NaN,"current_item":"B.mp4","current_item_duration_s":NaN}',
        "interval 1e400 (infinity)": '{"hostname":"MAIN","expected_interval_seconds":1e400,"instance_count":2}',
        "megabyte file name and next": json.dumps({"hostname": "BACKUP-SERVER", "expected_interval_seconds": 10, "current_item": big, "next_item": big}),
        "nested object as the name": json.dumps({"hostname": "SITE-A", "now_playing": {"filename": {"deep": [1] * 5000}, "result": "r" * 9999}}),
        "hostname is not text": json.dumps({"hostname": {"forged": True}}),
        "body is a list, not an object": "[1, 2, 3]",
    }
    for label, raw in bodies.items():
        status = post_raw("/heartbeat", raw)
        print(f"heartbeat: {label:34s} -> HTTP {status}")
        wanted = {"body is a list, not an object": 400, "megabyte file name and next": 413, "hostname is not text": 400}.get(label)
        if wanted is not None:
            if status != wanted:
                problems.append(f"{label}: should be a {wanted}, got {status}")
        elif status >= 500:
            problems.append(f"{label}: the heartbeat handler crashed (HTTP {status})")

    request = urllib.request.Request(BASE + "/status?limit=5", headers=HEADERS)
    try:
        body = urllib.request.urlopen(request, timeout=10).read().decode()
        status = json.loads(body, parse_constant=refuse)
        print("/status: HTTP 200, strict JSON, %d bytes" % len(body))
        for endpoint in status["endpoints"]:
            if len(endpoint["now_playing"]["title"]) > 300 or len(endpoint["now_playing"].get("next") or "") > 300 or len(endpoint["now_playing"]["detail"]) > 300:
                problems.append("an over-long name reached the dashboard: " + endpoint["endpoint_id"])
        if len(body) > 200_000:
            problems.append("the status payload is %d bytes: sender text is not bounded" % len(body))
    except urllib.error.HTTPError as error:
        problems.append("/status returned HTTP %d" % error.code)
    except ValueError as error:
        problems.append("/status is not strict JSON: %s" % error)

    # the first frame of the live stream, read as a browser reads it
    stream = urllib.request.urlopen(BASE + "/stream?token=test-token", timeout=15)
    frame_text = ""
    while True:
        line = stream.readline().decode()
        if line.startswith("data: "):
            frame_text = line[6:]
            break
    stream.close()
    try:
        frame = json.loads(frame_text, parse_constant=refuse)
        print("/stream first frame: strict JSON, %d bytes, system.cpu_percent present: %s" % (len(frame_text), "cpu_percent" in frame.get("system", {})))
        if len(frame_text) > 300_000:
            problems.append("a stream frame is %d bytes: sender text is not bounded" % len(frame_text))
    except ValueError as error:
        problems.append("the stream frame is not strict JSON (a browser would drop it): %s" % error)

    for path in ("/docs", "/redoc", "/openapi.json"):
        try:
            urllib.request.urlopen(BASE + path, timeout=10)
            problems.append(path + " is enabled even though WATCHDOG_ENABLE_DOCS=false")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                problems.append(path + " returned HTTP %d instead of 404" % error.code)

    response = urllib.request.urlopen(BASE + "/offline.html", timeout=10)
    for header in ("X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy", "Permissions-Policy", "Content-Security-Policy"):
        if response.headers.get(header) is None:
            problems.append("/offline.html is missing " + header)
finally:
    # terminate() works on Windows and Linux alike (uvicorn without --reload is one process, so there are no children to chase).
    # The Windows-only process killer this used to call passed every check on a laptop, then crashed the cleanup on the Linux CI runner.
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()

print("RESULT:", "FAIL - " + "; ".join(problems) if problems else "PASS")
sys.exit(1 if problems else 0)
