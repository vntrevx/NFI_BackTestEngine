from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest
from nfi_backtest_engine import combined_release, native_scorecard, release_provenance
from nfi_backtest_engine.canonical import write_json
from nfi_backtest_engine.combined_release import (
    PUBLIC_RELEASE_ASSET_COUNT,
    combine_full_x7_release,
    finalize_combined_release_publication,
    seal_combined_release_candidate,
    verify_combined_release_candidate,
)
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.evidence_bundle import write_evidence_bundle
from nfi_backtest_engine.fixture import sha256_file
from nfi_backtest_engine.platform_benchmark import EXACT_FIXTURE_LANE, seal_platform_evidence
from nfi_backtest_engine.release_gate import RELEASE_CHECKSUMS_NAME
from nfi_backtest_engine.release_provenance import candidate_distribution_identity
from provenance_support import TEST_POLICY, sign_report
from test_native_scorecard import _current_ref_proof, _scorecard_inputs


@pytest.fixture(autouse=True)
def _stable_current_ref_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        native_scorecard,
        "begin_packaged_semantic_registry_authorization",
        _current_ref_proof,
    )
    monkeypatch.setattr(
        native_scorecard,
        "finalize_packaged_semantic_registry_authorization",
        lambda _proof: None,
    )
    monkeypatch.setattr(
        native_scorecard,
        "require_fresh_current_ref_for_authorization",
        lambda _evidence, _identity, _operation: None,
    )


WHEEL_SHA = "a" * 64
NATIVE_SHA = "b" * 64
STRATEGY_SHA = "c" * 64
DURABLE_LEDGER_AVAILABLE = (
    os.name == "posix"
    and hasattr(os, "O_NOATIME")
    and Path("/proc/self/fd").is_dir()
)


def _certificate(
    tmp_path: Path,
    mode: str,
    *,
    wheel_sha256: str = WHEEL_SHA,
    portable_package_sha256: str = "8" * 64,
) -> Path:
    root = tmp_path / mode
    root.mkdir()
    trading_mode = "spot" if mode == "binance-spot" else "futures"
    timerange = (
        "20210101-20260101"
        if trading_mode == "spot"
        else "20210726-20260726"
    )
    report = {
        "schema_version": "2.0.0",
        "created_at": "2026-07-25T00:00:00Z",
        "status": "certified",
        "release_certified": True,
        "claim_scope": {
            "strategy": "NostalgiaForInfinityX7",
            "upstream_commit": "d" * 40,
            "mode_contract": mode,
            "trading_mode": trading_mode,
            "margin_mode": None if trading_mode == "spot" else "isolated",
            "exchange": "binance",
            "settlement_currency": "USDT",
            "required_data_roles": (
                ["candles"] if trading_mode == "spot" else ["candles", "funding_rate", "mark"]
            ),
            "timerange": timerange,
            "pair_count": 80,
            "timeframes": ["5m", "15m", "1h", "4h", "1d"],
            "history_coverage_policy": (
                "strict" if trading_mode == "spot" else "listing-aware"
            ),
            "continuous_timerange": True,
            "evidence": "continuous-oracle-plus-official-full-state-probes",
        },
        "inputs": {
            "release_lock": {
                "sha256": "e" * 64,
                "identity_sha256": "6" * 64,
            },
            "mode_contract": mode,
            "reference": {
                "version": "2026.5.1",
                "image_platform_digest": "sha256:" + "f" * 64,
            },
            "strategy_sha256": STRATEGY_SHA,
            "config_sha256": "1" * 64,
            "data_aggregate_sha256": "2" * 64,
            "engine_market_snapshot_sha256": "3" * 64,
            "reference_market_snapshot_sha256": "4" * 64,
        },
        "environment": {
            "hardware": {},
            "execution_profile": {},
            "package_version": "1.0.0",
            "engine_build": {
                "source_fingerprint": "5" * 64,
            },
        },
        "measurement": {
            "native_warmups_excluded": 1,
            "native_initial_repetitions": 3,
            "native_measured_repetitions": 3,
            "native_maximum_repetitions": 5,
            "native_spread_threshold": 0.05,
            "engine_relative_spread": 0.01,
            "official_reference_repetitions": 1,
            "official_reference_role": "single-continuous-exact-parity-oracle",
            "resumed": False,
        },
        "runs": {
            "engine": [{}, {}, {}],
            "official_reference": {},
            "engine_summary": {},
            "official_reference_summary": {},
        },
        "state_probes": [{}, {}, {}],
        "gates": {
            "input_lock": {"met": True},
            "installed_wheel": {
                "met": True,
                "sha256": wheel_sha256,
                "native_member_sha256": NATIVE_SHA,
                "portable_package_sha256": portable_package_sha256,
            },
            "native_pipeline": {"met": True},
            "official_parity": {"met": True},
            "determinism": {"met": True},
            "speed": {"met": True},
            "memory": {"met": True},
            "state_probes": {"met": True},
        },
    }
    report_path = root / "full-x7-certification.json"
    write_json(report_path, report)
    bundle = write_evidence_bundle(
        root,
        evidence_id="e" * 64,
        release_certified=True,
        archive_name="full-x7-certification-bundle.zip",
        include_paths=[report_path],
    )
    path = root / "full-x7-result.json"
    write_json(path, {**report, "bundle": bundle})
    return path


def _platform_evidence(
    tmp_path: Path,
    mode: str,
    *,
    wheel_sha256: str = WHEEL_SHA,
    portable_package_sha256: str = "8" * 64,
    platform_wheel_sha256: dict[str, str] | None = None,
    base_strategy_sha256: str = STRATEGY_SHA,
    candidate_id: str | None = None,
) -> Path:
    root = tmp_path / f"{mode}-platform"
    reports = root / "reports"
    reports.mkdir(parents=True)
    paths: list[str | Path] = []
    for _index, (system, machine) in enumerate(
        (("windows", "amd64"), ("linux", "x86_64"), ("darwin", "arm64")),
        start=1,
    ):
        wheel = (
            platform_wheel_sha256[system]
            if platform_wheel_sha256 is not None
            else wheel_sha256
            if system == "linux"
            else "9" * 64
        )
        report = {
            "schema_version": "1.2.0",
            "complete": True,
            "lane": EXACT_FIXTURE_LANE,
            "platform": {"system": system, "machine": machine, "wsl": False},
            "package": {
                "version": "1.0.0",
                "wheel_sha256": wheel,
                "native_extension_sha256": (
                    NATIVE_SHA
                    if system == "linux"
                    else ("1" if system == "windows" else "2") * 64
                ),
                "installed_extension_equal": True,
                "portable_package_sha256": portable_package_sha256,
            },
            "workload": {
                "lane": EXACT_FIXTURE_LANE,
                "mode_contract": mode,
                "fixture_id": "x7-mode",
                "manifest_sha256": "e" * 64,
                "strategy_sha256": "7" * 64,
                "base_strategy_sha256": base_strategy_sha256,
                "verification_level": "full",
                "identity_sha256": "0" * 64,
            },
            "measurement": {
                "result_sha256": ["a" * 64],
                "wall_time_seconds": {"median": 1.0},
                "peak_rss_bytes": {"maximum": 1000},
                "measured_repetitions": 3,
            },
        }
        path = reports / f"{system}.json"
        write_json(path, report)
        sign_report(
            path,
            run_id=100 if mode == "binance-spot" else 200,
            candidate_id=candidate_id or "2" * 64,
        )
        paths.append(path)
    seal_platform_evidence(paths, root / "sealed", provenance_policy=TEST_POLICY)
    return root / "sealed" / "platform-evidence.json"


def test_combined_release_rejects_before_output_without_score_or_platforms(
    tmp_path: Path,
) -> None:
    output = tmp_path / "release"
    with pytest.raises(SpecValidationError, match="scorecard evidence and identity are required"):
        combine_full_x7_release(
            spot_certificate_path=_certificate(tmp_path, "binance-spot"),
            futures_certificate_path=_certificate(tmp_path, "binance-usdtm-isolated"),
            platform_evidence_paths=[],
            output_directory=output,
        )
    assert not output.exists()


def test_combined_release_certifies_two_modes_and_three_os_evidence(
    tmp_path: Path,
) -> None:
    spot = _certificate(tmp_path, "binance-spot")
    futures = _certificate(tmp_path, "binance-usdtm-isolated")
    score_evidence, score_identity = _scorecard_inputs(tmp_path / "native-score")
    result = combine_full_x7_release(
        spot_certificate_path=spot,
        futures_certificate_path=futures,
        platform_evidence_paths=[
            _platform_evidence(tmp_path, "binance-spot"),
            _platform_evidence(tmp_path, "binance-usdtm-isolated"),
        ],
        output_directory=tmp_path / "release",
        native_score_evidence_path=score_evidence,
        native_score_identity_path=score_identity,
        provenance_policy=TEST_POLICY,
    )

    assert result["status"] == "certified"
    assert result["release_certified"] is True
    assert result["gates"]["platform_evidence"]["met"] is True
    assert (
        result["mode_scopes"]["binance-spot"]["timerange"]
        == "20210101-20260101"
    )
    assert (
        result["mode_scopes"]["binance-usdtm-isolated"]["timerange"]
        == "20210726-20260726"
    )
    assert (tmp_path / "release" / result["certificates"]["binance-spot"]["file"]).is_file()
    assert (
        tmp_path / "release" / "evidence" / "binance-usdtm-isolated" / "platform-bundle.zip"
    ).is_file()


def test_combined_release_rejects_a_certificate_changed_after_bundling(
    tmp_path: Path,
) -> None:
    spot = _certificate(tmp_path, "binance-spot")
    futures = _certificate(tmp_path, "binance-usdtm-isolated")
    document = json.loads(futures.read_text(encoding="utf-8"))
    document["gates"]["installed_wheel"]["sha256"] = "0" * 64
    write_json(futures, document)
    score_evidence, score_identity = _scorecard_inputs(tmp_path / "native-score")

    with pytest.raises(SpecValidationError, match="does not contain its report"):
        combine_full_x7_release(
            spot_certificate_path=spot,
            futures_certificate_path=futures,
            platform_evidence_paths=[],
            output_directory=tmp_path / "release",
            native_score_evidence_path=score_evidence,
            native_score_identity_path=score_identity,
            provenance_policy=TEST_POLICY,
        )


def test_combined_release_rejects_different_bundled_candidate_wheels(
    tmp_path: Path,
) -> None:
    spot = _certificate(tmp_path, "binance-spot")
    futures = _certificate(tmp_path, "binance-usdtm-isolated")
    root = futures.parent
    report_path = root / "full-x7-certification.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["gates"]["installed_wheel"]["sha256"] = "0" * 64
    write_json(report_path, report)
    for stale_bundle_asset in (
        root / "full-x7-certification-bundle.zip",
        root / "bundle-manifest.json",
        root / "bundle.json",
    ):
        stale_bundle_asset.unlink()
    bundle = write_evidence_bundle(
        root,
        evidence_id="e" * 64,
        release_certified=True,
        archive_name="full-x7-certification-bundle.zip",
        include_paths=[report_path],
    )
    write_json(futures, {**report, "bundle": bundle})
    score_evidence, score_identity = _scorecard_inputs(tmp_path / "native-score")

    with pytest.raises(SpecValidationError, match="different strategy, wheel"):
        combine_full_x7_release(
            spot_certificate_path=spot,
            futures_certificate_path=futures,
            platform_evidence_paths=[],
            output_directory=tmp_path / "release",
            native_score_evidence_path=score_evidence,
            native_score_identity_path=score_identity,
            provenance_policy=TEST_POLICY,
        )


def test_combined_release_rejects_platform_base_strategy_mismatch(
    tmp_path: Path,
) -> None:
    score_evidence, score_identity = _scorecard_inputs(tmp_path / "native-score")
    with pytest.raises(SpecValidationError, match="incomplete"):
        combine_full_x7_release(
            spot_certificate_path=_certificate(tmp_path, "binance-spot"),
            futures_certificate_path=_certificate(
                tmp_path,
                "binance-usdtm-isolated",
            ),
            platform_evidence_paths=[
                _platform_evidence(
                    tmp_path,
                    "binance-spot",
                    base_strategy_sha256="0" * 64,
                ),
                _platform_evidence(
                    tmp_path,
                    "binance-usdtm-isolated",
                    base_strategy_sha256="0" * 64,
                ),
            ],
            output_directory=tmp_path / "release",
            native_score_evidence_path=score_evidence,
            native_score_identity_path=score_identity,
            provenance_policy=TEST_POLICY,
        )


def _combined_gate_inputs(tmp_path: Path) -> dict[str, Any]:
    if not DURABLE_LEDGER_AVAILABLE:
        pytest.skip("requires the durable publication ledger platform contract")
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    wheel_names = (
        "nfi_backtest_engine-1.0.0-cp312-cp312-manylinux_2_17_x86_64.whl",
        "nfi_backtest_engine-1.0.0-cp312-cp312-manylinux_2_17_aarch64.whl",
        "nfi_backtest_engine-1.0.0-cp312-cp312-win_amd64.whl",
        "nfi_backtest_engine-1.0.0-cp312-cp312-macosx_11_0_arm64.whl",
    )
    for index, name in enumerate(wheel_names):
        (candidate / name).write_bytes(f"wheel-{index}".encode())
    (candidate / "nfi_backtest_engine-1.0.0.tar.gz").write_bytes(b"sdist")
    linux_wheel_sha256 = sha256_file(candidate / wheel_names[0])
    candidate_id = candidate_distribution_identity(
        {
            path.name: sha256_file(path)
            for path in candidate.iterdir()
            if path.name.endswith(".whl") or path.name.endswith(".tar.gz")
        }
    )
    platform_wheel_sha256 = {
        "linux": linux_wheel_sha256,
        "windows": sha256_file(candidate / wheel_names[2]),
        "darwin": sha256_file(candidate / wheel_names[3]),
    }

    platform_paths: dict[str, Path] = {}
    for slug, mode in (
        ("spot", "binance-spot"),
        ("futures", "binance-usdtm-isolated"),
    ):
        source = _platform_evidence(
            tmp_path,
            mode,
            wheel_sha256=linux_wheel_sha256,
            platform_wheel_sha256=platform_wheel_sha256,
            candidate_id=candidate_id,
        ).parent
        destination = candidate / "platform" / slug
        shutil.copytree(source, destination)
        platform_paths[mode] = destination / "platform-evidence.json"

    candidate_assets = sorted(
        (path for path in candidate.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(candidate).as_posix(),
    )
    (candidate / "SHA256SUMS.txt").write_text(
        "".join(
            f"{sha256_file(path)}  {path.relative_to(candidate).as_posix()}\n"
            for path in candidate_assets
        ),
        encoding="utf-8",
    )

    spot = _certificate(
        tmp_path,
        "binance-spot",
        wheel_sha256=linux_wheel_sha256,
    )
    futures = _certificate(
        tmp_path,
        "binance-usdtm-isolated",
        wheel_sha256=linux_wheel_sha256,
    )
    combined_directory = tmp_path / "combined"
    score_evidence, score_identity = _scorecard_inputs(
        tmp_path / "native-score", engine_artifact_sha256=candidate_id
    )
    combine_full_x7_release(
        spot_certificate_path=spot,
        futures_certificate_path=futures,
        platform_evidence_paths=list(platform_paths.values()),
        output_directory=combined_directory,
        native_score_evidence_path=score_evidence,
        native_score_identity_path=score_identity,
        provenance_policy=TEST_POLICY,
    )
    ledger_parent = tmp_path / "publication-ledger"
    ledger_parent.mkdir(mode=0o700)
    return {
        "candidate_directory": candidate,
        "combined_release_result_path": (combined_directory / "full-x7-release-result.json"),
        "candidate_commit": "1" * 40,
        "output_directory": tmp_path / "public",
        "provenance_policy": TEST_POLICY,
        "provenance_ledger_path": ledger_parent / "used.sqlite",
        "publication_attempt_id": "test-run-1",
        "native_score_evidence_path": score_evidence,
        "native_score_identity_path": score_identity,
    }


def _reseal_public_checksums(root: Path) -> None:
    assets = sorted(
        (path for path in root.iterdir() if path.is_file() and path.name != RELEASE_CHECKSUMS_NAME),
        key=lambda path: path.name,
    )
    (root / RELEASE_CHECKSUMS_NAME).write_text(
        "".join(f"{sha256_file(path)}  {path.name}\n" for path in assets),
        encoding="utf-8",
    )


def test_combined_release_gate_seals_exact_public_asset_set(tmp_path: Path) -> None:
    inputs = _combined_gate_inputs(tmp_path)

    result = seal_combined_release_candidate(**inputs)
    release = Path(inputs["output_directory"])
    finalize_combined_release_publication(
        release,
        provenance_ledger_path=inputs["provenance_ledger_path"],
        publication_attempt_id=inputs["publication_attempt_id"],
        expected_commit=inputs["candidate_commit"],
        provenance_policy=TEST_POLICY,
    )
    verified = verify_combined_release_candidate(
        release,
        expected_commit=str(inputs["candidate_commit"]),
        provenance_policy=TEST_POLICY,
        provenance_ledger_path=inputs["provenance_ledger_path"],
        native_score_evidence_path=inputs["native_score_evidence_path"],
        native_score_identity_path=inputs["native_score_identity_path"],
    )

    assert verified == result
    assert len(list(release.iterdir())) == PUBLIC_RELEASE_ASSET_COUNT
    assert len(result["distributions"]) == 5
    assert result["candidate_manifest"]["candidate_file"] == "SHA256SUMS.txt"
    assert result["candidate_manifest"]["sha256"] != sha256_file(
        release / "SHA256SUMS.txt"
    )
    assert set(result["platform_evidence"]) == {
        "binance-spot",
        "binance-usdtm-isolated",
    }
    assert all(
        record["candidate_file"].startswith("platform/")
        for record in result["platform_evidence"].values()
    )


def test_deferred_publication_stays_reserved_until_remote_finalize(
    tmp_path: Path,
) -> None:
    inputs = _combined_gate_inputs(tmp_path)
    seal_combined_release_candidate(**inputs)
    public = Path(inputs["output_directory"])
    with pytest.raises(SpecValidationError, match="published claim"):
        verify_combined_release_candidate(
            public,
            expected_commit=str(inputs["candidate_commit"]),
            provenance_policy=TEST_POLICY,
            provenance_ledger_path=inputs["provenance_ledger_path"],
            native_score_evidence_path=inputs["native_score_evidence_path"],
            native_score_identity_path=inputs["native_score_identity_path"],
        )
    with closing(sqlite3.connect(inputs["provenance_ledger_path"])) as connection:
        assert connection.execute(
            "SELECT state FROM certificate_publications"
        ).fetchone() == ("reserved",)

    finalize_combined_release_publication(
        public,
        provenance_ledger_path=inputs["provenance_ledger_path"],
        publication_attempt_id=inputs["publication_attempt_id"],
        expected_commit=inputs["candidate_commit"],
        provenance_policy=TEST_POLICY,
    )
    verify_combined_release_candidate(
        public,
        expected_commit=str(inputs["candidate_commit"]),
        provenance_policy=TEST_POLICY,
        provenance_ledger_path=inputs["provenance_ledger_path"],
        native_score_evidence_path=inputs["native_score_evidence_path"],
        native_score_identity_path=inputs["native_score_identity_path"],
    )
    finalize_combined_release_publication(
        public,
        provenance_ledger_path=inputs["provenance_ledger_path"],
        publication_attempt_id=inputs["publication_attempt_id"],
        expected_commit=inputs["candidate_commit"],
        provenance_policy=TEST_POLICY,
    )


def test_combined_release_publication_ledger_rejects_cross_bundle_replay(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "durable-used-certificates.sqlite"
    (tmp_path / "first").mkdir()
    first = _combined_gate_inputs(tmp_path / "first")
    first["provenance_ledger_path"] = ledger
    seal_combined_release_candidate(**first)

    (tmp_path / "replay").mkdir()
    replay = _combined_gate_inputs(tmp_path / "replay")
    replay["provenance_ledger_path"] = ledger
    with pytest.raises(SpecValidationError, match="already used"):
        seal_combined_release_candidate(**replay)
    assert not Path(replay["output_directory"]).exists()


@dataclass(frozen=True)
class _PublisherOutcome:
    attempt_id: str
    status: Literal["winner", "replay-rejected", "security-rejected", "unexpected"]
    detail: str


@pytest.mark.parametrize("delayed_index", [None, 0, 7])
def test_eight_concurrent_publishers_expose_exactly_one_complete_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delayed_index: int | None,
) -> None:
    ledger_parent = tmp_path / "durable"
    ledger_parent.mkdir(mode=0o700)
    ledger = ledger_parent / "used.sqlite"
    attempts = []
    for index in range(8):
        root = tmp_path / f"attempt-{index}"
        root.mkdir()
        inputs = _combined_gate_inputs(root)
        inputs["provenance_ledger_path"] = ledger
        inputs["publication_attempt_id"] = f"run-{index:02d}"
        attempts.append(inputs)

    start = threading.Barrier(8)
    release_delayed = threading.Event()
    local = threading.local()
    transition_lock = threading.Lock()
    transitions: list[tuple[str, str]] = []

    def checkpoint(name: str) -> None:
        if name != "before-reservation":
            return
        start.wait(timeout=10)
        if (
            delayed_index is not None
            and local.index == delayed_index
            and not release_delayed.wait(timeout=10)
        ):
            raise RuntimeError("publisher schedule was not released")

    def ledger_transition(
        _bundle_id: str,
        attempt_id: str,
        _previous: str | None,
        current: str,
    ) -> None:
        with transition_lock:
            transitions.append((attempt_id, current))
        release_delayed.set()

    monkeypatch.setattr(combined_release, "_publication_checkpoint", checkpoint)
    monkeypatch.setattr(
        release_provenance,
        "_ledger_transition_checkpoint",
        ledger_transition,
    )

    def publish(index_and_inputs: tuple[int, dict[str, Any]]) -> _PublisherOutcome:
        index, inputs = index_and_inputs
        local.index = index
        attempt_id = str(inputs["publication_attempt_id"])
        try:
            seal_combined_release_candidate(**inputs)
            output = Path(inputs["output_directory"])
            backend = _FakeRemoteDraft()

            def finalize() -> None:
                finalize_combined_release_publication(
                    output,
                    provenance_ledger_path=inputs["provenance_ledger_path"],
                    publication_attempt_id=attempt_id,
                    expected_commit=str(inputs["candidate_commit"]),
                    provenance_policy=TEST_POLICY,
                )

            combined_release.publish_remote_draft_release(
                backend,
                finalize=finalize,
                abort=lambda: None,
            )
            verify_combined_release_candidate(
                output,
                expected_commit=str(inputs["candidate_commit"]),
                provenance_policy=TEST_POLICY,
                provenance_ledger_path=inputs["provenance_ledger_path"],
                native_score_evidence_path=inputs["native_score_evidence_path"],
                native_score_identity_path=inputs["native_score_identity_path"],
            )
            return _PublisherOutcome(attempt_id, "winner", "remote verified and published")
        except SpecValidationError as exc:
            detail = str(exc)
            status = (
                "replay-rejected"
                if "already used by another publication" in detail
                else "security-rejected"
            )
            return _PublisherOutcome(attempt_id, status, detail)
        except Exception as exc:  # pragma: no cover - asserted as an unexpected result
            return _PublisherOutcome(attempt_id, "unexpected", repr(exc))

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(publish, enumerate(attempts)))

    assert [outcome.status for outcome in outcomes].count("winner") == 1, outcomes
    assert [outcome.status for outcome in outcomes].count("replay-rejected") == 7, outcomes
    assert not [
        outcome
        for outcome in outcomes
        if outcome.status in {"security-rejected", "unexpected"}
    ]
    winner = next(outcome for outcome in outcomes if outcome.status == "winner")
    assert transitions == [
        (winner.attempt_id, "reserved"),
        (winner.attempt_id, "published"),
    ]
    published = [
        Path(inputs["output_directory"])
        for inputs in attempts
        if Path(inputs["output_directory"]).exists()
    ]
    assert len(published) == 1
    assert len(list(published[0].iterdir())) == PUBLIC_RELEASE_ASSET_COUNT
    loser_outputs = [
        Path(inputs["output_directory"])
        for inputs in attempts
        if str(inputs["publication_attempt_id"]) != winner.attempt_id
    ]
    assert all(not output.exists() for output in loser_outputs)


@pytest.mark.parametrize(
    "checkpoint,public_exists,interrupted_state",
    [
        ("before-reservation", False, None),
        ("after-reservation", False, "aborted"),
        ("during-staged-publication", False, "aborted"),
        ("after-publication-before-finalize", True, "reserved"),
    ],
)
def test_interrupted_publication_same_attempt_recovers_without_partial_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checkpoint: str,
    public_exists: bool,
    interrupted_state: str | None,
) -> None:
    ledger_parent = tmp_path / "durable"
    ledger_parent.mkdir(mode=0o700)
    inputs = _combined_gate_inputs(tmp_path)
    inputs["provenance_ledger_path"] = ledger_parent / "used.sqlite"
    inputs["publication_attempt_id"] = "run-recovery"

    def interrupt(current_checkpoint: str) -> None:
        if current_checkpoint == checkpoint:
            raise KeyboardInterrupt

    monkeypatch.setattr(combined_release, "_publication_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        seal_combined_release_candidate(**inputs)
    public = Path(inputs["output_directory"])
    assert public.exists() is public_exists
    ledger = Path(inputs["provenance_ledger_path"])
    if interrupted_state is None:
        assert not ledger.exists()
    else:
        with closing(sqlite3.connect(ledger)) as connection:
            state = connection.execute(
                "SELECT state FROM certificate_publications"
            ).fetchone()
        assert state == (interrupted_state,)
    if public_exists:
        assert len(list(public.iterdir())) == PUBLIC_RELEASE_ASSET_COUNT
        with pytest.raises(SpecValidationError, match="published claim"):
            verify_combined_release_candidate(
                public,
                expected_commit=str(inputs["candidate_commit"]),
                provenance_policy=TEST_POLICY,
                provenance_ledger_path=inputs["provenance_ledger_path"],
                native_score_evidence_path=inputs["native_score_evidence_path"],
                native_score_identity_path=inputs["native_score_identity_path"],
            )
        replay = {**inputs, "publication_attempt_id": "different-run"}
        with pytest.raises(SpecValidationError, match="already used"):
            seal_combined_release_candidate(**replay)

    monkeypatch.setattr(combined_release, "_publication_checkpoint", lambda _name: None)
    seal_combined_release_candidate(**inputs)
    assert len(list(public.iterdir())) == PUBLIC_RELEASE_ASSET_COUNT
    finalize_combined_release_publication(
        public,
        provenance_ledger_path=inputs["provenance_ledger_path"],
        publication_attempt_id=inputs["publication_attempt_id"],
        expected_commit=inputs["candidate_commit"],
        provenance_policy=TEST_POLICY,
    )
    verify_combined_release_candidate(
        public,
        expected_commit=str(inputs["candidate_commit"]),
        provenance_policy=TEST_POLICY,
        provenance_ledger_path=inputs["provenance_ledger_path"],
        native_score_evidence_path=inputs["native_score_evidence_path"],
        native_score_identity_path=inputs["native_score_identity_path"],
    )
    with closing(sqlite3.connect(ledger)) as connection:
        state = connection.execute(
            "SELECT state FROM certificate_publications"
        ).fetchone()
    assert state == ("published",)


class _FakeRemoteDraft:
    def __init__(self, *, fail: str | None = None) -> None:
        self.release_state: Literal["absent", "draft", "public"] = "absent"
        self.fail = fail
        self.calls: list[str] = []

    def state(self) -> Literal["absent", "draft", "public"]:
        return self.release_state

    def create_draft(self) -> None:
        self.calls.append("create")
        self.release_state = "draft"
        if self.fail == "create":
            raise RuntimeError("create failed")

    def upload_assets(self) -> None:
        self.calls.append("upload")
        if self.fail == "upload":
            raise RuntimeError("upload failed")

    def verify_assets(self, *, public: bool) -> None:
        phase = "verify-public" if public else "verify-draft"
        self.calls.append(phase)
        if self.fail == phase:
            raise RuntimeError(f"{phase} failed")

    def publish_draft(self) -> None:
        self.calls.append("publish")
        if self.fail == "publish":
            raise RuntimeError("publish failed")
        self.release_state = "public"

    def delete_draft(self) -> None:
        self.calls.append("delete")
        self.release_state = "absent"


def test_remote_draft_publication_finalizes_only_after_public_verification() -> None:
    backend = _FakeRemoteDraft()
    events: list[str] = []
    combined_release.publish_remote_draft_release(
        backend,
        finalize=lambda: events.append("finalize"),
        abort=lambda: events.append("abort"),
    )

    assert backend.calls == [
        "create", "upload", "verify-draft", "publish", "verify-public"
    ]
    assert backend.release_state == "public"
    assert events == ["finalize"]


@pytest.mark.parametrize("failure", ["create", "upload", "verify-draft", "publish"])
def test_remote_draft_failure_aborts_without_public_release(failure: str) -> None:
    backend = _FakeRemoteDraft(fail=failure)
    events: list[str] = []
    with pytest.raises(RuntimeError, match="failed"):
        combined_release.publish_remote_draft_release(
            backend,
            finalize=lambda: events.append("finalize"),
            abort=lambda: events.append("abort"),
        )

    assert backend.release_state == "absent"
    assert events == ["abort"]


@pytest.mark.parametrize("checkpoint", ["after-create", "after-upload", "after-draft-verify"])
def test_remote_private_checkpoint_failure_aborts(checkpoint: str) -> None:
    backend = _FakeRemoteDraft()
    events: list[str] = []

    def interrupt(name: str) -> None:
        if name == checkpoint:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        combined_release.publish_remote_draft_release(
            backend,
            finalize=lambda: events.append("finalize"),
            abort=lambda: events.append("abort"),
            checkpoint=interrupt,
        )
    assert backend.release_state == "absent"
    assert events == ["abort"]


@pytest.mark.parametrize("checkpoint", ["after-publish", "after-public-verify", "before-finalize"])
def test_remote_public_crash_requires_same_attempt_recovery(checkpoint: str) -> None:
    backend = _FakeRemoteDraft()
    events: list[str] = []

    def interrupt(name: str) -> None:
        if name == checkpoint:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        combined_release.publish_remote_draft_release(
            backend,
            finalize=lambda: events.append("finalize"),
            abort=lambda: events.append("abort"),
            checkpoint=interrupt,
        )
    assert backend.release_state == "public"
    assert events == []

    combined_release.publish_remote_draft_release(
        backend,
        finalize=lambda: events.append("finalize"),
        abort=lambda: events.append("abort"),
    )
    assert events == ["finalize"]
    assert backend.calls[-1] == "verify-public"


def test_combined_release_gate_rejects_preview_result(tmp_path: Path) -> None:
    inputs = _combined_gate_inputs(tmp_path)
    result_path = Path(inputs["combined_release_result_path"])
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["status"] = "preview"
    result["release_certified"] = False
    write_json(result_path, result)

    with pytest.raises(SpecValidationError, match="preview"):
        seal_combined_release_candidate(**inputs)


def test_combined_release_gate_rejects_incomplete_distribution_set(
    tmp_path: Path,
) -> None:
    inputs = _combined_gate_inputs(tmp_path)
    candidate = Path(inputs["candidate_directory"])
    next(candidate.glob("*win_amd64.whl")).unlink()

    with pytest.raises(SpecValidationError, match="SHA256SUMS"):
        seal_combined_release_candidate(**inputs)


def test_combined_release_verifier_rejects_resealed_report_tamper(
    tmp_path: Path,
) -> None:
    inputs = _combined_gate_inputs(tmp_path)
    seal_combined_release_candidate(**inputs)
    release = Path(inputs["output_directory"])
    report_path = release / "full-x7-release.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["shared_candidate"]["strategy_sha256"] = "0" * 64
    write_json(report_path, report)
    _reseal_public_checksums(release)

    with pytest.raises(SpecValidationError, match="report differs"):
        verify_combined_release_candidate(
            release,
            expected_commit=str(inputs["candidate_commit"]),
            provenance_policy=TEST_POLICY,
            provenance_ledger_path=inputs["provenance_ledger_path"],
            native_score_evidence_path=inputs["native_score_evidence_path"],
            native_score_identity_path=inputs["native_score_identity_path"],
        )


def test_combined_release_verifier_rejects_resealed_distribution_rename(
    tmp_path: Path,
) -> None:
    inputs = _combined_gate_inputs(tmp_path)
    seal_combined_release_candidate(**inputs)
    release = Path(inputs["output_directory"])
    wheel = next(release.glob("*win_amd64.whl"))
    renamed = wheel.with_name("renamed-distribution.bin")
    wheel.rename(renamed)

    distribution_manifest = release / "SHA256SUMS.txt"
    distribution_manifest.write_text(
        distribution_manifest.read_text(encoding="utf-8").replace(
            wheel.name,
            renamed.name,
        ),
        encoding="utf-8",
    )
    gate_path = release / "release-gate.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    for record in gate["distributions"]:
        if record["file"] == wheel.name:
            record["file"] = renamed.name
            break
    write_json(gate_path, gate)
    _reseal_public_checksums(release)

    with pytest.raises(
        SpecValidationError,
        match="exactly four|candidate package identity differs",
    ):
        verify_combined_release_candidate(
            release,
            expected_commit=str(inputs["candidate_commit"]),
            provenance_policy=TEST_POLICY,
            provenance_ledger_path=inputs["provenance_ledger_path"],
            native_score_evidence_path=inputs["native_score_evidence_path"],
            native_score_identity_path=inputs["native_score_identity_path"],
        )
