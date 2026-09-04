from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from nfi_backtest_engine import cli
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.product_support_contract import (
    PRODUCT_SUPPORT_CONTRACT_RESOURCE,
    load_product_support_contract,
    validate_product_release_alignment,
)

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "planning/product-support-contract.json"


def test_repository_product_support_contract_is_valid() -> None:
    contract = load_product_support_contract(CONTRACT)

    assert [item["family"] for item in contract["strategies"]["native_supported"]] == [
        "NostalgiaForInfinityX7"
    ]
    assert contract["platforms"] == {
        "supported": [
            "linux-x86_64",
            "linux-aarch64",
            "macos-arm64",
            "windows-wsl2-x86_64",
        ],
        "native_windows_supported": False,
        "windows_abi": "linux-under-wsl2",
    }
    assert contract["certification"]["combined_status"] == "preview"
    assert {
        item["family"]: item["fallback_status"]
        for item in contract["strategies"]["official_only_legacy"]
    } == {
        "NostalgiaForInfinityNext": "qualified",
        "NostalgiaForInfinityNextGen": "qualified",
    }
    assert contract["distribution"]["channels"][1] == {
        "slug": "pypi",
        "status": "planned",
        "authentication": "oidc-trusted-publishing",
    }
    assert contract["release_train"] == [
        {
            "version": "v1.15.0",
            "milestones": ["M25", "M26", "M27", "M28", "M29", "M30"],
            "combined_full_x7_certified": True,
        }
    ]


def test_packaged_contract_is_byte_identical_to_planning_authority() -> None:
    packaged = (
        ROOT
        / "python/nfi_backtest_engine/contracts"
        / PRODUCT_SUPPORT_CONTRACT_RESOURCE
    )

    assert packaged.read_bytes() == CONTRACT.read_bytes()
    assert load_product_support_contract() == load_product_support_contract(CONTRACT)


def test_current_release_policy_does_not_exceed_product_contract() -> None:
    report = validate_product_release_alignment(
        load_product_support_contract(CONTRACT),
        read_json(ROOT / ".github/product-release-contract.json"),
    )

    assert report == {
        "schema_version": "1.0.0",
        "package_version": "1.11.0",
        "combined_full_x7_certified": False,
        "supported_platform_systems": ["darwin", "linux"],
        "valid": True,
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("build_once", False, "distribution policy differs"),
        ("byte_identical_rc_stable", False, "distribution policy differs"),
        ("supported_platform_systems", ["darwin", "linux", "windows"], "platform policy differs"),
    ],
)
def test_release_policy_drift_is_rejected(field: str, value: object, message: str) -> None:
    release = read_json(ROOT / ".github/product-release-contract.json")
    release["distribution_policy"][field] = value

    with pytest.raises(SpecValidationError, match=message):
        validate_product_release_alignment(load_product_support_contract(CONTRACT), release)


def test_combined_certification_claim_drift_is_rejected() -> None:
    release = read_json(ROOT / ".github/product-release-contract.json")
    release["combined_full_x7_certified"] = True

    with pytest.raises(SpecValidationError, match="combined-certification claim differs"):
        validate_product_release_alignment(load_product_support_contract(CONTRACT), release)


def test_contract_support_cli_is_machine_readable(capsys) -> None:
    assert cli.main(["contract", "support", "--json"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["contract_id"] == "nfi-backtest-engine-product-support-v1"
    assert output["certification"]["combined_status"] == "preview"


def test_product_contract_rejects_native_legacy_scope(tmp_path: Path) -> None:
    contract = deepcopy(load_product_support_contract(CONTRACT))
    contract["strategies"]["native_supported"].append(
        {
            "family": "NostalgiaForInfinityNext",
            "selection": "capability-derived",
            "exactness_required": True,
        }
    )
    path = tmp_path / "contract.json"
    write_json(path, contract)

    with pytest.raises(SpecValidationError, match="X7 only"):
        load_product_support_contract(path)


def test_product_contract_rejects_native_windows_claim(tmp_path: Path) -> None:
    contract = deepcopy(load_product_support_contract(CONTRACT))
    contract["platforms"]["native_windows_supported"] = True
    path = tmp_path / "contract.json"
    write_json(path, contract)

    with pytest.raises(SpecValidationError):
        load_product_support_contract(path)


def test_product_contract_rejects_multi_release_train(tmp_path: Path) -> None:
    contract = deepcopy(load_product_support_contract(CONTRACT))
    contract["release_train"].insert(
        0,
        {
            "version": "v1.14.0",
            "milestones": ["M25"],
            "combined_full_x7_certified": False,
        },
    )
    path = tmp_path / "contract.json"
    write_json(path, contract)

    with pytest.raises(SpecValidationError):
        load_product_support_contract(path)


def test_product_contract_rejects_drift_from_executable_performance_defaults(
    tmp_path: Path,
) -> None:
    contract = deepcopy(load_product_support_contract(CONTRACT))
    contract["certification"]["minimum_native_speedup"] = 9.5
    path = tmp_path / "contract.json"
    write_json(path, contract)

    with pytest.raises(SpecValidationError, match="minimum_native_speedup"):
        load_product_support_contract(path)
