"""Child process of the SIGKILL test (tests/test_brief_service.py).

Runs a Phase-1 session on the files in ``argv[1]``: it starts the session, answers its first
questions, and then the fake model hangs inside the draft. It prints `STARTED <session id>` after
the session is waiting for answers and `HANGING draft` when the model call starts, so the parent
can kill it at exactly that point.
"""

import sys
from pathlib import Path

from brief_rig import Models, kinds, rig

from app.brief.errors import BriefError


def main(base_dir: Path) -> None:
    r = rig(base_dir, Models(hang_on={"draft": 1}))
    view = r.service.start("Wie lange dauert der Rückbau eines Forschungsreaktors?")
    print(f"STARTED {view.session_id}", flush=True)
    try:
        r.service.answer(view.session_id, kinds("accept", "accept"))
    except BriefError as exc:  # never reached: the draft hangs until the parent kills us
        print(f"ERROR {exc}", flush=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
