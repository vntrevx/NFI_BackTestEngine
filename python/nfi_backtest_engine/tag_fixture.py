"""Generate deterministic tag evidence from pinned Freqtrade wrappers."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import numpy as np
import pandas as pd

from .signal_fixture import (
    _canonical_json,
    _encode_frame,
    _load_strategy,
    _repository_root,
    _sha256_file,
    _source_commit,
    _source_version,
    canonical_sha256,
)

PINNED_SOURCE = Path(".nfi/roadmap-acceptance/M20-05/freqtrade-2026.5.1")
CONTRACT_PATH = Path("benchmarks/reference/strategies/TagProgramContract.py")
FIXTURE_PATH = Path("benchmarks/reference/tags/freqtrade-2026.5.1.json")


def generate_fixture(source_root: Path | None = None) -> dict[str, object]:
    """Execute exact pinned advise_entry/advise_exit around the tag contract."""
    repository = _repository_root()
    root = source_root or repository / PINNED_SOURCE
    interface = root / "freqtrade/strategy/interface.py"
    contract = repository / CONTRACT_PATH
    strategy, method_hashes = _load_strategy(interface, contract)
    input_frame = _input_frame()
    entry = strategy.advise_entry(input_frame.copy(deep=True), {"pair": "ETH/USDT"})
    output = strategy.advise_exit(entry, {"pair": "ETH/USDT"})
    fixture: dict[str, object] = {
        "schema_version": "freqtrade-tag-fixture-v1",
        "source": {
            "version": _source_version(root),
            "commit": _source_commit(root),
            "interface": "freqtrade/strategy/interface.py",
            "interface_sha256": _sha256_file(interface),
            "method_sha256": method_hashes,
            "strategy": str(CONTRACT_PATH),
            "strategy_sha256": _sha256_file(contract),
            "pandas": pd.__version__,
        },
        "call_order": ["advise_entry", "advise_exit"],
        "input": _encode_frame(input_frame),
        "output": _encode_frame(output),
    }
    fixture["fingerprint"] = canonical_sha256(fixture)
    return fixture


def write_fixture(destination: Path, source_root: Path | None = None) -> dict[str, object]:
    """Generate and persist canonical tag fixture evidence."""
    fixture = generate_fixture(source_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(_canonical_json(fixture) + "\n", encoding="utf-8")
    return fixture


def encode_tag_columns(frame: pd.DataFrame) -> dict[str, object]:
    """Encode raw tag strings without canonicalizing or trimming them."""
    return _encode_frame(frame.loc[:, ["enter_tag", "exit_tag"]])


def assert_fixture_identity(fixture: Mapping[str, object]) -> None:
    """Reject a changed or self-inconsistent committed oracle."""
    if fixture.get("schema_version") != "freqtrade-tag-fixture-v1":
        raise ValueError("tag fixture schema differs")
    if fixture.get("fingerprint") != canonical_sha256(fixture):
        raise ValueError("tag fixture fingerprint differs")


def _input_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "score": [-2.0, -0.5, 0.0, 0.5, 1.5, 2.0, 2.5, np.nan],
            "exit_mask": pd.array(
                [False, True, False, True, False, True, pd.NA, False],
                dtype="boolean",
            ),
            "enter_tag": ["stale-entry"] * 8,
            "exit_tag": ["stale-exit"] * 8,
        }
    )
