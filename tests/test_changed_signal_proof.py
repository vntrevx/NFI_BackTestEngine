from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from nfi_backtest_engine import changed_signal_proof
from nfi_backtest_engine.changed_signal_proof import (
    ChangedSignalIdentity,
    validate_changed_signal_proof,
    write_changed_signal_proof,
)
from nfi_backtest_engine.errors import SpecValidationError

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "benchmarks/evidence/m22/current-x7-changed-signal-boundary.json"
CONTRACT = ROOT / "benchmarks/reference/strategies/CurrentChangedPredicateContract.py"


def _proof_document() -> dict[str, Any]:
    return json.loads(EVIDENCE.read_text(encoding="utf-8"))


def _identity(document: dict[str, Any]) -> ChangedSignalIdentity:
    return ChangedSignalIdentity(**document["identity"])


def _reseal(document: dict[str, Any]) -> None:
    unsigned = {key: value for key, value in document.items() if key != "fingerprint"}
    document["fingerprint"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_complete_identity_bound_dual_mode_proof_promotes() -> None:
    document = _proof_document()

    validate_changed_signal_proof(document, _identity(document))


def test_current_official_capture_is_exact_and_bound_to_execution_time_target() -> None:
    document = _proof_document()
    identity = _identity(document)

    validate_changed_signal_proof(document, identity)

    assert identity.upstream_commit == "eebaf97c1434bd8f208b7cd9c417606646e1e478"
    assert identity.source_sha256 == (
        "a4ba29b94b459511163f05cce6687b5b84542147b11715a69e3fa468fab2767a"
    )
    assert identity.target_id == (
        "286b19a0914ff96dec95adc322e7bbc7cf6e6c6ca357e4a063300fef8f2dbd47"
    )
    assert document["capture"]["kind"] == "sealed-freqtrade-backtest"
    assert document["capture"]["contract_sha256"] == hashlib.sha256(
        CONTRACT.read_bytes()
    ).hexdigest()
    assert document["capture"]["bounded_rows"] == 5
    for mode in ("spot", "futures"):
        proof = document["modes"][mode]
        assert proof["coverage"] == {
            "passing_rows": [1, 2, 3, 4],
            "failing_rows": [0],
            "independent_term_rows": [1, 2, 3, 4],
        }
        assert all(item["exact"] for item in proof["parity"].values())
        assert all(item["detected"] for item in proof["mutations"])
        assert proof["official_provenance"]["producer"] == "freqtrade-backtesting"
        assert proof["native_provenance"]["producer"] == "nfi-native-engine"


@pytest.mark.parametrize(
    "mutation",
    ["stale", "historical-target", "missing-column", "unreached", "partial", "mutation"],
)
def test_incomplete_or_stale_proof_blocks_promotion(mutation: str) -> None:
    document = _proof_document()
    identity = _identity(document)
    if mutation == "stale":
        document["identity"]["upstream_commit"] = "a" * 40
    elif mutation == "historical-target":
        document["predicate"]["target_id"] = "6" * 64
    elif mutation == "missing-column":
        document["predicate"]["required_columns"].pop()
    elif mutation == "unreached":
        document["modes"]["spot"]["coverage"]["failing_rows"] = []
    elif mutation == "partial":
        document["modes"]["futures"]["parity"]["full_state"]["exact"] = False
    else:
        document["modes"]["spot"]["mutations"][2]["detected"] = False
    _reseal(document)

    with pytest.raises(SpecValidationError):
        validate_changed_signal_proof(document, identity)


def test_interrupted_proof_publication_leaves_no_authoritative_or_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "proof.json"
    document = deepcopy(_proof_document())

    def interrupt(_temporary: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(changed_signal_proof, "_publication_checkpoint", interrupt)

    with pytest.raises(KeyboardInterrupt):
        write_changed_signal_proof(destination, document, _identity(document))

    assert not destination.exists()
    assert not list(tmp_path.iterdir())
