"""Public source-to-executable callback program compiler."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any, NoReturn

from .callback_source_identity import _fingerprint
from .errors import StrategyAnalysisError
from .executable_callback_compiler import ProgramCompiler
from .executable_callback_validation import (
    CONTRACT_FILE_SHA256,
    CONTRACT_FINGERPRINT,
    canonical_program_fingerprint,
    validate_executable_callback_program,
)

__all__ = ["compile_executable_callback_program", "validate_executable_callback_program"]


def compile_executable_callback_program(
    analysis: dict[str, Any],
    callback_execution_ir: dict[str, Any],
    *,
    trading_mode: str,
    run_mode: str,
) -> dict[str, Any]:
    """Compile one complete, hash-bound strategy callback program without executing source."""
    path, payload, text, class_node, strategy = _authenticated_source(analysis)
    _validate_execution_ir(
        callback_execution_ir,
        source_sha=hashlib.sha256(payload).hexdigest(),
        trading_mode=trading_mode,
        run_mode=run_mode,
    )
    callback_names = set(strategy.get("strategy_callbacks", []))
    expected = {
        "bot_loop_start",
        "leverage",
        "custom_stake_amount",
        "confirm_trade_entry",
        "order_filled",
        "adjust_trade_position",
        "custom_stoploss",
        "custom_exit",
        "confirm_trade_exit",
    }
    unknown = callback_names - expected
    if unknown:
        _reject(
            "CALLBACK_PROGRAM_UNKNOWN_CALLBACK",
            f"callbacks have no executable contract: {sorted(unknown)}",
        )
    tree = ast.parse(text, filename=str(path), type_comments=True)
    compiler = ProgramCompiler(tree, class_node, callback_execution_ir, str(path))
    try:
        entrypoints, registers, custom_state, required_inputs = compiler.compile()
    except ValueError as exc:
        raise StrategyAnalysisError(str(exc)) from exc
    if trading_mode == "spot":
        entrypoints["leverage"]["active"] = False
    identity: dict[str, Any] = {
        "callback_contract_file_sha256": CONTRACT_FILE_SHA256,
        "callback_contract_fingerprint": CONTRACT_FINGERPRINT,
        "callback_execution_ir_fingerprint": callback_execution_ir["fingerprint"],
        "program_fingerprint": "0" * 64,
        "run_mode": run_mode,
        "selected_class_ast_sha256": _ast_sha(class_node),
        "source_closure": _source_closure(
            class_node, strategy, callback_execution_ir, payload, str(path)
        ),
        "source_predicates": compiler.source_predicates,
        "trading_mode": trading_mode,
    }
    program: dict[str, Any] = {
        "schema_version": "executable-callback-program-v1",
        "identity": identity,
        "registers": registers,
        "required_custom_state": custom_state,
        "required_inputs": required_inputs,
        "entrypoints": entrypoints,
    }
    identity["program_fingerprint"] = canonical_program_fingerprint(program)
    validate_executable_callback_program(program)
    return program


def _authenticated_source(
    analysis: dict[str, Any],
) -> tuple[Path, bytes, str, ast.ClassDef, dict[str, Any]]:
    strategies, source = analysis.get("strategies"), analysis.get("source")
    if not isinstance(strategies, list) or len(strategies) != 1 or not isinstance(source, dict):
        _reject("CALLBACK_PROGRAM_SOURCE_STALE", "one selected hash-bound strategy is required")
    path_value, expected = source.get("path"), source.get("sha256")
    if not isinstance(path_value, str) or not isinstance(expected, str):
        _reject("CALLBACK_PROGRAM_SOURCE_STALE", "source identity is malformed")
    path = Path(path_value).resolve()
    try:
        payload = path.read_bytes()
        text = payload.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise StrategyAnalysisError(
            f"CALLBACK_PROGRAM_SOURCE_STALE: {path}: source cannot be read"
        ) from exc
    if hashlib.sha256(payload).hexdigest() != expected:
        _reject("CALLBACK_PROGRAM_SOURCE_STALE", f"{path}: source hash differs")
    tree = ast.parse(text, filename=str(path), type_comments=True)
    name = strategies[0].get("name")
    class_node = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name), None
    )
    if class_node is None:
        _reject("CALLBACK_PROGRAM_SOURCE_STALE", f"{path}: selected class disappeared")
    return path, payload, text, class_node, strategies[0]


def _validate_execution_ir(
    document: dict[str, Any], *, source_sha: str, trading_mode: str, run_mode: str
) -> None:
    if document.get("schema_version") != "callback-execution-ir-v1":
        _reject("CALLBACK_PROGRAM_EXECUTION_IR_STALE", "execution IR schema differs")
    contract = document.get("freqtrade_contract")
    if not isinstance(contract, dict) or contract.get("fingerprint") != CONTRACT_FINGERPRINT:
        _reject("CALLBACK_PROGRAM_CONTRACT_IDENTITY_MISMATCH", "execution IR contract differs")
    if document.get("fingerprint") != _fingerprint(document):
        _reject("CALLBACK_PROGRAM_EXECUTION_IR_STALE", "execution IR fingerprint differs")
    source = document.get("source")
    if not isinstance(source, dict) or source.get("sha256") != source_sha:
        _reject("CALLBACK_PROGRAM_EXECUTION_IR_STALE", "execution IR source differs")
    if document.get("trading_mode") != trading_mode or document.get("run_mode") != run_mode:
        _reject("CALLBACK_PROGRAM_EXECUTION_IR_STALE", "execution IR mode differs")
    callbacks = document.get("callbacks")
    if not isinstance(callbacks, list) or len(
        {item.get("name") for item in callbacks if isinstance(item, dict)}
    ) != len(callbacks):
        _reject("CALLBACK_PROGRAM_DUPLICATE_ENTRYPOINT", "execution IR callback names are invalid")


def _source_closure(
    class_node: ast.ClassDef,
    strategy: dict[str, Any],
    execution_ir: dict[str, Any],
    payload: bytes,
    path: str,
) -> list[dict[str, Any]]:
    methods = {node.name: node for node in class_node.body if isinstance(node, ast.FunctionDef)}
    records = {item["name"]: item for item in strategy.get("methods", [])}
    names = ["__init__"]
    for callback in execution_ir["callbacks"]:
        for name in callback["reachable_methods"]:
            if name not in names:
                names.append(name)
    source_id = "sha256:" + hashlib.sha256(payload).hexdigest()
    result: list[dict[str, Any]] = []
    for name in names:
        node = methods.get(name)
        record = records.get(name)
        if node is None or record is None:
            _reject("CALLBACK_PROGRAM_SOURCE_STALE", f"method {name} disappeared")
        result.append(
            {
                "source_id": source_id,
                "logical_owner_id": "owner:1",
                "logical_method_id": f"method:{name}",
                "ast_sha256": _ast_sha(node),
                "source_body_sha256": record["source_sha256"],
                "diagnostic_path": path,
            }
        )
    return result


def _ast_sha(node: ast.AST) -> str:
    canonical = ast.dump(node, annotate_fields=True, include_attributes=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _reject(code: str, message: str) -> NoReturn:
    raise StrategyAnalysisError(f"{code}: {message}")
