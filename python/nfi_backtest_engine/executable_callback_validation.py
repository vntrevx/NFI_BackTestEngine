"""Fail-closed structural and identity validation for executable callback programs."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, NoReturn

from .errors import StrategyAnalysisError

SCHEMA_VERSION = "executable-callback-program-v1"
CONTRACT_FILE_SHA256 = "a2cd2bf7ea60b131885122a2b5a308ba64f610942ce3869fda08c6dc3a258576"
CONTRACT_FINGERPRINT = "7c26cbaea6853a20b93932dbc0f3bc788cf0d43e58f243e9985029a727d6ec7f"
ENTRYPOINTS = {"loop_cadence_startup_lookback", "bot_loop_start", "leverage",
               "custom_stake_amount", "confirm_trade_entry", "order_filled",
               "adjust_trade_position", "custom_stoploss", "custom_exit", "confirm_trade_exit"}
STATEMENTS = {"let", "set_register", "set_register_item", "set_custom_state",
              "delete_custom_state", "if", "for_range", "return", "raise_callback",
              "emit_observation"}
EXPRESSIONS = {"literal", "read_input", "read_local", "read_register", "read_trade",
               "read_order", "read_candle", "read_wallet", "read_custom_state", "record",
               "list", "tuple", "index", "unary", "binary", "compare", "and", "or",
               "choose", "call_builtin", "timestamp_ms", "map_get"}
INPUT_TYPES = {"callback_dataframe": "record", "callback_dataframe_empty": "bool",
               "last_visible_timestamp_seconds": "f64", "visible_rows": "i64",
               "config.trading_mode": "string"}
HEX = re.compile(r"^[0-9a-f]{64}$")


def canonical_program_fingerprint(program: dict[str, Any]) -> str:
    value = copy.deepcopy(program)
    identity = value.get("identity")
    if not isinstance(identity, dict):
        _reject("CALLBACK_PROGRAM_FINGERPRINT_MISMATCH", "program identity is missing")
    identity.pop("program_fingerprint", None)
    closure = identity.get("source_closure", [])
    if isinstance(closure, list):
        for item in closure:
            if isinstance(item, dict):
                item.pop("diagnostic_path", None)
    try:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise StrategyAnalysisError(f"CALLBACK_PROGRAM_FINGERPRINT_MISMATCH: {exc}") from exc
    return hashlib.sha256(payload).hexdigest()


def validate_executable_callback_program(program: dict[str, Any]) -> None:
    if program.get("schema_version") != SCHEMA_VERSION:
        _reject("CALLBACK_PROGRAM_CONTRACT_IDENTITY_MISMATCH", "schema version differs")
    identity = program.get("identity")
    if not isinstance(identity, dict):
        _reject("CALLBACK_PROGRAM_CONTRACT_IDENTITY_MISMATCH", "identity is missing")
    if (
        identity.get("callback_contract_file_sha256") != CONTRACT_FILE_SHA256
        or identity.get("callback_contract_fingerprint") != CONTRACT_FINGERPRINT
    ):
        _reject("CALLBACK_PROGRAM_CONTRACT_IDENTITY_MISMATCH", "callback contract identity differs")
    for key in (
        "callback_contract_file_sha256",
        "callback_contract_fingerprint",
        "callback_execution_ir_fingerprint",
        "program_fingerprint",
        "selected_class_ast_sha256",
    ):
        if not isinstance(identity.get(key), str) or HEX.fullmatch(identity[key]) is None:
            _reject("CALLBACK_PROGRAM_CONTRACT_IDENTITY_MISMATCH", f"identity {key} is malformed")
    entrypoints = program.get("entrypoints")
    if not isinstance(entrypoints, dict) or set(entrypoints) != ENTRYPOINTS:
        _reject("CALLBACK_PROGRAM_INCOMPLETE_COVERAGE", "entrypoint set is not exact")
    register_ids = _validate_registers(program.get("registers"))
    _validate_custom_state(program.get("required_custom_state"))
    _validate_inputs(program.get("required_inputs"), entrypoints)
    for name, entrypoint in entrypoints.items():
        if not isinstance(entrypoint, dict) or entrypoint.get("name") != name:
            _reject(
                "CALLBACK_PROGRAM_DUPLICATE_ENTRYPOINT", f"entrypoint key/name differs for {name}"
            )
        instructions = entrypoint.get("instructions")
        if not isinstance(instructions, list) or not instructions:
            _reject("CALLBACK_PROGRAM_INCOMPLETE_COVERAGE", f"entrypoint {name} is empty")
        if (
            not isinstance(entrypoint.get("max_steps"), int)
            or not 0 < entrypoint["max_steps"] <= 4096
        ):
            _reject(
                "CALLBACK_PROGRAM_UNBOUNDED_CONTROL_FLOW",
                f"entrypoint {name} has invalid max_steps",
            )
        _validate_nodes(instructions, register_ids, statement_position=True)
    if identity["program_fingerprint"] != canonical_program_fingerprint(program):
        _reject("CALLBACK_PROGRAM_FINGERPRINT_MISMATCH", "program fingerprint differs")
    encoded = json.dumps(program, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > 1_048_576:
        _reject("CALLBACK_PROGRAM_UNBOUNDED_CONTROL_FLOW", "program exceeds byte limit")


def _validate_registers(value: object) -> set[str]:
    if not isinstance(value, list) or len(value) > 256:
        _reject("CALLBACK_PROGRAM_REGISTER_TYPE_UNRESOLVED", "register declarations are invalid")
    identifiers: set[str] = set()
    for item in value:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or item["id"] in identifiers
        ):
            _reject(
                "CALLBACK_PROGRAM_REGISTER_TYPE_CONFLICT", "register id is duplicate or malformed"
            )
        identifiers.add(item["id"])
        kind = item.get("type", {}).get("kind") if isinstance(item.get("type"), dict) else None
        if kind not in {"bool", "i64", "f64", "string", "timestamp_ms", "null", "list", "record"}:
            _reject(
                "CALLBACK_PROGRAM_REGISTER_TYPE_UNRESOLVED",
                f"register {item['id']} type is invalid",
            )
    return identifiers


def _validate_custom_state(value: object) -> None:
    if not isinstance(value, list):
        _reject(
            "CALLBACK_PROGRAM_DYNAMIC_CUSTOM_STATE_KEY", "custom state declarations are missing"
        )
    keys: set[str] = set()
    for item in value:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("key"), str)
            or item["key"] in keys
        ):
            _reject("CALLBACK_PROGRAM_DYNAMIC_CUSTOM_STATE_KEY", "custom state keys are invalid")
        keys.add(item["key"])


def _validate_inputs(value: object, entrypoints: dict[str, Any]) -> None:
    if not isinstance(value, list):
        _reject("CALLBACK_PROGRAM_REGISTER_TYPE_UNRESOLVED", "input declarations are missing")
    declared: dict[tuple[str, str], str] = {}
    for item in value:
        if not isinstance(item, dict):
            _reject("CALLBACK_PROGRAM_REGISTER_TYPE_UNRESOLVED", "input declaration malformed")
        entrypoint, name, type_ir = item.get("entrypoint"), item.get("name"), item.get("type")
        kind = type_ir.get("kind") if isinstance(type_ir, dict) else None
        if not isinstance(entrypoint, str) or not isinstance(name, str):
            _reject("CALLBACK_PROGRAM_REGISTER_TYPE_CONFLICT", "input declaration malformed")
        key, expected = (entrypoint, name), INPUT_TYPES.get(name)
        if key in declared or expected is None or kind != expected:
            _reject("CALLBACK_PROGRAM_REGISTER_TYPE_CONFLICT", f"input {name} type conflict")
        declared[key] = expected
    for entrypoint, body in entrypoints.items():
        for node in _walk_json(body.get("instructions", [])):
            if node.get("op") == "read_input":
                name = node.get("name")
                if not isinstance(name, str) or (entrypoint, name) not in declared:
                    _reject("CALLBACK_PROGRAM_REGISTER_TYPE_UNRESOLVED", f"input {name} undeclared")
            if node.get("op") == "compare":
                _validate_compare_input(node, entrypoint, declared)


def _validate_compare_input(
    node: dict[str, Any], entrypoint: str, declared: dict[tuple[str, str], str]
) -> None:
    left, comparisons = node.get("left"), node.get("comparisons")
    if not isinstance(left, dict) or left.get("op") != "read_input" \
            or not isinstance(comparisons, list):
        return
    first = comparisons[0] if comparisons else None
    right = first.get("right") if isinstance(first, dict) else None
    value = right.get("value") if isinstance(right, dict) and right.get("op") == "literal" else None
    inferred, input_name = _literal_type(value), left.get("name")
    if not isinstance(input_name, str):
        _reject("CALLBACK_PROGRAM_REGISTER_TYPE_UNRESOLVED", "comparison input malformed")
    if inferred is not None and declared.get((entrypoint, input_name)) != inferred:
        _reject("CALLBACK_PROGRAM_REGISTER_TYPE_CONFLICT", "comparison input type conflict")


def _literal_type(value: object) -> str | None:
    return {bool: "bool", str: "string", int: "i64", float: "f64"}.get(type(value))


def _validate_nodes(
    value: object, registers: set[str], *, statement_position: bool = False
) -> None:
    if isinstance(value, list):
        for item in value:
            _validate_nodes(item, registers, statement_position=statement_position)
        return
    if not isinstance(value, dict):
        return
    op = value.get("op")
    if isinstance(op, str):
        allowed = STATEMENTS if statement_position else EXPRESSIONS
        if op not in allowed:
            _reject(
                "CALLBACK_PROGRAM_UNKNOWN_OPCODE",
                f"unknown {'statement' if statement_position else 'expression'} opcode {op}",
            )
        if (
            op in {"set_register", "set_register_item", "read_register"}
            and value.get("register_id") not in registers
        ):
            _reject(
                "CALLBACK_PROGRAM_REGISTER_TYPE_UNRESOLVED",
                "instruction references unknown register",
            )
    for key, child in value.items():
        if key in {"op", "id", "predicate_ids"}:
            continue
        statement_child = (op == "if" and key in {"then", "otherwise"}) or (
            op == "for_range" and key == "body"
        )
        _validate_nodes(child, registers, statement_position=statement_child)


def _entrypoint_policy(name: str) -> dict[str, Any]:
    names = ["loop_cadence_startup_lookback", "bot_loop_start", "leverage",
             "custom_stake_amount", "confirm_trade_entry", "order_filled",
             "adjust_trade_position", "custom_stoploss", "custom_exit", "confirm_trade_exit"]
    cadences = ["synthetic_lifecycle", "once_per_main_candle", "per_initial_entry",
                "per_initial_entry", "per_initial_entry", "per_fill", "per_open_trade_candle",
                "per_open_trade_candle", "per_open_trade_candle", "per_exit_candidate"]
    returns = [["lifecycle-transition"], ["none"], ["finite-number"],
               ["finite-positive-number", "zero", "none"],
               ["truthy-accept", "falsy-reject"], ["none"],
               ["none", "zero", "positive-number", "negative-number", "number-and-tag"],
               ["none", "finite-number"], ["none", "false", "true", "non-empty-string"],
               ["truthy-accept", "falsy-reject"]]
    fallbacks: list[tuple[str, Any]] = [
        ("lifecycle_transition", "load_trim_execute"), ("none", None), ("leverage", 1.0),
        ("stake", "proposed_stake"), ("boolean", True), ("none", None),
        ("adjustment", [None, ""]), ("none", None), ("boolean", False), ("boolean", True)]
    index = names.index(name)
    fallback = {"class": fallbacks[index][0], "value": fallbacks[index][1]}
    transaction = {"ordinary_trade": "commit_on_success_rollback_on_exception",
                   "scheduler_prior": "preserve", "shared_custom_state": "commit_executed_writes",
                   "strategy_registers": "commit_executed_writes"}
    visibility = {"callback_dataframe_completed_candle_lag": 2, "signal_row_offset": -1,
                  "successful_state_visible": "next_callback_in_scheduler_order"}
    return {"name": name, "active": True, "cadence": cadences[index],
            "accepted_returns": returns[index], "exception_fallback": fallback,
            "order": {"phase": index, "after": [] if index < 3 else [names[index - 1]],
                      "before": []}, "predicate_ids": [], "transaction_policy": transaction,
            "visibility": visibility}


def _required_inputs(entrypoints: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for name, entrypoint in entrypoints.items():
        seen: set[str] = set()
        for node in _walk_json(entrypoint["instructions"]):
            input_name = node.get("name")
            if (
                node.get("op") == "read_input"
                and isinstance(input_name, str)
                and input_name not in seen
            ):
                seen.add(input_name)
                kind = INPUT_TYPES.get(input_name)
                if kind is None:
                    _reject("CALLBACK_PROGRAM_REGISTER_TYPE_UNRESOLVED", f"input {input_name}")
                result.append({"entrypoint": name, "name": input_name, "type": {"kind": kind}})
    return result


def _walk_json(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if isinstance(value, dict):
        result.append(value)
        for child in value.values():
            result.extend(_walk_json(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_walk_json(child))
    return result


def _reject(code: str, message: str) -> NoReturn:
    raise StrategyAnalysisError(f"{code}: {message}")
