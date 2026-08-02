"""Native callback and serialized-program checks for stateful coverage."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

STATEFUL_CALLBACKS = frozenset(
    {
        "adjust_trade_position",
        "custom_exit",
        "custom_stake_amount",
        "order_filled",
    }
)


def callback_coverage(
    callback_source: dict[str, Any],
    hot_ir: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Join source call-graph entrypoints to their executable Native lowerings."""
    compiled = {
        item["name"]: item
        for item in (hot_ir or {}).get("callbacks", [])
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }
    records: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    for source_record in callback_source["entrypoints"]:
        name = source_record["name"]
        native = compiled.get(name)
        active = bool(source_record["active_for_mode"])
        executable = bool(native and native.get("executable_in_rust"))
        backend = str(native.get("backend")) if native else "lowering-review-required"
        records.append(
            {
                "name": name,
                "source_order": source_record["source_order"],
                "active_for_mode": active,
                "reachable_methods": source_record["reachable_methods"],
                "route_keys": source_record["route_keys"],
                "backend": backend,
                "executable_in_rust": executable,
                "stateful": name in STATEFUL_CALLBACKS,
                "location": _without_path(source_record["location"]),
            }
        )
        if active and not executable:
            gaps.append(
                {
                    "code": "ACTIVE_CALLBACK_NOT_NATIVE",
                    "callback": name,
                    "side": None,
                    "tags": [],
                    "message": f"active callback {name} has no exact Native lowering",
                }
            )
    return records, gaps


def manager_operation(
    hot_ir: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return the manager operation and its independently reportable identity."""
    if not isinstance(hot_ir, Mapping):
        return None, None
    manager = hot_ir.get("nfi_trade_manager")
    if not isinstance(manager, Mapping):
        return None, None
    operation = manager.get("operation")
    if not isinstance(operation, dict):
        return None, None
    proof = manager.get("proof")
    return operation, {
        "hot_ir_schema_version": hot_ir.get("schema_version"),
        "hot_ir_fingerprint": hot_ir.get("fingerprint"),
        "manager_schema_version": manager.get("schema_version"),
        "manager_backend": manager.get("backend"),
        "operation_sha256": (
            proof.get("operation_sha256") if isinstance(proof, Mapping) else None
        ),
        "hot_loop_ready": bool(hot_ir.get("hot_loop_ready")),
    }


def native_contracts(
    operation: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, set[str]], dict[str, set[str]]]:
    """Inventory sealed exit and position-adjustment programs without tag constants."""
    contracts: list[dict[str, Any]] = []
    exit_tags = {"long": set(), "short": set()}
    adjustment_tags = {"long": set(), "short": set()}
    if operation is None:
        return contracts, exit_tags, adjustment_tags

    for side, route_field, order_field, managed_field in (
        ("long", "supported_routes", "route_order", "managed_exit_program"),
        (
            "short",
            "supported_short_routes",
            "short_route_order",
            "managed_short_exit_program",
        ),
    ):
        routes = operation.get(route_field)
        route_order = operation.get(order_field)
        managed = operation.get(managed_field)
        managed_routes = _managed_program_routes(managed)
        if not isinstance(routes, Mapping) or not isinstance(route_order, list):
            continue
        for route_key in route_order:
            route = routes.get(route_key) if isinstance(route_key, str) else None
            if not isinstance(route, Mapping):
                continue
            tags = _string_list(route.get("entry_tags"))
            program: Mapping[str, Any] | None = None
            program_path: str | None = None
            if route_key in managed_routes and isinstance(managed, Mapping):
                program = managed
                program_path = managed_field
            elif isinstance(route.get("program"), Mapping):
                program = route["program"]
                program_path = f"{route_field}.{route_key}.program"
            direct_match_tags = managed_routes.get(route_key)
            exact = _program_is_sealed(program) and (
                program is not managed
                or direct_match_tags is None
                or set(direct_match_tags) == set(tags)
            )
            if exact:
                exit_tags[side].update(tags)
            contracts.append(
                _contract_record(
                    contract_id=f"custom-exit:{side}:{route_key}",
                    callback="custom_exit",
                    side=side,
                    tags=tags,
                    program=program,
                    program_path=program_path,
                    exact=exact,
                )
            )

    for field, value in operation.items():
        if not isinstance(field, str) or not field.endswith("adjustment"):
            continue
        if not isinstance(value, Mapping):
            continue
        side = "short" if field.startswith("short_") else "long"
        tags = _string_list(value.get("entry_tags"))
        program = value.get("program")
        program = program if isinstance(program, Mapping) else None
        exact = _program_is_sealed(program)
        if exact:
            adjustment_tags[side].update(tags)
        contracts.append(
            _contract_record(
                contract_id=f"adjust-position:{side}:{field}",
                callback="adjust_trade_position",
                side=side,
                tags=tags,
                program=program,
                program_path=field + ".program",
                exact=exact,
            )
        )

    for side, route_field in (
        ("long", "supported_routes"),
        ("short", "supported_short_routes"),
    ):
        routes = operation.get(route_field)
        if not isinstance(routes, Mapping):
            continue
        for route_key, route in routes.items():
            if not isinstance(route_key, str) or not isinstance(route, Mapping):
                continue
            program = route.get("program")
            if not isinstance(program, Mapping):
                continue
            tags = _string_list(route.get("entry_tags"))
            exact = _program_is_sealed(program)
            if exact:
                adjustment_tags[side].update(tags)
            contracts.append(
                _contract_record(
                    contract_id=f"adjust-position:{side}:{route_key}",
                    callback="adjust_trade_position",
                    side=side,
                    tags=tags,
                    program=program,
                    program_path=f"{route_field}.{route_key}.program",
                    exact=exact,
                )
            )
            regular = route.get("regular_program")
            if isinstance(regular, Mapping):
                contracts.append(
                    _contract_record(
                        contract_id=f"adjust-position:{side}:{route_key}:regular",
                        callback="adjust_trade_position",
                        side=side,
                        tags=tags,
                        program=regular,
                        program_path=f"{route_field}.{route_key}.regular_program",
                        exact=_program_is_sealed(regular),
                    )
                )
    return contracts, exit_tags, adjustment_tags


def reachable_route_gaps(
    active_tags: dict[str, list[str]],
    *,
    exit_tags: dict[str, set[str]],
    adjustment_tags: dict[str, set[str]],
    adjustment_enabled: bool,
) -> list[dict[str, Any]]:
    """Return only mode-reachable tags missing an exact Native program."""
    gaps: list[dict[str, Any]] = []
    for side in ("long", "short"):
        missing_exit = [tag for tag in active_tags[side] if tag not in exit_tags[side]]
        if missing_exit:
            gaps.append(
                {
                    "code": "REACHABLE_EXIT_ROUTE_NOT_NATIVE",
                    "callback": "custom_exit",
                    "side": side,
                    "tags": missing_exit,
                    "message": f"reachable {side} entry tags lack a sealed Native exit route",
                }
            )
        if adjustment_enabled:
            missing_adjustment = [
                tag for tag in active_tags[side] if tag not in adjustment_tags[side]
            ]
            if missing_adjustment:
                gaps.append(
                    {
                        "code": "REACHABLE_ADJUSTMENT_ROUTE_NOT_NATIVE",
                        "callback": "adjust_trade_position",
                        "side": side,
                        "tags": missing_adjustment,
                        "message": (
                            f"reachable {side} entry tags lack a sealed Native adjustment route"
                        ),
                    }
                )
    return gaps


def _contract_record(
    *,
    contract_id: str,
    callback: str,
    side: str,
    tags: list[str],
    program: Mapping[str, Any] | None,
    program_path: str | None,
    exact: bool,
) -> dict[str, Any]:
    return {
        "id": contract_id,
        "callback": callback,
        "side": side,
        "entry_tags": tags,
        "program_path": program_path,
        "program_schema_version": (
            str(program.get("schema_version")) if program is not None else None
        ),
        "program_fingerprint": (
            str(program.get("fingerprint"))
            if program is not None and isinstance(program.get("fingerprint"), str)
            else None
        ),
        "status": "exact" if exact else "missing-or-unsealed",
    }


def _managed_program_routes(program: Any) -> dict[str, list[str] | None]:
    if not _program_is_sealed(program) or not isinstance(program, Mapping):
        return {}
    routes = program.get("routes")
    if not isinstance(routes, list):
        return {}
    result: dict[str, list[str] | None] = {}
    for item in routes:
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
            continue
        match = item.get("match")
        direct_tags = (
            _string_list(match.get("entry_tags"))
            if isinstance(match, Mapping) and isinstance(match.get("entry_tags"), list)
            else None
        )
        result[str(item["id"])] = direct_tags
    return result


def _program_is_sealed(program: Any) -> bool:
    return (
        isinstance(program, Mapping)
        and isinstance(program.get("schema_version"), str)
        and bool(program["schema_version"])
        and isinstance(program.get("fingerprint"), str)
        and re.fullmatch(r"[0-9a-f]{64}", str(program["fingerprint"])) is not None
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item for item in value if isinstance(item, str) and item))


def _without_path(location: Mapping[str, Any]) -> dict[str, int]:
    return {
        "line": int(location["line"]),
        "column": int(location["column"]),
        "end_line": int(location["end_line"]),
        "end_column": int(location["end_column"]),
    }
