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
from nfi_backtest_engine.signal_program.runtime import _cast
from nfi_backtest_engine.tag_program import compile_tag_program, execute_tag_program

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "benchmarks/evidence/m22/current-x7-changed-signal-boundary.json"


def _document() -> dict[str, Any]:
    return json.loads(EVIDENCE.read_text(encoding="utf-8"))


def _identity(document: dict[str, Any]) -> ChangedSignalIdentity:
    return ChangedSignalIdentity(**document["identity"])


def _reseal(document: dict[str, Any]) -> None:
    unsigned = {key: value for key, value in document.items() if key != "fingerprint"}
    document["fingerprint"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("capture", "kind"), "invented"),
        (("capture", "freqtrade_version"), "invented"),
        (("capture", "interface_sha256"), "0" * 64),
        (("capture", "contract_sha256"), "0" * 64),
        (("identity", "market_data_sha256"), "0" * 64),
    ],
)
def test_validator_rejects_resealed_invented_provenance(
    path: tuple[str, str], value: str
) -> None:
    # Given
    document = _document()
    document[path[0]][path[1]] = value
    _reseal(document)

    # When / Then
    with pytest.raises(SpecValidationError):
        validate_changed_signal_proof(document, _identity(document))


@pytest.mark.parametrize(
    "mutation",
    ["trade-shape", "state-shape", "copied-lane", "coverage", "difference"],
)
def test_validator_rejects_resealed_fabricated_execution_claims(mutation: str) -> None:
    # Given
    document = _document()
    mode = document["modes"]["spot"]
    if mutation == "trade-shape":
        mode["official"]["trades"] = [{"not": "a trade"}]
        mode["native"]["trades"] = deepcopy(mode["official"]["trades"])
    elif mutation == "state-shape":
        mode["official"]["full_state"] = [{"not": "state"}]
        mode["native"]["full_state"] = deepcopy(mode["official"]["full_state"])
    elif mutation == "copied-lane":
        mode["native"] = deepcopy(mode["official"])
        mode["native_provenance"] = deepcopy(mode["official_provenance"])
    elif mutation == "coverage":
        mode["coverage"] = {
            "passing_rows": [0],
            "failing_rows": [1],
            "independent_term_rows": [0, 0, 0, 0],
        }
    else:
        mode["mutations"][0]["first_difference"] = {"path": "$.lie", "reason": "lie"}
    _reseal(document)

    # When / Then
    with pytest.raises(SpecValidationError):
        validate_changed_signal_proof(document, _identity(document))


@pytest.mark.parametrize("mutation", ["operator", "threshold", "expression"])
def test_validator_recomputes_current_predicate_from_authoritative_source(mutation: str) -> None:
    # Given
    document = _document()
    predicate = document["predicate"]
    if mutation == "operator":
        predicate["atomic_terms"][0] = "rsi_3_15m < 15.0"
    elif mutation == "threshold":
        predicate["atomic_terms"][1] = "rsi_3_1h > 999.0"
    else:
        predicate["source_expression"] = "unrelated"
        predicate["source_expression_sha256"] = hashlib.sha256(b"unrelated").hexdigest()
    _reseal(document)

    # When / Then
    with pytest.raises(SpecValidationError):
        validate_changed_signal_proof(document, _identity(document))


def test_tracked_proof_binds_real_official_and_native_execution_boundaries() -> None:
    # Given / When
    document = _document()

    # Then
    assert document["capture"]["kind"] == "sealed-freqtrade-backtest"
    assert document["capture"]["image_ref"].endswith(
        "@sha256:bc5b7276118a8539d09ea797cb32c198d029a805815a29c6d27d5f610a3e0b6b"
    )
    for mode in ("spot", "futures"):
        proof = document["modes"][mode]
        assert proof["official_provenance"]["producer"] == "freqtrade-backtesting"
        assert proof["native_provenance"]["producer"] == "nfi-native-engine"
        assert proof["official_provenance"]["raw_output_sha256"] != (
            proof["native_provenance"]["raw_output_sha256"]
        )
        trades = proof["official"]["trades"]
        if mode == "spot":
            assert trades == []
            assert proof["execution_contract"]["short_execution_supported"] is False
        else:
            trade = trades[0]
            assert trade["direction"] == "short"
            assert trade["entry_tag"] == "562 "
            assert {
                "pair",
                "open_timestamp_ms",
                "close_timestamp_ms",
                "open_rate",
                "close_rate",
                "amount",
                "stake_amount",
                "fees",
                "orders",
                "is_open",
            } <= trade.keys()
        state = proof["official"]["full_state"][0]
        assert {"timestamp_ms", "wallet", "orders", "callbacks", "open_trades"} <= state.keys()


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["alpha", "beta"], ["alpha", "beta"]),
        ([1, 2.5], ["1", "2.5"]),
        ([None, pd.NA, "x"], [pd.NA, pd.NA, "x"]),
    ],
)
def test_pd_array_string_cast_matches_nullable_string_semantics(
    tmp_path: Path, values: list[Any], expected: list[Any]
) -> None:
    # Given
    source = tmp_path / "StringArrayContract.py"
    source.write_text(
        "import pandas as pd\n"
        "from freqtrade.strategy import IStrategy\n"
        "class StringArrayContract(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_entry_trend(self, dataframe, metadata):\n"
        "        dataframe.loc[:, 'enter_tag'] = pd.array(dataframe['value'], dtype='string')\n"
        "        return dataframe\n"
        "    def populate_exit_trend(self, dataframe, metadata):\n"
        "        dataframe.loc[:, 'exit_tag'] = ''\n"
        "        return dataframe\n",
        encoding="utf-8",
    )
    frame = pd.DataFrame({"value": pd.array(values, dtype="object")})
    program = compile_tag_program(source, class_name="StringArrayContract")

    # When
    python_output = _cast(frame["value"], "string-array")
    oracle = pd.array(values, dtype="string")

    # Then
    cast_node = next(node for node in program["nodes"] if node["op"] == "cast")
    assert cast_node["value_type"] == "string-column"
    assert cast_node["parameters"] == {"target": "string-array"}
    assert python_output.dtype == oracle.dtype
    assert python_output.tolist() == oracle.tolist()
    assert [None if pd.isna(value) else value for value in python_output] == [
        None if pd.isna(value) else value for value in expected
    ]


@pytest.mark.parametrize(
    "call",
    [
        "pd.array(dataframe['value'], dtype='string')",
        "pd.array(dataframe['value'], 'string')",
    ],
)
def test_pd_array_accepts_exact_keyword_and_positional_string_dtype(
    tmp_path: Path, call: str
) -> None:
    # Given
    source = tmp_path / "AcceptedStringArrayContract.py"
    source.write_text(
        "import pandas as pd\n"
        "from freqtrade.strategy import IStrategy\n"
        "class AcceptedStringArrayContract(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_entry_trend(self, dataframe, metadata):\n"
        f"        dataframe.loc[:, 'enter_tag'] = {call}\n"
        "        return dataframe\n"
        "    def populate_exit_trend(self, dataframe, metadata):\n"
        "        dataframe.loc[:, 'exit_tag'] = ''\n"
        "        return dataframe\n",
        encoding="utf-8",
    )

    # When
    program = compile_tag_program(source, class_name="AcceptedStringArrayContract")
    output = execute_tag_program(program, pd.DataFrame({"value": [1, 2.5]}))
    rust_output = _rust.execute_numeric_mutation_program(
        json.dumps(program, separators=(",", ":")),
        {"value": [1, 2.5]},
        {},
        ["enter_tag"],
    )["enter_tag"]["values"]

    # Then
    assert output["enter_tag"].tolist() == ["1.0", "2.5"]
    assert rust_output == ["1.0", "2.5"]


@pytest.mark.parametrize(
    "call",
    [
        "pd.array(dataframe['value'], dtype='Int64')",
        "pd.array(dataframe['value'], dtype=metadata['dtype'])",
    ],
)
def test_pd_array_rejects_unsupported_or_nonliteral_dtype(tmp_path: Path, call: str) -> None:
    # Given
    source = tmp_path / "RejectedStringArrayContract.py"
    source.write_text(
        "import pandas as pd\n"
        "from freqtrade.strategy import IStrategy\n"
        "class RejectedStringArrayContract(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_entry_trend(self, dataframe, metadata):\n"
        f"        dataframe.loc[:, 'enter_tag'] = {call}\n"
        "        return dataframe\n"
        "    def populate_exit_trend(self, dataframe, metadata):\n"
        "        dataframe.loc[:, 'exit_tag'] = ''\n"
        "        return dataframe\n",
        encoding="utf-8",
    )

    # When / Then
    with pytest.raises(Exception, match="pandas array dtype|dynamic indicator parameter"):
        compile_tag_program(source, class_name="RejectedStringArrayContract")
