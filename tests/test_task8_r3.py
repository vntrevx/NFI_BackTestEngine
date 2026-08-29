from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pandas as pd  # noqa: PANDAS_OK
import pytest
from nfi_backtest_engine import _rust
from nfi_backtest_engine.changed_signal_proof import (
    ChangedSignalIdentity,
    validate_changed_signal_proof,
)
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.tag_program import compile_tag_program

ROOT = Path(__file__).resolve().parents[1]
PROOF = ROOT / "benchmarks/evidence/m22/current-x7-changed-signal-boundary.json"
CONTRACT = ROOT / "benchmarks/reference/strategies/CurrentChangedPredicateContract.py"


def _document() -> dict[str, Any]:
    return json.loads(PROOF.read_text(encoding="utf-8"))


def _canonical_sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _reseal(document: dict[str, Any]) -> None:
    for mode in document["modes"].values():
        for lane_name in ("official", "native"):
            provenance = mode[f"{lane_name}_provenance"]
            provenance["raw_output_sha256"] = _canonical_sha(
                [artifact["sha256"] for artifact in provenance["artifacts"]]
            )
            provenance["normalized_sha256"] = _canonical_sha(mode[lane_name])
    unsigned = {key: value for key, value in document.items() if key != "fingerprint"}
    document["fingerprint"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _validate(document: dict[str, Any]) -> None:
    validate_changed_signal_proof(document, ChangedSignalIdentity(**document["identity"]))


def test_changed_signal_execution_is_mode_appropriate() -> None:
    # Given / When
    document = _document()
    spot = document["modes"]["spot"]
    futures = document["modes"]["futures"]

    # Then: Spot reaches the short signal but cannot execute shorts by Freqtrade contract.
    assert spot["execution_contract"] == {
        "can_short": False,
        "short_execution_supported": False,
        "changed_signal_reached": True,
    }
    assert spot["official"]["trades"] == spot["native"]["trades"] == []
    assert spot["official"]["signal_tag"]["enter_short"] == [0, 1, 1, 1, 1]
    assert spot["official"]["signal_tag"]["enter_tag"][1:] == ["562 "] * 4

    # And: Futures executes the changed signal through both independent lanes.
    assert futures["execution_contract"]["short_execution_supported"] is True
    for lane_name in ("official", "native"):
        trades = futures[lane_name]["trades"]
        assert trades
        assert all(trade["direction"] == "short" for trade in trades)
        assert all(trade["entry_tag"] == "562 " for trade in trades)
        assert all(len(trade["orders"]) >= 2 for trade in trades)
        assert futures[lane_name]["full_state"]


@pytest.mark.parametrize("attack", ["invalid-zip", "fabricated-state", "copied-raw-lane"])
def test_resealed_raw_artifact_attacks_fail_semantic_derivation(attack: str) -> None:
    # Given
    document = _document()
    mode = document["modes"]["futures"]
    if attack == "invalid-zip":
        payload = ROOT / "tests/test_task8_r3.py"
        artifact = next(
            item
            for item in mode["official_provenance"]["artifacts"]
            if item["role"] == "official_execution"
        )
        artifact["path"] = "tests/test_task8_r3.py"
        artifact["sha256"] = hashlib.sha256(payload.read_bytes()).hexdigest()
    elif attack == "fabricated-state":
        invented = deepcopy(mode["official"]["full_state"][:2])
        invented[0]["execution"]["trade_id_counter"] = 999
        mode["official"]["full_state"] = invented
        mode["native"]["full_state"] = deepcopy(invented)
    else:
        official = {
            artifact["role"]: artifact
            for artifact in mode["official_provenance"]["artifacts"]
        }
        for artifact in mode["native_provenance"]["artifacts"]:
            role = artifact["role"]
            if role == "native_execution":
                artifact["path"] = official["official_execution"]["path"]
                artifact["sha256"] = official["official_execution"]["sha256"]
            elif role == "native_state":
                artifact["path"] = official["official_state"]["path"]
                artifact["sha256"] = official["official_state"]["sha256"]
    _reseal(document)

    # When / Then
    with pytest.raises(SpecValidationError):
        _validate(document)


def test_resealed_commands_market_and_assertion_only_mutants_fail() -> None:
    # Given
    attacks = []
    command = _document()
    command["modes"]["spot"]["official_provenance"]["command"] = ["evil", "freqtrade"]
    attacks.append(command)
    market = _document()
    market["modes"]["spot"]["official"]["callback_columns"]["RSI_3_15m"][1] = 99.0
    market["modes"]["spot"]["native"]["callback_columns"]["RSI_3_15m"][1] = 99.0
    market["identity"]["market_data_sha256"] = _canonical_sha(
        market["modes"]["spot"]["official"]["callback_columns"]
    )
    attacks.append(market)
    mutant = _document()
    record = mutant["modes"]["futures"]["mutations"][0]
    record["source"] = {
        "role": "mutant_source",
        "path": CONTRACT.relative_to(ROOT).as_posix(),
        "sha256": hashlib.sha256(CONTRACT.read_bytes()).hexdigest(),
    }
    attacks.append(mutant)
    for document in attacks:
        _reseal(document)

    # When / Then
    for document in attacks:
        with pytest.raises(SpecValidationError):
            _validate(document)


def test_missing_tracked_replay_input_fails_typed() -> None:
    # Given
    document = _document()
    artifact = next(
        item
        for item in document["modes"]["spot"]["official_provenance"]["artifacts"]
        if item["role"] == "replay_manifest"
    )
    artifact["path"] = "benchmarks/evidence/m22/current-x7-raw/spot/replay/missing.json"
    _reseal(document)

    # When / Then
    with pytest.raises(SpecValidationError, match="artifact path|replay root"):
        _validate(document)


def test_replay_inventory_contains_every_clean_room_input() -> None:
    # Given / When
    document = _document()

    # Then
    required = {
        "strategy_input",
        "config_input",
        "candle_input",
        "market_input",
        "replay_manifest",
        "source_input",
        "capture_input",
    }
    for mode_name, mode in document["modes"].items():
        official_roles = {
            artifact["role"] for artifact in mode["official_provenance"]["artifacts"]
        }
        assert official_roles >= required
        if mode_name == "futures":
            assert official_roles >= {"funding_input", "mark_input", "leverage_input"}


def test_compiled_nullable_string_matches_pandas_for_supported_values(tmp_path: Path) -> None:
    # Given
    source = tmp_path / "StringContract.py"
    source.write_text(
        "import pandas as pd\n"
        "from freqtrade.strategy import IStrategy\n"
        "class StringContract(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_entry_trend(self, dataframe, metadata):\n"
        "        dataframe.loc[:, 'enter_tag'] = pd.array(dataframe['value'], dtype='string')\n"
        "        return dataframe\n"
        "    def populate_exit_trend(self, dataframe, metadata):\n"
        "        return dataframe\n",
        encoding="utf-8",
    )
    program = compile_tag_program(source, class_name="StringContract")
    cases = [
        ["alpha", "beta"],
        [1, 2],
        [1.0, 2.5],
        [True, False],
        [None, pd.NA],
        ["x", 1, 2.5, True, None, pd.NA],
    ]

    # When / Then
    for values in cases:
        actual = _rust.execute_numeric_mutation_program(
            json.dumps(program, separators=(",", ":")),
            {"value": values},
            {},
            ["enter_tag"],
        )["enter_tag"]["values"]
        expected = pd.array(values, dtype="string").tolist()
        assert actual == [None if value is pd.NA else value for value in expected]
    with pytest.raises(ValueError, match="unsupported"):
        _rust.execute_numeric_mutation_program(
            json.dumps(program, separators=(",", ":")),
            {"value": [{"not": "scalar"}]},
            {},
            ["enter_tag"],
        )
