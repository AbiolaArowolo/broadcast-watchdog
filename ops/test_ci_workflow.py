"""Checks of the GitHub Actions workflow and the Dependabot configuration.

The rules enforced here are the ones that make a CI file itself safe: read-only token, no secrets, no long-lived credentials in the checkout, every action pinned
to an exact version, every package installed from a hash-locked file, every image pinned by digest, a time limit on every job, and no test file in ops/ that
CI silently skips.

Run:  python ops/test_ci_workflow.py
"""
import re
import sys
import unittest
from pathlib import Path

import yaml

OPS = Path(__file__).resolve().parent
REPO = OPS.parent
WORKFLOW_PATH = REPO / ".github" / "workflows" / "ci.yml"
DEPENDABOT_PATH = REPO / ".github" / "dependabot.yml"
TEXT = WORKFLOW_PATH.read_text(encoding="utf-8")
WORKFLOW = yaml.safe_load(TEXT)
TRIGGERS = WORKFLOW.get("on", WORKFLOW.get(True))          # PyYAML reads the bare key `on` as the boolean True
JOBS = WORKFLOW["jobs"]

# Test files that CI does not run, and why. Everything else in ops/ named test_*.py must appear in the workflow.
NOT_IN_CI = {}


def steps(job):
    return JOBS[job]["steps"]


def run_text(job):
    return "\n".join(str(step.get("run", "")) for step in steps(job))


class SafetyTests(unittest.TestCase):
    def test_the_token_is_read_only_everywhere(self):
        self.assertEqual(WORKFLOW["permissions"], {"contents": "read"})
        for name, job in JOBS.items():
            with self.subTest(job=name):
                self.assertNotIn("permissions", job, "a job must not widen the workflow's read-only token")

    def test_it_runs_on_pushes_and_pull_requests_and_never_on_pull_request_target(self):
        self.assertIn("push", TRIGGERS)
        self.assertIn("pull_request", TRIGGERS)
        self.assertNotIn("pull_request_target", TRIGGERS)          # that trigger runs untrusted code with the repository's secrets
        self.assertNotIn("pull_request_target", TEXT)

    def test_no_secret_and_no_write_token_is_used(self):
        self.assertNotIn("secrets.", TEXT)
        self.assertNotIn("GITHUB_TOKEN", TEXT)
        self.assertNotIn("id-token", TEXT)

    def test_checkout_keeps_no_credentials_in_the_workspace(self):
        for name in JOBS:
            for step in steps(name):
                if str(step.get("uses", "")).startswith("actions/checkout@"):
                    self.assertIs(step["with"]["persist-credentials"], False, name)

    def test_every_job_has_a_time_limit(self):
        for name, job in JOBS.items():
            with self.subTest(job=name):
                self.assertIsInstance(job.get("timeout-minutes"), int)
                self.assertLessEqual(job["timeout-minutes"], 30)

    def test_every_action_is_a_github_owned_one_pinned_to_an_exact_commit_with_its_version_in_a_comment(self):
        # A tag can be moved by whoever controls the action's repository; a full commit SHA cannot. The comment says which release the SHA is.
        uses = re.findall(r"^\s*-?\s*uses:\s*(\S+)(.*)$", TEXT, re.M)
        self.assertGreaterEqual(len(uses), 4)
        for action, rest in uses:
            with self.subTest(action=action):
                self.assertRegex(action, r"^actions/[a-z-]+@[0-9a-f]{40}$")
                self.assertRegex(rest, r"^\s+# v\d+\.\d+\.\d+\s*$")

    def test_nothing_is_piped_into_a_shell_or_fetched_unpinned(self):
        for pattern in (r"curl[^\n]*\|\s*(ba)?sh", r"wget[^\n]*\|\s*(ba)?sh", r"bash\s+<\(", r"npx\s", r"pip install(?![^\n]*--require-hashes)[^\n]*\b[a-z]"):
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, TEXT), pattern)

    def test_every_pip_install_is_from_a_hash_locked_file(self):
        installs = re.findall(r"pip install[^\n]*", TEXT)
        self.assertGreaterEqual(len(installs), 2)
        for line in installs:
            with self.subTest(line=line):
                self.assertIn("--require-hashes", line)
                self.assertRegex(line, r"-r (core-listener|ops)/requirements[a-z-]*\.txt")

    def test_every_container_image_run_by_a_job_is_pinned_by_digest(self):
        for image in re.findall(r"docker run[^\n]*?\s([a-z0-9./_-]+:[A-Za-z0-9._-]+(?:@sha256:[0-9a-f]+)?)\s+(?:detect|\S+)", TEXT):
            with self.subTest(image=image):
                self.assertRegex(image, r"@sha256:[0-9a-f]{64}$")


class CoverageTests(unittest.TestCase):
    def test_every_python_test_file_in_ops_runs_in_ci(self):
        for path in sorted(OPS.glob("test_*.py")):
            with self.subTest(file=path.name):
                self.assertTrue(path.name in TEXT or path.name in NOT_IN_CI, f"{path.name} is not run by CI and has no reason listed")

    def test_no_test_that_runs_on_a_linux_job_uses_a_windows_only_command(self):
        # The first CI run found test_hostile_input_live.py stopping its throwaway server with `taskkill`, which only exists on Windows: the whole
        # test passed, then its cleanup crashed on the Linux runner. A test run on Linux must stop processes with something that works on both.
        for name, job in JOBS.items():
            if not job["runs-on"].startswith("ubuntu"):
                continue
            for test_name in re.findall(r"ops/(test_[a-z_]+\.py)", run_text(name)):
                if test_name == Path(__file__).name:
                    continue                                # this file names the banned commands, so it would always match itself
                with self.subTest(job=name, test=test_name):
                    source = (OPS / test_name).read_text(encoding="utf-8")
                    found = [word for word in ("taskkill", "powershell.exe", "cmd /c", "wmic") if word in source]
                    self.assertEqual(found, [], f"{test_name} runs on Linux in the {name} job but calls a Windows-only command")

    def test_the_jobs_cover_the_unit_container_edge_audit_and_scan_stages(self):
        self.assertEqual(set(JOBS), {"unit-tests", "edge-senders", "container", "dependency-audit", "static-analysis", "secrets-scan"})

    def test_the_powershell_tests_run_on_windows_where_powershell_5_exists(self):
        self.assertEqual(JOBS["edge-senders"]["runs-on"], "windows-latest")
        self.assertIn("test_edge_senders.py", run_text("edge-senders"))

    def test_the_real_container_test_runs_on_a_runner_with_docker(self):
        self.assertEqual(JOBS["container"]["runs-on"], "ubuntu-latest")
        self.assertIn("test_container_live.py", run_text("container"))
        self.assertIn("test_container_hardening.py", run_text("container"))

    def test_python_is_the_containers_python(self):
        """The tests run on the Python the image runs. That is read from the Dockerfile's FROM line: this test used to compare CI with the text "python:3.12",
        which passed only because a comment in the Dockerfile still said so, while the image had long been on 3.14."""
        dockerfile = (REPO / "core-listener" / "Dockerfile").read_text(encoding="utf-8")
        minor = re.search(r"^FROM python:(\d+\.\d+)-slim@sha256:", dockerfile, re.MULTILINE).group(1)
        for name in ("unit-tests", "container", "dependency-audit", "static-analysis"):
            versions = [str(step["with"]["python-version"]) for step in steps(name) if str(step.get("uses", "")).startswith("actions/setup-python@")]
            self.assertEqual(versions, [minor], name)


class AuditAndScanTests(unittest.TestCase):
    def test_the_dependency_audit_is_strict_apart_from_listed_and_justified_exceptions(self):
        audit = run_text("dependency-audit")
        self.assertIn("pip-audit", audit)
        self.assertIn("core-listener/requirements.txt", audit)
        self.assertIn("--disable-pip", audit)
        self.assertNotIn("continue-on-error", yaml.safe_dump(JOBS["dependency-audit"]))
        ignored = re.findall(r"--ignore-vuln\s+(\S+)", audit)
        self.assertEqual(len(ignored), len(set(ignored)))
        for vulnerability in ignored:
            with self.subTest(vulnerability=vulnerability):
                reasons = [line for line in TEXT.splitlines() if line.strip().startswith("#") and vulnerability in line]
                self.assertTrue(reasons, f"{vulnerability} is ignored without a written reason")
                self.assertRegex(" ".join(reasons).lower(), r"not reachable|never|does not")

    def test_the_static_analysis_fails_on_medium_and_above_in_the_service_code(self):
        self.assertRegex(run_text("static-analysis"), r"bandit -r core-listener/app -ll")

    def test_the_secrets_scan_reads_the_whole_history(self):
        checkout = next(step for step in steps("secrets-scan") if str(step.get("uses", "")).startswith("actions/checkout@"))
        self.assertEqual(checkout["with"]["fetch-depth"], 0)
        self.assertIn("gitleaks", run_text("secrets-scan"))
        self.assertIn("--redact", run_text("secrets-scan"))         # a found secret must not be printed into the public-ish log


class SecretsScanConfigTests(unittest.TestCase):
    def test_the_only_exception_is_the_one_fake_token_string_never_a_path_or_a_rule(self):
        import tomllib
        config = tomllib.loads((REPO / ".gitleaks.toml").read_text(encoding="utf-8"))
        self.assertTrue(config["extend"]["useDefault"])
        self.assertEqual(len(config["allowlists"]), 1)
        allow = config["allowlists"][0]
        self.assertEqual(allow["regexes"], ["^eyJhIjoiYWJjIn0=$"])          # gitleaks compares an allowlist regex with the secret VALUE, so the exact value, anchored
        for broad in ("paths", "commits", "stopwords", "targetRules", "condition"):
            self.assertNotIn(broad, allow)
        import base64
        self.assertEqual(base64.b64decode("eyJhIjoiYWJjIn0="), b'{"a":"abc"}')          # the string really is a harmless placeholder

    def test_the_two_private_key_look_alikes_in_history_are_made_up_and_no_file_holds_a_key_block_now(self):
        """Those two lines (commit 5544ed4, ops/test_staff_pcs.py, lines 98 and 100) were test strings for 'a private key is refused': a header, 24 to 32 base64 characters (the standard
        prefix every key starts with, not key material) and a footer. The file now builds such text from pieces, and this test keeps ANY file from holding a whole key block again, so the
        ignore file never has to grow for a new one."""
        pattern = re.compile(r"-----BEGIN" + r"[ A-Z0-9_-]{0,100}PRIVATE KEY(?: BLOCK)?-----[\s\S-]{64,}?KEY(?: BLOCK)?-----")
        suffixes = {".py", ".md", ".yml", ".yaml", ".toml", ".txt", ".sh", ".service", ".example", ".snippet", ".mjs", ".json", ".pem", ".env"}
        offenders = []
        for path in REPO.rglob("*"):
            if not path.is_file() or path.suffix not in suffixes or ".claude" in path.parts or "node_modules" in path.parts or ".git" in path.parts:
                continue
            if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
                offenders.append(str(path.relative_to(REPO)))
        self.assertEqual(offenders, [])

    def test_no_real_looking_token_value_is_in_the_repository_outside_that_fixture(self):
        for path in list(REPO.glob("**/*.py")) + list(REPO.glob("**/*.yml")) + list(REPO.glob("**/*.sh")):
            if ".claude" in path.parts or "node_modules" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for match in re.finditer(r"WATCHDOG_TUNNEL_TOKEN=([A-Za-z0-9+/=_-]{12,})", text):
                self.assertEqual(match.group(1), "eyJhIjoiYWJjIn0=", f"{path.name}: a tunnel-token-shaped value that is not the known placeholder")


class DependabotTests(unittest.TestCase):
    def config(self):
        return yaml.safe_load(DEPENDABOT_PATH.read_text(encoding="utf-8"))

    def test_every_ecosystem_this_repository_uses_is_watched(self):
        watched = {(u["package-ecosystem"], u["directory"]) for u in self.config()["updates"]}
        for expected in (("pip", "/core-listener"), ("pip", "/ops"), ("docker", "/core-listener"), ("docker-compose", "/core-listener"),
                         ("github-actions", "/"), ("npm", "/ops/browser_tests")):
            self.assertIn(expected, watched)

    def test_updates_are_weekly_and_bounded(self):
        for update in self.config()["updates"]:
            with self.subTest(update=update["package-ecosystem"] + update["directory"]):
                self.assertEqual(update["schedule"]["interval"], "weekly")
                self.assertLessEqual(update["open-pull-requests-limit"], 5)

    def test_the_directories_exist_and_hold_what_dependabot_reads(self):
        for update in self.config()["updates"]:
            self.assertTrue((REPO / update["directory"].lstrip("/")).is_dir(), update["directory"])
        self.assertTrue((REPO / "core-listener" / "requirements.in").is_file())

    def test_dependabot_may_not_move_the_python_image_past_the_minor_the_lock_installs_on(self):
        """On 2026-11-01 Dependabot moved the image to python:3.15-slim. The hash lock pins pydantic-core 2.46.5, which has wheels for Python 3.9 to 3.14 only
        (the rest is a Rust source file), so `pip install --require-hashes` found nothing to install and the image stopped building. A new Python minor needs
        a new lock first, so both Python-image watchers (the Dockerfile and the data-owner helper, which must stay the same image) carry a fence that starts
        at the first minor above the one the Dockerfile uses. Moving up on purpose means re-locking, then moving the image and this fence together."""
        dockerfile = (REPO / "core-listener" / "Dockerfile").read_text(encoding="utf-8")
        major, minor = (int(part) for part in re.search(r"^FROM python:(\d+)\.(\d+)", dockerfile, re.MULTILINE).groups())
        watchers = [u for u in self.config()["updates"] if u["package-ecosystem"] in ("docker", "docker-compose")]
        self.assertEqual({u["package-ecosystem"] for u in watchers}, {"docker", "docker-compose"})
        for update in watchers:
            with self.subTest(update=update["package-ecosystem"]):
                fences = [ignore for ignore in update.get("ignore", []) if ignore.get("dependency-name") == "python"]
                self.assertEqual(len(fences), 1, "exactly one ignore rule for the python image")
                self.assertEqual(fences[0].get("versions"), [f">= {major}.{minor + 1}"])
                self.assertNotIn("update-types", fences[0], "the fence is a version range, so a patch or digest update of the current minor still arrives")


class CiToolLockTests(unittest.TestCase):
    """The tools CI itself installs (pip-audit, bandit, PyYAML) are locked with hashes too: a CI that trusts an unlocked tool is a supply-chain door."""

    def test_the_ci_tools_are_hash_locked(self):
        source = OPS / "requirements-ci.in"
        lock = OPS / "requirements-ci.txt"
        self.assertTrue(source.is_file())
        self.assertTrue(lock.is_file(), "generate it: uv pip compile ops/requirements-ci.in --generate-hashes --python-platform x86_64-manylinux_2_28 "
                                        "--python-version 3.12 -o ops/requirements-ci.txt")
        text = lock.read_text(encoding="utf-8")
        for tool in ("pip-audit", "bandit", "pyyaml"):
            self.assertRegex(text, rf"(?im)^{tool}==\S+ \\")
        self.assertGreaterEqual(text.count("--hash=sha256:"), 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
