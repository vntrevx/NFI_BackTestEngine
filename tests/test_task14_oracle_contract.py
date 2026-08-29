from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.fixture import validate_fixture

ROOT = Path(__file__).parents[1]
CONTRACT_PATHS = (
    ROOT / "planning" / "freqtrade-callback-order-contract-v1.json",
    ROOT
    / "python"
    / "nfi_backtest_engine"
    / "contracts"
    / "freqtrade-callback-order-contract-v1.json",
)
FIXTURE_ROOT = ROOT / "benchmarks" / "fixtures" / "captured"
FIXTURES = (
    FIXTURE_ROOT / "current-callback-oracle-spot-r1",
    FIXTURE_ROOT / "current-callback-oracle-futures-r1",
)
PIN = {
    "version": "2026.5.1",
    "image_index_digest": "sha256:d47d7053dc07eca2ace20385575143090ba88621007e5e8b76052dca6038799a",
    "image_platform_digest": (
        "sha256:bc5b7276118a8539d09ea797cb32c198d029a805815a29c6d27d5f610a3e0b6b"
    ),
}
REQUIRED_INTERACTIONS = {
    "bot_loop_start",
    "leverage",
    "custom_stake_amount",
    "confirm_trade_entry",
    "order_filled",
    "adjust_trade_position",
    "custom_stoploss",
    "custom_exit",
    "confirm_trade_exit",
}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_callback_contract_is_packaged_byte_identically_and_source_closed() -> None:
    assert CONTRACT_PATHS[0].read_bytes() == CONTRACT_PATHS[1].read_bytes()
    contract = _load(CONTRACT_PATHS[0])
    assert contract["schema_version"] == "freqtrade-callback-order-contract-v1"
    unsigned = {key: value for key, value in contract.items() if key != "fingerprint"}
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(canonical).hexdigest() == contract["fingerprint"]
    assert {key: contract["reference"][key] for key in PIN} == PIN
    assert contract["semantic_profile"] == {
        "schema_version": "freqtrade-semantic-profile-v1",
        "fingerprint": "3bc4eb5d1fd94f87b2f0fbe7e18e647804093f2094ae89136f7fec3f67d53428",
        "migration": "none-additive-contract",
    }
    methods = {(item["owner"], item["method"]): item for item in contract["source_closure"]}
    assert len(methods) == len(contract["source_closure"])
    assert all(len(item["source_sha256"]) == 64 for item in methods.values())
    assert all(
        hashlib.sha256(item["source"].encode()).hexdigest() == item["source_sha256"]
        for item in methods.values()
    )
    assert {
        "_enter_trade",
        "_check_trade_exit",
        "_exit_trade",
        "_check_adjust_trade_for_candle",
        "strategy_safe_wrapper",
    } <= {name for _owner, name in methods}
    assert set(contract["interactions"]) >= REQUIRED_INTERACTIONS


@pytest.mark.parametrize("fixture", FIXTURES, ids=("spot", "futures"))
def test_official_callback_fixture_hashes_and_matrix(fixture: Path) -> None:
    manifest = validate_fixture(fixture / "manifest.json")
    assert manifest["schema_version"] == "2.0.0"
    assert manifest["fixture_kind"] == "normal-routing"
    assert {key: manifest["freqtrade"][key] for key in PIN} == PIN
    for record in (*manifest["inputs"], *manifest["artifacts"].values()):
        path = fixture / record["path"]
        assert path.stat().st_size == record["bytes"]
        assert _sha(path) == record["sha256"]
    assert set(manifest["artifacts"]) == {
        "freqtrade_result",
        "trade_surface",
        "state_trace",
        "state_projection",
    }

    companion = _load(fixture / "inputs" / "callback-evidence.json")
    assert companion["callback_contract"]["sha256"] == _sha(CONTRACT_PATHS[0])
    for record in companion["sealed_callback_evidence"]:
        evidence = fixture / record["path"]
        assert evidence.stat().st_size == record["bytes"]
        assert _sha(evidence) == record["sha256"]
        assert any(item["path"] == record["path"] for item in manifest["inputs"])

    trace = _load(fixture / "artifacts" / "callback-trace.json")
    names = {event["callback"] for event in trace["events"]}
    expected = REQUIRED_INTERACTIONS - ({"leverage"} if trace["trading_mode"] == "spot" else set())
    assert expected <= names
    assert trace["coverage"]["return_classes"] == ["accept", "none", "reject", "value"]
    assert trace["coverage"]["exception_handling"] is True
    assert trace["coverage"]["rollback_observed"] is True
    assert trace["coverage"]["same_candle_competition_winner"] in {"custom_exit", "stop_loss"}
    assert trace["coverage"]["state_visibility_observed"] is True
    rollback = next(event for event in trace["events"] if event["predicate"] == "rollback_probe")
    following = trace["events"][trace["events"].index(rollback) + 1]
    assert rollback["after"]["stake_amount"] == 1.0
    assert following["state"]["stake_amount"] == rollback["before"]["stake_amount"]
    assert following["state"]["derisk_level_1"] is True
    competition_time = next(
        event["timestamp_ms"]
        for event in trace["events"]
        if event["callback"] == "custom_stoploss" and event["predicate"] == "competition"
    )
    competition = [
        event["callback"]
        for event in trace["events"]
        if event["timestamp_ms"] == competition_time
    ]
    assert competition[:4] == [
        "bot_loop_start",
        "adjust_trade_position",
        "custom_stoploss",
        "custom_exit",
    ]



@pytest.mark.parametrize("fixture", FIXTURES, ids=("spot", "futures"))
def test_standard_fixture_boundary_rejects_hash_symlink_and_escape_mutations(
    fixture: Path, tmp_path: Path
) -> None:
    copied = tmp_path / fixture.name
    shutil.copytree(fixture, copied)
    callback = copied / "artifacts" / "callback-trace.json"
    callback.write_bytes(callback.read_bytes() + b" ")
    with pytest.raises(SpecValidationError, match="byte size differs"):
        validate_fixture(copied / "manifest.json")

    shutil.rmtree(copied)
    shutil.copytree(fixture, copied)
    strategy = copied / "inputs" / "strategy.py"
    outside = tmp_path / "outside.py"
    outside.write_bytes(strategy.read_bytes())
    strategy.unlink()
    strategy.symlink_to(outside)
    with pytest.raises(SpecValidationError, match="symlink|containment"):
        validate_fixture(copied / "manifest.json")

    shutil.rmtree(copied)
    shutil.copytree(fixture, copied)
    manifest = _load(copied / "manifest.json")
    manifest["inputs"][0]["path"] = "../outside.py"
    (copied / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SpecValidationError, match="portable|contained|escape"):
        validate_fixture(copied / "manifest.json")


def test_spot_and_futures_entry_order_and_cadence_are_exact() -> None:
    contract = _load(CONTRACT_PATHS[0])
    spot = contract["mode_profiles"]["spot"]
    futures = contract["mode_profiles"]["futures"]
    assert spot["initial_entry"] == ["custom_stake_amount", "confirm_trade_entry"]
    assert futures["initial_entry"] == [
        "leverage",
        "custom_stake_amount",
        "confirm_trade_entry",
    ]
    assert spot["per_candle_open_trade"] == futures["per_candle_open_trade"] == [
        "adjust_trade_position",
        "custom_stoploss",
        "custom_exit",
        "confirm_trade_exit",
    ]
    assert contract["visibility"]["signal_row_offset"] == -1
    assert contract["visibility"]["bot_loop_start_frequency"] == "once-per-main-candle-before-pairs"
