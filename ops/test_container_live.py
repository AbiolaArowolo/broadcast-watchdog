# File:         test_container_live.py
# Purpose:      Builds the listener image and runs the REAL docker-compose.yml on the local Docker daemon, starting from a data volume that the old root-run
#               container left behind (files owned by root, a database with data in it: the exact situation on Core), and checks every hardening claim
#               against the running container: unprivileged user, read-only filesystem, no capabilities, limits, the data kept and writable, nothing written
#               outside the volume, the service answering over real HTTP. Never touches production: its own project name (wdtest), volume and containers.
# Inputs:       none (needs a running Docker daemon and network access to PyPI and Docker Hub for the build; port 8420 free on this machine).
#               optional: --keep leaves the containers running for a look.
# Outputs:      PASS/FAIL lines; exit code 1 if anything is wrong. Skipped (exit 0, loudly) where there is no Docker daemon.
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO / "core-listener" / "docker-compose.yml"
PROJECT = "wdtest"
VOLUME = f"{PROJECT}_watchdog-data"
BASE = "http://127.0.0.1:8420"
TOKEN = "container-test-shared-key-0123456789abcd"
KEEP = "--keep" in sys.argv
problems = []


def check(label, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + label + ("" if ok or not detail else f"   ({detail})"))
    if not ok:
        problems.append(label)


def docker(*args, timeout=600, check_exit=False):
    done = subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if check_exit and done.returncode != 0:
        raise RuntimeError("docker " + " ".join(args[:3]) + " failed: " + (done.stderr or done.stdout)[-400:])
    return done


def compose(*args, timeout=900):
    env = {**os.environ, "WATCHDOG_SHARED_TOKEN": TOKEN}
    return subprocess.run(["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, env=env)


def http(method, path, body=None):
    headers = {"X-Watchdog-Token": TOKEN, "Content-Type": "application/json"}
    request = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as reply:
            return reply.status, reply.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def in_listener(command):
    return docker("exec", "clarix-watchdog-listener", "sh", "-c", command).stdout.strip()


if docker("info", "--format", "{{.ServerVersion}}").returncode != 0:
    print("SKIPPED: no Docker daemon is running here; the container could not be built or run.")
    sys.exit(0)

# The listener's own pinned base image is the one used to fake "the volume the old root container left behind".
base_image = next(line.split()[1] for line in (REPO / "core-listener" / "Dockerfile").read_text(encoding="utf-8").splitlines() if line.startswith("FROM "))

try:
    compose("down", "-v", "--remove-orphans")
    docker("volume", "rm", "-f", VOLUME)

    print("--- a data volume as the OLD (root-run) container left it")
    docker("volume", "create", VOLUME, check_exit=True)
    seed = ("import sqlite3; c = sqlite3.connect('/data/watchdog.db'); "
            "c.execute('CREATE TABLE IF NOT EXISTS marker (note TEXT)'); c.execute(\"INSERT INTO marker VALUES ('kept-from-before-the-hardening')\"); c.commit()")
    docker("run", "--rm", "-v", f"{VOLUME}:/data", base_image, "python", "-c", seed, check_exit=True)
    owner_before = docker("run", "--rm", "-v", f"{VOLUME}:/data", base_image, "sh", "-c", "ls -ln /data/watchdog.db | awk '{print $3\":\"$4}'").stdout.strip()
    check("the seeded database belongs to root, as on Core", owner_before == "0:0", owner_before)

    print("--- why the helper exists: the unprivileged listener cannot use a root-owned database")
    compose("build", "watchdog-listener")
    image = f"{PROJECT}-watchdog-listener"
    without = docker("run", "--rm", "--read-only", "--cap-drop", "ALL", "-e", f"WATCHDOG_SHARED_TOKEN={TOKEN}", "-v", f"{VOLUME}:/data", image,
                     "python", "-c", "import app.main as m; m.init_db(); print('opened')", timeout=120)
    check("without the data-owner helper the listener cannot open the root-owned database", "opened" not in without.stdout and without.returncode != 0,
          (without.stderr or without.stdout)[-200:])

    print("--- build the image and start the real compose file")
    built = compose("build", "watchdog-listener")
    check("the image builds (hash-locked install, compile, non-root user)", built.returncode == 0, (built.stderr or built.stdout)[-300:])
    started = compose("up", "-d", "watchdog-listener")
    check("`docker compose up -d watchdog-listener` succeeds (the data-owner helper runs first)", started.returncode == 0, (started.stderr or started.stdout)[-400:])
    helper = docker("inspect", "clarix-watchdog-data-owner", "--format", "{{.State.Status}} exit={{.State.ExitCode}}").stdout.strip()
    check("the data-owner helper ran once and exited cleanly", helper == "exited exit=0", helper)

    healthy = False
    for _ in range(40):
        state = docker("inspect", "clarix-watchdog-listener", "--format", "{{.State.Health.Status}}").stdout.strip()
        if state == "healthy":
            healthy = True
            break
        time.sleep(3)
    check("the listener becomes healthy", healthy, docker("logs", "--tail", "15", "clarix-watchdog-listener").stderr[-500:])

    print("--- who runs, and what it may do")
    check("the process runs as uid 10001, not root", in_listener("id -u") == "10001", in_listener("id"))
    inspect = json.loads(docker("inspect", "clarix-watchdog-listener").stdout)[0]["HostConfig"]
    check("the root filesystem is read-only", inspect["ReadonlyRootfs"] is True)
    check("every capability is dropped", inspect["CapDrop"] == ["ALL"] and not inspect["CapAdd"], inspect["CapDrop"])
    check("privilege cannot be gained", "no-new-privileges:true" in (inspect["SecurityOpt"] or []), inspect["SecurityOpt"])
    check("memory is capped at 256 MiB with no extra swap", inspect["Memory"] == 256 * 1024 * 1024 and inspect["MemorySwap"] == 256 * 1024 * 1024, (inspect["Memory"], inspect["MemorySwap"]))
    check("CPU is capped at one core and processes at 128", inspect["NanoCpus"] == 10 ** 9 and inspect["PidsLimit"] == 128, (inspect["NanoCpus"], inspect["PidsLimit"]))
    effective = in_listener("grep -E '^(CapEff|CapPrm|CapBnd|NoNewPrivs):' /proc/1/status | tr '\\n' ' '")
    check("the kernel agrees: no effective, permitted or bounding capabilities, NoNewPrivs 1", "CapEff:\t0000000000000000" in effective.replace("  ", "\t") or
          ("CapEff: 0000000000000000" in effective.replace("\t", " ") and "CapBnd: 0000000000000000" in effective.replace("\t", " ") and "NoNewPrivs: 1" in effective.replace("\t", " ")), effective)

    print("--- where it can write")
    check("it cannot write inside the image (/app)", "Read-only file system" in in_listener("touch /app/x 2>&1; echo $?") or in_listener("touch /app/x >/dev/null 2>&1; echo $?") != "0")
    check("it cannot write to /usr or /etc either", in_listener("touch /usr/x >/dev/null 2>&1; echo $?") != "0" and in_listener("touch /etc/x >/dev/null 2>&1; echo $?") != "0")
    check("it can write to the data volume", in_listener("touch /data/.probe && rm /data/.probe && echo yes") == "yes")
    check("it can use /tmp (a small memory-backed area)", in_listener("touch /tmp/p && rm /tmp/p && echo yes") == "yes")

    print("--- the old data came across, now owned by the listener's user")
    owner_after = in_listener("ls -ln /data/watchdog.db | awk '{print $3\":\"$4}'")
    check("the database now belongs to 10001:10001", owner_after == "10001:10001", owner_after)
    row = in_listener("python -c \"import sqlite3; print(sqlite3.connect('/data/watchdog.db').execute('select note from marker').fetchone()[0])\"")
    check("the data written before the hardening is still there", row == "kept-from-before-the-hardening", row)

    print("--- it really works, over HTTP")
    check("/health answers 200", http("GET", "/health")[0] == 200)
    check("a heartbeat is accepted and stored", http("POST", "/heartbeat", {"hostname": "MAIN"})[0] == 200)
    code, body = http("GET", "/status?limit=1")
    check("/status shows it", code == 200 and any(s["source_id"] == "MAIN" and s.get("last_seen_utc") for s in json.loads(body)["sources"]), code)
    check("the dashboard page is served", urllib.request.urlopen(BASE + "/", timeout=10).status == 200)
    docker("restart", "clarix-watchdog-listener", check_exit=True)
    for _ in range(40):
        if docker("inspect", "clarix-watchdog-listener", "--format", "{{.State.Health.Status}}").stdout.strip() == "healthy":
            break
        time.sleep(3)
    code, body = http("GET", "/status?limit=1")
    check("after a restart the stored heartbeat is still there (the database survives)", code == 200 and any(s["source_id"] == "MAIN" and s.get("last_seen_utc") for s in json.loads(body)["sources"]))

    print("--- nothing was written outside the volume")
    diff = docker("diff", "clarix-watchdog-listener").stdout.strip()
    check("`docker diff` shows no change to the container's own filesystem", diff == "", diff[:300])
    # One compiled file per module in app/ (counted from the repo, so a new module can never be forgotten in a hand-kept number: it was 4 until standby.py, control.py and control_access.py were added).
    modules = len(list((REPO / "core-listener" / "app").glob("*.py")))
    # Any CPython 3.x counts: the base image moved from 3.12 to 3.14 on main (a dependency update), and a test that names one minor version fails for a reason that has nothing to do with the bytecode.
    check("the compiled bytecode shipped in the image", in_listener("ls /app/app/__pycache__/ | grep -c 'cpython-3[0-9][0-9]*[.]pyc'") == str(modules), in_listener("ls /app/app/__pycache__/"))

    print("--- the real memory use is far below the limit")
    stats = docker("stats", "--no-stream", "--format", "{{.MemUsage}}", "clarix-watchdog-listener").stdout.strip()
    check("memory in use is well under 256 MiB", "MiB" in stats and float(stats.split("MiB")[0]) < 120, stats)

    print("--- running it again changes nothing (the helper is idempotent)")
    again = compose("up", "-d", "watchdog-listener")
    check("a second `docker compose up -d` succeeds", again.returncode == 0, (again.stderr or again.stdout)[-300:])
    check("the data is still owned by the listener's user", in_listener("ls -ln /data/watchdog.db | awk '{print $3\":\"$4}'") == "10001:10001")
finally:
    if not KEEP:
        compose("down", "-v", "--remove-orphans")
        docker("volume", "rm", "-f", VOLUME)

print()
print("ALL PASSED" if not problems else f"{len(problems)} FAILED: " + "; ".join(problems))
sys.exit(1 if problems else 0)
