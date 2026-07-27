from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path

import pytest
from nfi_backtest_engine import cli
from nfi_backtest_engine.canonical import write_json
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.regression_contract import (
    load_regression_contract,
    parse_release_asset_roots,
    verify_regression_contract,
)
from nfi_backtest_engine.specs import validate_regression_contract

ROOT = Path(__file__).parents[1]


def test_bundled_v11_contract_verifies_all_repository_evidence_offline() -> None:
    report = verify_regression_contract(repository_root=ROOT)

    assert report["complete"] is True
    assert report["contract_version"] == "1.1.0"
    assert report["checks"] == {
        "schema": "valid",
        "repository_files": 17,
        "full_state_fixtures": 9,
        "public_command_paths": 64,
        "stable_error_codes": 48,
        "behavior_contracts": 7,
        "release_assets": 0,
        "release_certificates": 0,
        "release_mode": "identity-pinned",
    }


def test_contract_rejects_one_same_size_evidence_mutation_immediately(tmp_path: Path) -> None:
    manifest, _ = load_regression_contract()
    mutated = bytearray((ROOT / "README.md").read_bytes())
    mutated[0] = ord("!") if mutated[0] != ord("!") else ord("#")
    (tmp_path / "README.md").write_bytes(mutated)

    contract = deepcopy(manifest)
    contract["repository_files"][0] = {
        "kind": "document",
        "path": "README.md",
        "bytes": len(mutated),
        "sha256": manifest["repository_files"][0]["sha256"],
    }
    contract_path = tmp_path / "contract.json"
    write_json(contract_path, contract)

    with pytest.raises(SpecValidationError, match=r"README\.md: SHA-256 differs"):
        verify_regression_contract(contract_path, repository_root=tmp_path)


def test_contract_verifies_existing_release_asset_directories(tmp_path: Path) -> None:
    manifest, _ = load_regression_contract()
    contract = deepcopy(manifest)
    roots: dict[str, Path] = {}
    for release in contract["releases"]:
        tag = release["tag"]
        root = tmp_path / tag
        root.mkdir()
        certificate = root / "certificate.json"
        write_json(certificate, {"status": "certified"})
        payload = certificate.read_bytes()
        release["certificate"] = {
            "asset": certificate.name,
            "assertions": [
                {
                    "path": ["status"],
                    "equals": "certified",
                }
            ],
        }
        release["assets"] = [
            {
                "name": certificate.name,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "url": (
                    "https://github.com/vntrevx/NFI_BackTestEngine/"
                    f"releases/download/{tag}/{certificate.name}"
                ),
            }
        ]
        roots[tag] = root
    contract_path = tmp_path / "contract.json"
    write_json(contract_path, contract)

    report = verify_regression_contract(
        contract_path,
        repository_root=ROOT,
        release_asset_roots=roots,
    )

    assert report["checks"]["release_assets"] == 2
    assert report["checks"]["release_certificates"] == 2
    assert report["checks"]["release_mode"] == "verified"


def test_contract_cli_reports_offline_success(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["contract", "verify", "--root", str(ROOT), "--offline"])

    assert exit_code == 0
    assert "regression contract valid: version=1.1.0" in capsys.readouterr().out


def test_contract_cli_maps_evidence_mutation_to_exit_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest, _ = load_regression_contract()
    changed = deepcopy(manifest)
    changed["repository_files"][0]["sha256"] = "0" * 64
    contract_path = tmp_path / "contract.json"
    write_json(contract_path, changed)

    exit_code = cli.main(
        [
            "contract",
            "verify",
            "--manifest",
            str(contract_path),
            "--root",
            str(ROOT),
            "--offline",
        ]
    )

    assert exit_code == 2
    assert "README.md: SHA-256 differs" in capsys.readouterr().err


def test_contract_schema_rejects_unknown_fields() -> None:
    manifest, _ = load_regression_contract()
    changed = deepcopy(manifest)
    changed["runtime_strategy_override"] = "forbidden"

    with pytest.raises(SpecValidationError, match="Additional properties"):
        validate_regression_contract(changed)


def test_contract_allows_additive_commands_but_rejects_a_missing_frozen_command(
    tmp_path: Path,
) -> None:
    manifest, _ = load_regression_contract()
    assert "clean" not in manifest["cli"]["command_paths"]
    assert verify_regression_contract(repository_root=ROOT)["complete"] is True

    changed = deepcopy(manifest)
    changed["cli"]["command_paths"].append("removed frozen command")
    contract_path = tmp_path / "contract.json"
    write_json(contract_path, changed)

    with pytest.raises(SpecValidationError, match="frozen public commands are missing"):
        verify_regression_contract(contract_path, repository_root=ROOT)


def test_release_asset_root_parser_is_explicit_and_unique() -> None:
    assert parse_release_asset_roots(["v1.0.0=one", "v1.1.0=two"]) == {
        "v1.0.0": Path("one"),
        "v1.1.0": Path("two"),
    }
    with pytest.raises(SpecValidationError, match="expected TAG=DIR"):
        parse_release_asset_roots(["v1.0.0"])
    with pytest.raises(SpecValidationError, match="duplicate"):
        parse_release_asset_roots(["v1.0.0=one", "v1.0.0=two"])
