from __future__ import annotations

import copy
import hashlib
import itertools
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.fixture import fixture_input_sha256, validate_fixture
from nfi_backtest_engine.state_trace import iter_trace_records

ROOT = Path(__file__).parents[1]
CONTRACTS = (
    ROOT / "planning/freqtrade-portfolio-pressure-contract-v1.json",
    ROOT / "python/nfi_backtest_engine/contracts/freqtrade-portfolio-pressure-contract-v1.json",
)
SCHEDULER = ROOT / "planning/freqtrade-scheduler-contract.json"
FIXTURE_ROOT = ROOT / "benchmarks/fixtures/captured"
PAIRS = ("BTC/USDT", "ETH/USDT", "AAVE/USDT")
PIN = {
    "version": "2026.5.1",
    "image_index_digest": "sha256:d47d7053dc07eca2ace20385575143090ba88621007e5e8b76052dca6038799a",
    "image_platform_digest": (
        "sha256:bc5b7276118a8539d09ea797cb32c198d029a805815a29c6d27d5f610a3e0b6b"
    ),
}
T_ENTRY_1 = 1_735_690_500_000
T_ROTATE_1 = 1_735_691_100_000
T_PARTIAL = 1_735_691_700_000
T_ROTATE_2 = 1_735_692_300_000
T_FORCE = 1_735_692_900_000


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _fixture(order: tuple[str, ...]) -> Path:
    dimension = f"{len(order)}p"
    label = "-".join(pair.split("/")[0].lower() for pair in order)
    return FIXTURE_ROOT / f"current-portfolio-pressure-{dimension}-{label}-r1"


def _auxiliary(fixture: Path, schema: str) -> Path:
    manifest = _load(fixture / "manifest.json")
    for item in manifest["inputs"]:
        path = fixture / item["path"]
        if item["role"] == "auxiliary" and _load(path).get("schema_version") == schema:
            return path
    raise AssertionError(f"missing {schema} in {fixture}")


def _portfolio(fixture: Path) -> dict[str, Any]:
    return _load(_auxiliary(fixture, "freqtrade-portfolio-pressure-trace-v1"))


def _surface(fixture: Path) -> dict[str, Any]:
    manifest = _load(fixture / "manifest.json")
    return _load(fixture / manifest["artifacts"]["trade_surface"]["path"])


def _callback(trace: dict[str, Any], callback: str) -> list[dict[str, Any]]:
    return [event for event in trace["callbacks"] if event["callback"] == callback]


def _after(trace: dict[str, Any], timestamp: int) -> list[dict[str, Any]]:
    return [
        event
        for event in trace["events"]
        if event["phase"] == "candle.after" and event["timestamp_ms"] == timestamp
    ]


def _assert_three_pair_oracle(
    trace: dict[str, Any], surface: dict[str, Any], order: tuple[str, str, str]
) -> None:
    assert trace["configured_pair_order"] == list(order)
    stakes = _callback(trace, "custom_stake_amount")
    assert [event["pair"] for event in stakes] == [order[0], order[1], order[2], order[0]]
    assert [event["decision"] for event in stakes] == ["accept", "reject", "accept", "accept"]
    assert [event["call_at_timestamp"] for event in stakes] == [1, 1, 2, 1]
    assert [event["timestamp_ms"] for event in stakes] == [
        T_ENTRY_1,
        T_ROTATE_1,
        T_ROTATE_1,
        T_ROTATE_2,
    ]
    proposed = [event["proposed_stake"] for event in stakes]
    assert proposed[:3] == [900.0, 981.0, 981.0]
    assert 900.0 < proposed[3] < proposed[2]

    adjustment = _callback(trace, "adjust_trade_position")
    assert len(adjustment) == 1
    assert adjustment[0]["pair"] == order[2]
    assert adjustment[0]["trade_id"] == 2
    assert adjustment[0]["timestamp_ms"] == T_PARTIAL
    assert adjustment[0]["decision"] == "partial_exit"

    before_partial = _after(trace, T_PARTIAL - 300_000)[0]["state"]
    after_partial = _after(trace, T_PARTIAL)[0]["state"]
    assert before_partial["open_slots"] == after_partial["open_slots"] == 0
    assert float(after_partial["quote_wallet"][1]) > float(before_partial["quote_wallet"][1])
    assert before_partial["counters"]["order_id"] == 3
    assert after_partial["counters"]["order_id"] == 4
    assert after_partial["counters"]["trade_id"] == 2

    rotation = _after(trace, T_ROTATE_2)
    assert [event["pair"] for event in rotation] == [order[2], order[0], order[1]]
    assert rotation[0]["state"]["open_slots"] == 1
    assert rotation[0]["state"]["counters"]["order_id"] == 5
    assert rotation[1]["state"]["open_slots"] == 0
    assert rotation[1]["state"]["counters"]["trade_id"] == 3
    assert rotation[1]["state"]["counters"]["order_id"] == 6

    force = [
        event
        for event in trace["events"]
        if event["timestamp_ms"] == T_FORCE and event["phase"] == "trade.exit_order"
    ]
    assert len(force) == 1
    assert force[0]["pair"] == order[0]
    assert force[0]["state"]["counters"]["trade_id"] == 3
    assert force[0]["state"]["counters"]["order_id"] == 7
    assert [trade["pair"] for trade in surface["trades"]] == [order[0], order[2], order[0]]
    assert [trade["exit_reason"] for trade in surface["trades"]] == [
        "pressure-rotate",
        "pressure-rotate",
        "force_exit",
    ]


def test_portfolio_contract_is_byte_equal_authenticated_and_additive() -> None:
    assert CONTRACTS[0].read_bytes() == CONTRACTS[1].read_bytes()
    contract = _load(CONTRACTS[0])
    unsigned = {key: value for key, value in contract.items() if key != "fingerprint"}
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(canonical).hexdigest() == contract["fingerprint"]
    assert contract["schema_version"] == "freqtrade-portfolio-pressure-contract-v1"
    assert {key: contract["reference"][key] for key in PIN} == PIN
    assert contract["semantic_profile"]["fingerprint"] == (
        "3bc4eb5d1fd94f87b2f0fbe7e18e647804093f2094ae89136f7fec3f67d53428"
    )
    assert contract["scheduler_contract"]["fingerprint"] == (
        "90f813bc66c479415f5eb1cd30868d39abec1f3b74b644be367114af4eff422a"
    )
    assert contract["scheduler_contract"]["mutation"] == "forbidden"
    assert _sha(SCHEDULER) == "0a03b4cf158b8b355f547f1d331d3bebac90c511cab73d5f4b03b2db7704d7ba"

    closure = contract["source_closure"]
    assert len(closure) == contract["derivation"]["closure_count"] == 16
    closure_bytes = json.dumps(closure, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(closure_bytes).hexdigest() == contract["derivation"][
        "source_closure_sha256"
    ]
    assert all(
        hashlib.sha256(item["source"].encode()).hexdigest() == item["source_sha256"]
        for item in closure
    )
    methods = {(item["owner"], item["method"]) for item in closure}
    assert {
        ("freqtrade.optimize.backtesting.Backtesting", "backtest"),
        ("freqtrade.optimize.backtesting.Backtesting", "backtest_loop"),
        ("freqtrade.optimize.backtesting.Backtesting", "_enter_trade"),
        ("freqtrade.optimize.backtesting.Backtesting", "_process_exit_order"),
        ("freqtrade.wallets.Wallets", "get_available_stake_amount"),
        ("freqtrade.wallets.Wallets", "validate_stake_amount"),
        ("freqtrade.persistence.trade_model.LocalTrade", "add_bt_trade"),
        ("freqtrade.persistence.trade_model.LocalTrade", "close_bt_trade"),
    } <= methods


@pytest.mark.parametrize(
    "order",
    [*itertools.permutations(PAIRS[:2]), *itertools.permutations(PAIRS)],
    ids=lambda order: "-".join(pair.split("/")[0].lower() for pair in order),
)
def test_every_official_lane_is_standard_sealed_and_identity_bound(order: tuple[str, ...]) -> None:
    fixture = _fixture(order)
    manifest = validate_fixture(fixture / "manifest.json")
    assert manifest["schema_version"] == "2.0.0"
    assert manifest["fixture_kind"] == "normal-routing"
    assert manifest["evidence_status"] == "captured"
    assert {key: manifest["freqtrade"][key] for key in PIN} == PIN
    assert manifest["freqtrade"]["command"][
        manifest["freqtrade"]["command"].index("--pairs") + 1 :
        manifest["freqtrade"]["command"].index("--fee")
    ] == list(order)
    assert set(manifest["artifacts"]) == {
        "freqtrade_result",
        "trade_surface",
        "state_trace",
        "state_projection",
    }
    assert {item["role"] for item in manifest["inputs"]} >= {
        "strategy",
        "config",
        "candles",
        "market_metadata",
        "auxiliary",
    }
    authentication = _load(_auxiliary(fixture, "official-source-authentication-v1"))
    assert authentication["configured_pair_order"] == list(order)
    assert authentication["network"] == "none"
    assert authentication["capture_first"] is True
    assert authentication["portfolio_contract"]["sha256"] == _sha(CONTRACTS[0])
    trace = _portfolio(fixture)
    assert trace["configured_pair_order"] == list(order)
    assert [event["sequence"] for event in trace["callbacks"]] == list(
        range(1, len(trace["callbacks"]) + 1)
    )
    assert [event["sequence"] for event in trace["events"]] == sorted(
        event["sequence"] for event in trace["events"]
    )


def test_standard_boundary_rejects_schema_hash_symlink_and_containment_mutations(
    tmp_path: Path,
) -> None:
    source = _fixture(PAIRS)
    copied = tmp_path / "fixture"

    shutil.copytree(source, copied)
    manifest_path = copied / "manifest.json"
    manifest = _load(manifest_path)
    manifest["schema_version"] = "task15-custom"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SpecValidationError, match="unsupported fixture version"):
        validate_fixture(manifest_path)

    shutil.rmtree(copied)
    shutil.copytree(source, copied)
    portfolio = _auxiliary(copied, "freqtrade-portfolio-pressure-trace-v1")
    portfolio.write_bytes(portfolio.read_bytes() + b" ")
    with pytest.raises(SpecValidationError, match="byte size differs"):
        validate_fixture(copied / "manifest.json")

    shutil.rmtree(copied)
    shutil.copytree(source, copied)
    strategy = copied / "inputs/strategy.py"
    outside = tmp_path / "outside.py"
    outside.write_bytes(strategy.read_bytes())
    strategy.unlink()
    strategy.symlink_to(outside)
    with pytest.raises(SpecValidationError, match="symlink|containment"):
        validate_fixture(copied / "manifest.json")

    shutil.rmtree(copied)
    shutil.copytree(source, copied)
    manifest_path = copied / "manifest.json"
    manifest = _load(manifest_path)
    manifest["inputs"][0]["path"] = "../outside.py"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SpecValidationError, match="portable|contained|escape"):
        validate_fixture(manifest_path)


def test_retained_raw_traces_are_full_state_and_identity_bound() -> None:
    for order in itertools.permutations(PAIRS):
        fixture = _fixture(order)
        manifest = _load(fixture / "manifest.json")
        trace_path = fixture / manifest["artifacts"]["state_trace"]["path"]
        records = iter_trace_records(trace_path)
        header = next(records)
        assert header["include_state"] is True
        first_event = next(records)
        assert first_event["kind"] == "event"
        assert isinstance(first_event["state"], dict)
        assert header["input_sha256"] == fixture_input_sha256(manifest["inputs"])


def test_two_pair_projection_uses_configured_winner_and_force_exit_ids() -> None:
    surfaces = []
    for order in itertools.permutations(PAIRS[:2]):
        trace = _portfolio(_fixture(order))
        surface = _surface(_fixture(order))
        stakes = _callback(trace, "custom_stake_amount")
        assert [event["pair"] for event in stakes] == [
            order[0],
            order[1],
            order[0],
            order[1],
        ]
        assert [event["decision"] for event in stakes] == [
            "accept",
            "reject",
            "accept",
            "accept",
        ]
        force = [event for event in trace["events"] if event["phase"] == "trade.exit_order"][-1]
        assert force["timestamp_ms"] == T_FORCE
        assert force["pair"] == order[1]
        assert force["state"]["counters"]["trade_id"] == 3
        assert force["state"]["counters"]["order_id"] == 6
        assert [trade["pair"] for trade in surface["trades"]] == [
            order[0],
            order[0],
            order[1],
        ]
        assert surface["trades"][-1]["exit_reason"] == "force_exit"
        surfaces.append(surface)
    assert surfaces[0] != surfaces[1]


@pytest.mark.parametrize("order", list(itertools.permutations(PAIRS)))
def test_three_pair_permutations_cover_complete_pressure_timeline(
    order: tuple[str, str, str],
) -> None:
    fixture = _fixture(order)
    _assert_three_pair_oracle(_portfolio(fixture), _surface(fixture), order)


def test_oracle_trace_is_sensitive_to_required_scheduler_wallet_slot_and_id_mutations() -> None:
    order = PAIRS
    fixture = _fixture(order)
    expected = _portfolio(fixture)
    surface = _surface(fixture)

    swapped = _portfolio(_fixture((PAIRS[0], PAIRS[2], PAIRS[1])))
    with pytest.raises(AssertionError):
        _assert_three_pair_oracle(swapped, surface, order)

    mutations: list[dict[str, Any]] = []
    entry_before_exit = copy.deepcopy(expected)
    rotation = _after(entry_before_exit, T_ROTATE_2)
    first_index = entry_before_exit["events"].index(rotation[0])
    second_index = entry_before_exit["events"].index(rotation[1])
    entry_before_exit["events"][first_index], entry_before_exit["events"][second_index] = (
        entry_before_exit["events"][second_index],
        entry_before_exit["events"][first_index],
    )
    mutations.append(entry_before_exit)

    stale_wallet = copy.deepcopy(expected)
    _callback(stale_wallet, "custom_stake_amount")[2]["proposed_stake"] = 900.0
    mutations.append(stale_wallet)

    released_slot = copy.deepcopy(expected)
    _after(released_slot, T_PARTIAL)[0]["state"]["open_slots"] = 1
    mutations.append(released_slot)

    stopped_after_rejection = copy.deepcopy(expected)
    accepted_after_reject = _callback(stopped_after_rejection, "custom_stake_amount")[2]
    stopped_after_rejection["callbacks"].remove(accepted_after_reject)
    mutations.append(stopped_after_rejection)

    fixed_base = copy.deepcopy(expected)
    _callback(fixed_base, "custom_stake_amount")[-1]["proposed_stake"] = 900.0
    mutations.append(fixed_base)

    changed_ids = copy.deepcopy(expected)
    force = [event for event in changed_ids["events"] if event["phase"] == "trade.exit_order"][-1]
    force["state"]["counters"]["order_id"] = 6
    mutations.append(changed_ids)

    for mutation in mutations:
        with pytest.raises(AssertionError):
            _assert_three_pair_oracle(mutation, surface, order)
