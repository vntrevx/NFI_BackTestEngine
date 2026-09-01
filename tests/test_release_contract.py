from __future__ import annotations

import importlib.util
import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path
from threading import Event
from tomllib import load
from types import ModuleType
from typing import Any

import pytest
from nfi_backtest_engine import native_scorecard, release_gate
from nfi_backtest_engine.canonical import write_json
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.fixture import sha256_file
from nfi_backtest_engine.platform_benchmark import (
    EXACT_FIXTURE_LANE,
    LEGACY_REQUIRED_PLATFORM_SYSTEMS,
    seal_platform_evidence,
)
from nfi_backtest_engine.release_contract import (
    FUTURES_RELEASE_CONTRACT,
    SPOT_RELEASE_CONTRACT,
    release_contract_for_config,
    release_contract_for_scope,
)
from nfi_backtest_engine.release_gate import (
    RELEASE_CHECKSUMS_NAME,
    seal_release_gate,
    verify_release_gate,
)
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


def _config(
    *,
    trading_mode: str,
    margin_mode: str | None,
    pair: str,
) -> dict:
    return {
        "trading_mode": trading_mode,
        "margin_mode": margin_mode,
        "stake_currency": "USDT",
        "exchange": {
            "name": "binance",
            "pair_whitelist": [pair],
        },
    }


def test_release_contract_is_derived_from_the_effective_config() -> None:
    spot = release_contract_for_config(
        _config(trading_mode="spot", margin_mode="", pair="BTC/USDT")
    )
    futures = release_contract_for_config(
        _config(
            trading_mode="futures",
            margin_mode="isolated",
            pair="BTC/USDT:USDT",
        )
    )

    assert spot is SPOT_RELEASE_CONTRACT
    assert futures is FUTURES_RELEASE_CONTRACT
    assert futures.required_data_roles == ("candles", "funding_rate", "mark")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("margin_mode", "cross", "isolated margin"),
        ("pair", "BTC/USDT", "invalid form"),
        ("exchange", "bybit", "requires exchange"),
        ("stake_currency", "USDC", "requires stake currency"),
    ],
)
def test_futures_release_contract_rejects_out_of_scope_configs(
    field: str,
    value: str,
    message: str,
) -> None:
    config = _config(
        trading_mode="futures",
        margin_mode="isolated",
        pair="BTC/USDT:USDT",
    )
    if field == "pair":
        config["exchange"]["pair_whitelist"] = [value]
    elif field == "exchange":
        config["exchange"]["name"] = value
    else:
        config[field] = value

    with pytest.raises(SpecValidationError, match=message):
        release_contract_for_config(config)


def test_release_scope_must_exactly_match_its_mode_contract() -> None:
    scope = FUTURES_RELEASE_CONTRACT.scope_fields()
    assert release_contract_for_scope(scope) is FUTURES_RELEASE_CONTRACT

    scope["margin_mode"] = "cross"
    with pytest.raises(SpecValidationError, match="contradicts"):
        release_contract_for_scope(scope)


def test_futures_release_contract_requires_real_lifecycle_evidence() -> None:
    requirements = {
        requirement.probe_kind: requirement
        for requirement in FUTURES_RELEASE_CONTRACT.probe_evidence
    }
    lifecycle = requirements["futures-lifecycle"]

    assert "tag-121" in FUTURES_RELEASE_CONTRACT.required_probe_kinds
    assert lifecycle.missing_from(
        {
            "sides": ["long"],
            "funded_trades": 0,
        }
    ) == ["sides:short", "funded_trades:0<1"]
    assert (
        lifecycle.missing_from(
            {
                "sides": ["long", "short"],
                "funded_trades": 1,
            }
        )
        == []
    )


PORTABLE_PACKAGE_SHA = "b" * 64
RELEASE_COMMIT = "c" * 40


def _release_certificate(
    path: Path,
    *,
    wheel_sha256: str,
    release_certified: bool = True,
) -> dict:
    status = "certified" if release_certified else "failed"
    report = {
        "schema_version": "2.0.0",
        "created_at": "2026-07-28T00:00:00Z",
        "status": status,
        "release_certified": release_certified,
        "claim_scope": {
            "strategy": "NostalgiaForInfinityX7",
            "upstream_commit": "d" * 40,
            "mode_contract": "binance-usdtm-isolated",
            "trading_mode": "futures",
            "margin_mode": "isolated",
            "exchange": "binance",
            "settlement_currency": "USDT",
            "required_data_roles": ["candles", "funding_rate", "mark"],
            "timerange": "20210101-20260101",
            "pair_count": 80,
            "timeframes": ["5m", "15m", "1h", "4h", "1d"],
            "history_coverage_policy": "strict",
            "continuous_timerange": True,
            "evidence": "continuous-oracle-plus-official-full-state-probes",
        },
        "inputs": {
            "release_lock": {"sha256": "e" * 64},
            "mode_contract": "binance-usdtm-isolated",
            "reference": {
                "version": "2026.5.1",
                "image_platform_digest": "sha256:" + "f" * 64,
            },
            "strategy_sha256": "1" * 64,
            "config_sha256": "2" * 64,
            "data_aggregate_sha256": "3" * 64,
            "engine_market_snapshot_sha256": "4" * 64,
            "reference_market_snapshot_sha256": "5" * 64,
        },
        "environment": {
            "hardware": {},
            "execution_profile": {},
            "package_version": "1.1.0",
            "engine_build": {"source_fingerprint": "6" * 64},
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
                "met": release_certified,
                "sha256": wheel_sha256,
                "native_member_sha256": "7" * 64,
                "installed_extension_equal": release_certified,
                "portable_package_sha256": PORTABLE_PACKAGE_SHA,
            },
            "native_pipeline": {"met": release_certified},
            "official_parity": {"met": release_certified},
            "determinism": {"met": release_certified},
            "speed": {"met": release_certified},
            "memory": {"met": release_certified},
            "state_probes": {"met": release_certified},
        },
    }
    write_json(path, report)
    return report


def _write_certificate_evidence(
    path: Path,
    certificate_path: Path,
) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.write(
            certificate_path,
            arcname="evidence/full-x7-certification.json",
        )


def _release_gate_inputs(tmp_path: Path) -> dict[str, Any]:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    wheel = candidate / ("nfi_backtest_engine-1.1.0-cp312-cp312-manylinux_2_17_x86_64.whl")
    wheel.write_bytes(b"sealed candidate wheel")
    (candidate / "nfi_backtest_engine-1.1.0.tar.gz").write_bytes(b"sealed source distribution")
    wheel_sha256 = sha256_file(wheel)
    candidate_id = candidate_distribution_identity(
        {
            path.name: sha256_file(path)
            for path in candidate.iterdir()
            if path.name.endswith(".whl") or path.name.endswith(".tar.gz")
        }
    )
    report_root = tmp_path / "platform-reports"
    report_root.mkdir()
    report_paths: list[str | Path] = []
    for _run_id, (system, machine, digest) in enumerate(
        (("windows", "amd64", "a"), ("linux", "x86_64", "0"), ("darwin", "arm64", "f")),
        start=1,
    ):
        report_path = report_root / f"{system}.json"
        write_json(
            report_path,
            {
                "schema_version": "1.2.0",
                "complete": True,
                "lane": EXACT_FIXTURE_LANE,
                "platform": {"system": system, "machine": machine, "wsl": False},
                "package": {
                    "version": "1.1.0",
                    "wheel_sha256": wheel_sha256 if system == "linux" else digest * 64,
                    "native_extension_sha256": "7" * 64 if system == "linux" else digest * 64,
                    "installed_extension_equal": True,
                    "portable_package_sha256": PORTABLE_PACKAGE_SHA,
                },
                "workload": {
                    "lane": EXACT_FIXTURE_LANE,
                    "mode_contract": "binance-usdtm-isolated",
                    "fixture_id": "x7-futures",
                    "manifest_sha256": "8" * 64,
                    "strategy_sha256": "1" * 64,
                    "base_strategy_sha256": "1" * 64,
                    "verification_level": "full",
                    "identity_sha256": "9" * 64,
                },
                "measurement": {
                    "result_sha256": ["9" * 64],
                    "wall_time_seconds": {"median": 1.0},
                    "peak_rss_bytes": {"maximum": 1000},
                    "measured_repetitions": 3,
                },
            },
        )
        sign_report(
            report_path,
            run_id=1,
            commit=RELEASE_COMMIT,
            candidate_id=candidate_id,
        )
        report_paths.append(report_path)
    sealed_platform = tmp_path / "sealed-platform"
    seal_platform_evidence(
        report_paths,
        sealed_platform,
        provenance_policy=TEST_POLICY,
        required_platform_systems=LEGACY_REQUIRED_PLATFORM_SYSTEMS,
    )
    platform = candidate / "full-x7-futures-platform-evidence.json"
    platform.write_bytes((sealed_platform / "platform-evidence.json").read_bytes())
    candidate_assets = sorted(candidate.iterdir(), key=lambda item: item.name)
    (candidate / "SHA256SUMS.txt").write_text(
        "".join(f"{sha256_file(asset)}  {asset.name}\n" for asset in candidate_assets),
        encoding="utf-8",
    )
    certificate = tmp_path / "full-x7-certification.json"
    _release_certificate(certificate, wheel_sha256=wheel_sha256)
    certificate_evidence = tmp_path / "full-x7-certification-bundle.zip"
    _write_certificate_evidence(certificate_evidence, certificate)
    score_evidence, score_identity = _scorecard_inputs(
        tmp_path / "native-score",
        engine_artifact_sha256=candidate_id,
        candidate_commit=RELEASE_COMMIT,
    )
    return {
        "candidate_directory": candidate,
        "certificate_path": certificate,
        "certificate_evidence_path": certificate_evidence,
        "platform_evidence_path": platform,
        "candidate_commit": RELEASE_COMMIT,
        "output_directory": tmp_path / "release",
        "provenance_policy": TEST_POLICY,
        "native_score_evidence_path": score_evidence,
        "native_score_identity_path": score_identity,
    }


def _reseal_candidate_manifest(candidate: Path) -> None:
    assets = sorted(
        (path for path in candidate.rglob("*") if path.is_file() and path.name != "SHA256SUMS.txt"),
        key=lambda item: item.relative_to(candidate).as_posix(),
    )
    (candidate / "SHA256SUMS.txt").write_text(
        "".join(
            f"{sha256_file(asset)}  {asset.relative_to(candidate).as_posix()}\n" for asset in assets
        ),
        encoding="utf-8",
    )


def test_release_gate_binds_certificate_and_complete_asset_manifest(
    tmp_path: Path,
) -> None:
    inputs = _release_gate_inputs(tmp_path)
    candidate = Path(inputs["candidate_directory"])
    candidate_manifest_sha = sha256_file(candidate / "SHA256SUMS.txt")

    result = seal_release_gate(**inputs)
    release = Path(inputs["output_directory"])
    verified = verify_release_gate(
        release,
        expected_commit=RELEASE_COMMIT,
        provenance_policy=TEST_POLICY,
    )
    checksums = {
        line.partition("  ")[2]
        for line in (release / RELEASE_CHECKSUMS_NAME).read_text(encoding="utf-8").splitlines()
    }

    assert result == verified
    assert result["certificate"]["wheel_sha256"] == sha256_file(next(candidate.glob("*.whl")))
    assert sha256_file(release / "SHA256SUMS.txt") == candidate_manifest_sha
    assert checksums == {
        path.name for path in release.iterdir() if path.name != RELEASE_CHECKSUMS_NAME
    }


def _publication_inputs(
    root: Path,
    *,
    ledger: Path,
    attempt: str,
) -> dict[str, Any]:
    root.mkdir()
    inputs = _release_gate_inputs(root)
    inputs["provenance_ledger_path"] = ledger
    inputs["publication_attempt_id"] = attempt
    return inputs


def test_release_gate_durable_first_winner_publishes_exact_asset_set(
    tmp_path: Path,
) -> None:
    ledger_parent = tmp_path / "durable"
    ledger_parent.mkdir(mode=0o700)
    inputs = _publication_inputs(
        tmp_path / "winner",
        ledger=ledger_parent / "ledger.sqlite",
        attempt="run-winner",
    )

    seal_release_gate(**inputs)

    output = Path(inputs["output_directory"])
    assert len([path for path in output.rglob("*") if path.is_file()]) == 8
    with closing(sqlite3.connect(inputs["provenance_ledger_path"])) as connection:
        assert connection.execute(
            "SELECT attempt_id, state FROM certificate_publications"
        ).fetchone() == ("run-winner", "published")
    assert not list(output.parent.glob(f".{output.name}.stage-*"))


@pytest.mark.parametrize("attempt", ["run-winner", "run-loser"])
def test_release_gate_durable_replay_exposes_no_loser_output(
    tmp_path: Path,
    attempt: str,
) -> None:
    ledger_parent = tmp_path / "durable"
    ledger_parent.mkdir(mode=0o700)
    ledger = ledger_parent / "ledger.sqlite"
    winner = _publication_inputs(
        tmp_path / "winner",
        ledger=ledger,
        attempt="run-winner",
    )
    seal_release_gate(**winner)
    loser = _publication_inputs(
        tmp_path / "loser",
        ledger=ledger,
        attempt=attempt,
    )

    with pytest.raises(SpecValidationError, match="already used|already published"):
        seal_release_gate(**loser)

    output = Path(loser["output_directory"])
    assert not output.exists()
    assert not list(output.parent.glob(f".{output.name}.stage-*"))


@pytest.mark.parametrize(
    ("checkpoint", "visible_after_interrupt", "interrupted_state"),
    [
        ("after-reservation", False, "aborted"),
        ("after-publication-before-finalize", True, "reserved"),
    ],
)
def test_release_gate_same_owner_recovers_exact_interruption_without_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checkpoint: str,
    visible_after_interrupt: bool,
    interrupted_state: str,
) -> None:
    ledger_parent = tmp_path / "durable"
    ledger_parent.mkdir(mode=0o700)
    inputs = _publication_inputs(
        tmp_path / "winner",
        ledger=ledger_parent / "ledger.sqlite",
        attempt="run-recovery",
    )
    reached = Event()

    def interrupt(name: str) -> None:
        if name == checkpoint:
            reached.set()
            raise KeyboardInterrupt

    monkeypatch.setattr(release_gate, "_publication_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        seal_release_gate(**inputs)
    assert reached.is_set()

    output = Path(inputs["output_directory"])
    assert output.exists() is visible_after_interrupt
    assert not list(output.parent.glob(f".{output.name}.stage-*"))
    with closing(sqlite3.connect(inputs["provenance_ledger_path"])) as connection:
        assert connection.execute(
            "SELECT state FROM certificate_publications"
        ).fetchone() == (interrupted_state,)

    monkeypatch.setattr(release_gate, "_publication_checkpoint", lambda _name: None)
    seal_release_gate(**inputs)
    assert len([path for path in output.rglob("*") if path.is_file()]) == 8
    with closing(sqlite3.connect(inputs["provenance_ledger_path"])) as connection:
        assert connection.execute(
            "SELECT state FROM certificate_publications"
        ).fetchone() == ("published",)


def test_release_gate_rejects_mismatched_candidate_wheel(
    tmp_path: Path,
) -> None:
    inputs = _release_gate_inputs(tmp_path)
    certificate = Path(inputs["certificate_path"])
    _release_certificate(certificate, wheel_sha256="0" * 64)
    _write_certificate_evidence(
        Path(inputs["certificate_evidence_path"]),
        certificate,
    )

    with pytest.raises(SpecValidationError, match="wheel SHA"):
        seal_release_gate(**inputs)

    assert not Path(inputs["output_directory"]).exists()


def test_release_gate_preserves_nested_candidate_evidence(tmp_path: Path) -> None:
    inputs = _release_gate_inputs(tmp_path)
    candidate = Path(inputs["candidate_directory"])
    platform = Path(inputs["platform_evidence_path"])
    nested = candidate / "platform" / "futures" / platform.name
    nested.parent.mkdir(parents=True)
    platform.rename(nested)
    inputs["platform_evidence_path"] = nested
    _reseal_candidate_manifest(candidate)

    result = seal_release_gate(**inputs)
    release = Path(inputs["output_directory"])

    assert result["platform_evidence"]["file"] == (
        "platform/futures/full-x7-futures-platform-evidence.json"
    )
    assert (release / result["platform_evidence"]["file"]).is_file()


def test_release_gate_rejects_checksum_path_traversal(tmp_path: Path) -> None:
    inputs = _release_gate_inputs(tmp_path)
    candidate = Path(inputs["candidate_directory"])
    manifest = candidate / "SHA256SUMS.txt"
    manifest.write_text(
        f"{'0' * 64}  ../outside.whl\n",
        encoding="utf-8",
    )

    with pytest.raises(SpecValidationError, match="invalid checksum manifest"):
        seal_release_gate(**inputs)


def test_release_gate_rejects_missing_host_certificate(tmp_path: Path) -> None:
    inputs = _release_gate_inputs(tmp_path)
    Path(inputs["certificate_path"]).unlink()

    with pytest.raises(SpecValidationError, match="host certificate does not exist"):
        seal_release_gate(**inputs)

    assert not Path(inputs["output_directory"]).exists()


def test_release_gate_rejects_semantically_failed_certificate_with_true_gates(
    tmp_path: Path,
) -> None:
    inputs = _release_gate_inputs(tmp_path)
    certificate = Path(inputs["certificate_path"])
    document = json.loads(certificate.read_text(encoding="utf-8"))
    document["status"] = "failed"
    document["release_certified"] = False
    for gate in document["gates"].values():
        gate["met"] = True
    document["gates"]["installed_wheel"]["installed_extension_equal"] = True
    write_json(certificate, document)
    _write_certificate_evidence(Path(inputs["certificate_evidence_path"]), certificate)

    with pytest.raises(SpecValidationError, match="failed|incomplete|certified"):
        seal_release_gate(**inputs)

    assert not Path(inputs["output_directory"]).exists()


@pytest.mark.parametrize(
    ("semantic_field", "semantic_value"),
    [
        ("exact", False),
        ("complete", False),
        ("certification_eligible", False),
        ("execution_mode", "unsafe_research_override"),
    ],
)
def test_release_gate_rejects_nested_semantic_failure_despite_true_gates(
    tmp_path: Path,
    semantic_field: str,
    semantic_value: object,
) -> None:
    inputs = _release_gate_inputs(tmp_path)
    certificate = Path(inputs["certificate_path"])
    document = json.loads(certificate.read_text(encoding="utf-8"))
    document["runs"]["official_reference"] = {semantic_field: semantic_value}
    write_json(certificate, document)
    _write_certificate_evidence(Path(inputs["certificate_evidence_path"]), certificate)

    with pytest.raises(SpecValidationError, match="exact|incomplete|ineligible|unsafe"):
        seal_release_gate(**inputs)

    assert not Path(inputs["output_directory"]).exists()


def test_release_gate_rejects_preview_certificate(tmp_path: Path) -> None:
    inputs = _release_gate_inputs(tmp_path)
    certificate = Path(inputs["certificate_path"])
    wheel_sha256 = sha256_file(next(Path(inputs["candidate_directory"]).glob("*.whl")))
    _release_certificate(
        certificate,
        wheel_sha256=wheel_sha256,
        release_certified=False,
    )
    _write_certificate_evidence(
        Path(inputs["certificate_evidence_path"]),
        certificate,
    )

    with pytest.raises(SpecValidationError, match="preview, failed, or incomplete"):
        seal_release_gate(**inputs)

    assert not Path(inputs["output_directory"]).exists()


def test_release_gate_rejects_portable_package_identity_drift(
    tmp_path: Path,
) -> None:
    inputs = _release_gate_inputs(tmp_path)
    platform = Path(inputs["platform_evidence_path"])
    document = json.loads(platform.read_text(encoding="utf-8"))
    document["portable_package_sha256"] = "0" * 64
    write_json(platform, document)
    _reseal_candidate_manifest(Path(inputs["candidate_directory"]))

    with pytest.raises(SpecValidationError, match="recomputed fields differ"):
        seal_release_gate(**inputs)


def test_release_workflows_enforce_certificate_and_promotion_contract() -> None:
    root = Path(__file__).parents[1]
    build = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
    publish = (root / ".github/workflows/publish-release-candidate.yml").read_text(encoding="utf-8")
    certify = (root / ".github/workflows/certify-release-candidate.yml").read_text(encoding="utf-8")
    promote = (root / ".github/workflows/promote-release.yml").read_text(encoding="utf-8")
    assert "permissions:\n  contents: read" in build
    assert "release_candidate_contract.py" in build
    assert "Measure exact Spot and Futures fixtures" in build
    assert (
        "path: .platform-evidence/*/platform-benchmark*.json\n"
        "          if-no-files-found: error\n"
        "          include-hidden-files: true"
    ) in build
    assert "Seal supported-platform Spot and Futures evidence" in build
    assert "native_score_run_id" not in build
    assert "native-score" not in build
    wheels_section = build[build.index("  wheels:"):build.index("  sdist:")]
    assert "environment: release-provenance" not in wheels_section
    assert "NFI_RELEASE_PROVENANCE_PRIVATE" not in wheels_section
    assert "sccache: true" in wheels_section
    sdist_section = build[build.index("  sdist:"):build.index("  provenance-prepare:")]
    assert "sccache: true" not in sdist_section
    assert "  provenance-prepare:" in build
    prepare_section = build[
        build.index("  provenance-prepare:"):build.index("  provenance-signing:")
    ]
    signing_section = build[
        build.index("  provenance-signing:"):build.index("  provenance-assemble:")
    ]
    assemble_section = build[
        build.index("  provenance-assemble:"):build.index("  platform-evidence:")
    ]
    assert "needs: [verify]" in prepare_section
    assert "needs: [verify, provenance-prepare]" in signing_section
    assert "runs-on: ubuntu-latest" in signing_section
    assert "environment: release-provenance" not in signing_section
    assert "NFI_RELEASE_PROVENANCE_PRIVATE_KEY" in signing_section
    assert "Prepare canonical DSSE signing inputs" in prepare_section
    assert "reports/provenance-coordinator.json" in prepare_section
    assert '--arg run_id "$GITHUB_RUN_ID"' in prepare_section
    assert '--argjson run_attempt "$GITHUB_RUN_ATTEMPT"' in prepare_section
    assert "Assemble signed provenance envelopes" in assemble_section
    assert "NFI_RELEASE_PROVENANCE_PRIVATE" not in prepare_section
    assert "NFI_RELEASE_PROVENANCE_PRIVATE" not in assemble_section
    secret_step = signing_section[
        signing_section.index("      - name: Sign canonical DSSE PAE bytes"):
        signing_section.index("      - name: Upload isolated signatures")
    ]
    assert "openssl pkeyutl -sign -rawin" in secret_step
    assert "sha256sum --check" in secret_step
    assert ".sign-venv" not in secret_step
    assert "nfi-bte" not in secret_step
    assert "python" not in secret_step
    assert "jq " not in secret_step
    assert "platform prepare-attestation" in prepare_section
    assert "platform assemble-attestation" in assemble_section
    assert "actions/checkout" not in signing_section
    assert "setup-uv" not in signing_section
    assert "find . -type f ! -name SHA256SUMS.txt" in build
    assert "name: Certify release candidate" in certify
    assert "runs-on: [self-hosted, linux, x64, nfi-certification]" in certify
    assert "environment: full-x7-certification" in certify
    assert "id-token: write" in certify
    assert "group: full-x7-certification-${{ inputs.mode }}" in certify
    assert "cancel-in-progress: false" in certify
    assert "certification_config:" in certify
    assert "long_certification_contract.py plan" in certify
    assert "--release-candidate-plan .release-candidate-plan.json" in certify
    assert "flock --exclusive --nonblock" in certify
    assert "--if-none-match '*'" in certify
    assert "full-x7-certifications" not in certify
    assert "candidate and certification commits differ" in certify
    assert "nfi-bte release gate" in certify
    assert "--native-score-evidence candidate/native-score/score-evidence.json" in certify
    assert "--native-score-identity candidate/native-score/identity.json" in certify
    assert "runs-on: [self-hosted, linux, x64, nfi-release-signer]" in promote
    assert "environment: release-publication" in promote
    assert promote.count(
        "/var/lib/nfi-release/provenance/used-certificates.sqlite"
    ) == 1
    assert promote.count("--provenance-ledger \"$PROVENANCE_LEDGER\"") == 2
    assert "runs-on: ubuntu-latest" not in promote
    combined_consumers = {
        path.name: path.read_text(encoding="utf-8")
        for path in (root / ".github/workflows").glob("*.yml")
        if "release verify-combined" in path.read_text(encoding="utf-8")
    }
    assert set(combined_consumers) == {
        "publish-release-candidate.yml",
        "promote-release.yml",
    }
    for consumer_workflow in combined_consumers.values():
        assert "runs-on: [self-hosted, linux, x64, nfi-release-signer]" in consumer_workflow
        assert "environment: release-publication" in consumer_workflow
        assert "/var/lib/nfi-release/provenance/used-certificates.sqlite" in consumer_workflow
        remaining = consumer_workflow
        while "release verify-combined" in remaining:
            invocation = remaining[remaining.index("release verify-combined"):]
            command = invocation.split("\n      - name:", 1)[0]
            assert "--provenance-ledger \"$PROVENANCE_LEDGER\"" in command
            assert "--native-score-evidence" in command
            assert "--native-score-identity" in command
            remaining = invocation[len("release verify-combined"):]
    assert "candidate/release-candidate-plan.json" in certify
    assert "host-certificate-${{ inputs.mode }}-${{ github.sha }}" in certify
    assert 'cp "$OUTPUT_DIRECTORY/bundle.json" host-certificate/' in certify
    assert "contents: write" not in certify
    assert "runs-on: [self-hosted, linux, x64, nfi-release-signer]" in publish
    assert "environment: release-publication" in publish
    assert publish.count(
        "/var/lib/nfi-release/provenance/used-certificates.sqlite"
    ) == 1
    assert publish.count("--provenance-ledger \"$PROVENANCE_LEDGER\"") >= 3
    assert publish.index("release gate-combined") < publish.index(
        "gh release create \"$RELEASE_TAG\" --draft"
    ) < publish.index("release finalize-combined")
    assert "gh release create \"$RELEASE_TAG\" --draft" in publish
    assert "gh release edit \"$RELEASE_TAG\" --draft=false" in publish
    assert "release finalize-combined" in publish
    assert "release abort-combined" in publish
    assert "existing release tag targets another candidate" in publish
    assert "existing draft targets another candidate" in publish
    assert "tag already exists" not in publish
    assert "NFI_RELEASE_PROVENANCE_LEDGER_PATH" not in publish
    assert "runner.temp" not in publish
    assert "/tmp/" not in publish
    assert "runs-on: ubuntu-latest" not in publish
    assert "spot_certificate_run_id:" in publish
    assert "futures_certificate_run_id:" in publish
    assert "host-certificate-spot-${{ steps.candidate.outputs.sha }}" in publish
    assert "host-certificate-futures-${{ steps.candidate.outputs.sha }}" in publish
    assert "candidate and host certificate commits differ" in publish
    assert "nfi-bte release combine" in publish
    assert "nfi-bte release gate-combined" in publish
    assert publish.count("--native-score-evidence candidate/native-score/score-evidence.json") == 4
    assert publish.count("--native-score-identity candidate/native-score/identity.json") == 4
    assert "nfi-bte release verify-combined" in publish
    assert "RELEASE-SHA256SUMS.txt" in publish
    assert publish.index("nfi-bte release combine") < publish.index("nfi-bte release gate-combined")
    assert publish.index("nfi-bte release gate-combined") < publish.index("gh release create")
    assert "RELEASE-SHA256SUMS.txt" in promote
    assert "nfi-bte release verify-combined" in promote
    assert "--native-score-evidence" in promote
    assert "--native-score-identity" in promote
    assert promote.index("nfi-bte release verify-combined") < promote.index("gh release create")


def test_external_certificate_and_combined_publication_reauthorize_each_write() -> None:
    root = Path(__file__).parents[1] / ".github/workflows"
    certify = (root / "certify-release-candidate.yml").read_text(encoding="utf-8")
    publish = (root / "publish-release-candidate.yml").read_text(encoding="utf-8")
    promote = (root / "promote-release.yml").read_text(encoding="utf-8")

    assert (
        'release authorize-current \\\n              --operation "certification-immutable-upload:'
        in certify
    )
    assert certify.index("release authorize-current") < certify.index("aws s3api put-object")
    for operation, consequence in (
        ("combined-draft-create:", 'gh release create "$RELEASE_TAG" --draft'),
        ("combined-draft-upload:", 'gh release upload "$RELEASE_TAG" release/*'),
        ("combined-draft-publish:", 'gh release edit "$RELEASE_TAG" --draft=false'),
    ):
        authorization = publish.index(f'--operation "{operation}')
        write = publish.index(consequence)
        assert authorization < write
        assert "release authorize-current" in publish[authorization - 100 : authorization]
    stable_authorization = promote.index('--operation "combined-stable-create:')
    stable_write = promote.index('gh release create "$STABLE_TAG" candidate/*')
    assert stable_authorization < stable_write
    assert "release authorize-current" in promote[
        stable_authorization - 100 : stable_authorization
    ]


def test_product_release_workflows_preserve_non_combined_boundary() -> None:
    root = Path(__file__).parents[1]
    contract = json.loads(
        (root / ".github/product-release-contract.json").read_text(
            encoding="utf-8"
        )
    )
    build = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
    publish = (
        root / ".github/workflows/publish-product-release-candidate.yml"
    ).read_text(encoding="utf-8")
    promote = (
        root / ".github/workflows/promote-product-release.yml"
    ).read_text(encoding="utf-8")
    with (root / "pyproject.toml").open("rb") as project_file:
        project_version = load(project_file)["project"]["version"]

    assert contract["package_version"] == project_version
    assert contract == {
        "schema_version": "1.0.0",
        "package_version": "1.10.4",
        "release_kind": "product",
        "combined_full_x7_certified": False,
        "distribution_policy": {
            "build_once": True,
            "byte_identical_rc_stable": True,
            "sha256_manifest_required": True,
            "required_ci_commit_match": True,
            "supported_platform_exact_fixture_evidence": True,
            "supported_platform_systems": ["darwin", "linux"],
        },
        "certification_boundary": {
            "latest_same_candidate_spot_certificate": False,
            "latest_same_candidate_futures_certificate": False,
            "prior_certificates_remain_version_bound": True,
            "spot_certificate_tag": "v1.0.0",
            "futures_certificate_tag": "v1.1.0",
        },
    }
    assert "x86_64-pc-windows-msvc" not in build
    assert "cp .github/product-release-contract.json" in build
    assert "name: product-release-bundle-${{ github.sha }}" in build
    assert "full-x7-$mode-platform-evidence.json" in build
    assert "full-x7-$mode-platform-evidence.zip" in build
    assert "name: Publish product release candidate" in publish
    assert "group: product-release-${{ inputs.release_tag }}" in publish
    assert "cancel-in-progress: false" in publish
    assert "product releases must be built from main" in publish
    assert "candidate and publication workflow commits differ" in publish
    assert "actions/workflows/ci.yml/runs?head_sha=" in publish
    assert "product-release-bundle-${{ steps.candidate.outputs.sha }}" in publish
    assert "combined_full_x7_certified == false" in publish
    assert "supported_platform_exact_fixture_evidence == true" in publish
    assert 'supported_platform_systems == ["darwin", "linux"]' in publish
    assert "test \"$(find candidate -maxdepth 1 -type f -name '*.whl' | wc -l)\" -eq 3" in publish
    assert "test \"$(find candidate -mindepth 1 -maxdepth 1 | wc -l)\" -eq 11" in publish
    assert '== ["darwin", "linux"]' in publish
    assert "native-score" not in publish
    assert "nfi-bte release score" not in publish
    assert "find candidate -mindepth 1 ! -type f" in publish
    assert 'gh release create "$RELEASE_TAG" "${assets[@]}"' in publish
    assert "candidate/*" not in publish
    assert "test ! -e candidate/release-gate.json" in publish
    assert "test ! -e candidate/full-x7-release-result.json" in publish
    assert "windows-latest" not in publish
    assert "Audit macOS product installer" in publish
    assert publish.count("GITHUB_TOKEN: ${{ github.token }}") == 1
    assert publish.index("sha256sum --check SHA256SUMS.txt") < publish.index(
        "gh release create"
    )
    assert "name: Promote product stable release" in promote
    assert "group: product-release-${{ inputs.stable_tag }}" in promote
    assert "cancel-in-progress: false" in promote
    assert "actions/workflows/publish-product-release-candidate.yml/runs" in promote
    assert "candidate and promotion workflow commits differ" in promote
    assert "combined_full_x7_certified == false" in promote
    assert "supported_platform_exact_fixture_evidence == true" in promote
    assert 'supported_platform_systems == ["darwin", "linux"]' in promote
    assert "native-score" not in promote
    assert "nfi-bte release score" not in promote
    assert "find candidate -mindepth 1 ! -type f" in promote
    assert 'gh release create "$STABLE_TAG" "${assets[@]}"' in promote
    assert "candidate/*" not in promote
    assert "diff -qr candidate stable" in promote
    assert "windows-latest" not in promote
    assert "Audit latest macOS installer" in promote
    assert promote.count("GITHUB_TOKEN: ${{ github.token }}") == 1
    assert promote.index("sha256sum --check SHA256SUMS.txt") < promote.index(
        "gh release create"
    )
    unix_installer = (root / "install.sh").read_text(encoding="utf-8")
    assert 'os.environ.get("GITHUB_TOKEN")' in unix_installer
    assert 'os.environ.get("GH_TOKEN")' in unix_installer


def test_native_stateful_rc_and_stable_promotion_have_separate_profiles() -> None:
    root = Path(__file__).parents[1]
    commands = json.loads(
        (root / "planning/acceptance-commands.json").read_text(encoding="utf-8")
    )
    roadmap = json.loads(
        (root / "planning/roadmap-state.json").read_text(encoding="utf-8")
    )
    tasks = {task["id"]: task for task in roadmap["tasks"]}
    candidate = commands["profiles"]["full_native_release_candidate"]
    promotion = commands["profiles"]["full_native_release_promotion"]

    assert tasks["M19-03"]["acceptance_profile"] == "full_native_release_candidate"
    assert tasks["M19-04"]["acceptance_profile"] == "full_native_release_promotion"
    assert "platform_release_workflow" in candidate
    assert "publish_product_release" in candidate
    assert "promote_product_release" not in candidate
    assert promotion == [
        "planning_json",
        "diff_check",
        "lock_check",
        "promote_product_release",
    ]


def _long_certification_module() -> ModuleType:
    path = Path(__file__).parents[1] / ".github/scripts/long_certification_contract.py"
    spec = importlib.util.spec_from_file_location(
        "nfi_long_certification_contract",
        path,
    )
    if spec is None or spec.loader is None:
        raise AssertionError("long-certification contract module is not loadable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _release_candidate_contract_module() -> ModuleType:
    path = Path(__file__).parents[1] / ".github/scripts/release_candidate_contract.py"
    spec = importlib.util.spec_from_file_location(
        "nfi_release_candidate_contract",
        path,
    )
    if spec is None or spec.loader is None:
        raise AssertionError("release-candidate contract module is not loadable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_candidate_contract_declares_both_modes_without_identity_drift() -> None:
    root = Path(__file__).parents[1]
    module = _release_candidate_contract_module()

    plan = module.load_release_candidate_contract(
        root / ".github/release-candidate-contract.json",
        repository_root=root,
    )

    platform = plan["platform_evidence"]
    assert platform["runs"] in {3, 4, 5}
    assert platform["timeout_seconds"] > 0
    assert {item["mode_contract"] for item in platform["modes"]} == {
        "binance-spot",
        "binance-usdtm-isolated",
    }
    assert len({item["strategy_sha256"] for item in platform["modes"]}) == 1
    assert platform["base_strategy_sha256"] == (
        "82514cf8d122ef79f3baafa2d33e1a0a96871c0725af6e9300e173d2233cd2db"
    )
    assert {
        item["base_strategy_sha256"] for item in platform["modes"]
    } == {platform["base_strategy_sha256"]}
    assert all(item["manifest_sha256"] for item in platform["modes"])
    probes = plan["certification_probes"]
    assert probes["upstream_commit"] == "8da6038be51654bdaa36839f3f1e296d2fc290ff"
    assert probes["base_source_sha256"] == (
        "82514cf8d122ef79f3baafa2d33e1a0a96871c0725af6e9300e173d2233cd2db"
    )
    assert {
        item["slug"]: len(item["manifests"])
        for item in probes["modes"]
    } == {"spot": 4, "futures": 9}
    assert all(
        record["manifest_sha256"]
        for mode in probes["modes"]
        for record in mode["manifests"]
    )


def _long_certification_plan_inputs(
    tmp_path: Path,
) -> tuple[ModuleType, dict, Path]:
    root = Path(__file__).parents[1]
    module = _long_certification_module()
    contract = module.load_contract(root / ".github/long-certification-contract.json")
    release_lock = tmp_path / "release-lock.json"
    strategy = tmp_path / "strategy.py"
    selected_config = tmp_path / "config.json"
    execution_profile = tmp_path / "execution-profile.json"
    engine_markets = tmp_path / "engine-markets.json"
    reference_markets = tmp_path / "reference-markets.json"
    state_probe = tmp_path / "state-probe.json"
    release_candidate_plan = tmp_path / "release-candidate-plan.json"
    data_directory = tmp_path / "data"
    oracle_directory = tmp_path / "oracle"
    oracle_index = tmp_path / "oracle-index.json"
    wheel = tmp_path / "candidate.whl"
    output = tmp_path / "output"
    data_directory.mkdir()
    oracle_directory.mkdir()
    strategy.write_text("class Strategy: pass\n", encoding="utf-8")
    wheel.write_bytes(b"sealed-wheel")
    write_json(selected_config, {"trading_mode": "spot"})
    write_json(execution_profile, {"hardware_fingerprint": "host"})
    write_json(engine_markets, {"markets": {}})
    write_json(reference_markets, {"markets": {}})
    write_json(state_probe, {"fixture_id": "probe"})
    write_json(
        release_candidate_plan,
        {
            "schema_version": "1.0.0",
            "certification_probes": {
                "modes": [
                    {
                        "slug": "spot",
                        "required_manifests": 1,
                        "manifests": [
                            {
                                "manifest": state_probe.name,
                                "manifest_sha256": sha256_file(state_probe),
                            }
                        ],
                    }
                ]
            },
        },
    )
    write_json(
        release_lock,
        {
            "identity_sha256": "a" * 64,
            "data": {"aggregate_sha256": "b" * 64},
            "reference": {
                "image_platform_digest": "sha256:" + "c" * 64,
            },
        },
    )
    run_report = oracle_directory / "run.json"
    write_json(run_report, {"complete": True, "result_sha256": "d" * 64})
    config = {
        "schema_version": "1.0.0",
        "mode": "spot",
        "release_lock": str(release_lock),
        "execution_profile": str(execution_profile),
        "strategy": str(strategy),
        "strategy_class": "Strategy",
        "config": str(selected_config),
        "data_directory": str(data_directory),
        "engine_markets": str(engine_markets),
        "reference_markets": str(reference_markets),
        "oracle_index": str(oracle_index),
        "oracle_fingerprint": "0" * 64,
        "host_lock": str(tmp_path / "locks/certification.lock"),
        "state_probes": [str(state_probe)],
    }
    identity = module._input_identity(config)
    fingerprint = module.canonical_sha256(identity)
    config["oracle_fingerprint"] = fingerprint
    write_json(
        oracle_index,
        {
            "schema_version": "1.0.0",
            "oracles": [
                {
                    "mode": "spot",
                    "fingerprint": fingerprint,
                    "identity": identity,
                    "directory": str(oracle_directory),
                    "run_json_sha256": sha256_file(run_report),
                    "tree_sha256": module.directory_tree_sha256(oracle_directory),
                    "status": "exact_parity",
                    "immutable": True,
                }
            ],
        },
    )
    config_path = tmp_path / "certification-config.json"
    write_json(config_path, config)
    arguments = {
        "contract": contract,
        "config_path": config_path,
        "release_candidate_plan_path": release_candidate_plan,
        "mode": "spot",
        "candidate_commit": "e" * 40,
        "candidate_wheel": wheel,
        "output_directory": output,
        "executable": "/installed/nfi-bte",
        "resume": False,
    }
    return module, arguments, config_path


def test_long_certification_plan_reuses_only_the_indexed_oracle(
    tmp_path: Path,
) -> None:
    module, arguments, _config_path = _long_certification_plan_inputs(tmp_path)

    plan = module.build_plan(**arguments)

    assert plan["oracle"]["reused"] is True
    assert plan["oracle"]["new_run_allowed"] is False
    assert plan["oracle"]["immutable"] is True
    assert plan["candidate_commit"] == "e" * 40
    assert plan["mode"] == "spot"
    assert "--official-oracle" in plan["command"]
    assert "--resume" not in plan["command"]
    assert "reference" not in plan["command"]
    assert plan["state_probes"] == [
        {
            "path": str(Path(arguments["release_candidate_plan_path"]).parent / "state-probe.json"),
            "sha256": sha256_file(
                Path(arguments["release_candidate_plan_path"]).parent
                / "state-probe.json"
            ),
        }
    ]


def test_long_certification_plan_rejects_probe_drift_from_candidate(
    tmp_path: Path,
) -> None:
    module, arguments, config_path = _long_certification_plan_inputs(tmp_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    replacement = tmp_path / "replacement-probe.json"
    write_json(replacement, {"fixture_id": "different"})
    config["state_probes"] = [str(replacement)]
    write_json(config_path, config)

    with pytest.raises(ValueError, match="differ from the sealed"):
        module.build_plan(**arguments)


def test_long_certification_plan_rejects_fingerprint_or_tree_drift(
    tmp_path: Path,
) -> None:
    module, arguments, config_path = _long_certification_plan_inputs(tmp_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["oracle_fingerprint"] = "f" * 64
    write_json(config_path, config)

    with pytest.raises(ValueError, match="fingerprint differs"):
        module.build_plan(**arguments)

    config["oracle_fingerprint"] = module.canonical_sha256(module._input_identity(config))
    write_json(config_path, config)
    index_path = Path(config["oracle_index"])
    index = json.loads(index_path.read_text(encoding="utf-8"))
    oracle_run = Path(index["oracles"][0]["directory"]) / "run.json"
    write_json(oracle_run, {"complete": False})

    with pytest.raises(ValueError, match="tree seal"):
        module.build_plan(**arguments)


def test_long_certification_resume_requires_explicit_identity_checked_plan(
    tmp_path: Path,
) -> None:
    module, arguments, _config_path = _long_certification_plan_inputs(tmp_path)
    output = Path(arguments["output_directory"])
    output.mkdir()
    (output / "interrupted-checkpoint.json").write_text(
        "{}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="requires explicit resume"):
        module.build_plan(**arguments)

    arguments["resume"] = True
    plan = module.build_plan(**arguments)

    assert plan["resume"] is True
    assert plan["command"][-1] == "--resume"


def _ci_contract_module() -> ModuleType:
    path = Path(__file__).parents[1] / ".github/scripts/ci_contract.py"
    spec = importlib.util.spec_from_file_location("nfi_ci_contract", path)
    if spec is None or spec.loader is None:
        raise AssertionError("CI contract module is not loadable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ci_contract_selects_risk_tier_from_all_changed_paths() -> None:
    root = Path(__file__).parents[1]
    module = _ci_contract_module()
    contract = module.load_contract(root / ".github/ci-contract.json")

    assert (
        module.classify_paths(
            ["README.md", "docs/release-gate.md"],
            contract,
        )
        == module.DOCS_CLASSIFICATION
    )
    assert (
        module.classify_paths(["planning/roadmap-state.json"], contract)
        == module.DOCS_CLASSIFICATION
    )
    assert (
        module.classify_paths(
            ["docs/ci-policy.md", ".github/workflows/ci.yml"],
            contract,
        )
        == module.POLICY_CLASSIFICATION
    )
    assert (
        module.classify_paths(["planning/futures-discovery-policy.json"], contract)
        == module.CODE_CLASSIFICATION
    )
    assert (
        module.classify_paths([".github/workflows/release.yml"], contract)
        == module.CODE_CLASSIFICATION
    )
    assert (
        module.classify_paths(["docs/ci-policy.md", "python/package.py"], contract)
        == module.CODE_CLASSIFICATION
    )
    assert module.classify_paths([], contract) == module.CODE_CLASSIFICATION


def test_ci_contract_requires_only_the_selected_job_matrix() -> None:
    root = Path(__file__).parents[1]
    module = _ci_contract_module()
    contract = module.load_contract(root / ".github/ci-contract.json")
    paths_by_classification = {
        module.DOCS_CLASSIFICATION: ["README.md"],
        module.POLICY_CLASSIFICATION: [".github/workflows/ci.yml"],
        module.AUTOMATION_CLASSIFICATION: [".github/workflows/nfi-compatibility.yml"],
        module.CODE_CLASSIFICATION: ["pyproject.toml"],
    }
    conditional_jobs = contract["conditional_job_ids"]
    for classification, paths in paths_by_classification.items():
        plan = module.plan_affected_validation(
            paths,
            contract,
            event_name="pull_request",
        )
        selected = set(plan["selected_jobs"])
        expected = {
            job: "success" if job in selected else "skipped"
            for job in conditional_jobs
        }
        assert module.required_results_pass(
            classification,
            validation_plan=plan,
            changes_result="success",
            documentation_result="success",
            job_results=expected,
            contract=contract,
        )
        broken = dict(expected)
        broken[conditional_jobs[0]] = (
            "skipped" if broken[conditional_jobs[0]] == "success" else "success"
        )
        assert not module.required_results_pass(
            classification,
            validation_plan=plan,
            changes_result="success",
            documentation_result="success",
            job_results=broken,
            contract=contract,
        )


def test_ci_workflow_matches_machine_readable_policy() -> None:
    root = Path(__file__).parents[1]
    contract = json.loads((root / ".github/ci-contract.json").read_text(encoding="utf-8"))
    workflow = (root / contract["workflow"]).read_text(encoding="utf-8")

    assert "pull_request_target:" not in workflow
    assert "permissions:\n  contents: read" in workflow
    assert contract["pull_request"]["permissions"] == {"contents": "read"}
    assert contract["pull_request"]["allows_secrets"] is False
    assert contract["pull_request"]["allows_privileged_fork_execution"] is False
    assert contract["pull_request"]["allows_official_reference"] is False
    required_capabilities = contract["pull_request"][
        "required_capabilities_by_classification"
    ]
    assert set().union(*(set(value) for value in required_capabilities.values())) == set(
        contract["coverage"]
    )
    assert f"  group: {contract['concurrency']['group']}" in workflow
    assert "  cancel-in-progress: true" in workflow
    for job_id, job in contract["jobs"].items():
        assert f"  {job_id}:" in workflow
        assert f"    name: {job['name']}" in workflow
        assert f"    timeout-minutes: {job['timeout_minutes']}" in workflow
    assert "matrix: ${{ fromJSON(needs.changes.outputs.python_matrix_json) }}" in workflow
    for job_id in contract["conditional_job_ids"]:
        start = workflow.index(f"  {job_id}:")
        following = [
            position
            for other_id in contract["jobs"]
            if other_id != job_id
            and (position := workflow.find(f"\n  {other_id}:", start + 1)) != -1
        ]
        end = min(following, default=len(workflow))
        section = workflow[start:end]
        expected_condition = (
            "if: needs.changes.outputs.policy_changes == 'true'"
            if job_id == "policy"
            else (
                "contains(fromJSON(needs.changes.outputs.selected_jobs_json), "
                f"'{job_id}')"
            )
        )
        assert expected_condition in section
    assert "    name: Required CI" in workflow
    assert "    if: always()" in workflow
    assert contract["branch_protection"]["api"]["required_status_checks"]["contexts"] == [
        contract["required_check"]["name"]
    ]


def test_nightly_matrix_covers_each_discovered_x7_fixture_once(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[1]
    module = _ci_contract_module()
    contract = module.load_contract(root / ".github/ci-contract.json")

    first = module.build_nightly_matrix(root, contract)
    second = module.build_nightly_matrix(root, contract)
    expected = sorted(
        path.relative_to(root).as_posix()
        for pattern in contract["nightly"]["fixture_globs"]
        for path in root.glob(pattern)
    )
    observed = [fixture["manifest"] for shard in first["shards"] for fixture in shard["fixtures"]]
    shard_sizes = [shard["logical_bytes"] for shard in first["shards"]]
    largest_fixture = max(fixture["logical_bytes"] for fixture in first["inventory"])

    assert first == second
    assert sorted(observed) == expected
    assert len(observed) == len(set(observed)) == first["fixture_count"]
    assert first["shard_count"] == contract["nightly"]["shard_count"]
    assert max(shard_sizes) - min(shard_sizes) <= largest_fixture

    reports = []
    for shard_index in range(first["shard_count"]):
        report, passed = module.run_nightly_shard(
            root,
            contract,
            shard_index=shard_index,
            artifact_root=tmp_path / f"shard-{shard_index}",
            dry_run=True,
        )
        assert passed is True
        assert report["dry_run"] is True
        reports.append(report)
    summary = module.summarize_nightly_reports(
        first,
        reports,
        job_results={job: "success" for job in contract["nightly"]["job_ids"]},
        contract=contract,
    )

    assert summary["passed"] is True
    assert summary["dry_run"] is True
    assert summary["expected_fixture_count"] == len(expected)
    assert summary["unique_observed_fixture_count"] == len(expected)
    assert summary["duplicates"] == []
    assert summary["missing"] == []


def test_nightly_summary_deduplicates_repeated_root_cause_failures(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[1]
    module = _ci_contract_module()
    contract = module.load_contract(root / ".github/ci-contract.json")
    matrix = module.build_nightly_matrix(root, contract)
    reports = [
        module.run_nightly_shard(
            root,
            contract,
            shard_index=shard_index,
            artifact_root=tmp_path / f"shard-{shard_index}",
            dry_run=True,
        )[0]
        for shard_index in range(matrix["shard_count"])
    ]
    fingerprint = "a" * 64
    for report in reports[:2]:
        fixture = report["results"][0]
        report["failures"] = [
            {
                "fingerprint": fingerprint,
                "stage": "fixture-full-parity",
                "fixture_id": fixture["fixture_id"],
                "manifest": fixture["manifest"],
                "exit_code": 1,
                "message": "shared failure",
            }
        ]
        report["passed"] = False
    job_results = {job: "success" for job in contract["nightly"]["job_ids"]}
    job_results["fixtures"] = "failure"

    summary = module.summarize_nightly_reports(
        matrix,
        reports,
        job_results=job_results,
        contract=contract,
    )

    assert summary["passed"] is False
    assert summary["unique_failure_count"] == 1
    assert summary["failure_occurrence_count"] == 2
    assert summary["failures"][0]["fingerprint"] == fingerprint
    assert len(summary["failures"][0]["fixture_ids"]) == 2


def test_nightly_summary_rejects_duplicate_fixture_assignment(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[1]
    module = _ci_contract_module()
    contract = module.load_contract(root / ".github/ci-contract.json")
    matrix = module.build_nightly_matrix(root, contract)
    reports = [
        module.run_nightly_shard(
            root,
            contract,
            shard_index=shard_index,
            artifact_root=tmp_path / f"shard-{shard_index}",
            dry_run=True,
        )[0]
        for shard_index in range(matrix["shard_count"])
    ]
    duplicated = reports[0]["assignments"][0]
    reports[1]["assignments"].append(duplicated)

    summary = module.summarize_nightly_reports(
        matrix,
        reports,
        job_results={job: "success" for job in contract["nightly"]["job_ids"]},
        contract=contract,
    )

    assert summary["passed"] is False
    assert summary["duplicates"] == [duplicated]


def test_nightly_workflow_matches_read_only_trust_boundary() -> None:
    root = Path(__file__).parents[1]
    contract = json.loads((root / ".github/ci-contract.json").read_text(encoding="utf-8"))
    nightly = contract["nightly"]
    workflow = (root / nightly["workflow"]).read_text(encoding="utf-8")

    assert "pull_request_target:" not in workflow
    assert "pull_request:" not in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "secrets:" not in workflow
    assert "runs-on: [self-hosted" not in workflow
    assert f"  group: {nightly['concurrency']['group']}" in workflow
    assert (
        f"  cancel-in-progress: "
        f"{str(nightly['concurrency']['cancel_in_progress']).lower()}" in workflow
    )
    assert "nightly-matrix" in workflow
    assert "run-nightly-shard" in workflow
    assert "summarize-nightly" in workflow
    assert "if: always()" in workflow
    assert "merge-multiple: true" in workflow
    assert "reference run" in workflow
    assert nightly["official_reference_smoke"]["manifest"] in workflow
    assert f"--trace {nightly['official_reference_smoke']['trace']}" in workflow
