from __future__ import annotations

import ast

from nfi_backtest_engine.x7 import legacy


def test_legacy_grind_constant_families_have_no_fixed_level_ceiling() -> None:
    constants = {
        "grinding_v1_max_stake": 1.0,
        "grind_mode_stake_multiplier_futures": [0.2],
        "grind_mode_stake_multiplier_spot": [0.2],
        "regular_mode_derisk_1_reentry_futures": -0.1,
        "regular_mode_derisk_1_reentry_spot": -0.1,
        **_legacy_level("grind_12"),
        **_legacy_level("grind_7_derisk_1"),
    }

    result = legacy._build_legacy_grind_constants(constants)

    assert [
        (cluster["entry_tag"], cluster["stop_tag"])
        for cluster in result["clusters"]
    ] == [("gd12", "dd12"), ("dl7", "ddl7")]


def test_regular_grind_constant_families_have_no_fixed_level_ceiling(
    monkeypatch,
) -> None:
    constants = {
        "regular_mode_use_grind_stops": True,
        "derisk_enable": True,
        "regular_mode_rebuy_stakes_futures": [0.2],
        "regular_mode_rebuy_thresholds_futures": [-0.1],
        "regular_mode_rebuy_stakes_spot": [0.2],
        "regular_mode_rebuy_thresholds_spot": [-0.1],
        "regular_mode_derisk_futures": -0.6,
        "regular_mode_derisk_spot": -0.6,
        "regular_mode_derisk_1_futures": -0.4,
        "regular_mode_derisk_1_spot": -0.4,
        **_regular_level(12),
    }
    monkeypatch.setattr(
        legacy,
        "_regular_adjustment_literal_policy",
        lambda _method: {
            "entry_retry_ms": 1,
            "grind_force_order_age_ms": 2,
            "grind_order_age_ms": 3,
            "rebuy_order_age_ms": 4,
            "grind_entry_profit_gate": -0.02,
            "additional_grind_profit_gate": -0.03,
            "forced_age_profit_gate": -0.06,
            "minimum_entry_multiplier": 1.5,
            "minimum_remaining_multiplier": 1.55,
        },
    )

    method = ast.parse("def adjust_trade_position():\n    pass\n").body[0]
    assert isinstance(method, ast.FunctionDef)
    result = legacy._build_regular_adjustment_constants(
        constants,
        method,
        {
            "source_order": [
                {
                    "kind": "grind",
                    "level": 12,
                    "entry_tag": "future-lane",
                    "stop_tag": "future-lane-stop",
                }
            ]
        },
    )

    assert [(item["entry_tag"], item["stop_tag"]) for item in result["grinds"]] == [
        ("future-lane", "future-lane-stop")
    ]


def _legacy_level(prefix: str) -> dict[str, object]:
    result: dict[str, object] = {}
    for mode in ("futures", "spot"):
        result[f"{prefix}_stakes_{mode}"] = [0.2]
        result[f"{prefix}_sub_thresholds_{mode}"] = [-0.1]
        result[f"{prefix}_stop_grinds_{mode}"] = -0.2
        result[f"{prefix}_profit_threshold_{mode}"] = 0.02
    return result


def _regular_level(level: int) -> dict[str, object]:
    prefix = f"regular_mode_grind_{level}"
    result: dict[str, object] = {}
    for mode in ("futures", "spot"):
        result[f"{prefix}_stakes_{mode}"] = [0.2]
        result[f"{prefix}_thresholds_{mode}"] = [-0.1]
        result[f"{prefix}_stop_grinds_{mode}"] = -0.2
        result[f"{prefix}_profit_threshold_{mode}"] = 0.02
    return result
