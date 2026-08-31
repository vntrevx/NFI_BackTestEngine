from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest
from nfi_backtest_engine import pair_selection
from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.errors import BenchmarkError


def _workspace(tmp_path: Path) -> Path:
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "pairlist-volume-binance-usdt.json").write_text(
        '{"pairlists":[{"method":"VolumePairList","number_assets":100}]}',
        encoding="utf-8",
    )
    (configs / "blacklist-binance.json").write_text(
        '{"exchange":{"pair_blacklist":["(BAD)/.*"]}}',
        encoding="utf-8",
    )
    return tmp_path


def _config() -> dict:
    return {
        "dry_run": True,
        "trading_mode": "spot",
        "max_open_trades": 1,
        "stake_currency": "USDT",
        "stake_amount": "unlimited",
        "exchange": {
            "name": "binance",
            "key": "",
            "secret": "",
            "pair_whitelist": [],
            "pair_blacklist": [],
            "ccxt_config": {"options": {"fetchMarkets": {"types": ["spot"]}}},
        },
        "pairlists": [{"method": "StaticPairList"}],
    }


def test_live_nfi_policy_is_resolved_once_and_returned_in_ranked_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    observed: dict[str, object] = {}
    monkeypatch.setattr(pair_selection, "ensure_docker_config", lambda: tmp_path)
    monkeypatch.setattr(pair_selection, "ensure_reference_image", lambda **_kwargs: None)

    @contextmanager
    def fake_managed_run(**_kwargs):
        yield {"command_prefix": ["docker", "run"]}

    monkeypatch.setattr(pair_selection, "managed_docker_run", fake_managed_run)

    def fake_subprocess_run(command, **_kwargs):
        input_mount = next(value for value in command if value.endswith(":/input:ro"))
        input_root = Path(input_mount.removesuffix(":/input:ro"))
        observed["config"] = read_json(input_root / "config.json")
        observed["pairlist"] = (input_root / "pairlist-volume-binance-usdt.json").read_text(
            encoding="utf-8"
        )
        observed["blacklist"] = (input_root / "blacklist-binance.json").read_text(
            encoding="utf-8"
        )
        return subprocess.CompletedProcess(
            command,
            0,
            '["BTC/USDT","ETH/USDT","SOL/USDT"]\n',
            "Freqtrade diagnostic log",
        )

    monkeypatch.setattr("nfi_backtest_engine.pair_selection.subprocess.run", fake_subprocess_run)

    pairs = pair_selection.resolve_nfi_volume_pairs(
        _config(),
        workspace,
        diagnostic_path=tmp_path / ".nfi/pair-selection-error.log",
    )

    assert pairs == ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    prepared = observed["config"]
    assert isinstance(prepared, dict)
    assert "pairlists" not in prepared
    assert prepared["add_config_files"] == [
        "pairlist-volume-binance-usdt.json",
        "blacklist-binance.json",
    ]
    assert "pair_whitelist" not in prepared["exchange"]
    assert "pair_blacklist" not in prepared["exchange"]
    assert "VolumePairList" in str(observed["pairlist"])
    assert "(BAD)/.*" in str(observed["blacklist"])


def test_transient_ranking_failure_retries_then_writes_short_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    attempts = 0
    delays: list[int] = []
    diagnostic = tmp_path / ".nfi/pair-selection-error.log"
    monkeypatch.setattr(pair_selection, "ensure_docker_config", lambda: tmp_path)
    monkeypatch.setattr(pair_selection, "ensure_reference_image", lambda **_kwargs: None)
    monkeypatch.setattr("nfi_backtest_engine.pair_selection.time.sleep", delays.append)

    @contextmanager
    def fake_managed_run(**_kwargs):
        yield {"command_prefix": ["docker", "run"]}

    monkeypatch.setattr(pair_selection, "managed_docker_run", fake_managed_run)

    def failed_run(command, **_kwargs):
        nonlocal attempts
        attempts += 1
        return subprocess.CompletedProcess(
            command,
            1,
            "",
            "traceback internals\nsocket: Temporary failure in name resolution",
        )

    monkeypatch.setattr("nfi_backtest_engine.pair_selection.subprocess.run", failed_run)

    with pytest.raises(BenchmarkError, match="temporarily unavailable after 3 attempts") as error:
        pair_selection.resolve_nfi_volume_pairs(
            _config(),
            workspace,
            diagnostic_path=diagnostic,
        )

    assert attempts == 3
    assert delays == [2, 5]
    assert "traceback internals" not in str(error.value)
    assert str(diagnostic) in str(error.value)
    assert "Temporary failure in name resolution" in diagnostic.read_text(encoding="utf-8")
