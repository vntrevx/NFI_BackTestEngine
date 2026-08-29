"""Versioned Native-lane policy for source-compiled stateful programs."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .errors import StrategyAnalysisError
from .x7.serialization import _nfi_trade_manager_config

STATEFUL_EXECUTION_POLICY_VERSION = "1.2.0"
X7_GENERIC_STATEFUL_LANE = "x7-generic-stateful"
X7_VECTOR_TRANSPORT = "x7-vector-manifest"
GENERIC_VECTOR_TRANSPORT = "generic-vector-manifest"
FULL_NATIVE_VECTOR_TRANSPORT = "full-native-vector-manifest"

_STATEFUL_PROGRAM_SCHEMA_PREFIXES = (
    "adjustment-transition-program-",
    "grind-transition-program-",
    "managed-exit-program-",
    "regular-transition-program-",
    "system-adjustment-program-",
)
_PRIMARY_EXECUTION_MODES = frozenset({"primary"})


def build_native_execution_policy(
    hot_ir: dict[str, Any],
    *,
    state_machine_program: dict[str, Any] | None,
    executable_callback_program: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe and validate the Native primary lane without strategy identity gates."""

    if executable_callback_program is not None:
        return _finalize_policy(
            {
                "schema_version": STATEFUL_EXECUTION_POLICY_VERSION,
                "adapter_lane": "generic-executable-callback",
                "transport": GENERIC_VECTOR_TRANSPORT,
                "primary": "source-compiled-executable-callback-program",
                "programs": [
                    {
                        "path": "executable_callback.program",
                        "schema_version": executable_callback_program.get("schema_version"),
                        "execution_mode": "primary",
                    }
                ],
                "legacy_shadow": _shadow_contract(0),
                "official_fallback": _official_fallback_contract(),
                "blockers": [],
            }
        )

    if state_machine_program is not None and not hot_ir.get("hot_loop_ready"):
        return _finalize_policy(
            {
                "schema_version": STATEFUL_EXECUTION_POLICY_VERSION,
                "adapter_lane": "generic-state-machine",
                "transport": GENERIC_VECTOR_TRANSPORT,
                "primary": "source-compiled-state-machine-program",
                "programs": [
                    {
                        "path": "state_machine.program",
                        "schema_version": state_machine_program.get("schema_version"),
                        "execution_mode": "primary",
                    }
                ],
                "legacy_shadow": _shadow_contract(0),
                "official_fallback": _official_fallback_contract(),
                "blockers": [],
            }
        )

    if _x7_trade_manager_selected(hot_ir):
        try:
            manager = _nfi_trade_manager_config(hot_ir)
        except (KeyError, TypeError, StrategyAnalysisError) as exc:
            return _x7_policy(
                programs=[],
                blockers=[
                    {
                        "code": "GENERIC_STATEFUL_CONTRACT_INVALID",
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                ],
            )
        if manager is None:
            return _x7_policy(
                programs=[],
                blockers=[
                    {
                        "code": "GENERIC_STATEFUL_CONTRACT_MISSING",
                        "message": "selected X7 callback manager has no serializable contract",
                    }
                ],
            )
        return build_x7_generic_stateful_policy(manager)

    return _finalize_policy(
        {
            "schema_version": STATEFUL_EXECUTION_POLICY_VERSION,
            "adapter_lane": "generic-signal",
            "transport": GENERIC_VECTOR_TRANSPORT,
            "primary": "source-compiled-vector-and-callback-programs",
            "programs": [],
            "legacy_shadow": _shadow_contract(0),
            "official_fallback": _official_fallback_contract(),
            "blockers": [],
        }
    )


def build_x7_generic_stateful_policy(manager: dict[str, Any]) -> dict[str, Any]:
    """Prove that every serialized X7 stateful root selects a generic primary."""

    programs = _stateful_program_inventory(manager)
    blockers: list[dict[str, Any]] = []
    if not programs:
        blockers.append(
            {
                "code": "GENERIC_STATEFUL_PROGRAM_MISSING",
                "message": "X7 manager has no source-compiled stateful primary program",
            }
        )
    for program in programs:
        mode = program["execution_mode"]
        if mode is None:
            blockers.append(
                {
                    "code": "GENERIC_STATEFUL_EXECUTION_MODE_MISSING",
                    "program_path": program["path"],
                    "message": "stateful program does not declare its Native primary mode",
                }
            )
        elif mode not in _PRIMARY_EXECUTION_MODES:
            blockers.append(
                {
                    "code": "GENERIC_STATEFUL_EXECUTION_MODE_UNSUPPORTED",
                    "program_path": program["path"],
                    "execution_mode": mode,
                    "message": "stateful program is not configured as a generic Native primary",
                }
            )
    return _x7_policy(programs=programs, blockers=blockers)


def _x7_policy(
    *,
    programs: list[dict[str, Any]],
    blockers: list[dict[str, Any]],
) -> dict[str, Any]:
    shadow_count = sum(
        program.get("execution_mode") == "primary-with-legacy-shadow"
        for program in programs
    )
    return _finalize_policy(
        {
            "schema_version": STATEFUL_EXECUTION_POLICY_VERSION,
            "adapter_lane": X7_GENERIC_STATEFUL_LANE,
            "transport": FULL_NATIVE_VECTOR_TRANSPORT,
            "primary": "source-compiled-full-native-and-generic-stateful-programs",
            "programs": programs,
            "legacy_shadow": _shadow_contract(shadow_count),
            "official_fallback": _official_fallback_contract(),
            "blockers": blockers,
        }
    )


def add_native_execution_blockers(
    policy: dict[str, Any],
    blockers: list[dict[str, Any]],
) -> dict[str, Any]:
    """Append source-compiler blockers and reseal the policy fingerprint."""
    if not blockers:
        return policy
    base = {
        key: value
        for key, value in policy.items()
        if key not in {"fingerprint", "native_ready"}
    }
    base["blockers"] = [*policy.get("blockers", []), *blockers]
    return _finalize_policy(base)


def _stateful_program_inventory(manager: dict[str, Any]) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            schema_version = value.get("schema_version")
            if isinstance(schema_version, str) and schema_version.startswith(
                _STATEFUL_PROGRAM_SCHEMA_PREFIXES
            ):
                mode = value.get("execution_mode")
                inventory.append(
                    {
                        "path": path,
                        "schema_version": schema_version,
                        "execution_mode": mode if isinstance(mode, str) else None,
                    }
                )
            for name, nested in value.items():
                visit(nested, f"{path}.{name}")
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                visit(nested, f"{path}[{index}]")

    visit(manager, "nfi_x7_trade_manager")
    return sorted(inventory, key=lambda item: item["path"])


def _x7_trade_manager_selected(hot_ir: dict[str, Any]) -> bool:
    callbacks = hot_ir.get("callbacks")
    return isinstance(callbacks, list) and any(
        isinstance(callback, dict)
        and callback.get("name") == "custom_exit"
        and callback.get("active_for_run") is True
        and callback.get("backend") == "rust-nfi-x7-trade-manager"
        for callback in callbacks
    )


def _shadow_contract(program_count: int) -> dict[str, Any]:
    return {
        "enabled": program_count > 0,
        "program_count": program_count,
        "comparison": "decision-and-state-exact" if program_count > 0 else None,
        "mismatch_action": "fail-closed" if program_count > 0 else None,
        "removal_gate": "independent-exact-proof" if program_count > 0 else None,
    }


def _official_fallback_contract() -> dict[str, Any]:
    return {
        "available_on_native_blocker": True,
        "activation": "ask-or-explicit",
        "announcement": "required-before-execution",
        "native_evidence_mutation": False,
    }


def _finalize_policy(policy: dict[str, Any]) -> dict[str, Any]:
    identity = {**policy, "native_ready": not policy["blockers"]}
    fingerprint = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {**identity, "fingerprint": fingerprint}
