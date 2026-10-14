"""Security checks: bandit on the code, pip-audit on the locked dependencies, and trivy.

    uv run python -m scripts.security

bandit and pip-audit always run (CI runs this script on every push). trivy, which also
looks for committed secrets and Dockerfile misconfiguration, runs when it is installed and
is skipped with a message otherwise. Exits non-zero if any check that ran found a problem.
"""

import shutil

# Only the fixed commands below are run, never anything from input.
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The code that ships; tests use fixed passwords and asserts on purpose.
CODE = ["app", "scripts", "migrations"]


def run(name: str, command: list[str]) -> bool:
    print(f"\n== {name}", flush=True)
    result = subprocess.run(command, cwd=ROOT, check=False)  # noqa: S603  # nosec B603
    return result.returncode == 0


def bandit() -> bool:
    # Skipped rules and the reasons are in pyproject.toml, [tool.bandit].
    return run(
        "bandit (code)",
        [sys.executable, "-m", "bandit", "-c", "pyproject.toml", "-q", "-r", *CODE],
    )


def pip_audit() -> bool:
    uv = shutil.which("uv")
    if uv is None:
        print(
            "\n== pip-audit: needs uv to export the locked dependencies (https://docs.astral.sh/uv/)"
        )
        return False
    with tempfile.TemporaryDirectory() as tmp:
        requirements = Path(tmp) / "requirements.txt"
        # Exactly the versions uv.lock pins, with hashes, so nothing is resolved again.
        exported = subprocess.run(  # noqa: S603  # nosec B603
            [
                uv,
                "export",
                "--frozen",
                "--all-groups",
                "--no-emit-project",
                "--quiet",
                "--format",
                "requirements-txt",
                "--output-file",
                str(requirements),
            ],
            cwd=ROOT,
            check=False,
        )
        if exported.returncode != 0:
            print("\n== pip-audit: `uv export` failed")
            return False
        return run(
            "pip-audit (dependencies in uv.lock)",
            [
                sys.executable,
                "-m",
                "pip_audit",
                "--requirement",
                str(requirements),
                "--require-hashes",
                "--disable-pip",
                "--progress-spinner",
                "off",
            ],
        )


def trivy() -> bool | None:
    path = shutil.which("trivy")
    if path is None:
        print(
            "\n== trivy: SKIPPED, not installed. Install it (https://trivy.dev) to also scan"
            " for committed secrets, Dockerfile misconfiguration and vulnerable packages."
        )
        return None
    return run(
        "trivy (secrets, misconfiguration, vulnerabilities)",
        [
            path,
            "fs",
            "--scanners",
            "vuln,secret,misconfig",
            "--severity",
            "HIGH,CRITICAL",
            "--exit-code",
            "1",
            "--skip-dirs",
            ".venv",
            "--skip-dirs",
            "data",
            ".",
        ],
    )


def main() -> None:
    results = {"bandit": bandit(), "pip-audit": pip_audit(), "trivy": trivy()}
    print("\nSummary")
    for name, passed in results.items():
        print(f"  {name:<10} {'skipped' if passed is None else 'passed' if passed else 'FAILED'}")
    if False in results.values():
        sys.exit(1)


if __name__ == "__main__":
    main()
