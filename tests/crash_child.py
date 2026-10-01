"""Child process of the SIGKILL test (tests/test_fetch_pipeline.py).

Ingests a synthetic corpus into ``<base_dir>`` and prints ``STORED <n>`` as soon as the n-th source
is committed. The fake model hangs in the second extraction, so a parent that kills this process
after ``STORED 2`` always stops it with source 2 stored but not extracted.
"""

import sys
from pathlib import Path
from typing import Any

from fixtures_corpus import FakeModels, build, simple_corpus

from app.events import Level


class PrintingSink:
    def __init__(self) -> None:
        self.stored = 0

    def emit(self, type: str, *, level: Level = "info", **data: Any) -> None:
        if type == "source_stored":
            self.stored += 1
            print(f"STORED {self.stored}", flush=True)


def main(base_dir: Path) -> None:
    served, urls = simple_corpus(6)
    built = build(base_dir, served, models=FakeModels(hang_on_extraction=2), events=PrintingSink())
    for url in urls:
        built.pipeline.ingest(url)
    print("DONE", flush=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
