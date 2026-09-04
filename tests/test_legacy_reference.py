from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nfi_backtest_engine import cli
from nfi_backtest_engine.errors import BenchmarkError, SpecValidationError
from nfi_backtest_engine.legacy_reference import (
    LEGACY_QUALIFICATION_SPEC_VERSION,
    legacy_runtime_for_source,
    load_legacy_runtime_registry,
    qualify_legacy_runtimes,
)

ROOT = Path(__file__).parents[1]
FIXTURE = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "x7-tag121-spot-v17.4.435-2023-01-01_02"
    / "manifest.json"
)


def _source(path: Path, family: str) -> Path:
    path.write_text(f"class {family}:\n    pass\n", encoding="utf-8")
    return path


def _spec(tmp_path: Path) -> Path:
    document = {
        "schema_version": LEGACY_QUALIFICATION_SPEC_VERSION,
        "fixture_manifest": str(FIXTURE),
        "strategies": [
            {
                "family": "NostalgiaForInfinityNext",
                "generation": "V8",
                "source": str(
                    _source(
                        tmp_path / "NostalgiaForInfinityNext.py",
                        "NostalgiaForInfinityNext",
                    )
                ),
            },
            {
                "family": "NostalgiaForInfinityNextGen",
                "generation": "V9",
                "source": str(
                    _source(
                        tmp_path / "NostalgiaForInfinityNextGen.py",
                        "NostalgiaForInfinityNextGen",
                    )
                ),
            },
        ],
        "candidates": [
            {
                "version": "2026.5.1",
                "image": "freqtradeorg/freqtrade",
                "image_index_digest": f"sha256:{'1' * 64}",
                "image_platform_digest": f"sha256:{'2' * 64}",
                "platform": "linux/amd64",
            },
            {
                "version": "2025.5",
                "image": "freqtradeorg/freqtrade",
                "image_index_digest": f"sha256:{'3' * 64}",
                "image_platform_digest": f"sha256:{'4' * 64}",
                "platform": "linux/amd64",
            },
        ],
    }
    path = tmp_path / "qualification.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_qualification_selects_newest_successful_runtime_per_exact_source(
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_runner(arguments: list[str], **kwargs: Any) -> tuple[Any, dict[str, Any]]:
        calls.append((arguments, kwargs))
        is_v8 = arguments[-1] == "NostalgiaForInfinityNext"
        is_backtest = "backtesting" in arguments
        is_newest = any(value.endswith(f"sha256:{'2' * 64}") for value in arguments)
        returncode = 1 if is_v8 and is_backtest and is_newest else 0
        return SimpleNamespace(returncode=returncode, stdout="out", stderr="err"), {}

    output = tmp_path / "registry.json"
    registry = qualify_legacy_runtimes(_spec(tmp_path), output, runner=fake_runner)

    assert [item["runtime"]["version"] for item in registry["strategies"]] == [
        "2025.5",
        "2026.5.1",
    ]
    assert all(item["result_status"] == "official_only" for item in registry["strategies"])
    assert all(item["native_supported"] is False for item in registry["strategies"])
    assert all(
        item["network_requirement"] == "public-exchange-metadata"
        for item in registry["strategies"]
    )
    assert load_legacy_runtime_registry(output) == registry
    first = registry["strategies"][0]
    assert legacy_runtime_for_source(
        first["family"], first["source_sha256"], registry=registry
    ) == first
    assert legacy_runtime_for_source(first["family"], "0" * 64, registry=registry) is None
    assert len(calls) == 6
    assert all("--privileged" not in arguments for arguments, _kwargs in calls)
    assert all(
        any(value.endswith(":/strategy:ro") for value in arguments)
        for arguments, _ in calls
    )
    assert all("sealed-source:/strategy:ro" in " ".join(arguments) for arguments, _ in calls)
    assert all(kwargs["role"] == "legacy-qualification" for _arguments, kwargs in calls)
    assert all(kwargs["swap_mode"] == "disabled" for _arguments, kwargs in calls)


def test_qualification_rejects_non_descending_candidates(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    document["candidates"].reverse()
    spec.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(SpecValidationError, match="newest-first"):
        qualify_legacy_runtimes(spec, tmp_path / "registry.json", runner=lambda *_a, **_k: None)


def test_qualification_fails_closed_when_no_runtime_qualifies(tmp_path: Path) -> None:
    def failing_runner(*_args: Any, **_kwargs: Any) -> tuple[Any, dict[str, Any]]:
        return SimpleNamespace(returncode=1, stdout="", stderr="unsupported"), {}

    with pytest.raises(BenchmarkError, match="LEGACY_REFERENCE_UNAVAILABLE"):
        qualify_legacy_runtimes(
            _spec(tmp_path),
            tmp_path / "registry.json",
            runner=failing_runner,
        )


def test_legacy_qualification_cli_contract() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        ["reference", "qualify-legacy", "qualification.json", "-o", "registry.json"]
    )

    assert args.reference_command == "qualify-legacy"
    assert args.spec == Path("qualification.json")
    assert args.output == Path("registry.json")
    assert args.timeout == 900


def test_packaged_legacy_registry_matches_planning_authority() -> None:
    planning = ROOT / "planning" / "legacy-reference-runtimes.json"
    packaged = (
        ROOT
        / "python"
        / "nfi_backtest_engine"
        / "contracts"
        / "legacy-reference-runtimes-v1.json"
    )

    assert packaged.read_bytes() == planning.read_bytes()
    assert load_legacy_runtime_registry(planning) == load_legacy_runtime_registry()
