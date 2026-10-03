"""Running pandoc (PRD M5 export). The only code that starts it; core logic gets a `PandocRunner`.

pandoc is an external GPL binary: it is run, never linked or vendored (PRD R14). Its PDF engine,
`weasyprint`, is installed with the project, so the environment's scripts directory goes first on
the PATH of the child process.
"""

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from app.research.export import PandocResult

NOT_FOUND = 127  # the shell's code for a command that does not exist
TIMED_OUT = 124  # the code of `timeout(1)`


class SubprocessPandoc:
    def __init__(self, binary: str = "pandoc", *, timeout_s: float = 180.0) -> None:
        self._binary = binary
        self._timeout_s = timeout_s

    def run(self, args: Sequence[str], *, cwd: Path) -> PandocResult:
        """Run pandoc with ``args`` in ``cwd``; never raises for a missing binary or a hang."""
        scripts = str(Path(sys.executable).parent)
        env = {**os.environ, "PATH": f"{scripts}{os.pathsep}{os.environ.get('PATH', '')}"}
        try:
            done = subprocess.run(
                [self._binary, *args],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
            )
        except OSError:
            return PandocResult(NOT_FOUND, f"{self._binary}: not found or not executable")
        except subprocess.TimeoutExpired:
            return PandocResult(TIMED_OUT, f"timed out after {self._timeout_s:g} s")
        return PandocResult(done.returncode, done.stderr.strip())
