from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from nfi_backtest_engine import cli, semantic_inventory
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.semantic_inventory import build_semantic_obligation_registry
from nfi_backtest_engine.semantic_registry import (
    _CfgStatement,
    _registry_fingerprint,
    _statement_edges,
    _statement_key,
    semantic_obligation_preimage,
    validate_semantic_obligation_registry,
)
from nfi_backtest_engine.semantic_registry_validation import _validate_record

SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "python/nfi_backtest_engine/schemas/semantic-obligation-registry-v1.schema.json"
)


def test_linear_record_validator_is_equivalent_to_trusted_record_schema() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    validator = Draft202012Validator(
        {
            "$schema": schema["$schema"],
            "$ref": "#/$defs/obligation_record",
            "$defs": schema["$defs"],
        }
    )
    valid = {
        "obligation_id": f"obl-ast-node-{'a' * 64}",
        "preimage": {
            "source": ["b" * 64, "@strategy", [1, 0, 1, 0]],
            "normalized_semantics": ["subject", "c" * 64],
        },
    }
    variants = [
        valid,
        {
            **valid,
            "preimage": {**valid["preimage"], "source": ["b" * 64, "@semantic-contract", None]},
        },
        {
            **valid,
            "preimage": {
                **valid["preimage"],
                "source": ["b" * 64, "@source/helper.py", [2, 3, 4, 5]],
            },
        },
    ]
    integral_number_variants = [
        {
            **valid,
            "preimage": {
                **valid["preimage"],
                "source": [
                    "b" * 64,
                    "@strategy",
                    [
                        value if index == slot else original
                        for index, original in enumerate([1, 0, 1, 0])
                    ],
                ],
            },
        }
        for slot in range(4)
        for value in (1.0, -0.0, 2.0, 1e308)
        if slot not in {0, 2} or value != -0.0
    ]
    invalid_number_variants = [
        {
            **valid,
            "preimage": {
                **valid["preimage"],
                "source": [
                    "b" * 64,
                    "@strategy",
                    [
                        value if index == slot else original
                        for index, original in enumerate([1, 0, 1, 0])
                    ],
                ],
            },
        }
        for slot in range(4)
        for value in (True, 1.5, float("inf"), float("-inf"), float("nan"))
    ]
    mutations = [
        {},
        {**valid, "extra": True},
        {**valid, "obligation_id": f"obl-AST-{'a' * 64}"},
        {**valid, "obligation_id": f"obl-ast-node-{'a' * 63}"},
        {**valid, "obligation_id": f"obl-ast-node-{'a' * 65}"},
        {**valid, "obligation_id": f"obl-ast-node-{'a' * 64}\n"},
        {**valid, "preimage": []},
        {**valid, "preimage": {**valid["preimage"], "extra": True}},
        {**valid, "preimage": {**valid["preimage"], "source": ["b" * 64, "@strategy"]}},
        {**valid, "preimage": {**valid["preimage"], "source": ["b" * 63, "@strategy", None]}},
        {**valid, "preimage": {**valid["preimage"], "source": ["b" * 65, "@strategy", None]}},
        {
            **valid,
            "preimage": {**valid["preimage"], "source": [f"{'b' * 64}\n", "@strategy", None]},
        },
        {**valid, "preimage": {**valid["preimage"], "source": ["b" * 64, "strategy", None]}},
        {**valid, "preimage": {**valid["preimage"], "source": ["b" * 64, "@strategy\n", None]}},
        {
            **valid,
            "preimage": {**valid["preimage"], "source": ["b" * 64, "@strategy", [0, 0, 1, 0]]},
        },
        {
            **valid,
            "preimage": {**valid["preimage"], "source": ["b" * 64, "@strategy", [1, -1, 1, 0]]},
        },
        {
            **valid,
            "preimage": {**valid["preimage"], "source": ["b" * 64, "@strategy", [1, 0, 0, 0]]},
        },
        {
            **valid,
            "preimage": {**valid["preimage"], "source": ["b" * 64, "@strategy", [1, 0, 1, -1]]},
        },
        {
            **valid,
            "preimage": {**valid["preimage"], "source": ["b" * 64, "@strategy", [True, 0, 1, 0]]},
        },
        {**valid, "preimage": {**valid["preimage"], "normalized_semantics": ["subject"]}},
        {**valid, "preimage": {**valid["preimage"], "normalized_semantics": ["", "c" * 64]}},
        {**valid, "preimage": {**valid["preimage"], "normalized_semantics": ["subject", "c" * 63]}},
        {**valid, "preimage": {**valid["preimage"], "normalized_semantics": ["subject", "c" * 65]}},
        {
            **valid,
            "preimage": {**valid["preimage"], "normalized_semantics": ["subject", f"{'c' * 64}\n"]},
        },
    ]

    for record in [
        *variants,
        *integral_number_variants,
        *invalid_number_variants,
        *mutations,
    ]:
        schema_valid = validator.is_valid(record)
        try:
            _validate_record(record)
        except SpecValidationError:
            linear_valid = False
        else:
            linear_valid = True
        assert linear_valid is schema_valid, record


def _write_strategy(path: Path, expression: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class AdversarialStrategy(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_indicators(self, dataframe, metadata):\n"
        f"        dataframe['feature'] = {expression}\n"
        "        return dataframe\n"
        "    def populate_entry_trend(self, dataframe, metadata):\n"
        "        return dataframe\n"
        "    def populate_exit_trend(self, dataframe, metadata):\n"
        "        return dataframe\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("expression", "expected_code"),
    [
        ("dataframe['close'] @ 2", "UNKNOWN_REACHABLE_OPERATOR"),
        ("(lambda value: value)(dataframe)", "UNKNOWN_REACHABLE_CALL"),
        ("future_callable(dataframe)", "UNKNOWN_REACHABLE_CALL"),
        ("dataframe.future_transform()", "UNKNOWN_REACHABLE_CALL"),
    ],
)
def test_reachable_unsupported_operator_or_call_blocks_native_promotion(
    tmp_path: Path,
    expression: str,
    expected_code: str,
) -> None:
    source = tmp_path / "AdversarialStrategy.py"
    _write_strategy(source, expression)

    registry = build_semantic_obligation_registry(
        source,
        class_name="AdversarialStrategy",
    )

    assert expected_code in {item["code"] for item in registry["blockers"]}
    assert registry["summary"]["unknown_obligations"] > 0
    assert registry["summary"]["native_promotion"] is False
    blocker_ids = {item["obligation_id"] for item in registry["blockers"]}
    official_ids = {
        record["obligation_id"]
        for group in registry["obligation_groups"]
        if group["mapping"] == "official-only-blocker"
        for record in group["obligations"]
    }
    assert blocker_ids <= official_ids


def test_reviewed_numpy_and_pandas_array_calls_are_generic_compiled_semantics(
    tmp_path: Path,
) -> None:
    source = tmp_path / "ArrayStrategy.py"
    source.write_text(
        "import numpy as np\n"
        "import pandas as pd\n"
        "from freqtrade.strategy import IStrategy\n"
        "class ArrayStrategy(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_indicators(self, dataframe, metadata):\n"
        "        values = dataframe['close'].to_numpy(copy=False)\n"
        "        indexes = np.arange(values.size)\n"
        "        dataframe['running'] = np.maximum.accumulate(indexes)\n"
        "        return dataframe\n"
        "    def populate_entry_trend(self, dataframe, metadata):\n"
        "        dataframe.loc[:, 'enter_tag'] = pd.array([], dtype='string')\n"
        "        return dataframe\n"
        "    def populate_exit_trend(self, dataframe, metadata):\n"
        "        return dataframe\n",
        encoding="utf-8",
    )

    registry = build_semantic_obligation_registry(
        source,
        class_name="ArrayStrategy",
    )

    assert {item["code"] for item in registry["blockers"]} == {"UNOBSERVED_UPSTREAM_REF"}


def _source_line(node: _CfgStatement) -> int:
    statement = getattr(node, "statement", node)
    return statement.lineno


def _edge_projection(function_source: str) -> tuple[set[tuple[int, int, str]], int]:
    function = ast.parse(function_source).body[0]
    assert isinstance(function, ast.FunctionDef)
    edges = _statement_edges(function.body)
    projected = {
        (_source_line(source), _source_line(target), label) for source, target, label in edges
    }
    sequence_count = sum(
        1
        for _, first_target, _ in edges
        for second_source, _, _ in edges
        if first_target == second_source
    )
    return projected, sequence_count


def _raw_cfg(
    function_source: str,
) -> tuple[
    set[tuple[_CfgStatement, _CfgStatement, str]],
    set[tuple[_CfgStatement, _CfgStatement, str, _CfgStatement, str]],
]:
    function = ast.parse(function_source).body[0]
    assert isinstance(function, ast.FunctionDef)
    edges = _statement_edges(function.body)
    sequences = {
        (source, middle, first_label, target, second_label)
        for source, middle, first_label in edges
        for second_source, target, second_label in edges
        if middle == second_source
    }
    return edges, sequences


def test_cfg_has_branch_joins_loop_backedges_and_terminal_control_flow() -> None:
    source = """def flow(flag, items):
    start = 0
    if flag:
        left = 1
    else:
        right = 2
    merged = 3
    for item in items:
        if item:
            continue
        body = 4
    else:
        exhausted = 5
    after_for = 6
    while flag:
        if start:
            break
        start += 1
    after_while = 7
    return after_while
    dead = 8
"""
    expected = {
        (2, 3, "next"),
        (3, 4, "if-true"),
        (3, 6, "if-false"),
        (4, 7, "if-join"),
        (6, 7, "if-join"),
        (7, 8, "next"),
        (8, 9, "loop-body"),
        (8, 13, "loop-exit"),
        (9, 10, "if-true"),
        (9, 11, "if-false"),
        (10, 8, "continue"),
        (11, 8, "loop-back"),
        (13, 14, "loop-else-join"),
        (14, 15, "next"),
        (15, 16, "loop-body"),
        (15, 19, "loop-exit"),
        (16, 17, "if-true"),
        (16, 18, "if-false"),
        (17, 19, "break"),
        (18, 15, "loop-back"),
        (19, 20, "next"),
    }

    actual, actual_sequence_count = _edge_projection(source)
    expected_sequence_count = sum(
        1
        for _, first_target, _ in expected
        for second_source, _, _ in expected
        if first_target == second_source
    )

    assert actual == expected
    assert actual_sequence_count == expected_sequence_count == 28
    assert all(source_line != 20 for source_line, _, _ in actual)
    assert all(target_line != 21 for _, target_line, _ in actual)


def test_cfg_handles_elif_and_targets_nested_loop_breaks_and_continues() -> None:
    source = """def nested(flag, outer):
    if flag == 0:
        first = 1
    elif flag == 1:
        second = 2
    else:
        third = 3
    merged = 4
    for item in outer:
        while flag:
            if item:
                continue
            break
        if flag:
            break
        continue
    after = 5
    return after
"""
    expected = {
        (2, 3, "if-true"),
        (2, 4, "if-false"),
        (4, 5, "if-true"),
        (4, 7, "if-false"),
        (3, 8, "if-join"),
        (5, 8, "if-join"),
        (7, 8, "if-join"),
        (8, 9, "next"),
        (9, 10, "loop-body"),
        (9, 17, "loop-exit"),
        (10, 11, "loop-body"),
        (10, 14, "loop-exit"),
        (11, 12, "if-true"),
        (11, 13, "if-false"),
        (12, 10, "continue"),
        (13, 14, "break"),
        (14, 15, "if-true"),
        (14, 16, "if-false"),
        (15, 17, "break"),
        (16, 9, "continue"),
        (17, 18, "next"),
    }

    actual, actual_sequence_count = _edge_projection(source)
    expected_sequence_count = sum(
        1
        for _, first_target, _ in expected
        for second_source, _, _ in expected
        if first_target == second_source
    )

    assert actual == expected
    assert actual_sequence_count == expected_sequence_count


def test_cfg_routes_abrupt_try_exits_through_finally_without_fallthrough() -> None:
    source = """def guarded(flag):
    try:
        if flag:
            return 1
        value = 2
    except ValueError:
        raise RuntimeError()
    finally:
        cleanup()
    after = 3
    return after
"""
    expected = {
        (2, 3, "try-body"),
        (2, 7, "except-0"),
        (3, 4, "if-true"),
        (3, 5, "if-false"),
        (4, 9, "finally-return"),
        (5, 9, "finally-normal"),
        (7, 9, "finally-raise"),
        (9, 10, "finally-resume-normal"),
        (10, 11, "next"),
    }

    actual, actual_sequence_count = _edge_projection(source)
    expected_sequence_count = sum(
        1
        for _, first_target, _ in expected
        for second_source, _, _ in expected
        if first_target == second_source
    )

    assert actual == expected
    assert actual_sequence_count == expected_sequence_count - 2 == 7
    assert (4, 10, "next") not in actual
    assert (7, 10, "next") not in actual

    raw_edges, raw_sequences = _raw_cfg(source)
    return_cleanup = next(
        target
        for edge_source, target, label in raw_edges
        if _source_line(edge_source) == 4 and label == "finally-return"
    )
    raise_cleanup = next(
        target
        for edge_source, target, label in raw_edges
        if _source_line(edge_source) == 7 and label == "finally-raise"
    )
    normal_cleanup = next(
        target
        for edge_source, target, label in raw_edges
        if _source_line(edge_source) == 5 and label == "finally-normal"
    )
    assert len({return_cleanup, raise_cleanup, normal_cleanup}) == 3
    assert not any(middle == return_cleanup for _, middle, _, _, _ in raw_sequences)
    assert not any(middle == raise_cleanup for _, middle, _, _, _ in raw_sequences)
    assert any(
        middle == normal_cleanup and _source_line(target) == 10
        for _, middle, _, target, _ in raw_sequences
    )


def test_cfg_finally_preserves_break_and_continue_destinations() -> None:
    source = """def loop(items):
    for item in items:
        try:
            if item:
                continue
            break
        finally:
            cleanup()
    after()
    return 1
"""
    edges, sequences = _raw_cfg(source)
    continue_cleanup = next(
        target
        for edge_source, target, label in edges
        if _source_line(edge_source) == 5 and label == "finally-continue"
    )
    break_cleanup = next(
        target
        for edge_source, target, label in edges
        if _source_line(edge_source) == 6 and label == "finally-break"
    )

    assert continue_cleanup != break_cleanup
    assert any(
        middle == continue_cleanup and _source_line(target) == 2 and second_label == "continue"
        for _, middle, _, target, second_label in sequences
    )
    assert not any(
        middle == continue_cleanup and _source_line(target) == 9
        for _, middle, _, target, _ in sequences
    )
    assert any(
        middle == break_cleanup and _source_line(target) == 9 and second_label == "break"
        for _, middle, _, target, second_label in sequences
    )
    assert not any(
        middle == break_cleanup and _source_line(target) == 2
        for _, middle, _, target, _ in sequences
    )


def test_cfg_nested_finally_keeps_pending_return_separate_from_normal_resume() -> None:
    source = """def nested(flag):
    try:
        try:
            if flag:
                return 1
            value = 2
        finally:
            inner_cleanup()
    finally:
        outer_cleanup()
    after()
"""
    edges, sequences = _raw_cfg(source)
    return_inner = next(
        target
        for edge_source, target, label in edges
        if _source_line(edge_source) == 5 and label == "finally-return"
    )
    normal_inner = next(
        target
        for edge_source, target, label in edges
        if _source_line(edge_source) == 6 and label == "finally-normal"
    )
    return_outer = next(
        target
        for edge_source, target, label in edges
        if edge_source == return_inner and label == "finally-return"
    )
    normal_outer = next(
        target
        for edge_source, target, label in edges
        if edge_source == normal_inner and label == "finally-normal"
    )

    assert len({return_inner, normal_inner, return_outer, normal_outer}) == 4
    assert not any(middle == return_outer for _, middle, _, _, _ in sequences)
    assert any(
        middle == normal_outer and _source_line(target) == 11
        for _, middle, _, target, _ in sequences
    )
    assert not any(
        middle in {return_inner, return_outer} and _source_line(target) == 11
        for _, middle, _, target, _ in sequences
    )


def test_registry_records_only_context_compatible_finally_sequences(
    tmp_path: Path,
) -> None:
    source = tmp_path / "FinallyStrategy.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class FinallyStrategy(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def cleanup(self): return None\n"
        "    def custom_exit(self, pair, trade, current_time, current_rate, "
        "current_profit, **kwargs):\n"
        "        try:\n"
        "            if current_profit > 1:\n"
        "                return 'exit'\n"
        "            value = 2\n"
        "        finally:\n"
        "            self.cleanup()\n"
        "        return None\n"
        "    def populate_indicators(self, dataframe, metadata): return dataframe\n"
        "    def populate_entry_trend(self, dataframe, metadata): return dataframe\n"
        "    def populate_exit_trend(self, dataframe, metadata): return dataframe\n",
        encoding="utf-8",
    )
    tree = ast.parse(source.read_bytes())
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "custom_exit"
    )
    edges = _statement_edges(function.body)
    expected = {
        (
            f"@strategy:custom_exit:{_statement_key(first_source)}:{first_label}:"
            f"{_statement_key(middle)}:{second_label}:{_statement_key(target)}"
        )
        for first_source, middle, first_label in edges
        for second_source, target, second_label in edges
        if middle == second_source
    }

    registry = build_semantic_obligation_registry(
        source,
        class_name="FinallyStrategy",
    )
    actual = {
        record["preimage"]["normalized_semantics"][0]
        for group in registry["obligation_groups"]
        if group["kind"] == "state-machine-two-edge-sequence"
        and group["semantic_owner"] == "nfi-strategy"
        for record in group["obligations"]
    }

    assert actual == expected
    assert not any(
        ":8:16:Return:finally-return:11:12:Expr@" in subject
        and ":finally-resume-normal:" in subject
        for subject in actual
    )


def test_large_record_schema_projection_still_checks_every_record_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "AdversarialStrategy.py"
    _write_strategy(source, "dataframe['close'] * 2")
    registry = build_semantic_obligation_registry(
        source,
        class_name="AdversarialStrategy",
    )
    group = next(item for item in registry["obligation_groups"] if len(item["obligations"]) > 1)
    group["obligations"][1]["preimage"]["source"][1] = "invalid-source-path"
    registry["fingerprint"] = _registry_fingerprint(registry)
    observed_group_sizes: list[int] = []

    def validate_projection(document: Any, _schema: str) -> None:
        observed_group_sizes.extend(
            len(item["obligations"]) for item in document["obligation_groups"]
        )

    monkeypatch.setattr(
        "nfi_backtest_engine.semantic_registry.validate_schema",
        validate_projection,
    )

    with pytest.raises(SpecValidationError, match="preimage source path"):
        validate_semantic_obligation_registry(registry)

    assert observed_group_sizes
    assert set(observed_group_sizes) == {1}


def test_obligation_preimages_are_complete_auditable_and_self_verifying(
    tmp_path: Path,
) -> None:
    source = tmp_path / "AdversarialStrategy.py"
    _write_strategy(source, "dataframe['close'] * 2")
    registry = build_semantic_obligation_registry(
        source,
        class_name="AdversarialStrategy",
    )

    records = [
        (group, record)
        for group in registry["obligation_groups"]
        for record in group["obligations"]
    ]
    assert records
    assert all(set(record) == {"obligation_id", "preimage"} for _, record in records)
    for group, record in records:
        assert set(record["preimage"]) == {"source", "normalized_semantics"}
        preimage = semantic_obligation_preimage(group, record)
        assert preimage["schema_version"] == "semantic-obligation-preimage-v1"
        assert {
            "family",
            "source",
            "normalized_semantics",
            "semantic_owner",
        } <= preimage.keys()
        payload = json.dumps(
            preimage,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        expected = f"obl-{preimage['family']}-{hashlib.sha256(payload).hexdigest()}"
        assert record["obligation_id"] == expected

    tampered = json.loads(json.dumps(registry))
    first_record = tampered["obligation_groups"][0]["obligations"][0]
    first_record["preimage"]["normalized_semantics"][0] += ":tampered"
    tampered["fingerprint"] = _registry_fingerprint(tampered)
    with pytest.raises(SpecValidationError, match="preimage"):
        validate_semantic_obligation_registry(tampered)

    hidden = json.loads(json.dumps(registry))
    hidden_record = hidden["obligation_groups"][0]["obligations"][0]
    hidden["obligation_groups"][0]["obligations"][0] = {
        "obligation_id": hidden_record["obligation_id"]
    }
    hidden["fingerprint"] = _registry_fingerprint(hidden)
    with pytest.raises(SpecValidationError):
        validate_semantic_obligation_registry(hidden)

    duplicate = json.loads(json.dumps(registry))
    duplicate_group = duplicate["obligation_groups"][0]
    duplicate_group["obligations"].insert(
        1,
        json.loads(json.dumps(duplicate_group["obligations"][0])),
    )
    duplicate["fingerprint"] = _registry_fingerprint(duplicate)
    with pytest.raises(SpecValidationError, match="duplicate semantic obligation preimage"):
        validate_semantic_obligation_registry(duplicate)


def _git(command: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *command],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.mark.parametrize(
    ("case", "upstream_arguments", "observation_method", "blocker_code"),
    [
        (
            "absent-ref",
            [
                "--upstream-repository",
                "https://example.invalid/nfi.git",
                "--upstream-source-path",
                "AdversarialStrategy.py",
            ],
            "offline-unverified-source-v1",
            "UNOBSERVED_UPSTREAM_REF",
        ),
        (
            "local-checkout-without-ref",
            [],
            "unconfigured-local-source-v1",
            "UNOBSERVED_UPSTREAM_REF",
        ),
        (
            "offline-path-without-ref",
            ["--upstream-source-path", "AdversarialStrategy.py"],
            "unconfigured-local-source-v1",
            "UNOBSERVED_UPSTREAM_REF",
        ),
        (
            "commit-only",
            [
                "--upstream-commit",
                "0" * 40,
                "--upstream-source-path",
                "AdversarialStrategy.py",
            ],
            "offline-unverified-commit-v1",
            "UNOBSERVED_UPSTREAM_COMMIT",
        ),
    ],
)
def test_public_cli_never_promotes_without_a_configured_upstream_ref(
    tmp_path: Path,
    case: str,
    upstream_arguments: list[str],
    observation_method: str,
    blocker_code: str,
) -> None:
    source = tmp_path / "AdversarialStrategy.py"
    _write_strategy(source, "dataframe['close'] * 2")
    output = tmp_path / f"{case}.json"

    exit_code = cli.main(
        [
            "strategy",
            "semantic-registry",
            str(source),
            "--class",
            "AdversarialStrategy",
            "--source-root",
            str(tmp_path),
            *upstream_arguments,
            "--output",
            str(output),
        ]
    )

    registry = json.loads(output.read_text(encoding="utf-8"))
    upstream = registry["strategy"]["upstream"]
    assert exit_code == 1
    assert upstream["ref"] is None
    assert upstream["observed_commit"] is None
    assert upstream["observation_method"] == observation_method
    assert {item["code"] for item in registry["blockers"]} == {blocker_code}
    assert registry["summary"]["native_promotion"] is False


def test_unobserved_upstream_commit_cannot_claim_native_promotion(
    tmp_path: Path,
) -> None:
    source = tmp_path / "AdversarialStrategy.py"
    _write_strategy(source, "dataframe['close'] * 2")

    registry = build_semantic_obligation_registry(
        source,
        class_name="AdversarialStrategy",
        upstream_repository="https://github.com/iterativv/NostalgiaForInfinity.git",
        upstream_commit="0" * 40,
        upstream_source_path="NostalgiaForInfinityX7.py",
    )

    assert registry["strategy"]["upstream"] == {
        "repository": "https://github.com/iterativv/NostalgiaForInfinity.git",
        "ref": None,
        "configured_commit": "0" * 40,
        "observed_commit": None,
        "observed_commit_timestamp": None,
        "source_path": "NostalgiaForInfinityX7.py",
        "observation_method": "offline-unverified-commit-v1",
    }
    assert {item["code"] for item in registry["blockers"]} == {"UNOBSERVED_UPSTREAM_COMMIT"}
    assert registry["summary"]["native_promotion"] is False


def test_public_cli_requires_matching_dynamically_observed_ref_for_promotion(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    _git(["init", "--initial-branch=main"], cwd=work)
    _git(["config", "user.name", "Registry CLI Test"], cwd=work)
    _git(["config", "user.email", "registry-cli@example.invalid"], cwd=work)
    source = work / "AdversarialStrategy.py"
    _write_strategy(source, "dataframe['close'] * 2")
    _git(["add", source.name], cwd=work)
    _git(["commit", "-m", "first"], cwd=work)
    first_commit = _git(["rev-parse", "HEAD"], cwd=work)
    remote = tmp_path / "remote.git"
    _git(["clone", "--bare", str(work), str(remote)], cwd=tmp_path)
    _write_strategy(source, "dataframe['close'] * 3")
    _git(["add", source.name], cwd=work)
    _git(["commit", "-m", "second"], cwd=work)
    second_commit = _git(["rev-parse", "HEAD"], cwd=work)
    _git(["push", str(remote), "main"], cwd=work)

    def generate(name: str, commit: str) -> tuple[int, dict[str, Any]]:
        output = tmp_path / f"{name}.json"
        exit_code = cli.main(
            [
                "strategy",
                "semantic-registry",
                str(source),
                "--class",
                "AdversarialStrategy",
                "--upstream-repository",
                str(remote),
                "--upstream-ref",
                "refs/heads/main",
                "--upstream-commit",
                commit,
                "--upstream-source-path",
                source.name,
                "--output",
                str(output),
            ]
        )
        return exit_code, json.loads(output.read_text(encoding="utf-8"))

    stale_exit, stale = generate("stale", first_commit)
    current_exit, current = generate("current", second_commit)

    assert stale_exit == 1
    assert stale["strategy"]["upstream"]["observed_commit"] == second_commit
    assert {item["code"] for item in stale["blockers"]} == {"STALE_UPSTREAM_REF"}
    assert stale["summary"]["native_promotion"] is False
    assert current_exit == 0
    assert current["strategy"]["upstream"] == {
        "repository": str(remote),
        "ref": "refs/heads/main",
        "configured_commit": second_commit,
        "observed_commit": second_commit,
        "observed_commit_timestamp": _git(["show", "-s", "--format=%cI", second_commit], cwd=work),
        "source_path": source.name,
        "observation_method": "git-fetch-depth-1-v1",
    }
    assert current["blockers"] == []
    assert current["summary"]["native_promotion"] is True

    forged = json.loads(json.dumps(current))
    forged["strategy"]["upstream"] = {
        "repository": None,
        "ref": None,
        "configured_commit": None,
        "observed_commit": None,
        "observed_commit_timestamp": None,
        "source_path": source.name,
        "observation_method": "unconfigured-local-source-v1",
    }
    forged["fingerprint"] = _registry_fingerprint(forged)
    with pytest.raises(SpecValidationError, match="configured upstream ref"):
        validate_semantic_obligation_registry(forged)


def test_public_cli_publishes_typed_audits_for_every_ref_resolution_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    _git(["init", "--initial-branch=main"], cwd=work)
    _git(["config", "user.name", "Registry Failure Test"], cwd=work)
    _git(["config", "user.email", "registry-failure@example.invalid"], cwd=work)
    source = work / "AdversarialStrategy.py"
    _write_strategy(source, "dataframe['close'] * 2")
    _git(["add", source.name], cwd=work)
    _git(["commit", "-m", "current"], cwd=work)
    commit = _git(["rev-parse", "HEAD"], cwd=work)
    tree = _git(["rev-parse", "HEAD^{tree}"], cwd=work)
    remote = tmp_path / "remote.git"
    _git(["clone", "--bare", str(work), str(remote)], cwd=tmp_path)
    _git(["update-ref", "refs/tags/non-commit", tree], cwd=remote)

    cases = [
        (
            "invalid-ref",
            str(remote),
            "main",
            commit,
            source.name,
            "invalid-upstream-ref-v1",
            "invalid-ref",
            "INVALID_UPSTREAM_REF",
        ),
        (
            "wildcard-refspec",
            str(remote),
            "refs/heads/*",
            commit,
            source.name,
            "invalid-upstream-ref-v1",
            "invalid-ref",
            "INVALID_UPSTREAM_REF",
        ),
        (
            "invalid-commit",
            str(remote),
            "refs/heads/main",
            "not-a-commit",
            source.name,
            "invalid-upstream-commit-v1",
            "invalid-commit",
            "INVALID_UPSTREAM_COMMIT",
        ),
        (
            "invalid-source-path",
            str(remote),
            "refs/heads/main",
            commit,
            "../AdversarialStrategy.py",
            "invalid-upstream-source-v1",
            "invalid-source-path",
            "INVALID_UPSTREAM_SOURCE_PATH",
        ),
        (
            "missing-repository",
            None,
            "refs/heads/main",
            commit,
            source.name,
            "invalid-upstream-configuration-v1",
            "invalid-configuration",
            "INVALID_UPSTREAM_CONFIGURATION",
        ),
        (
            "missing-source-configuration",
            str(remote),
            "refs/heads/main",
            commit,
            None,
            "invalid-upstream-configuration-v1",
            "invalid-configuration",
            "INVALID_UPSTREAM_CONFIGURATION",
        ),
        (
            "missing-ref",
            str(remote),
            "refs/heads/missing",
            commit,
            source.name,
            "upstream-fetch-failed-v1",
            "fetch-failed",
            "UPSTREAM_FETCH_FAILED",
        ),
        (
            "transport-failure",
            str(tmp_path / "absent.git"),
            "refs/heads/main",
            commit,
            source.name,
            "upstream-fetch-failed-v1",
            "fetch-failed",
            "UPSTREAM_FETCH_FAILED",
        ),
        (
            "non-commit-object",
            str(remote),
            "refs/tags/non-commit",
            commit,
            source.name,
            "upstream-ref-not-commit-v1",
            "not-a-commit",
            "UPSTREAM_REF_NOT_COMMIT",
        ),
        (
            "missing-source",
            str(remote),
            "refs/heads/main",
            commit,
            "MissingStrategy.py",
            "upstream-source-missing-v1",
            "source-missing",
            "UPSTREAM_SOURCE_MISSING",
        ),
    ]
    outputs: dict[str, bytes] = {}
    for (
        name,
        repository,
        ref,
        configured_commit,
        configured_source,
        method,
        status,
        blocker,
    ) in cases:
        output = tmp_path / f"{name}.json"
        arguments = [
            "strategy",
            "semantic-registry",
            str(source),
            "--class",
            "AdversarialStrategy",
            "--source-root",
            str(work),
            "--upstream-ref",
            ref,
            "--upstream-commit",
            configured_commit,
            "--output",
            str(output),
        ]
        if repository is not None:
            arguments.extend(("--upstream-repository", repository))
        if configured_source is not None:
            arguments.extend(("--upstream-source-path", configured_source))
        exit_code = cli.main(arguments)

        assert exit_code == 1
        assert output.is_file()
        outputs[name] = output.read_bytes()
        registry = json.loads(outputs[name])
        upstream = registry["strategy"]["upstream"]
        assert registry["schema_version"] == "semantic-obligation-registry-v1"
        assert upstream["repository"] == repository
        assert upstream["ref"] == ref
        assert upstream["observed_commit"] is None
        assert upstream["observed_commit_timestamp"] is None
        assert upstream["observation_method"] == method
        assert upstream["observation_status"] == status
        assert {item["code"] for item in registry["blockers"]} == {blocker}
        assert registry["summary"]["native_promotion"] is False

    repeated = tmp_path / "invalid-ref-repeated.json"
    repeated_exit = cli.main(
        [
            "strategy",
            "semantic-registry",
            str(source),
            "--class",
            "AdversarialStrategy",
            "--source-root",
            str(work),
            "--upstream-repository",
            str(remote),
            "--upstream-ref",
            "main",
            "--upstream-commit",
            commit,
            "--upstream-source-path",
            source.name,
            "--output",
            str(repeated),
        ]
    )
    assert repeated_exit == 1
    assert repeated.read_bytes() == outputs["invalid-ref"]

    relocated = tmp_path / "invalid-ref-relocated.json"
    relocated_repository = str(tmp_path / "different" / "remote.git")
    relocated_exit = cli.main(
        [
            "strategy",
            "semantic-registry",
            str(source),
            "--class",
            "AdversarialStrategy",
            "--source-root",
            str(work),
            "--upstream-repository",
            relocated_repository,
            "--upstream-ref",
            "main",
            "--upstream-commit",
            commit,
            "--upstream-source-path",
            source.name,
            "--output",
            str(relocated),
        ]
    )
    relocated_registry = json.loads(relocated.read_bytes())
    original_registry = json.loads(outputs["invalid-ref"])
    assert relocated_exit == 1
    assert relocated_registry["strategy"]["upstream"]["repository"] == (relocated_repository)
    assert (
        relocated_registry["blockers"][0]["obligation_id"]
        == (original_registry["blockers"][0]["obligation_id"])
    )

    real_git = shutil.which("git")
    assert real_git is not None
    wrapper_directory = tmp_path / "timeout-bin"
    wrapper_directory.mkdir()
    wrapper = wrapper_directory / "git"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, sys\n"
        "if 'fetch' in sys.argv:\n"
        "    signal.pause()\n"
        f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{wrapper_directory}{os.pathsep}{os.environ['PATH']}")
    timeout_output = tmp_path / "timeout.json"
    timeout_exit = cli.main(
        [
            "strategy",
            "semantic-registry",
            str(source),
            "--class",
            "AdversarialStrategy",
            "--source-root",
            str(work),
            "--upstream-repository",
            str(remote),
            "--upstream-ref",
            "refs/heads/main",
            "--upstream-commit",
            commit,
            "--upstream-source-path",
            source.name,
            "--upstream-fetch-timeout",
            "1",
            "--output",
            str(timeout_output),
        ]
    )
    timeout_registry = json.loads(timeout_output.read_bytes())
    assert timeout_exit == 1
    assert timeout_registry["strategy"]["upstream"]["observation_status"] == ("fetch-timeout")
    assert {item["code"] for item in timeout_registry["blockers"]} == {"UPSTREAM_FETCH_TIMEOUT"}
    assert timeout_registry["summary"]["native_promotion"] is False


def test_fetch_timeout_and_missing_exact_object_return_stable_observations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["git"], 180)

    monkeypatch.setattr(subprocess, "run", timeout)
    timeout_root = tmp_path / "timeout"
    timeout_root.mkdir()
    checkout, observation = semantic_inventory._fetch_upstream_ref_once(
        timeout_root,
        repository="https://example.invalid/nfi.git",
        ref="refs/heads/main",
        source_path="AdversarialStrategy.py",
    )
    assert checkout is None
    assert observation == {
        "ref": "refs/heads/main",
        "source_path": "AdversarialStrategy.py",
        "observation_method": "upstream-fetch-timeout-v1",
        "observation_status": "fetch-timeout",
        "blocker_code": "UPSTREAM_FETCH_TIMEOUT",
    }

    def empty_success(
        arguments: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(subprocess, "run", empty_success)
    missing_root = tmp_path / "missing-object"
    missing_root.mkdir()
    checkout, observation = semantic_inventory._fetch_upstream_ref_once(
        missing_root,
        repository="https://example.invalid/nfi.git",
        ref="refs/heads/main",
        source_path="AdversarialStrategy.py",
    )
    assert checkout is None
    assert observation["observation_method"] == "unresolved-upstream-ref-v1"
    assert observation["observation_status"] == "requested-object-missing"
    assert observation["blocker_code"] == "UNOBSERVED_UPSTREAM_REF"


def test_generation_resolves_configured_upstream_ref_and_blocks_stale_commit(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    _git(["init", "--initial-branch=main"], cwd=work)
    _git(["config", "user.name", "Registry Test"], cwd=work)
    _git(["config", "user.email", "registry@example.invalid"], cwd=work)
    source = work / "AdversarialStrategy.py"
    _write_strategy(source, "dataframe['close'] * 2")
    _git(["add", "AdversarialStrategy.py"], cwd=work)
    _git(["commit", "-m", "first"], cwd=work)
    first_commit = _git(["rev-parse", "HEAD"], cwd=work)

    remote = tmp_path / "remote.git"
    _git(["clone", "--bare", str(work), str(remote)], cwd=tmp_path)
    _write_strategy(source, "dataframe['close'] * 3")
    _git(["add", "AdversarialStrategy.py"], cwd=work)
    _git(["commit", "-m", "second"], cwd=work)
    second_commit = _git(["rev-parse", "HEAD"], cwd=work)
    _git(["push", str(remote), "main"], cwd=work)

    stale = build_semantic_obligation_registry(
        source,
        class_name="AdversarialStrategy",
        upstream_repository=str(remote),
        upstream_ref="refs/heads/main",
        upstream_source_path="AdversarialStrategy.py",
        upstream_commit=first_commit,
    )
    assert stale["strategy"]["upstream"]["observed_commit"] == second_commit
    assert stale["strategy"]["upstream"]["ref"] == "refs/heads/main"
    assert stale["strategy"]["upstream"]["observed_commit_timestamp"]
    assert {item["code"] for item in stale["blockers"]} == {"STALE_UPSTREAM_REF"}
    assert stale["summary"]["native_promotion"] is False

    current = build_semantic_obligation_registry(
        source,
        class_name="AdversarialStrategy",
        upstream_repository=str(remote),
        upstream_ref="refs/heads/main",
        upstream_source_path="AdversarialStrategy.py",
        upstream_commit=second_commit,
    )
    repeated = build_semantic_obligation_registry(
        source,
        class_name="AdversarialStrategy",
        upstream_repository=str(remote),
        upstream_ref="refs/heads/main",
        upstream_source_path="AdversarialStrategy.py",
        upstream_commit=second_commit,
    )
    assert current == repeated
    assert (
        current["strategy"]["source"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert current["summary"]["native_promotion"] is True
