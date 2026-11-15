"""Static checks of the container files: what the image is built from, who runs inside it, what it may do, and that the
dependency lock is complete. Nothing is built or run here (ops/test_container_live.py does that, where a Docker daemon is available).

Run:  python ops/test_container_hardening.py
"""
import re
import sys
import unittest
from pathlib import Path

import yaml

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))
LISTENER = OPS.parent / "core-listener"
DOCKERFILE = (LISTENER / "Dockerfile").read_text(encoding="utf-8")
COMPOSE_TEXT = (LISTENER / "docker-compose.yml").read_text(encoding="utf-8")
COMPOSE = yaml.safe_load(COMPOSE_TEXT)
LOCK = (LISTENER / "requirements.txt").read_text(encoding="utf-8")
REQUIREMENTS_IN = (LISTENER / "requirements.in").read_text(encoding="utf-8")
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
LISTENER_UID = 10001


def docker_instructions():
    """The Dockerfile as (instruction, arguments) with line continuations joined and comments dropped."""
    joined = re.sub(r"\\\n", " ", DOCKERFILE)
    rows = []
    for line in joined.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            name, _, rest = line.partition(" ")
            rows.append((name.upper(), rest.strip()))
    return rows


def dockerfile_python_minor():
    """'3.14' for `FROM python:3.14-slim@sha256:...`: the one place the image's Python is decided."""
    image = next(rest.split()[0] for name, rest in docker_instructions() if name == "FROM")
    return re.match(r"python:(\d+\.\d+)-slim@", image).group(1)


class ImageIsPinnedTests(unittest.TestCase):
    def test_the_base_image_is_pinned_by_digest_and_never_a_moving_tag_alone(self):
        froms = [rest for name, rest in docker_instructions() if name == "FROM"]
        self.assertEqual(len(froms), 1)
        image = froms[0].split()[0]
        self.assertRegex(image, DIGEST.pattern.replace("$", "") + "$")
        # Any 3.x slim image, as long as it is pinned by digest: a dependency update moved main's base from 3.12 to 3.14, and a test that names one minor version made every
        # later pull request fail for a reason that has nothing to do with the pinning this test exists to guard.
        self.assertRegex(image, r"^python:3\.\d+-slim@sha256:")

    def test_every_image_in_the_compose_file_is_pinned_by_digest_and_none_is_latest(self):
        images = {name: service["image"] for name, service in COMPOSE["services"].items() if "image" in service}
        self.assertIn("cloudflared", images)
        for name, image in images.items():
            with self.subTest(service=name):
                self.assertRegex(image, DIGEST.pattern)
                self.assertNotIn(":latest", image)
        self.assertTrue(images["cloudflared"].startswith("cloudflare/cloudflared:2026."), images["cloudflared"])     # a version tag, so a reader sees what it is

    def test_the_helper_container_uses_the_same_base_image_as_the_listener(self):
        base = next(rest.split()[0] for name, rest in docker_instructions() if name == "FROM")
        self.assertEqual(COMPOSE["services"]["data-owner"]["image"], base)


class DependencyLockTests(unittest.TestCase):
    def blocks(self):
        """{package: [hashes]} from the lock; every requirement line must be name==version followed by hashes."""
        found, current = {}, None
        for line in LOCK.splitlines():
            stripped = line.strip().rstrip("\\").strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("--hash=sha256:"):
                found[current].append(stripped.split(":", 1)[1])
            else:
                match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([0-9][A-Za-z0-9_.+!-]*)", stripped)
                self.assertIsNotNone(match, "not a pinned requirement: " + stripped)
                current = match.group(1).lower() + "==" + match.group(2)
                found[current] = []
        return found

    def test_every_package_is_pinned_to_one_exact_version_with_hashes(self):
        blocks = self.blocks()
        self.assertGreaterEqual(len(blocks), 10)
        for package, hashes in blocks.items():
            with self.subTest(package=package):
                self.assertGreaterEqual(len(hashes), 1)
                for digest in hashes:
                    self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_there_is_no_url_git_or_editable_dependency(self):
        for line in LOCK.splitlines():
            if not line.strip().startswith("#"):
                for forbidden in ("git+", "http://", "https://", "file:", "-e ", "--index-url", "--extra-index-url", "--find-links"):
                    self.assertNotIn(forbidden, line)

    def test_the_lock_contains_what_requirements_in_asks_for(self):
        wanted = dict(line.strip().split("==") for line in REQUIREMENTS_IN.splitlines() if "==" in line and not line.startswith("#"))
        self.assertEqual(set(wanted), {"fastapi", "uvicorn"})
        locked = {key.split("==")[0]: key.split("==")[1] for key in self.blocks()}
        for name, version in wanted.items():
            self.assertEqual(locked.get(name), version)

    def test_the_image_installs_only_what_the_lock_names_and_refuses_anything_unhashed(self):
        installs = [rest for name, rest in docker_instructions() if name == "RUN" and "pip install" in rest]
        self.assertEqual(len(installs), 1)
        self.assertIn("--require-hashes", installs[0])
        self.assertIn("--no-deps", installs[0])
        self.assertIn("-r requirements.txt", installs[0])
        self.assertNotRegex(installs[0], r"pip install (?!--)[a-z]")       # no package named on the command line

    def test_the_lock_is_regenerated_with_the_containers_platform_and_python(self):
        import lock_requirements
        command = lock_requirements.command(upgrade=False)
        self.assertIn("--generate-hashes", command)
        self.assertEqual(command[command.index("--python-platform") + 1], "x86_64-manylinux_2_28")
        # The Python the lock is resolved for is the Python the image runs, read from the Dockerfile's FROM line (not from a comment, and not a number typed here).
        self.assertEqual(command[command.index("--python-version") + 1], dockerfile_python_minor())
        self.assertIn("-c", command)                                         # production versions held as they are ...
        self.assertNotIn("-c", lock_requirements.command(upgrade=True))      # ... unless an upgrade is asked for on purpose

    def test_every_version_held_in_constraints_production_is_the_version_in_the_lock(self):
        """Dependabot edits constraints-production.txt (it looks like a requirements file to it), but only ops/lock_requirements.py changes what the image
        installs. Before this test, constraints said pydantic 2.14.1, pydantic_core 2.50.2 and starlette 1.10.0 while the lock still had 2.13.5, 2.46.5 and
        1.8.0: those merged bumps never reached the image, and CI stayed green on versions it never ran. Now such a bump turns this red until
        `python ops/lock_requirements.py` brings the lock to it."""
        locked = {key.split("==")[0]: key.split("==")[1] for key in self.blocks()}
        held = {}
        for line in (LISTENER / "constraints-production.txt").read_text(encoding="utf-8").splitlines():
            if "==" in line and not line.lstrip().startswith("#"):
                name, version = line.strip().split("==")
                held[re.sub(r"[-_.]+", "-", name).lower()] = version
        self.assertGreaterEqual(len(held), 10)
        for name, version in held.items():
            with self.subTest(package=name):
                self.assertEqual(locked.get(name), version, f"{name}: constraints-production.txt holds {version}, the lock has {locked.get(name)}")


class ListenerContainerTests(unittest.TestCase):
    def service(self):
        return COMPOSE["services"]["watchdog-listener"]

    def test_it_runs_as_a_fixed_unprivileged_user_that_owns_nothing_in_the_image(self):
        users = [rest for name, rest in docker_instructions() if name == "USER"]
        self.assertEqual(users, [f"{LISTENER_UID}:{LISTENER_UID}"])
        self.assertNotIn("sudo", DOCKERFILE.lower())
        run_lines = " ".join(rest for name, rest in docker_instructions() if name == "RUN")
        self.assertIn("--no-create-home", run_lines)
        self.assertIn("/usr/sbin/nologin", run_lines)
        self.assertIn(f"chown {LISTENER_UID}:{LISTENER_UID} /data", run_lines)       # a brand-new volume starts out owned by the listener's user

    def test_the_filesystem_is_read_only_with_one_data_volume_and_a_small_tmp(self):
        service = self.service()
        self.assertIs(service["read_only"], True)
        self.assertEqual(service["volumes"], ["watchdog-data:/data"])
        self.assertEqual(len(service["tmpfs"]), 1)
        self.assertTrue(service["tmpfs"][0].startswith("/tmp:"))
        self.assertRegex(service["tmpfs"][0], r"size=\d+m")

    def test_python_never_needs_to_write_bytecode_into_the_read_only_image(self):
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", DOCKERFILE)
        self.assertTrue(any(name == "RUN" and "compileall" in rest for name, rest in docker_instructions()))
        compiled = [i for i, (name, rest) in enumerate(docker_instructions()) if name == "RUN" and "compileall" in rest][0]
        copied = [i for i, (name, rest) in enumerate(docker_instructions()) if name == "COPY" and rest.startswith("app")][0]
        self.assertGreater(compiled, copied, "the code must be copied in BEFORE it is compiled")

    def test_every_capability_is_dropped_and_privilege_cannot_be_gained(self):
        service = self.service()
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertNotIn("cap_add", service)
        self.assertIn("no-new-privileges:true", service["security_opt"])
        self.assertNotIn("privileged", service)
        self.assertNotIn("network_mode", service)
        self.assertNotIn("pid", service)

    def test_it_has_memory_cpu_and_process_limits_well_above_what_it_uses(self):
        service = self.service()
        # measured on Core 2026-10-17: about 45 MiB, 0.2 % of a core, 2 processes
        self.assertEqual(service["mem_limit"], "256m")
        self.assertEqual(service["memswap_limit"], "256m")                  # no swap on top of it
        self.assertEqual(float(service["cpus"]), 1.0)
        self.assertEqual(service["pids_limit"], 128)

    def test_the_published_port_the_senders_use_is_unchanged(self):
        self.assertEqual(self.service()["ports"], ["8420:8420", "127.0.0.1:8421:8421"])          # the staff door (2026-11-02) is on Core's own loopback only

    def test_it_starts_only_after_the_data_volume_belongs_to_its_user(self):
        self.assertEqual(self.service()["depends_on"], {"data-owner": {"condition": "service_completed_successfully"}})

    def test_the_command_keeps_every_flag_production_needs(self):
        command = self.service()["command"]
        for flag in ("--timeout-graceful-shutdown", "--no-proxy-headers"):
            self.assertIn(flag, command)
        self.assertEqual(command[:3], ["python", "-m", "app.serve"])                            # the launcher: the same uvicorn server and flags, plus the staff door


class DataOwnerTests(unittest.TestCase):
    """The data volume was created by the old root-run container, so its files belong to root; the unprivileged listener could not write its database.
    A one-shot helper hands the volume to the listener's user before the listener starts."""

    def service(self):
        return COMPOSE["services"]["data-owner"]

    def test_it_changes_ownership_of_the_data_volume_to_the_listener_user_and_nothing_else(self):
        service = self.service()
        self.assertEqual(service["command"], ["chown", "-R", f"{LISTENER_UID}:{LISTENER_UID}", "/data"])
        self.assertEqual(service["volumes"], ["watchdog-data:/data"])
        self.assertEqual(service["user"], "0:0")

    def test_it_has_exactly_the_one_capability_chown_needs_no_network_and_a_read_only_filesystem(self):
        service = self.service()
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertEqual(service["cap_add"], ["CHOWN"])
        self.assertEqual(service["network_mode"], "none")
        self.assertIs(service["read_only"], True)
        self.assertIn("no-new-privileges:true", service["security_opt"])
        self.assertEqual(service["restart"], "no")

    def test_it_uses_the_same_numeric_user_as_the_image(self):
        users = [rest for name, rest in docker_instructions() if name == "USER"]
        self.assertTrue(users[0].startswith(str(LISTENER_UID)))
        self.assertIn(f"--uid {LISTENER_UID}", DOCKERFILE)
        self.assertIn(f"--gid {LISTENER_UID}", DOCKERFILE)


class TunnelContainerTests(unittest.TestCase):
    def service(self):
        return COMPOSE["services"]["cloudflared"]

    def test_it_drops_every_capability_and_cannot_gain_privilege(self):
        service = self.service()
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertNotIn("cap_add", service)
        self.assertIn("no-new-privileges:true", service["security_opt"])

    def test_it_has_memory_and_process_limits_above_what_it_uses(self):
        service = self.service()
        # measured on Core 2026-10-17: about 31 MiB and 20 processes
        self.assertEqual(service["mem_limit"], "192m")
        self.assertEqual(service["pids_limit"], 256)

    def test_the_tunnel_token_still_comes_from_the_environment_and_the_public_hostname_is_unchanged(self):
        self.assertEqual(self.service()["environment"], {"TUNNEL_TOKEN": "${WATCHDOG_TUNNEL_TOKEN}"})
        self.assertEqual(self.service()["command"], "tunnel run")

    def test_the_read_only_filesystem_for_the_tunnel_is_not_claimed_untested(self):
        # read_only on cloudflared was not tried against the real tunnel; it stays off until a canary on Core proves it (the unpublished design notes says so).
        self.assertNotIn("read_only", self.service())


class LineEndingTests(unittest.TestCase):
    def test_files_built_or_read_on_linux_are_pinned_to_unix_line_endings(self):
        attributes = (OPS.parent / ".gitattributes").read_text(encoding="utf-8")
        for name in ("Dockerfile", ".dockerignore", "docker-compose.yml", "requirements.in", "requirements.txt", "constraints-production.txt"):
            self.assertIn(f"core-listener/{name} text eol=lf", attributes)

    def test_the_files_as_they_are_now_have_no_carriage_returns(self):
        for name in ("Dockerfile", ".dockerignore", "docker-compose.yml", "requirements.in", "requirements.txt", "constraints-production.txt"):
            with self.subTest(name=name):
                self.assertNotIn(bytes([13]), (LISTENER / name).read_bytes())


class WholeFileTests(unittest.TestCase):
    def test_the_networks_volumes_and_log_limits_are_unchanged(self):
        self.assertEqual(COMPOSE["networks"]["watchdog-net"]["name"], "clarix-watchdog-net")
        self.assertIn("watchdog-data", COMPOSE["volumes"])
        for name in ("watchdog-listener", "cloudflared"):
            self.assertEqual(COMPOSE["services"][name]["logging"]["options"]["max-size"], "10m")

    def test_no_secret_value_is_written_into_the_compose_file_or_the_dockerfile(self):
        for text in (COMPOSE_TEXT, DOCKERFILE):
            self.assertNotRegex(text, r"(?i)(password|token|secret|key)\s*[:=]\s*['\"]?[A-Za-z0-9+/_-]{24,}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
