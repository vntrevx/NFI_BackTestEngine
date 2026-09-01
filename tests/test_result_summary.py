from __future__ import annotations

from pathlib import Path

import pytest
from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.result_summary import build_result_summary

FIXTURE = (
    Path(__file__).parents[1]
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "normal-routing-spot-2025-01-01_04"
    / "artifacts"
    / "trade-surface.json"
)
FUTURES_FIXTURE = (
    Path(__file__).parents[1]
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "x7-liquidation-stoploss-guard-futures-v17.4.435-2022-04-29_05-02"
    / "artifacts"
    / "trade-surface.json"
)


def _run_report() -> dict:
    return {
        "run_id": "fixture-run",
        "status": "complete",
        "complete": True,
        "created_at": "2026-07-24T00:00:00Z",
        "inputs": {
            "strategy": {
                "class_name": "ContractLifecycleStrategy",
                "file_sha256": "a" * 64,
            },
            "timerange": "20250101-20250104",
        },
        "vectors": {"pair_count": 1},
        "execution": {
            "indicator_workers": 2,
            "cpu_process_limit": 4,
            "portfolio_simulator_threads": 2,
            "working_memory_bytes": 8 * 1024**3,
        },
        "timings": {"pipeline_wall_time_seconds": 12.5},
        "resumed_stages": [],
        "official_confirmation": {"status": "not_run"},
    }


def test_summary_calculates_readable_performance_and_risk_metrics() -> None:
    surface = read_json(FIXTURE)
    summary = build_result_summary(_run_report(), surface)

    assert summary["schema_version"] == "2.1.0"
    assert summary["run"]["strategy"] == "ContractLifecycleStrategy"
    assert summary["artifacts"]["markdown_report"] == "report.md"
    assert "html_report" not in summary["artifacts"]
    assert summary["performance"]["profit_total_abs"] == pytest.approx(1.30196076)
    assert summary["performance"]["return_ratio"] == pytest.approx(0.00130196076)
    expected_profit_factor = (1.56284197 + 0.26631842 + 0.33077569) / (
        0.49538895 + 0.1114823 + 0.25110407
    )
    assert summary["performance"]["profit_factor"] == pytest.approx(expected_profit_factor)
    assert summary["activity"]["pairs"] == 1
    assert summary["activity"]["trades"] == 6
    assert summary["activity"]["wins"] == 3
    assert summary["activity"]["losses"] == 3
    assert summary["activity"]["draws"] == 0
    assert summary["activity"]["win_rate"] == 0.5
    assert summary["activity"]["average_duration_minutes"] == pytest.approx(
        358.333333333333
    )
    assert summary["activity"]["median_duration_minutes"] == 360.0
    assert summary["activity"]["average_stake_amount"] == pytest.approx(124.183681866667)
    assert summary["activity"]["winning_duration_minutes"] == {
        "minimum": 360,
        "maximum": 360,
        "average": 360.0,
    }
    assert summary["activity"]["losing_duration_minutes"]["minimum"] == 350
    assert summary["activity"]["open_trades"] == 0
    assert summary["activity"]["rejected_signals"] == 0
    assert summary["activity"]["entry_timeouts"] == 0
    assert summary["activity"]["exit_timeouts"] == 0
    assert summary["activity"]["max_open_trades"] == 1
    assert summary["activity"]["locks"] == 0
    assert summary["activity"]["total_volume"] == pytest.approx(1494.4921414423)
    assert summary["risk"]["max_closed_trade_drawdown_ratio"] > 0
    assert summary["risk"]["maximum_consecutive_wins"] == 3
    assert summary["risk"]["maximum_consecutive_losses"] == 2
    assert summary["breakdowns"]["by_pair"][0]["pair"] == "BTC/USDT"
    assert summary["breakdowns"]["by_open_pair"] == []
    assert summary["breakdowns"]["by_entry_exit"][0]["entry_tag"] == "contract_route"
    assert summary["breakdowns"]["by_day"][0]["day"] == "2025-01-01"
    assert summary["breakdowns"]["by_month"][0]["month"] == "2025-01"
    assert len(summary["equity_curve"]["points"]) == 7
    assert summary["futures"] is None


def test_zero_trade_summary_keeps_profile_pair_in_freqtrade_report() -> None:
    surface = read_json(FIXTURE)
    surface["trades"] = []
    surface["summary"].update(
        {
            "total_trades": 0,
            "final_balance": surface["summary"]["starting_balance"],
            "profit_total_abs": "0",
            "total_volume": "0",
        }
    )
    report = _run_report()
    report["result"] = {
        "execution": {
            "profile": {
                "phases": {
                    "input": {
                        "pair_identities": ["BTC/USDT|5m"],
                    }
                }
            }
        }
    }

    summary = build_result_summary(report, surface)

    assert summary["breakdowns"]["by_pair"] == [
        {
            "pair": "BTC/USDT",
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "win_rate": 0.0,
            "profit_abs": 0.0,
            "profit_ratio_sum": 0.0,
            "average_profit_ratio": 0.0,
            "average_duration_minutes": 0.0,
        }
    ]


def test_summary_exposes_exact_futures_lifecycle_metrics() -> None:
    summary = build_result_summary(_run_report(), read_json(FUTURES_FIXTURE))
    futures = summary["futures"]

    assert futures["margin_mode"] == "isolated"
    assert futures["long_trades"] == 3
    assert futures["short_trades"] == 0
    assert futures["funded_trades"] == 2
    assert futures["funding_total"] == pytest.approx(784.210046632299)
    assert futures["liquidation_exits"] == 1
    assert futures["protection_locks"] == 2
    assert futures["distinct_leverages"] == 1
    assert futures["minimum_leverage"] == 5.0
    assert futures["maximum_leverage"] == 5.0
    assert futures["by_leverage"][0]["leverage"] == "5"


def test_summary_represents_a_safe_block_without_fake_trading_metrics() -> None:
    run = _run_report()
    run.update(
        status="blocked_unsupported_semantics",
        complete=False,
        result=None,
        capability={
            "blockers": [
                {
                    "code": "EXACT_LOWERING_REVIEW_REQUIRED",
                    "callback": "custom_exit",
                    "message": "new callback shape",
                }
            ]
        },
    )

    summary = build_result_summary(run, None)

    assert summary["performance"] is None
    assert summary["risk"] is None
    assert summary["activity"]["trades"] == 0
    assert summary["blockers"] == [
        {
            "code": "EXACT_LOWERING_REVIEW_REQUIRED",
            "callback": "custom_exit",
            "message": "new callback shape",
        }
    ]


def test_pair_breakdown_includes_configured_pairs_without_trades() -> None:
    run = _run_report()
    run["vectors"]["pair_count"] = 2
    run["execution"]["workload_calibration"] = {
        "identity": {
            "pairs": [
                {"pair": "BTC/USDT"},
                {"pair": "ETH/USDT"},
            ]
        }
    }

    summary = build_result_summary(run, read_json(FIXTURE))
    pairs = {row["pair"]: row for row in summary["breakdowns"]["by_pair"]}

    assert pairs["BTC/USDT"]["trades"] == 6
    assert pairs["ETH/USDT"] == {
        "pair": "ETH/USDT",
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "draws": 0,
        "win_rate": 0.0,
        "profit_abs": 0.0,
        "profit_ratio_sum": 0.0,
        "average_profit_ratio": 0.0,
        "average_duration_minutes": 0.0,
    }
