from __future__ import annotations

from pathlib import Path

import nfi_backtest_engine.targeted_verification as targeted
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.fixture import sha256_file
from nfi_backtest_engine.targeted_verification import (
    assess_targeted_coverage,
    observable_tag_forms,
    plan_targeted_verification,
    verify_targeted_strategy,
)


def _target(
    identifier: str,
    *,
    kind: str,
    change: str,
    value: str | int,
    observable: bool = True,
    tags: list[str] | None = None,
) -> dict:
    return {
        "id": identifier * 64,
        "kind": kind,
        "change": change,
        "value": value,
        "methods": [],
        "tags": tags or [],
        "runtime_observable": observable,
    }


def _difference(*targets: dict) -> dict:
    return {
        "schema_version": "1.2.0",
        "classification": "ir-compatible",
        "behavior_targets": list(targets),
    }


def test_observable_tag_forms_preserve_exact_and_freqtrade_route() -> None:
    assert observable_tag_forms("exit_long_rebuy_e_r ( 65 )") == {
        "exit_long_rebuy_e_r ( 65 )",
        "exit_long_rebuy_e_r",
    }
    assert observable_tag_forms("65 ") == {"65"}


def _fixture(
    root: Path,
    *,
    fixture_id: str,
    mode: str,
    callbacks: list[str] | None = None,
    entry_tags: list[str] | None = None,
    order_tags: list[str] | None = None,
    direction: str | None = None,
) -> Path:
    fixture = root / fixture_id
    artifacts = fixture / "artifacts"
    artifacts.mkdir(parents=True)
    surface = {
        "schema_version": "2.0.0",
        "trades": [
            {
                "entry_tag": (entry_tags or [""])[0],
                "orders": [{"tag": tag} for tag in order_tags or []],
                **({"direction": direction} if direction is not None else {}),
            }
        ],
    }
    coverage = {
        "schema_version": "1.0.0",
        "source": "official-freqtrade-observer",
        "bindings": {},
        "observed": {
            "callbacks": callbacks or [],
            "entry_tags": entry_tags or [],
            "compound_tags": [],
            "exit_reasons": [],
        },
    }
    surface_path = artifacts / "trade-surface.json"
    coverage_path = artifacts / "coverage-report.json"
    write_json(surface_path, surface)
    write_json(coverage_path, coverage)
    manifest = {
        "schema_version": "3.0.0",
        "fixture_id": fixture_id,
        "freqtrade": {"trading_mode": mode},
        "artifacts": {
            "trade_surface": _record(surface_path, fixture),
            "coverage_report": _record(coverage_path, fixture),
        },
    }
    manifest_path = fixture / "manifest.json"
    write_json(manifest_path, manifest)
    return manifest_path


def _record(path: Path, root: Path) -> dict:
    return {
        "path": str(path.relative_to(root)),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def test_targeted_plan_uses_deterministic_minimum_fixture_set(
    tmp_path: Path,
) -> None:
    _fixture(
        tmp_path,
        fixture_id="b-signal",
        mode="spot",
        entry_tags=["63"],
    )
    _fixture(
        tmp_path,
        fixture_id="a-combined",
        mode="spot",
        callbacks=["adjust_trade_position"],
        entry_tags=["63"],
    )
    report = plan_targeted_verification(
        _difference(
            _target("a", kind="signal", change="added", value="63"),
            _target(
                "b",
                kind="callback",
                change="changed",
                value="adjust_trade_position",
            ),
        ),
        tmp_path,
        trading_mode="spot",
    )

    assert report["status"] == "ready"
    assert [item["fixture_id"] for item in report["selected_fixtures"]] == ["a-combined"]
    assert report["missing_targets"] == []


def test_targeted_probe_requires_only_selected_changed_behavior(
    tmp_path: Path,
) -> None:
    manifest_path = _fixture(
        tmp_path,
        fixture_id="multi-purpose",
        mode="futures",
        callbacks=["adjust_trade_position"],
        entry_tags=["63", "145"],
    )
    manifest = read_json(manifest_path)
    manifest["required_coverage"] = {
        "callbacks": ["adjust_trade_position"],
        "entry_tags": ["63", "145"],
        "compound_tags": [],
        "protection_methods": [],
        "exit_reasons": [],
        "sides": ["long"],
        "minimum_lock_count": 0,
        "minimum_distinct_leverages": 2,
        "minimum_funded_trades": 0,
        "require_rejected_locked_entry": False,
    }

    required = targeted._targeted_required_coverage(
        manifest_path.parent,
        manifest,
        [
            _target(
                "z",
                kind="callback",
                change="changed",
                value="populate_entry_trend",
                tags=["63"],
            )
        ],
    )

    assert required["entry_tags"] == ["63"]
    assert required["callbacks"] == []
    assert required["minimum_distinct_leverages"] == 0


def test_targeted_probe_derives_informative_pairs_from_sealed_inputs() -> None:
    manifest = {
        "inputs": [
            {
                "role": "candles",
                "path": "inputs/data/futures/ALGO_USDT_USDT-5m-futures.feather",
            },
            {
                "role": "candles",
                "path": "inputs/data/futures/BTC_USDT_USDT-5m-futures.feather",
            },
            {
                "role": "funding_candles",
                "path": ("inputs/data/futures/BTC_USDT_USDT-1h-funding_rate.feather"),
            },
        ]
    }

    pairs = targeted._fixture_candle_pairs(
        manifest,
        timeframes=["5m", "1h"],
        trading_mode="futures",
    )

    assert pairs == ["ALGO/USDT:USDT", "BTC/USDT:USDT"]


def test_unobservable_target_remains_a_coverage_gap(tmp_path: Path) -> None:
    _fixture(
        tmp_path,
        fixture_id="callback",
        mode="futures",
        callbacks=["custom_exit"],
    )

    report = plan_targeted_verification(
        _difference(
            _target(
                "c",
                kind="custom_state_key",
                change="added",
                value="dynamic",
                observable=False,
            )
        ),
        tmp_path,
        trading_mode="futures",
    )

    assert report["status"] == "coverage-gap"
    assert report["selected_fixtures"] == []
    assert [item["id"] for item in report["missing_targets"]] == ["c" * 64]


def test_removed_target_requires_baseline_presence_and_candidate_absence(
    tmp_path: Path,
) -> None:
    baseline = _fixture(
        tmp_path / "baseline",
        fixture_id="old",
        mode="spot",
        entry_tags=["62"],
    )
    candidate = _fixture(
        tmp_path / "candidate",
        fixture_id="new",
        mode="spot",
        entry_tags=["63"],
    )
    target = _target("d", kind="signal", change="removed", value="62")

    report = assess_targeted_coverage(
        [target],
        baseline_manifest=baseline,
        candidate_manifest=candidate,
    )

    assert report["complete"] is True
    assert report["changed_branch_reached"] is True
    assert report["target_proofs"] == [
        {
            "target_id": "d" * 64,
            "proof_mode": "absence",
            "baseline_observed": True,
            "candidate_observed": False,
            "complete": True,
        }
    ]


def test_removed_target_is_not_required_from_candidate_capture(
    tmp_path: Path,
) -> None:
    manifest_path = _fixture(
        tmp_path,
        fixture_id="removed-route",
        mode="spot",
        entry_tags=["62"],
        direction="long",
    )
    manifest = read_json(manifest_path)

    required = targeted._targeted_required_coverage(
        manifest_path.parent,
        manifest,
        [_target("e", kind="signal", change="removed", value="62")],
    )

    assert required["entry_tags"] == []
    assert required["sides"] == ["long"]
    assert not any(
        (
            required["callbacks"],
            required["compound_tags"],
            required["exit_reasons"],
            required["minimum_distinct_leverages"],
        )
    )


def test_changed_callback_requires_old_and_new_route_observation(
    tmp_path: Path,
) -> None:
    baseline = _fixture(
        tmp_path / "baseline",
        fixture_id="old",
        mode="spot",
        callbacks=["custom_exit"],
        order_tags=["route-65"],
    )
    candidate = _fixture(
        tmp_path / "candidate",
        fixture_id="new",
        mode="spot",
        callbacks=["custom_exit"],
        order_tags=["route-65"],
    )
    target = _target(
        "t",
        kind="callback",
        change="changed",
        value="custom_exit",
        tags=["route-65"],
    )

    complete = assess_targeted_coverage(
        [target],
        baseline_manifest=baseline,
        candidate_manifest=candidate,
    )
    assert complete["changed_branch_reached"] is True

    missing_candidate = _fixture(
        tmp_path / "missing",
        fixture_id="missing",
        mode="spot",
        callbacks=["custom_exit"],
    )
    incomplete = assess_targeted_coverage(
        [target],
        baseline_manifest=baseline,
        candidate_manifest=missing_candidate,
    )
    assert incomplete["changed_branch_reached"] is False
    assert incomplete["target_proofs"][0]["candidate_observed"] is False


def test_targeted_verifier_stays_latest_checked_without_branch_fixture(
    tmp_path: Path,
) -> None:
    source = tmp_path / "strategy.py"
    source.write_text("class X7:\\n    pass\\n", encoding="utf-8")
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    output = tmp_path / "result"
    target = _target(
        "e",
        kind="custom_state_key",
        change="added",
        value="unknown",
        observable=False,
    )

    report = verify_targeted_strategy(
        source,
        _difference(target),
        {
            "native_compatible": True,
            "source": {"sha256": "a" * 64},
            "blockers": [],
        },
        fixtures,
        output,
        class_name="X7",
        trading_mode="spot",
        upstream_repository="example/NFI",
        upstream_commit="b" * 40,
        timeout_seconds=60,
        capture_service=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("capture must not run")
        ),
    )

    assert report["verification_state"] == "latest_checked"
    assert report["complete"] is False
    assert report["runs"] == []
    assert {item["code"] for item in report["blockers"]} >= {
        "TARGETED_COVERAGE_GAP",
        "CHANGED_BRANCH_PROOF_REQUIRED",
    }
    assert (output / "verification-plan.json").is_file()
    assert (output / "qualification.json").is_file()
    assert (output / "run.json").is_file()


def test_targeted_verifier_promotes_only_after_separate_exact_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "strategy.py"
    source.write_text("class X7:\\n    pass\\n", encoding="utf-8")
    fixtures = tmp_path / "fixtures"
    baseline = _fixture(
        fixtures,
        fixture_id="signal-63",
        mode="spot",
        entry_tags=["63"],
    )
    output = tmp_path / "result"
    target = _target("f", kind="signal", change="added", value="63")

    monkeypatch.setattr(
        targeted,
        "_write_probe_spec",
        lambda *_args, **_kwargs: write_json(_args[3], {"probe": True}),
    )

    def profile(path: Path) -> dict:
        write_json(path, {"profile": True})
        return {"profile": True}

    def capture(
        _spec: Path,
        fixture_output: Path,
        _work: Path,
        **_kwargs,
    ) -> dict:
        candidate = _fixture(
            fixture_output.parent,
            fixture_id=fixture_output.name,
            mode="spot",
            entry_tags=["63"],
        )
        assert candidate == fixture_output / "manifest.json"
        return {"complete": True}

    def run_fixture(
        manifest: Path,
        output_directory: Path,
        **_kwargs,
    ) -> dict:
        assert manifest != baseline
        output_directory.mkdir(parents=True)
        write_json(output_directory / "run.json", {"complete": True})
        return {
            "parity": {
                "trade_surface": {"equal": True},
                "state_trace": {"equal": True},
            }
        }

    report = verify_targeted_strategy(
        source,
        _difference(target),
        {
            "native_compatible": True,
            "source": {"sha256": "a" * 64},
            "blockers": [],
        },
        fixtures,
        output,
        class_name="X7",
        trading_mode="spot",
        upstream_repository="example/NFI",
        upstream_commit="b" * 40,
        timeout_seconds=60,
        capture_service=capture,
        fixture_service=run_fixture,
        profile_service=profile,
    )

    assert report["verification_state"] == "quick_verified"
    assert report["proof"] == {
        "complete": True,
        "changed_branch_reached": True,
        "trade_surface_exact": True,
        "full_state_exact": True,
    }
    assert report["complete"] is True
