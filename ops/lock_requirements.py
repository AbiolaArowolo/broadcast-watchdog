"""Regenerates core-listener/requirements.txt: every package the listener needs, at one exact version, with a hash for every downloadable file.

    python ops/lock_requirements.py            lock to the versions held in constraints-production.txt (Dependabot edits that file; run this to bring the lock to it)
    python ops/lock_requirements.py --upgrade  ignore that file and take the newest versions that satisfy requirements.in (the dependency-upgrade branch);
                                               afterwards copy the new versions into constraints-production.txt, because a test requires the two to agree

Why hashes: the image installs with `pip install --require-hashes --no-deps`, so a package that was swapped or changed on PyPI after it was locked is
REFUSED at build time instead of installed. `--python-platform` and `--python-version` pin the lock to what the container is (Linux x86_64, and the Python of
the Dockerfile's base image: PYTHON_VERSION below, which ops/test_container_hardening.py compares with the Dockerfile's FROM line), whatever machine this
runs on. Needs `uv` (https://docs.astral.sh/uv/).
"""
import shutil
import subprocess
import sys
from pathlib import Path

LISTENER = Path(__file__).resolve().parent.parent / "core-listener"
PLATFORM = "x86_64-manylinux_2_28"          # Core is x86_64; the base image is Debian (glibc)
PYTHON_VERSION = "3.14"                     # the base image's Python: must equal the minor in the Dockerfile's FROM line (a test checks it)


def command(upgrade: bool, output: Path = LISTENER / "requirements.txt") -> list:
    args = ["uv", "pip", "compile", str(LISTENER / "requirements.in"), "--generate-hashes", "--python-platform", PLATFORM,
            "--python-version", PYTHON_VERSION, "--no-header", "-o", str(output)]
    if not upgrade:
        args += ["-c", str(LISTENER / "constraints-production.txt")]
    return args


def main(argv) -> int:
    if argv not in ([], ["--upgrade"]):
        print(__doc__)
        return 2
    if not shutil.which("uv"):
        print("uv is not installed (pip install uv)")
        return 1
    result = subprocess.run(command(upgrade=bool(argv)), cwd=str(LISTENER))
    if result.returncode == 0:
        print("wrote", LISTENER / "requirements.txt")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
