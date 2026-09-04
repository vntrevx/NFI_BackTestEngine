from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _module() -> ModuleType:
    path = Path(__file__).parents[1] / ".github/scripts/release_supply_chain.py"
    spec = importlib.util.spec_from_file_location("nfi_release_supply_chain", path)
    if spec is None or spec.loader is None:
        raise AssertionError("release supply-chain module is not loadable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _distributions(tmp_path: Path) -> list[Path]:
    wheel = tmp_path / "nfi_backtest_engine-1.15.0-py3-none-any.whl"
    sdist = tmp_path / "nfi_backtest_engine-1.15.0.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    return [wheel, sdist]


def test_supply_chain_seals_spdx_and_four_channel_identity(tmp_path: Path) -> None:
    module = _module()
    distributions = _distributions(tmp_path)

    graph = module.seal_supply_chain(
        distributions,
        tmp_path,
        project="nfi-backtest-engine",
        version="1.15.0",
        candidate_commit="a" * 40,
        created_at="2026-09-05T00:00:00Z",
    )

    assert [channel["slug"] for channel in graph["channels"]] == [
        "github-rc",
        "testpypi",
        "github-stable",
        "pypi",
    ]
    assert module.verify_supply_chain(
        tmp_path,
        tmp_path / "distribution-identity.json",
        tmp_path / "nfi-backtest-engine.spdx.json",
    ) == graph


def test_supply_chain_rejects_distribution_mutation(tmp_path: Path) -> None:
    module = _module()
    distributions = _distributions(tmp_path)
    module.seal_supply_chain(
        distributions,
        tmp_path,
        project="nfi-backtest-engine",
        version="1.15.0",
        candidate_commit="a" * 40,
        created_at="2026-09-05T00:00:00Z",
    )
    distributions[0].write_bytes(b"mutated")

    with pytest.raises(ValueError, match="bytes differ"):
        module.verify_supply_chain(
            tmp_path,
            tmp_path / "distribution-identity.json",
            tmp_path / "nfi-backtest-engine.spdx.json",
        )


def test_supply_chain_rejects_symlink_distribution(tmp_path: Path) -> None:
    module = _module()
    distributions = _distributions(tmp_path)
    link = tmp_path / "alias.whl"
    link.symlink_to(distributions[0])

    with pytest.raises(ValueError, match="non-symlink"):
        module.seal_supply_chain(
            [link, distributions[1]],
            tmp_path / "output",
            project="nfi-backtest-engine",
            version="1.15.0",
            candidate_commit="a" * 40,
            created_at="2026-09-05T00:00:00Z",
        )
