"""One-shot verification runner.

Executes, in order:
  1. the unit/integration test suite (pytest),
  2. an image build check (rebuilds the app image from the Dockerfile),
  3. an HTTP smoke test performing a valid clip against the running app.

Every step must succeed; the process exit code reports the overall result
(0 = all checks passed, 1 = at least one step failed).
"""

import os
import subprocess
import sys

WORKDIR = os.environ.get("VERIFY_WORKDIR", "/workspace")

STEPS = [
    # The repository is bind-mounted read-only, so keep pytest from writing
    # its cache directory into it.
    ("unit tests", [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]),
    ("image build check",
     ["docker", "build", "-f", "Dockerfile", "-t", "bwf-clip:verify-check", "."]),
    ("api smoke", [sys.executable, "verify/smoke.py"]),
]


def main():
    failed = []
    for name, cmd in STEPS:
        print(f"\n=== verify: {name} ===", flush=True)
        rc = subprocess.run(cmd, cwd=WORKDIR).returncode
        print(f"=== verify: {name} -> exit {rc} ===", flush=True)
        if rc != 0:
            failed.append(name)
    if failed:
        print("VERIFY FAILED: " + ", ".join(failed), flush=True)
        return 1
    print("VERIFY PASSED", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
