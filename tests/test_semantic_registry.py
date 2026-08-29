from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest
from nfi_backtest_engine import specs
from nfi_backtest_engine.errors import SpecValidationError, StrategyAnalysisError
from nfi_backtest_engine.semantic_inventory import (
    build_semantic_obligation_registry,
    load_semantic_obligation_registry,
)
from nfi_backtest_engine.semantic_registry import (
    SEMANTIC_OBLIGATION_REGISTRY_VERSION,
    _registry_fingerprint,
    write_semantic_obligation_registry,
)
from nfi_backtest_engine.semantic_registry import (
    build_semantic_obligation_registry as build_registry_direct,
)
from nfi_backtest_engine.specs import (
    SEMANTIC_OBLIGATION_REGISTRY_SCHEMA,
    validate_schema,
)

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_CONTRACTS = ROOT / "python" / "nfi_backtest_engine" / "contracts"


def _write_strategy(path: Path, *, unknown: bool = False) -> None:
    unknown_method = (
        "    def future_exchange_callback(self, trade, **kwargs):\n"
        "        match trade:\n"
        "            case None:\n"
        "                return False\n"
        "            case _:\n"
        "                return True\n"
        if unknown
        else ""
    )
    path.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class RegistryStrategy(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_indicators(self, dataframe, metadata):\n"
        "        dataframe['feature'] = dataframe['close'] * 2\n"
        "        return dataframe\n"
        "    def populate_entry_trend(self, dataframe, metadata):\n"
        "        condition = dataframe['feature'] > 1\n"
        "        dataframe.loc[condition, 'enter_long'] = 1\n"
        "        dataframe.loc[condition, 'enter_tag'] = 'registry-entry'\n"
        "        return dataframe\n"
        "    def populate_exit_trend(self, dataframe, metadata):\n"
        "        return dataframe\n"
        "    def custom_exit(self, pair, trade, current_time, current_rate, "
        "current_profit, **kwargs):\n"
        "        exit_enabled = True\n"
        "        if exit_enabled and current_profit > 0.10 and current_rate > 1:\n"
        "            return 'registry-exit'\n"
        "        return None\n"
        "    def boundary_helper(self, value):\n"
        "        rounded = round(value, 2)\n"
        "        return 0 < rounded <= 1\n"
        + unknown_method,
        encoding="utf-8",
    )


def test_semantic_registry_schema_identity_is_compiled_independently() -> None:
    identity = specs.semantic_obligation_registry_schema_identity()

    assert identity == {
        "$id": (
            "https://github.com/vntrevx/NFI_BackTestEngine/schemas/"
            "semantic-obligation-registry-v1.schema.json"
        ),
        "schema_version": "semantic-obligation-registry-v1",
        "bytes": 20881,
        "sha256": "b68588db19867595d626f674c3abbd85ef8678ceef5fe214999d53e462406083",
    }


@pytest.mark.parametrize(
    "mutation",
    ["empty", "widened", "truncated", "byte-swapped", "missing", "symlink"],
)
def test_semantic_registry_schema_identity_fails_closed_before_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    schema_name = "semantic-obligation-registry-v1.schema.json"
    source = ROOT / "python" / "nfi_backtest_engine" / "schemas" / schema_name
    fake_package = tmp_path / "schemas"
    fake_package.mkdir()
    candidate = fake_package / schema_name
    payload = source.read_bytes()
    if mutation == "empty":
        candidate.write_bytes(b"{}")
    elif mutation == "widened":
        document = json.loads(payload)
        document["required"] = []
        document["additionalProperties"] = True
        candidate.write_text(json.dumps(document), encoding="utf-8")
    elif mutation == "truncated":
        candidate.write_bytes(payload[: len(payload) // 2])
    elif mutation == "byte-swapped":
        candidate.write_bytes(payload[::-1])
    elif mutation == "symlink":
        candidate.symlink_to(source)
    else:
        assert mutation == "missing"

    monkeypatch.setattr(specs, "files", lambda _package: fake_package)
    specs._validator.cache_clear()
    try:
        with pytest.raises(
            SpecValidationError,
            match="SEMANTIC_REGISTRY_SCHEMA_IDENTITY",
        ):
            validate_schema({}, SEMANTIC_OBLIGATION_REGISTRY_SCHEMA)
    finally:
        specs._validator.cache_clear()


def test_semantic_registry_schema_duplicate_resource_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema_name = "semantic-obligation-registry-v1.schema.json"
    package = tmp_path / "schemas"
    package.mkdir()
    source = ROOT / "python" / "nfi_backtest_engine" / "schemas" / schema_name
    (package / schema_name).write_bytes(source.read_bytes())
    (package / f"{schema_name}.duplicate").write_bytes(source.read_bytes())
    monkeypatch.setattr(specs, "files", lambda _package: package)
    monkeypatch.setattr(
        specs,
        "_semantic_registry_schema_package_locations",
        lambda: (package,),
    )

    with pytest.raises(SpecValidationError, match="SEMANTIC_REGISTRY_SCHEMA_IDENTITY"):
        validate_schema({}, SEMANTIC_OBLIGATION_REGISTRY_SCHEMA)


def test_semantic_registry_schema_duplicate_package_copy_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = ROOT / "python" / "nfi_backtest_engine" / "schemas"
    duplicate = tmp_path / "duplicate-schemas"
    duplicate.mkdir()
    schema_name = "semantic-obligation-registry-v1.schema.json"
    (duplicate / schema_name).write_bytes((package / schema_name).read_bytes())
    monkeypatch.setattr(
        specs,
        "_semantic_registry_schema_package_locations",
        lambda: (package, duplicate),
        raising=False,
    )

    with pytest.raises(SpecValidationError, match="SEMANTIC_REGISTRY_SCHEMA_IDENTITY"):
        validate_schema({}, SEMANTIC_OBLIGATION_REGISTRY_SCHEMA)


@pytest.mark.parametrize(
    "name",
    [
        "freqtrade-semantic-profile.json",
        "freqtrade-scheduler-contract.json",
        "freqtrade-execution-contract.json",
        "freqtrade-futures-contract.json",
    ],
)
def test_packaged_contract_snapshot_matches_canonical_planning_contract(
    name: str,
) -> None:
    assert (PACKAGE_CONTRACTS / name).read_bytes() == (
        ROOT / "planning" / name
    ).read_bytes()


def test_registry_publication_is_atomic_and_does_not_follow_destination_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = tmp_path / "sentinel.json"
    sentinel.write_bytes(b"sentinel\n")
    symlink = tmp_path / "symlink.json"
    symlink.symlink_to(sentinel)

    with pytest.raises(StrategyAnalysisError):
        write_semantic_obligation_registry(symlink, {"new": True})
    assert sentinel.read_bytes() == b"sentinel\n"
    assert symlink.is_symlink()

    destination = tmp_path / "registry.json"
    destination.write_bytes(b"existing\n")

    def fail_replace(_self: Path, _target: Path) -> Path:
        raise OSError("injected replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError):
        write_semantic_obligation_registry(destination, {"new": True})

    assert destination.read_bytes() == b"existing\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "registry.json",
        "sentinel.json",
        "symlink.json",
    ]


def test_registry_is_contract_bound_complete_and_exactly_once_mapped(
    tmp_path: Path,
) -> None:
    source = tmp_path / "RegistryStrategy.py"
    _write_strategy(source)

    registry = build_semantic_obligation_registry(
        source,
        class_name="RegistryStrategy",
    )

    validate_schema(registry, SEMANTIC_OBLIGATION_REGISTRY_SCHEMA)
    assert registry["schema_version"] == SEMANTIC_OBLIGATION_REGISTRY_VERSION
    assert registry["identity"] == {
        "obligation_id_algorithm": "sha256-canonical-semantic-preimage-v1",
        "registry_fingerprint_algorithm": "sha256-canonical-semantic-content-v1",
        "source_closure_algorithm": "sha256-merkle-source-closure-v1",
    }
    assert registry["freqtrade"]["reference"]["version"] == "2026.5.1"
    assert registry["freqtrade"]["reference"]["image_platform_digest"] == (
        "sha256:bc5b7276118a8539d09ea797cb32c198d029a805815a29c6d27d5f610a3e0b6b"
    )
    assert registry["freqtrade"]["source"] == {
        "repository": "https://github.com/freqtrade/freqtrade.git",
        "commit": "6fa470939cc74bf0672e0e348a4d9b293072e43c",
        "identity_kind": "git-commit-and-observed-method-merkle-v1",
        "observed_method_count": 15,
        "observed_method_merkle_root": (
            "54e428105e8b2108b76a5ae1fbdf4d948e1a27a853b1c0bcdee6f1ac5d1b0192"
        ),
    }
    assert registry["source_closure"]["complete"] is True
    assert registry["source_closure"]["file_count"] == 1

    groups = registry["obligation_groups"]
    obligation_ids = [
        record["obligation_id"]
        for group in groups
        for record in group["obligations"]
    ]
    summary = registry["summary"]
    assert len(obligation_ids) == len(set(obligation_ids))
    assert len(obligation_ids) == summary["mapped_obligations"]
    assert summary["total_obligations"] == (
        summary["statically_witnessed_obligations"]
        + summary["machine_proven_unreachable_obligations"]
    )
    assert summary["total_obligations"] == (
        summary["generic_runtime_obligations"]
        + summary["compiled_program_obligations"]
        + summary["official_only_blocker_obligations"]
    )
    assert summary["unknown_obligations"] == 1
    assert {item["code"] for item in registry["blockers"]} == {
        "UNOBSERVED_UPSTREAM_REF"
    }
    assert summary["ast_node_obligations"] > 0
    assert summary["operator_obligations"] > 0
    assert summary["call_obligations"] > 0
    assert summary["callable_obligations"] > 0
    assert summary["ir_node_obligations"] > 0
    tree = ast.parse(source.read_text(encoding="utf-8"))
    semantic_node_types = (
        ast.mod
        | ast.stmt
        | ast.expr
        | ast.comprehension
        | ast.keyword
        | ast.withitem
        | ast.ExceptHandler
        | ast.arguments
        | ast.arg
        | ast.alias
    )
    assert summary["source_node_obligations"] == sum(
        isinstance(node, semantic_node_types) for node in ast.walk(tree)
    )
    assert summary["source_node_obligations"] == (
        summary["ast_node_obligations"] + summary["call_obligations"]
    )
    assert summary["callable_obligations"] == sum(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        for node in ast.walk(tree)
    )
    assert summary["decision_outcomes"] > 0
    assert summary["mcdc_terms"] >= 2
    assert summary["threshold_boundaries"] >= 6
    assert summary["signal_obligations"] > 0
    assert summary["tag_obligations"] > 0
    assert summary["callback_action_obligations"] > 0
    assert summary["state_machine_edges"] > 0
    assert summary["state_machine_two_edge_sequences"] > 0
    assert summary["scheduler_transitions"] > 0
    assert summary["wallet_transitions"] > 0
    assert summary["order_transitions"] > 0
    assert summary["futures_paths"] > 0
    assert summary["protection_obligations"] > 0
    assert summary["historical_non_observation_exclusions"] == 0
    assert summary["native_promotion"] is False


def test_inactive_nested_callable_is_only_machine_proven_unreachable(
    tmp_path: Path,
) -> None:
    source = tmp_path / "RegistryStrategy.py"
    _write_strategy(source)
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "        rounded = round(value, 2)\n",
            "        def never_called():\n"
            "            return value\n"
            "        rounded = round(value, 2)\n",
        ),
        encoding="utf-8",
    )

    registry = build_semantic_obligation_registry(
        source,
        class_name="RegistryStrategy",
    )

    excluded = [
        group
        for group in registry["obligation_groups"]
        if group["reachability"] == "machine-proven-unreachable"
    ]
    assert excluded
    assert {group["mapping"] for group in excluded} == {
        "machine-proven-unreachable"
    }
    assert {group["proof"] for group in excluded} == {
        "static-call-graph-no-path"
    }
    assert registry["summary"]["machine_proven_unreachable_obligations"] == sum(
        len(group["obligations"]) for group in excluded
    )


def test_registry_output_is_byte_deterministic_and_ignores_unrelated_dirty_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "RegistryStrategy.py"
    _write_strategy(source)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    relocated_output = tmp_path / "relocated.json"

    first_registry = build_semantic_obligation_registry(
        source,
        class_name="RegistryStrategy",
        output_path=first,
    )
    (tmp_path / "unrelated-user-file.txt").write_text("dirty\n", encoding="utf-8")
    second_registry = build_semantic_obligation_registry(
        source,
        class_name="RegistryStrategy",
        output_path=second,
    )
    relocated = tmp_path / "nested" / "RenamedStrategy.py"
    relocated.parent.mkdir()
    relocated.write_bytes(source.read_bytes())
    relocated_registry = build_registry_direct(
        relocated,
        class_name="RegistryStrategy",
        output_path=relocated_output,
    )

    assert first.read_bytes() == second.read_bytes()
    assert first_registry["fingerprint"] == second_registry["fingerprint"]
    assert first_registry["fingerprint"] == relocated_registry["fingerprint"]
    assert first_registry["obligation_groups"] == relocated_registry[
        "obligation_groups"
    ]
    relocated_load = load_semantic_obligation_registry(
        first,
        strategy_source=relocated,
    )
    assert relocated_load["fingerprint"] == first_registry["fingerprint"]
    assert hashlib.sha256(first.read_bytes()).hexdigest() == hashlib.sha256(
        second.read_bytes()
    ).hexdigest()


def test_registry_loader_rejects_stale_or_missing_transitive_helper(
    tmp_path: Path,
) -> None:
    helper = tmp_path / "local_helper.py"
    helper.write_text("def scale(value):\n    return value * 2\n", encoding="utf-8")
    source = tmp_path / "RegistryStrategy.py"
    _write_strategy(source)
    source.write_text(
        "from local_helper import scale\n"
        + source.read_text(encoding="utf-8").replace(
            "dataframe['close'] * 2",
            "scale(dataframe['close'])",
        ),
        encoding="utf-8",
    )
    output = tmp_path / "registry.json"
    build_semantic_obligation_registry(
        source,
        class_name="RegistryStrategy",
        source_root=tmp_path,
        output_path=output,
    )
    loaded = load_semantic_obligation_registry(
        output,
        strategy_source=source,
        source_root=tmp_path,
    )
    assert loaded["source_closure"]["file_count"] == 2

    helper.write_text("def scale(value):\n    return value * 3\n", encoding="utf-8")
    with pytest.raises(SpecValidationError, match="source closure is stale"):
        load_semantic_obligation_registry(
            output,
            strategy_source=source,
            source_root=tmp_path,
        )

    helper.unlink()
    with pytest.raises(SpecValidationError, match="source closure is stale"):
        load_semantic_obligation_registry(
            output,
            strategy_source=source,
            source_root=tmp_path,
        )


def test_registry_rejects_malformed_source_and_misleading_derived_totals(
    tmp_path: Path,
) -> None:
    malformed = tmp_path / "Malformed.py"
    malformed.write_text("class Malformed(IStrategy):\n    def broken(\n", encoding="utf-8")
    with pytest.raises(StrategyAnalysisError, match="PYTHON_SYNTAX"):
        build_semantic_obligation_registry(malformed, class_name="Malformed")

    source = tmp_path / "RegistryStrategy.py"
    _write_strategy(source)
    output = tmp_path / "registry.json"
    registry = build_semantic_obligation_registry(
        source,
        class_name="RegistryStrategy",
        output_path=output,
    )
    registry["summary"]["total_obligations"] += 1
    registry["fingerprint"] = _registry_fingerprint(registry)
    output.write_text(json.dumps(registry), encoding="utf-8")

    with pytest.raises(SpecValidationError, match="summary is not derived"):
        load_semantic_obligation_registry(output)


def test_unknown_external_semantics_are_typed_before_native_promotion(
    tmp_path: Path,
) -> None:
    source = tmp_path / "RegistryStrategy.py"
    _write_strategy(source)
    source.write_text(
        "import unexplored_exchange_semantics\n" + source.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    registry = build_semantic_obligation_registry(
        source,
        class_name="RegistryStrategy",
    )

    assert {item["code"] for item in registry["blockers"]} == {
        "UNKNOWN_EXTERNAL_IMPORT",
        "UNOBSERVED_UPSTREAM_REF",
    }
    assert registry["summary"]["unknown_obligations"] == 2
    assert registry["summary"]["native_promotion"] is False


def test_unknown_callback_and_ast_are_typed_blockers_before_native_promotion(
    tmp_path: Path,
) -> None:
    source = tmp_path / "RegistryStrategy.py"
    _write_strategy(source, unknown=True)

    registry = build_semantic_obligation_registry(
        source,
        class_name="RegistryStrategy",
    )

    assert {item["code"] for item in registry["blockers"]} == {
        "UNKNOWN_REACHABLE_AST_NODE",
        "UNKNOWN_STRATEGY_CALLBACK",
        "UNOBSERVED_UPSTREAM_REF",
    }
    assert registry["summary"]["unknown_obligations"] == len(
        registry["blockers"]
    )
    assert registry["summary"]["unknown_obligations"] >= 2
    assert registry["summary"]["native_promotion"] is False
    blocker_ids = {item["obligation_id"] for item in registry["blockers"]}
    official_ids = {
        record["obligation_id"]
        for group in registry["obligation_groups"]
        if group["mapping"] == "official-only-blocker"
        for record in group["obligations"]
    }
    assert blocker_ids <= official_ids


def test_reviewed_finite_and_sequence_extension_calls_do_not_block_registry(
    tmp_path: Path,
) -> None:
    source = tmp_path / "RegistryStrategy.py"
    _write_strategy(source)
    source.write_text(
        "import numpy as np\n"
        + source.read_text(encoding="utf-8").replace(
            "        exit_enabled = True\n",
            "        observed = []\n"
            "        observed.extend([current_rate])\n"
            "        exit_enabled = np.isfinite(observed[0])\n",
        ),
        encoding="utf-8",
    )

    registry = build_semantic_obligation_registry(
        source,
        class_name="RegistryStrategy",
    )

    assert "UNKNOWN_REACHABLE_CALL" not in {
        item["code"] for item in registry["blockers"]
    }
