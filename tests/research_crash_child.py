"""Child process of the Phase-2 SIGKILL tests (tests/test_research_crash.py).

Runs a whole Lite run on the rig of `research_run_rig` in ``argv[1]``. With mode ``sweep`` the
fake search hangs in its third call, with mode ``draft`` the fake model hangs in its third section.
It prints `RUN <id>` when the run exists and `HANGING ...` when the hanging call starts, so the
parent can kill it at exactly that point.
"""

import sys
from pathlib import Path

from research_rig import ResearchModels
from research_run_rig import make_rig, make_searcher


def main(base_dir: Path, mode: str) -> None:
    if mode == "sweep":
        rig = make_rig(base_dir, searcher=make_searcher(hang_at=3))
    else:
        rig = make_rig(base_dir, models=ResearchModels(hang_on={"text": 3}))
    run_id = rig.create().run_id
    print(f"RUN {run_id}", flush=True)
    rig.service.run(run_id)
    rig.service.approve_plan(run_id, str(rig.service.view(run_id).plan_sha256))
    print("DONE", flush=True)  # never reached: a call hangs until the parent kills us


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2])
