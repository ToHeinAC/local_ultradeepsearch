import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.adapters.outbound.ledger import MonthLedger, RunLedger, extract_credits, search_credits
from app.adapters.outbound.log import OutboundLog, OutboundRecord
from app.adapters.outbound.throttle import HostThrottle

OCT = datetime(2026, 10, 1, 12, tzinfo=UTC)
NOV = datetime(2026, 11, 1, 0, 0, 1, tzinfo=UTC)
FIELDS = {
    "ts",
    "step",
    "provider",
    "original_query",
    "sent_query",
    "removed_terms",
    "url",
    "status",
    "credits",
}


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


# ---- log ------------------------------------------------------------------------------------


def test_every_record_has_every_field(tmp_path: Path) -> None:
    log = OutboundLog(tmp_path / "run" / "outbound.jsonl", now=lambda: OCT)
    log.write(
        OutboundRecord(
            step="2",
            provider="tavily_search",
            status="200",
            original_query="Acme Q",
            sent_query="Q",
            removed_terms=("Acme",),
            credits=1,
        )
    )
    log.write(
        OutboundRecord(
            step="2", provider="http_get", status="blocked:private_ip", url="http://10.0.0.1/"
        )
    )
    first, second = lines(tmp_path / "run" / "outbound.jsonl")
    assert set(first) == FIELDS
    assert set(second) == FIELDS
    assert first == {
        "ts": OCT.isoformat(),
        "step": "2",
        "provider": "tavily_search",
        "original_query": "Acme Q",
        "sent_query": "Q",
        "removed_terms": ["Acme"],
        "url": None,
        "status": "200",
        "credits": 1,
    }
    assert second["original_query"] is None
    assert second["credits"] == 0


def test_log_is_thread_safe(tmp_path: Path) -> None:
    log = OutboundLog(tmp_path / "o.jsonl")
    threads = [
        threading.Thread(
            target=log.write, args=(OutboundRecord(step=str(i), provider="p", status="ok"),)
        )
        for i in range(40)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(int(r["step"]) for r in lines(tmp_path / "o.jsonl")) == list(range(40))


# ---- pricing --------------------------------------------------------------------------------


def test_prd_credit_example() -> None:
    """M2 AC4: 10 basic searches plus one extract call with 12 successful URLs = 13 credits."""
    assert 10 * search_credits() + extract_credits(12) == 13


@pytest.mark.parametrize(
    ("successful", "depth", "credits"),
    [(0, "basic", 0), (1, "basic", 1), (5, "basic", 1), (6, "basic", 2), (5, "advanced", 2)],
)
def test_extract_credits(successful: int, depth: str, credits: int) -> None:
    assert extract_credits(successful, depth) == credits  # type: ignore[arg-type]


def test_search_credits_by_depth() -> None:
    assert (search_credits("basic"), search_credits("advanced")) == (1, 2)


# ---- run ledger -----------------------------------------------------------------------------


def test_run_ledger_cap() -> None:
    ledger = RunLedger(cap=3)
    assert ledger.can_afford(3)
    ledger.charge(2)
    assert ledger.used == 2
    assert ledger.can_afford(1)
    assert not ledger.can_afford(2)
    assert RunLedger(cap=0).can_afford(1) is False


# ---- month ledger ---------------------------------------------------------------------------


def test_month_ledger_persists_and_counts(tmp_path: Path) -> None:
    path = tmp_path / "tavily-ledger.json"
    ledger = MonthLedger(path, limit=10, now=lambda: OCT)
    assert ledger.used() == 0
    ledger.charge(3)
    ledger.charge(4)
    assert MonthLedger(path, limit=10, now=lambda: OCT).used() == 7
    assert json.loads(path.read_text(encoding="utf-8")) == {"month": "2026-10", "credits": 7}


def test_month_ledger_resets_in_a_new_month(tmp_path: Path) -> None:
    path = tmp_path / "l.json"
    MonthLedger(path, limit=10, now=lambda: OCT).charge(9)
    november = MonthLedger(path, limit=10, now=lambda: NOV)
    assert november.used() == 0
    assert not november.at_limit()
    november.charge(1)
    assert json.loads(path.read_text(encoding="utf-8")) == {"month": "2026-11", "credits": 1}


def test_month_ledger_limit_and_warning_crossing(tmp_path: Path) -> None:
    ledger = MonthLedger(tmp_path / "l.json", limit=10, now=lambda: OCT)
    assert ledger.charge(7).crossed_warning is False
    assert ledger.charge(1).crossed_warning is True  # 7 -> 8 crosses 80 %
    assert ledger.charge(1).crossed_warning is False
    assert not ledger.at_limit()
    result = ledger.charge(1)
    assert result.total == 10
    assert ledger.at_limit()


def test_a_zero_limit_disables_tavily(tmp_path: Path) -> None:
    assert MonthLedger(tmp_path / "l.json", limit=0, now=lambda: OCT).at_limit()


def test_a_corrupt_ledger_file_counts_as_unknown_and_is_rewritten(tmp_path: Path) -> None:
    path = tmp_path / "l.json"
    path.write_text("{oops", encoding="utf-8")
    ledger = MonthLedger(path, limit=10, now=lambda: OCT)
    assert ledger.used() == 0
    ledger.charge(2)
    assert json.loads(path.read_text(encoding="utf-8")) == {"month": "2026-10", "credits": 2}


def test_concurrent_charges_are_not_lost(tmp_path: Path) -> None:
    path = tmp_path / "l.json"
    ledgers = [MonthLedger(path, limit=1000, now=lambda: OCT) for _ in range(4)]
    threads = [
        threading.Thread(target=lambda lg=lg: [lg.charge(1) for _ in range(25)]) for lg in ledgers
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert MonthLedger(path, limit=1000, now=lambda: OCT).used() == 100


# ---- throttle -------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 3))
        self.now += seconds


def test_throttle_spaces_requests_per_host() -> None:
    clock = Clock()
    throttle = HostThrottle(monotonic=clock.monotonic, sleep=clock.sleep)
    throttle.wait("https://example.com/a")
    throttle.wait("https://example.com/b")
    throttle.wait("https://other.org/")  # a different host does not wait
    clock.now += 0.4
    throttle.wait("https://example.com/c")
    assert clock.slept == [1.0, 0.6]


def test_throttle_host_overrides() -> None:
    clock = Clock()
    throttle = HostThrottle(monotonic=clock.monotonic, sleep=clock.sleep)
    for _ in range(2):
        throttle.wait("http://export.arxiv.org/api/query?x")
    for _ in range(2):
        throttle.wait("https://api.tavily.com/search")
    for _ in range(2):
        throttle.wait("https://api.openalex.org/works")
    assert clock.slept == [3.0, 0.1]


def test_throttle_host_matching_is_case_insensitive() -> None:
    clock = Clock()
    throttle = HostThrottle(monotonic=clock.monotonic, sleep=clock.sleep)
    throttle.wait("https://Example.COM/a")
    throttle.wait("https://example.com/b")
    assert clock.slept == [1.0]
