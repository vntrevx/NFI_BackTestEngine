from __future__ import annotations

from pathlib import Path

from nfi_backtest_engine.canonical import read_json


def test_x7_ape_evidence_keeps_the_public_claim_narrow() -> None:
    """Prevent documentation edits from widening a one-route certificate."""
    root = Path(__file__).resolve().parents[1]
    evidence = read_json(root / "benchmarks/evidence/x7-ape-top-coins-v17.4.413.json")

    comparison = evidence["exact_comparison"]
    assert evidence["status"] == "captured-final-surface-exact"
    assert evidence["claim_boundary"] == ("APE/USDT spot top-coins route only; not full X7")
    assert comparison["equal"] is True
    assert comparison["numeric_tolerance"] == 0
    assert comparison["official_surface_sha256"] == comparison["engine_surface_sha256"]
    assert len(comparison["official_surface_sha256"]) == 64
    assert evidence["scope"]["nfi_trade_manager_schema"] == "0.7.0"
    assert evidence["scope"]["x7_adapter_version"] == "0.10.0"
    assert len(evidence["sealed_artifacts"]["vector_feather_sha256"]) == 64


def test_x7_rebuy_evidence_does_not_claim_an_unreached_adjustment() -> None:
    root = Path(__file__).resolve().parents[1]
    evidence = read_json(root / "benchmarks/evidence/x7-ape-rebuy-exit-v17.4.413.json")

    comparison = evidence["exact_comparison"]
    assert evidence["claim_boundary"] == (
        "APE/USDT spot tag-62 rebuy exit route only; adjustment ladder not reached"
    )
    assert evidence["oracle"]["network_during_backtest"] is False
    assert comparison["equal"] is True
    assert comparison["entry_tag"] == "62"
    assert comparison["adjustment_orders"] == 0
    assert comparison["official_surface_sha256"] == comparison["engine_surface_sha256"]


def test_x7_legacy_grind_evidence_names_only_reached_branches() -> None:
    root = Path(__file__).resolve().parents[1]
    evidence = read_json(root / "benchmarks/evidence/x7-zec-legacy-grind-v17.4.413.json")

    comparison = evidence["exact_comparison"]
    assert evidence["claim_boundary"] == (
        "ZEC/USDT spot tag-120 legacy grind route; fixture reaches gm0, gd1, and gd2 only"
    )
    assert evidence["oracle"]["network_during_backtest"] is False
    assert evidence["scope"]["adjustment_scope"] == "spot-grind-backtest-v1"
    assert evidence["scope"]["nfi_trade_manager_schema"] == "0.7.0"
    assert evidence["scope"]["x7_adapter_version"] == "0.10.0"
    assert comparison["equal"] is True
    assert comparison["numeric_tolerance"] == 0
    assert comparison["trades"] == 1
    assert comparison["orders"] == 13
    assert comparison["adjustment_orders"] == 11
    assert comparison["reached_adjustment_tags"] == ["gm0", "gd1", "gd2"]
    assert comparison["official_surface_sha256"] == comparison["engine_surface_sha256"]


def test_x7_static_inventory_keeps_dormant_tag_121_out_of_entry_branches() -> None:
    root = Path(__file__).resolve().parents[1]
    evidence = read_json(root / "benchmarks/evidence/x7-v17.4.413-static-entry-inventory.json")

    indices = evidence["literal_condition_indices"]["populate_entry_trend"]
    assert evidence["status"] == "source-static-inventory"
    assert evidence["strategy"]["strategy_ir_version"] == "1.6.0"
    assert len(evidence["strategy"]["source_sha256"]) == 64
    assert indices["long_entry_condition_index"][-2:] == [162, 163]
    assert 120 in indices["long_entry_condition_index"]
    assert 121 not in indices["long_entry_condition_index"]
    assert evidence["dormant_route_tags"] == ["121"]
    assert evidence["safety_policy"] == (
        "fail-before-simulation if a vector emits a dormant or unsupported tag"
    )


def test_x7_shared_slot_evidence_keeps_the_multi_pair_claim_narrow() -> None:
    """Pin the one captured pair-order conflict without implying arbitrary X7 parity."""
    root = Path(__file__).resolve().parents[1]
    evidence = read_json(
        root / "benchmarks/evidence/x7-ape-aave-shared-slot-v17.4.413.json"
    )

    comparison = evidence["exact_comparison"]
    assert evidence["claim_boundary"] == (
        "APE/AAVE spot equal-timestamp max_open_trades=1 slot competition only; "
        "not full X7"
    )
    assert evidence["oracle"]["network_during_backtest"] is False
    assert evidence["scope"]["pair_order"] == ["APE/USDT", "AAVE/USDT"]
    assert evidence["scope"]["execution_start_index"] == {
        "APE/USDT": 801,
        "AAVE/USDT": 801,
    }
    assert comparison["equal"] is True
    assert comparison["numeric_tolerance"] == 0
    assert [item["pair"] for item in comparison["entry_candidates"]] == [
        "APE/USDT",
        "AAVE/USDT",
    ]
    assert comparison["selected_pair"] == "APE/USDT"
    assert comparison["rejected_pair"] == "AAVE/USDT"
    assert comparison["rejected_signals"] == 1
    assert comparison["maximum_concurrent_trades"] == 1
    assert comparison["official_surface_sha256"] == comparison["engine_surface_sha256"]


def test_x7_futures_annual_evidence_is_exact_without_widening_the_claim() -> None:
    """Pin the annual certificate while leaving unobserved futures paths blocked."""
    root = Path(__file__).resolve().parents[1]
    evidence = read_json(
        root / "benchmarks/evidence/x7-ape-futures-2022-v17.4.413.json"
    )

    comparison = evidence["exact_comparison"]
    assert evidence["status"] == "captured-final-surface-exact"
    assert evidence["claim_boundary"] == (
        "APE/USDT:USDT single-pair futures from 2022-04-01 through 2023-01-01; "
        "reached long, short, leverage, funding, derisk, and grind orders; "
        "does not certify arbitrary pairs, liquidation, protections, or pair locks"
    )
    assert evidence["oracle"]["version"] == "2026.5.1"
    assert evidence["oracle"]["network_during_backtest"] is False
    assert evidence["oracle"]["image_digest"].startswith("sha256:")
    assert evidence["scope"]["trading_mode"] == "futures"
    assert evidence["scope"]["nfi_trade_manager_schema"] == "0.8.0"
    assert evidence["scope"]["x7_adapter_version"] == "0.13.0"
    assert evidence["scope"]["data_seal_version"] == "1.2.0"
    assert evidence["scope"]["vector_pipeline_version"] == "1.12.0"
    assert evidence["scope"]["vector_runtime"] == {
        "python": "3.12.3",
        "numpy": "2.4.5",
        "pandas": "3.0.3",
        "pyarrow": "24.0.0",
        "ta_lib": "0.6.8",
    }
    assert comparison["equal"] is True
    assert comparison["numeric_tolerance"] == 0
    assert comparison["first_difference"] is None
    assert comparison["trades"] == 11
    assert comparison["orders"] == 164
    assert comparison["adjustment_orders"] == 142
    assert comparison["short_trades"] == 1
    assert comparison["funded_trades"] == 8
    assert comparison["locks"] == 0
    assert comparison["official_surface_sha256"] == comparison["engine_surface_sha256"]
    assert len(comparison["official_surface_sha256"]) == 64


def test_latest_x7_futures_evidence_pins_collision_fix_without_widening_scope() -> None:
    """Keep the daily-source proof distinct from actual liquidation-event coverage."""
    root = Path(__file__).resolve().parents[1]
    evidence = read_json(
        root / "benchmarks/evidence/x7-ape-futures-2022-v17.4.418.json"
    )

    comparison = evidence["exact_comparison"]
    assert evidence["scope"]["strategy_version"] == "17.4.418"
    assert evidence["scope"]["nfi_trade_manager_schema"] == "0.9.0"
    assert evidence["scope"]["x7_adapter_version"] == "0.14.0"
    assert evidence["oracle"]["network_during_backtest"] is False
    assert "without certifying a liquidation exit" in evidence["claim_boundary"]
    assert "tag 121" in evidence["claim_boundary"]
    assert "protections" in evidence["claim_boundary"]
    assert "pair locks" in evidence["claim_boundary"]
    assert comparison["equal"] is True
    assert comparison["numeric_tolerance"] == 0
    assert comparison["trades"] == 11
    assert comparison["orders"] == 164
    assert comparison["adjustment_orders"] == 142
    assert comparison["liquidation_exits"] == 0
    assert comparison["official_surface_sha256"] == comparison["engine_surface_sha256"]
    assert evidence["resource_observation"][
        "timings_are_not_a_full_pipeline_speed_certificate"
    ] is True


def test_host_scaling_evidence_is_explicitly_diagnostic_only() -> None:
    """Keep one-host process scaling separate from the public speed certificate."""
    root = Path(__file__).resolve().parents[1]
    evidence = read_json(
        root / "benchmarks/evidence/host-scaling-x7-prepare-2026-07-19.json"
    )

    observation = evidence["observation"]
    assert evidence["status"] == "diagnostic-only"
    assert evidence["claim_boundary"] == (
        "four concurrent, identical single-pair annual X7 prepare-only jobs on one "
        "WSL2 development host; not an engine speedup, 80-pair benchmark, or "
        "cross-host performance claim"
    )
    assert evidence["workload"]["job_count"] == 4
    assert evidence["automatic_tuning"]["parallel_job_processes"] == 4
    assert evidence["automatic_tuning"]["nested_numeric_threads_per_process"] == 1
    assert observation["complete"] is True
    assert len(set(observation["worker_process_ids"])) == 4
    assert 2.9 < observation["effective_parallelism"] < 3.1
    assert 0.7 < observation["parallel_efficiency"] < 0.8


def test_x7_80pair_five_year_native_evidence_stays_diagnostic() -> None:
    """Pin the large native A/B result without promoting it to official parity."""
    root = Path(__file__).resolve().parents[1]
    evidence = read_json(
        root / "benchmarks/evidence/x7-80pair-spot-5y-native-2026-07-20.json"
    )

    comparison = evidence["native_same_input_comparison"]
    inputs = evidence["sealed_inputs"]
    speedup = evidence["diagnostic_speedup"]
    official = evidence["official_confirmation"]

    assert evidence["status"] == "native-complete-diagnostic"
    assert evidence["scope"]["strategy_version"] == "17.4.418"
    assert evidence["scope"]["pair_count"] == 80
    assert evidence["scope"]["timerange"] == "20210101-20260101"
    assert evidence["scope"]["history_coverage_policy"] == "available"
    assert inputs["coverage_shortfall_count"] == 275
    assert inputs["data_checkpoint_reused"] is True
    assert inputs["vector_checkpoint_reused"] is True
    assert comparison["equal"] is True
    assert comparison["byte_equal"] is True
    assert comparison["numeric_tolerance"] == 0
    assert comparison["baseline_result_sha256"] == comparison[
        "optimized_result_sha256"
    ]
    assert len(comparison["optimized_result_sha256"]) == 64
    assert comparison["trades"] == 750
    assert speedup["core_process_wall_ratio"] > 2.6
    assert speedup["event_loop_ratio"] > 3.4
    assert speedup["controlled_repetition_count"] == 1
    assert speedup["release_speed_certificate"] is False
    assert official == {
        "required_for_release_claim": True,
        "status": "blocked_oom",
        "evidence": "x7-80pair-spot-5y-official-oom-2026-07-20.json",
        "freqtrade_surface_equal": None,
    }


def test_x7_80pair_official_oom_is_not_misreported_as_parity() -> None:
    """Keep an exhausted Docker budget distinct from a semantic mismatch."""
    root = Path(__file__).resolve().parents[1]
    evidence = read_json(
        root / "benchmarks/evidence/x7-80pair-spot-5y-official-oom-2026-07-20.json"
    )

    observation = evidence["observation"]
    policy = evidence["container_policy"]
    next_safe = evidence["next_safe_verification"]

    assert evidence["status"] == "official-blocked-oom"
    assert evidence["scope"]["pair_count"] == 80
    assert evidence["scope"]["timerange"] == "20210101-20260101"
    assert evidence["scope"]["include_inactive_configured_pairs"] is True
    assert policy["execution_mode"] == "sequential"
    assert policy["maximum_parallel_containers"] == 1
    assert observation["exit_code"] == 137
    assert observation["memory_verdict"] == "oom_killed"
    assert observation["peak_bytes"] <= policy["container_memory_limit_bytes"]
    assert observation["peak_ratio"] > 0.999
    assert observation["oom_kill_count"] == 1
    assert observation["official_result_produced"] is False
    assert observation["parity_evaluated"] is False
    assert next_safe["continuous_five_year_state_claim"] is False


def test_x7_80pair_bounded_official_surface_is_exact_without_a_five_year_claim() -> None:
    """Pin the broad bounded proof without hiding the continuous-run OOM boundary."""
    root = Path(__file__).resolve().parents[1]
    evidence = read_json(
        root / "benchmarks/evidence/x7-80pair-spot-2025h2-parity-2026-07-20.json"
    )

    comparison = evidence["exact_comparison"]
    resources = evidence["resource_observation"]
    limitations = evidence["limitations"]

    assert evidence["status"] == "captured-bounded-final-surface-exact"
    assert evidence["scope"]["strategy_version"] == "17.4.418"
    assert evidence["scope"]["pair_count"] == 80
    assert evidence["scope"]["timerange"] == "20250701-20260101"
    assert evidence["scope"]["nfi_trade_manager_schema"] == "0.10.0"
    assert comparison["equal"] is True
    assert comparison["byte_equal"] is True
    assert comparison["numeric_tolerance"] == 0
    assert comparison["first_difference"] is None
    assert comparison["trades"] == 167
    assert comparison["orders"] == 402
    assert comparison["rejected_signals"] == 23
    assert comparison["official_surface_sha256"] == comparison[
        "engine_surface_sha256"
    ]
    assert resources["observed_wall_ratio"] > 4.2
    assert resources["observed_peak_memory_ratio"] > 100
    assert "not a cold end-to-end" in resources["comparison_note"]
    assert any("five-year official run remains blocked" in item for item in limitations)
