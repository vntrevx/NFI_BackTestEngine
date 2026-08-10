from __future__ import annotations

import copy
from pathlib import Path

import pytest
from nfi_backtest_engine import stateful_execution_policy
from nfi_backtest_engine.hot_ir import build_hot_callback_ir
from nfi_backtest_engine.stateful_execution_policy import (
    GENERIC_VECTOR_TRANSPORT,
    X7_GENERIC_STATEFUL_LANE,
    X7_VECTOR_TRANSPORT,
    build_native_execution_policy,
    build_x7_generic_stateful_policy,
)
from nfi_backtest_engine.strategy_ir import analyze_strategy

_X7_SOURCE = Path(
    "benchmarks/fixtures/captured/"
    "x7-futures-lifecycle-short-v17.4.435-2022-04-01_04-20/inputs/strategy.py"
)


def _program(schema_version: str, execution_mode: str | None) -> dict:
    program = {"schema_version": schema_version}
    if execution_mode is not None:
        program["execution_mode"] = execution_mode
    return program


def _manager() -> dict:
    return {
        "schema_version": "contract-under-test",
        "managed_exit_program": _program(
            "managed-exit-program-v1",
            "primary",
        ),
        "managed_short_exit_program": _program(
            "managed-exit-program-v1",
            "primary",
        ),
        "position_adjustment": {
            "program": _program(
                "system-adjustment-program-v1",
                "primary",
            )
        },
        "rebuy_adjustment": {
            "program": _program("adjustment-transition-program-v1", "primary")
        },
        "supported_routes": {
            "source_defined_route": {
                "program": _program(
                    "grind-transition-program-v3",
                    "primary",
                )
            }
        },
    }


def _x7_hot_ir() -> dict:
    return {
        "hot_loop_ready": True,
        "callbacks": [
            {
                "name": "custom_exit",
                "active_for_run": True,
                "backend": "rust-nfi-x7-trade-manager",
            }
        ],
    }


def test_x7_generic_stateful_programs_are_the_default_native_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        stateful_execution_policy,
        "_nfi_trade_manager_config",
        lambda _hot_ir: _manager(),
    )

    policy = build_native_execution_policy(
        _x7_hot_ir(),
        state_machine_program=None,
    )

    assert policy["adapter_lane"] == X7_GENERIC_STATEFUL_LANE
    assert policy["transport"] == X7_VECTOR_TRANSPORT
    assert policy["primary"] == "source-compiled-generic-stateful-programs"
    assert policy["native_ready"] is True
    assert policy["blockers"] == []
    assert len(policy["programs"]) == 5
    assert policy["legacy_shadow"] == {
        "enabled": False,
        "program_count": 0,
        "comparison": None,
        "mismatch_action": None,
        "removal_gate": None,
    }


def test_x7_policy_discovers_source_defined_route_keys_as_data() -> None:
    manager = _manager()
    manager["supported_routes"]["new_upstream_route"] = {
        "regular_program": _program(
            "regular-transition-program-v1",
            "primary",
        )
    }

    policy = build_x7_generic_stateful_policy(manager)

    assert any(
        program["path"].endswith("new_upstream_route.regular_program")
        for program in policy["programs"]
    )
    assert policy["native_ready"] is True


def test_x7_policy_fails_closed_when_a_generic_primary_mode_is_missing() -> None:
    manager = _manager()
    del manager["position_adjustment"]["program"]["execution_mode"]

    policy = build_x7_generic_stateful_policy(manager)

    assert policy["native_ready"] is False
    assert policy["blockers"] == [
        {
            "code": "GENERIC_STATEFUL_EXECUTION_MODE_MISSING",
            "program_path": "nfi_x7_trade_manager.position_adjustment.program",
            "message": "stateful program does not declare its Native primary mode",
        }
    ]


def test_x7_policy_rejects_a_retired_legacy_shadow_mode() -> None:
    manager = copy.deepcopy(_manager())
    manager["managed_exit_program"]["execution_mode"] = "primary-with-legacy-shadow"

    policy = build_x7_generic_stateful_policy(manager)

    assert policy["native_ready"] is False
    assert policy["blockers"][0]["code"] == (
        "GENERIC_STATEFUL_EXECUTION_MODE_UNSUPPORTED"
    )
    assert policy["official_fallback"] == {
        "available_on_native_blocker": True,
        "activation": "ask-or-explicit",
        "announcement": "required-before-execution",
        "native_evidence_mutation": False,
    }


def test_x7_lane_turns_a_malformed_serialized_contract_into_a_fallback_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def malformed(_hot_ir: dict) -> dict:
        raise KeyError("source_route")

    monkeypatch.setattr(
        stateful_execution_policy,
        "_nfi_trade_manager_config",
        malformed,
    )

    policy = build_native_execution_policy(
        _x7_hot_ir(),
        state_machine_program=None,
    )

    assert policy["adapter_lane"] == X7_GENERIC_STATEFUL_LANE
    assert policy["native_ready"] is False
    assert policy["blockers"] == [
        {
            "code": "GENERIC_STATEFUL_CONTRACT_INVALID",
            "message": "KeyError: 'source_route'",
        }
    ]
    assert policy["official_fallback"]["available_on_native_blocker"] is True


def test_existing_generic_state_machine_lane_remains_generic() -> None:
    program = {"schema_version": "state-machine-program-v3"}

    policy = build_native_execution_policy(
        {"hot_loop_ready": False, "callbacks": []},
        state_machine_program=program,
    )

    assert policy["adapter_lane"] == "generic-state-machine"
    assert policy["transport"] == GENERIC_VECTOR_TRANSPORT
    assert policy["native_ready"] is True


def test_signal_only_lane_does_not_claim_a_stateful_shadow() -> None:
    policy = build_native_execution_policy(
        {"hot_loop_ready": True, "callbacks": []},
        state_machine_program=None,
    )

    assert policy["adapter_lane"] == "generic-signal"
    assert policy["transport"] == GENERIC_VECTOR_TRANSPORT
    assert policy["legacy_shadow"]["enabled"] is False


def test_captured_x7_contract_selects_nine_generic_stateful_roots() -> None:
    analysis = analyze_strategy(_X7_SOURCE, class_name="NostalgiaForInfinityX7")
    hot_ir = build_hot_callback_ir(
        analysis,
        trading_mode="futures",
        run_mode="backtest",
        config={"trading_mode": "futures"},
    )

    policy = build_native_execution_policy(hot_ir, state_machine_program=None)

    assert policy["adapter_lane"] == X7_GENERIC_STATEFUL_LANE
    assert policy["native_ready"] is True
    assert len(policy["programs"]) == 9
    assert policy["legacy_shadow"]["program_count"] == 0
    assert {program["execution_mode"] for program in policy["programs"]} == {"primary"}
