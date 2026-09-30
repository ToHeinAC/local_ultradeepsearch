"""Stop hook: keep Claude working while changed Python code fails the quality gate.

Runs the pre-commit gate only if .py files are modified or untracked. Exit code 2
blocks the stop and feeds the failure tail back to Claude; the `stop_hook_active`
guard allows at most one forced continuation per stop.
"""

import json
import subprocess
import sys
from typing import TextIO

GATE = ["uv", "run", "pre-commit", "run", "--all-files"]
TAIL_LINES = 40


def python_changed() -> bool:
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", "*.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    return status.returncode == 0 and bool(status.stdout.strip())


def main(stdin: TextIO = sys.stdin) -> int:
    if json.load(stdin).get("stop_hook_active") or not python_changed():
        return 0
    gate = subprocess.run(GATE, capture_output=True, text=True, check=False)
    if gate.returncode == 0:
        return 0
    lines = (gate.stdout + gate.stderr).splitlines()
    tail = [ln for ln in lines if not ln.endswith(("Passed", "Skipped"))][-TAIL_LINES:]
    print("Quality gate failed. Fix it before finishing:\n" + "\n".join(tail), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
