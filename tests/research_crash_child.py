"""Child process of the Phase-2 SIGKILL test (tests/test_research_resume.py).

Creates a run, approves its plan and searches; the fake gateway prints ``SEARCH <query>`` as each
search starts and hangs inside the fourth, so a parent that kills this process after that line
always stops it with three searches stored and the fourth in flight.
"""

import sys
import time
from dataclasses import dataclass
from pathlib import Path

from research_rig import FakeGateway, build_research_rig, corpus_hits

from app.adapters.outbound.gateway import PreparedQuery
from app.adapters.outbound.types import SearchHit

HANG_ON = 4
HANG_SECONDS = 120


@dataclass
class PrintingGateway(FakeGateway):
    def search_web(
        self, prepared: PreparedQuery, *, step: str, include_domains=(), max_results: int = 10
    ) -> list[SearchHit]:
        print(f"SEARCH {prepared.sent}", flush=True)
        if len(self.web_calls) + 1 == HANG_ON:
            time.sleep(HANG_SECONDS)
        return super().search_web(
            prepared, step=step, include_domains=include_domains, max_results=max_results
        )


def main(base_dir: Path) -> None:
    rig = build_research_rig(base_dir, gateway=PrintingGateway(web_hits=corpus_hits()))
    run_id = rig.new_run()
    print(f"RUN {run_id}", flush=True)
    view = rig.service.start(run_id)
    assert view.plan is not None
    rig.service.approve_plan(run_id, view.plan.plan_sha256)
    print("DONE", flush=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
