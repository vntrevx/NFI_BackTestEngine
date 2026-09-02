from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from nfi_backtest_engine.fixture import validate_fixture

ROOT = Path(__file__).parents[1]
CONTRACTS = (
    ROOT / "planning/freqtrade-fill-competition-contract-v1.json",
    ROOT / "python/nfi_backtest_engine/contracts/freqtrade-fill-competition-contract-v1.json",
)
EXECUTION = ROOT / "planning/freqtrade-execution-contract.json"
MATRIX = ROOT / "benchmarks/evidence/task16/fill-competition-matrix.json"
PIN = {
    "version": "2026.5.1",
    "image_index_digest": "sha256:d47d7053dc07eca2ace20385575143090ba88621007e5e8b76052dca6038799a",
    "image_platform_digest": (
        "sha256:bc5b7276118a8539d09ea797cb32c198d029a805815a29c6d27d5f610a3e0b6b"
    ),
}


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_bytes())


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_additive_contract_is_byte_equal_source_authenticated_and_bound() -> None:
    assert CONTRACTS[0].read_bytes() == CONTRACTS[1].read_bytes()
    contract = _load(CONTRACTS[0])
    unsigned = {key: value for key, value in contract.items() if key != "fingerprint"}
    assert hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest() == contract["fingerprint"]
    assert contract["schema_version"] == "freqtrade-fill-competition-contract-v1"
    assert contract["reference"]["source_tag"] == "2026.5.1"
    assert contract["bindings"]["execution_contract"] == {
        "fingerprint": "ad3d78db362c872a99bc81dbda77024d3665a5eb230d06fef02fbf7172605ff0",
        "file_sha256": "7bead3237000586d57dd11be44cb564ee440fef32759117a7633437bdce75551",
        "bytes": 3864,
        "mutation": "forbidden",
    }
    closure = contract["source_closure"]
    canonical = json.dumps(closure, sort_keys=True, separators=(",", ":")).encode()
    assert len(closure) == contract["derivation"]["closure_count"] == 29
    assert hashlib.sha256(canonical).hexdigest() == contract["derivation"][
        "source_closure_sha256"
    ]
    pinned_nfi = contract["current_nfi_head"]
    for item in closure:
        assert hashlib.sha256(item["source"].encode()).hexdigest() == item["source_sha256"]
        source_path = ROOT / item["path"]
        if source_path.is_file():
            if (
                item["path"] == pinned_nfi["strategy_path"]
                and _sha(source_path) != pinned_nfi["strategy_sha256"]
            ):
                # .nfi is generated state. A different local checkout must not
                # invalidate the source-authenticated contract embedded above.
                continue
            assert item["source"] in source_path.read_text(encoding="utf-8")


def test_current_nfi_reachability_and_official_corrections_are_explicit() -> None:
    contract = _load(CONTRACTS[0])
    nfi = contract["current_nfi_head"]
    assert nfi["commit"] == "2bc3058ed4f8480ed7498efca49b5195c7b47e9b"
    strategy_path = ROOT / nfi["strategy_path"]
    if not strategy_path.is_file():
        pytest.skip("pinned NFI source checkout is required for reachability")
    if nfi["strategy_sha256"] != _sha(strategy_path):
        pytest.skip("local generated NFI checkout is not the contract-pinned source")
    assert nfi["declares_order_types"] is False
    assert nfi["inherited_official_default"] == {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "limit",
    }
    corrections = {item["id"]: item for item in contract["qualification"]["source_corrections"]}
    assert set(corrections) == {
        "T16-ORDER-TYPE-EXPANSION",
        "T16-FALLTHROUGH",
        "T16-MIN-STAKE-STAGE",
        "T16-FEE-SCOPE",
    }
    semantics = contract["official_semantics"]
    assert semantics["candidate_order"] == [
        "exit_signal_or_custom_exit (mutually exclusive)",
        "stop_loss_or_liquidation",
        "roi",
        "trailing_stop_loss",
    ]
    assert "continues to the next candidate" in semantics["confirmation_rejection"]
    supersession = contract["qualification"]["todo14_supersession"]
    assert supersession["status"] == "reverification-required"
    assert "tests/test_task14_oracle_contract.py" in supersession["impacted"]
    assert contract["qualification"]["verdict"] == "qualified_official_matrix"


def test_matrix_uses_only_standard_source_pinned_official_manifests() -> None:
    matrix = _load(MATRIX)
    assert {key: matrix["reference"][key] for key in PIN} == PIN
    assert matrix["reference"]["network"] == "none"
    assert matrix["contract"]["sha256"] == _sha(CONTRACTS[0])
    observed: set[str] = set()
    for lane in matrix["official_fixtures"]:
        manifest_path = ROOT / lane["manifest"]
        assert lane["manifest_sha256"] == _sha(manifest_path)
        manifest = validate_fixture(manifest_path)
        assert manifest["schema_version"] == "2.0.0"
        assert manifest["evidence_status"] == "captured"
        assert {key: manifest["freqtrade"][key] for key in PIN} == PIN
        assert set(manifest["artifacts"]) >= {
            "freqtrade_result",
            "trade_surface",
            "state_trace",
            "state_projection",
        }
        observed.update(lane["coverage"])
    assert {
        "entry-open-fill",
        "exit-open-fill",
        "amount-floor-multiple-decimals",
        "entry-confirm-rejection-id-consumption",
        "explicit-signal-reject",
        "profitable-partial-exit",
        "custom-reject-stoploss-fallthrough",
        "trailing-update-timing",
        "each-adjustment-fee",
        "partial-exit-order-id",
    } <= observed
    assert matrix["verdict"] == "official_oracle_matrix_captured_qualified"


def _task16_trace(lane: str) -> dict[str, Any]:
    root = ROOT / f"benchmarks/fixtures/captured/current-fill-competition-{lane}-spot-r1"
    manifest = _load(root / "manifest.json")
    paths = [
        root / item["path"]
        for item in manifest["inputs"]
        if item["role"] == "auxiliary"
        and _load(root / item["path"]).get("schema_version")
        == "freqtrade-fill-competition-trace-v1"
    ]
    assert len(paths) == 1
    return _load(paths[0])


def test_limit_confirmation_fallthrough_and_all_rejected_are_officially_captured() -> None:
    fallthrough = _task16_trace("limit-fallthrough")
    decisions = [
        (event["exit_reason"], event["result"])
        for event in fallthrough["callbacks"]
        if event["callback"] == "confirm_trade_exit"
    ]
    assert decisions[-2:] == [("task16-primary", False), ("stop_loss", True)]

    rejected = _task16_trace("limit-all-rejected")
    rejected_decisions = [
        (event["exit_reason"], event["result"])
        for event in rejected["callbacks"]
        if event["callback"] == "confirm_trade_exit"
    ]
    assert all(not result for reason, result in rejected_decisions if reason != "force_exit")
    assert rejected_decisions[-1] == ("roi", False)


def test_limit_and_explicit_market_fill_rates_differ_exactly() -> None:
    surfaces = {}
    for lane in ("limit-primary-accept", "market-primary-accept"):
        root = ROOT / f"benchmarks/fixtures/captured/current-fill-competition-{lane}-spot-r1"
        manifest = _load(root / "manifest.json")
        surfaces[lane] = _load(root / manifest["artifacts"]["trade_surface"]["path"])[
            "trades"
        ][0]
    limit = surfaces["limit-primary-accept"]
    market = surfaces["market-primary-accept"]
    assert (limit["open_rate"], limit["close_rate"]) == ("99.24", "101.24")
    assert (market["open_rate"], market["close_rate"]) == ("100", "100")
    assert all(order["price"] == "100" for order in market["orders"])


def test_official_surfaces_retain_exact_numeric_and_order_fields() -> None:
    for lane in _load(MATRIX)["official_fixtures"]:
        manifest_path = ROOT / lane["manifest"]
        manifest = _load(manifest_path)
        surface = _load(manifest_path.parent / manifest["artifacts"]["trade_surface"]["path"])
        for trade in surface["trades"]:
            for field in ("open_rate", "close_rate", "amount", "stake_amount"):
                assert isinstance(trade[field], str) and trade[field]
            assert set(trade["fees"]) >= {"open_rate", "close_rate", "funding"}
            assert [order["sequence"] for order in trade["orders"]] == list(
                range(len(trade["orders"]))
            )
            for order in trade["orders"]:
                assert all(isinstance(order[field], str) for field in ("amount", "price", "cost"))


def test_every_required_mutation_changes_the_authenticated_contract() -> None:
    contract = _load(CONTRACTS[0])
    expected = contract["fingerprint"]
    targets = contract["mutation_targets"]
    assert len(targets) == 10
    for index, target in enumerate(targets):
        mutated = copy.deepcopy(contract)
        mutated["mutation_targets"][index] = f"mutated-{target}"
        unsigned = {key: value for key, value in mutated.items() if key != "fingerprint"}
        actual = hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        assert actual != expected


def test_execution_contract_bytes_remain_preserved() -> None:
    assert EXECUTION.stat().st_size == 3864
    assert _sha(EXECUTION) == "6b9b11cacfb36836e12974c7436d7325660ff8f8a2a6268594c38b36c90d4afa"
    assert _load(EXECUTION)["fingerprint"] == (
        "15a22468b693039546db79f932159804975bea7fc3c1f428f7e7bd4636b69293"
    )


@pytest.mark.parametrize("lane", _load(MATRIX)["official_fixtures"])
def test_matrix_manifest_hash_is_mutation_sensitive(lane: dict[str, Any]) -> None:
    payload = (ROOT / lane["manifest"]).read_bytes()
    assert hashlib.sha256(payload + b" ").hexdigest() != lane["manifest_sha256"]
