from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from nfi_backtest_engine.changed_signal_replay import replay_changed_signal


def _recursive_manifest(root: Path) -> tuple[tuple[str, str], ...]:
    return tuple(
        (path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def test_complete_published_replay_root_is_byte_deterministic(
    tmp_path: Path,
) -> None:
    # Given: one complete Native replay root from a clean sequential execution.
    first = replay_changed_signal("spot", "native")
    first_root = Path(str(first["output"]))
    snapshot = tmp_path / "first-published-root"
    shutil.copytree(first_root, snapshot)

    # When: the same lane is independently replayed from the sealed inputs.
    second = replay_changed_signal("spot", "native")
    second_root = Path(str(second["output"]))

    # Then: every published path and byte is identical and no transient member leaks.
    first_manifest = _recursive_manifest(snapshot)
    assert first_manifest == _recursive_manifest(second_root)
    assert {path for path, _digest in first_manifest} == {
        "engine-events.jsonl",
        "manifest.json",
        "simulation-result.json",
        "state-projection.nfitrace",
    }
    assert not list(Path("/tmp").glob("task8-r6-private-spot-native-*"))
