import sys
from collections.abc import Sequence
from pathlib import Path

from app.adapters.system_probe import (
    Gpu,
    find_binary,
    find_models_dir,
    free_disk_bytes,
    list_gpus,
    parse_gpus,
    run_command,
)

SMI = """0, NVIDIA GeForce RTX 4090, 24564, 83
1, NVIDIA GeForce RTX 4090, 24564, 15
"""


def test_parse_gpus() -> None:
    assert parse_gpus(SMI) == [
        Gpu(0, "NVIDIA GeForce RTX 4090", 24564, 83),
        Gpu(1, "NVIDIA GeForce RTX 4090", 24564, 15),
    ]


def test_parse_gpus_skips_garbage_lines() -> None:
    assert parse_gpus("\nnot csv\n0, X, many, 5\n1, Y, 100, 5\n") == [Gpu(1, "Y", 100, 5)]
    assert parse_gpus("") == []


def test_list_gpus_distinguishes_unavailable_from_none_found() -> None:
    gpus = list_gpus(lambda argv: SMI)
    assert gpus is not None
    assert gpus[1].index == 1
    assert list_gpus(lambda argv: None) is None
    assert list_gpus(lambda argv: "") == []


def test_list_gpus_asks_nvidia_smi_for_the_right_columns() -> None:
    seen: list[Sequence[str]] = []

    def runner(argv: Sequence[str]) -> str:
        seen.append(argv)
        return SMI

    list_gpus(runner)
    assert seen[0][0] == "nvidia-smi"
    assert "--query-gpu=index,name,memory.total,memory.used" in seen[0]
    assert "--format=csv,noheader,nounits" in seen[0]


def test_run_command_returns_stdout_or_none() -> None:
    assert run_command([sys.executable, "-c", "print('hi')"]) == "hi\n"
    assert run_command([sys.executable, "-c", "raise SystemExit(3)"]) is None
    assert run_command(["definitely-not-installed-binary-xyz"]) is None
    assert run_command([sys.executable, "-c", "import time; time.sleep(5)"], timeout_s=0.2) is None


def test_free_disk_bytes(tmp_path: Path) -> None:
    assert free_disk_bytes(tmp_path) > 0


def test_free_disk_bytes_walks_up_to_an_existing_parent(tmp_path: Path) -> None:
    assert free_disk_bytes(tmp_path / "does" / "not" / "exist") > 0


def test_find_binary() -> None:
    assert find_binary("sh") is not None
    assert find_binary("definitely-not-installed-binary-xyz") is None


def make_store(root: Path, manifests: int) -> Path:
    folder = root / "manifests" / "registry.ollama.ai" / "library"
    folder.mkdir(parents=True)
    for i in range(manifests):
        (folder / f"m{i}").write_text("{}")
    return root


def test_find_models_dir_prefers_the_store_with_the_most_manifests(tmp_path: Path) -> None:
    empty = make_store(tmp_path / "empty", 0)
    big = make_store(tmp_path / "big", 5)
    small = make_store(tmp_path / "small", 2)
    assert find_models_dir(None, [empty, small, big]) == big


def test_find_models_dir_explicit_setting_wins_even_if_smaller(tmp_path: Path) -> None:
    chosen = make_store(tmp_path / "chosen", 1)
    big = make_store(tmp_path / "big", 9)
    assert find_models_dir(chosen, [big]) == chosen


def test_find_models_dir_ignores_missing_and_empty_stores(tmp_path: Path) -> None:
    assert find_models_dir(None, [tmp_path / "missing", make_store(tmp_path / "e", 0)]) is None
    assert find_models_dir(tmp_path / "also-missing", [tmp_path / "missing"]) is None
