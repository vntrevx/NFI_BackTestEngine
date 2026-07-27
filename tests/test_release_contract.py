from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from nfi_backtest_engine.canonical import write_json
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.fixture import sha256_file
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
    assert lifecycle.missing_from(
        {
            "sides": ["long", "short"],
            "funded_trades": 1,
        }
    ) == []


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


def _release_gate_inputs(tmp_path: Path) -> dict[str, Path | str]:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    wheel = candidate / (
        "nfi_backtest_engine-1.1.0-cp312-cp312-manylinux_2_17_x86_64.whl"
    )
    wheel.write_bytes(b"sealed candidate wheel")
    (candidate / "nfi_backtest_engine-1.1.0.tar.gz").write_bytes(
        b"sealed source distribution"
    )
    wheel_sha256 = sha256_file(wheel)
    platform = candidate / "full-x7-futures-platform-evidence.json"
    write_json(
        platform,
        {
            "schema_version": "1.0.0",
            "created_at": "2026-07-28T00:00:00Z",
            "release_certified": True,
            "lane": "exact-fixture",
            "mode_contract": "binance-usdtm-isolated",
            "workload_identity_sha256": "8" * 64,
            "workload": {},
            "result_sha256": "9" * 64,
            "package_version": "1.1.0",
            "portable_package_sha256": PORTABLE_PACKAGE_SHA,
            "platforms": [
                {
                    "system": system,
                    "wheel_sha256": (
                        wheel_sha256 if system == "linux" else digest * 64
                    ),
                }
                for system, digest in (
                    ("windows", "a"),
                    ("linux", "0"),
                    ("darwin", "f"),
                )
            ],
        },
    )
    candidate_assets = sorted(candidate.iterdir(), key=lambda item: item.name)
    (candidate / "SHA256SUMS.txt").write_text(
        "".join(
            f"{sha256_file(asset)}  {asset.name}\n" for asset in candidate_assets
        ),
        encoding="utf-8",
    )
    certificate = tmp_path / "full-x7-certification.json"
    _release_certificate(certificate, wheel_sha256=wheel_sha256)
    certificate_evidence = tmp_path / "full-x7-certification-bundle.zip"
    _write_certificate_evidence(certificate_evidence, certificate)
    return {
        "candidate_directory": candidate,
        "certificate_path": certificate,
        "certificate_evidence_path": certificate_evidence,
        "platform_evidence_path": platform,
        "candidate_commit": RELEASE_COMMIT,
        "output_directory": tmp_path / "release",
    }


def _reseal_candidate_manifest(candidate: Path) -> None:
    assets = sorted(
        (
            path
            for path in candidate.iterdir()
            if path.name != "SHA256SUMS.txt"
        ),
        key=lambda item: item.name,
    )
    (candidate / "SHA256SUMS.txt").write_text(
        "".join(f"{sha256_file(asset)}  {asset.name}\n" for asset in assets),
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
    verified = verify_release_gate(release, expected_commit=RELEASE_COMMIT)
    checksums = {
        line.partition("  ")[2]
        for line in (release / RELEASE_CHECKSUMS_NAME)
        .read_text(encoding="utf-8")
        .splitlines()
    }

    assert result == verified
    assert result["certificate"]["wheel_sha256"] == sha256_file(
        next(candidate.glob("*.whl"))
    )
    assert sha256_file(release / "SHA256SUMS.txt") == candidate_manifest_sha
    assert checksums == {
        path.name
        for path in release.iterdir()
        if path.name != RELEASE_CHECKSUMS_NAME
    }


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


def test_release_gate_rejects_missing_host_certificate(tmp_path: Path) -> None:
    inputs = _release_gate_inputs(tmp_path)
    Path(inputs["certificate_path"]).unlink()

    with pytest.raises(SpecValidationError, match="host certificate does not exist"):
        seal_release_gate(**inputs)

    assert not Path(inputs["output_directory"]).exists()


def test_release_gate_rejects_preview_certificate(tmp_path: Path) -> None:
    inputs = _release_gate_inputs(tmp_path)
    certificate = Path(inputs["certificate_path"])
    wheel_sha256 = sha256_file(
        next(Path(inputs["candidate_directory"]).glob("*.whl"))
    )
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

    with pytest.raises(SpecValidationError, match="portable package differs"):
        seal_release_gate(**inputs)


def test_release_workflows_enforce_certificate_and_promotion_contract() -> None:
    root = Path(__file__).parents[1]
    build = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
    publish = (
        root / ".github/workflows/publish-release-candidate.yml"
    ).read_text(encoding="utf-8")
    certify = (
        root / ".github/workflows/certify-release-candidate.yml"
    ).read_text(encoding="utf-8")
    promote = (root / ".github/workflows/promote-release.yml").read_text(
        encoding="utf-8"
    )

    assert "permissions:\n  contents: read" in build
    assert "name: Certify release candidate" in certify
    assert "runs-on: [self-hosted, linux, x64, nfi-certification]" in certify
    assert "certification_config:" in certify
    assert ".official_oracle | type ==" in certify
    assert "--official-oracle \"$official_oracle\"" in certify
    assert "candidate and certification commits differ" in certify
    assert "nfi-bte release gate" in certify
    assert "contents: write" not in certify
    assert "certificate_run_id:" in publish
    assert "host-certificate-${{ steps.candidate.outputs.sha }}" in publish
    assert "candidate and host certificate commits differ" in publish
    assert "nfi-bte release gate" in publish
    assert "RELEASE-SHA256SUMS.txt" in publish
    assert publish.index("nfi-bte release gate") < publish.index("gh release create")
    assert "RELEASE-SHA256SUMS.txt" in promote
    assert '.status == "release_certified"' in promote
    assert promote.index('.status == "release_certified"') < promote.index(
        "gh release create"
    )
