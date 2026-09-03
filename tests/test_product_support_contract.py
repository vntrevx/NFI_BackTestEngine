from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from nfi_backtest_engine.canonical import write_json
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.product_support_contract import load_product_support_contract

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
    assert contract["distribution"]["channels"][1] == {
        "slug": "pypi",
        "status": "planned",
        "authentication": "oidc-trusted-publishing",
    }


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


def test_product_contract_rejects_early_combined_claim(tmp_path: Path) -> None:
    contract = deepcopy(load_product_support_contract(CONTRACT))
    contract["release_train"][2]["combined_full_x7_certified"] = True
    path = tmp_path / "contract.json"
    write_json(path, contract)

    with pytest.raises(SpecValidationError, match="only the gated v1.15.0"):
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
