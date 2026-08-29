"""Transitive scalar trade-callback bundle assembly."""

from __future__ import annotations

import hashlib
import json

from .callback_contract import CALLBACK_LOWERING_VERSION, JsonObject
from .trade_ir import build_trade_dependency_ir


def _lower_scalar_trade_callback(
    analysis: JsonObject,
    *,
    callback_name: str,
    backend: str,
    opcode: str,
) -> JsonObject | None:
    report = build_trade_dependency_ir(analysis, roots=(callback_name,))
    compiled = report.get("compiled_scalar_methods")
    if not isinstance(compiled, dict) or callback_name not in compiled:
        return None
    pending = [callback_name]
    selected: JsonObject = {}
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        record = compiled.get(name)
        if not isinstance(record, dict) or not isinstance(record.get("program"), dict):
            return None
        selected[name] = record["program"]
        called_methods = record.get("called_methods", [])
        if not isinstance(called_methods, list) or not all(
            isinstance(item, str) for item in called_methods
        ):
            return None
        pending.extend(called_methods)
    operation = {
        "opcode": opcode,
        "schema_version": "1.0.0",
        "entry": callback_name,
        "programs": {name: selected[name] for name in sorted(selected)},
    }
    return {
        "backend": backend,
        "executable_in_rust": True,
        "operation": operation,
        "proof": {
            "compiler_version": CALLBACK_LOWERING_VERSION,
            "matcher": f"transitive-scalar-{callback_name.replace('_', '-')}-v1",
            "trade_ir_fingerprint": report["fingerprint"],
            "program_count": len(selected),
            "program_sha256": hashlib.sha256(
                json.dumps(
                    operation,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        },
    }
