"""PostToolUse hook: ruff-format a Python file right after Claude edits it.

Only formats, never `ruff check --fix`: an import added one edit before its
first use must not be deleted as unused. Lint fixes run in the gate.
"""

import json
import subprocess
import sys
from typing import TextIO


def main(stdin: TextIO = sys.stdin) -> int:
    path = json.load(stdin).get("tool_input", {}).get("file_path", "")
    if path.endswith(".py"):
        subprocess.run(["ruff", "format", "--quiet", path], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
