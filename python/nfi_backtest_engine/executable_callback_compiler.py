"""Bounded statement compiler for source-authenticated strategy callbacks."""
from __future__ import annotations

import ast
import copy
import hashlib
from typing import Any, NoReturn

from .executable_callback_expressions import (
    ReplaceNames,
    compile_expression,
    custom_key,
    dataframe_call,
    literal,
    literal_kind,
    node_path,
    self_attribute,
    valid_emit,
)
from .executable_callback_validation import _entrypoint_policy, _required_inputs

_CALLBACKS = ("bot_loop_start", "leverage", "custom_stake_amount", "confirm_trade_entry",
              "order_filled", "adjust_trade_position", "custom_stoploss", "custom_exit",
              "confirm_trade_exit")
_RETURN = dict(bot_loop_start="none", leverage="leverage", custom_stake_amount="stake",
               confirm_trade_entry="boolean", order_filled="none",
               adjust_trade_position="adjustment", custom_stoploss="stoploss",
               custom_exit="exit_reason", confirm_trade_exit="boolean")

class ProgramCompiler:
    def __init__(self, tree: ast.Module, cls: ast.ClassDef, ir: dict[str, Any], path: str) -> None:
        del tree
        self.class_node, self.path = cls, path
        self.methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
        self.execution = {x["name"]: x for x in ir["callbacks"]}
        self.locals: set[str] = set()
        self.current = ""
        self.predicates: list[str] = []
        self.instructions = self._instruction_id = 0
        self.register_ids: dict[str, str] = {}
        self.register_types: dict[str, str] = {}
        self.initials: dict[str, dict[str, Any]] = {}
        self.custom_types: dict[str, str | None] = {}
        self.source_predicates: list[dict[str, Any]] = []
        self._predicate_map: dict[tuple[str, str], list[str]] = {}
        self._callback_predicates: dict[str, list[str]] = {}
        self._collect_predicates()
        self._collect_registers()

    def fail(self, code: str, node: ast.AST, message: str) -> NoReturn:
        loc = f"{self.path}:{getattr(node, 'lineno', 1)}:{getattr(node, 'col_offset', 0)}"
        raise ValueError(f"{loc}: {code}: {message}")

    def register(self, name: str, node: ast.AST) -> str:
        value = self.register_ids.get(name)
        if value is None:
            self.fail("CALLBACK_PROGRAM_REGISTER_TYPE_UNRESOLVED", node, f"self.{name}")
        return value

    def compile(self) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]],
                               list[dict[str, Any]]]:
        entries: dict[str, Any] = {}
        for name in _CALLBACKS:
            method = self.methods.get(name)
            if method is None:
                entries[name] = self._default_entrypoint(name)
                continue
            self.current = name
            self.locals = {x.arg for x in method.args.args if x.arg != "self"}
            self.instructions = 0
            body = self._statements(method.body)
            if not body or body[-1]["op"] not in {"return", "raise_callback"}:
                body.append(self._stmt("return", method, result={"class": "none", "value": None}))
            policy = _entrypoint_policy(name)
            policy["predicate_ids"] = self._callback_predicates.get(name, [])
            entries[name] = dict(instructions=body, max_steps=max(1, self.instructions), **policy)
        policy = _entrypoint_policy("loop_cadence_startup_lookback")
        result = {"class": "lifecycle_transition", "value": literal("load_trim_execute")}
        lifecycle = [self._stmt("return", self.class_node, result=result)]
        policy.update(instructions=lifecycle, max_steps=1)
        entries["loop_cadence_startup_lookback"] = policy
        registers = [dict(id=rid,
                          logical_name_hash=hashlib.sha256(f"owner:1:{name}".encode()).hexdigest(),
                          scope="strategy_run", type={"kind": self.register_types[name]},
                          initial=self.initials[name]) for name, rid in self.register_ids.items()]
        custom = [dict(key=key, type=None if kind is None else {"kind": kind})
                  for key, kind in sorted(self.custom_types.items())]
        return entries, registers, custom, _required_inputs(entries)

    def _default_entrypoint(self, name: str) -> dict[str, Any]:
        policy = _entrypoint_policy(name)
        fallback = policy["exception_fallback"]
        value = fallback["value"]
        if name == "custom_stake_amount":
            value = {"op": "read_input", "name": "proposed_stake"}
        else:
            value = literal(value)
        result = {"class": fallback["class"], "value": value}
        instructions = [self._stmt("return", self.class_node, result=result)]
        policy.update(instructions=instructions, max_steps=1)
        return policy

    def _statements(self, values: list[ast.stmt]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for node in values:
            if isinstance(node, ast.Assign):
                out.extend(self._assign(node.targets, node.value, node))
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                out.extend(self._assign([node.target], node.value, node))
            elif isinstance(node, ast.AugAssign):
                out.append(self._augassign(node))
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                out.extend(self._call(node.value))
            elif isinstance(node, ast.If):
                predicate = self._predicate_id(node.test)
                self.predicates.append(predicate)
                body = self._statements(node.body)
                self.predicates.pop()
                fields = dict(condition=compile_expression(node.test, self), then=body,
                              otherwise=self._statements(node.orelse))
                out.append(self._stmt("if", node, **fields))
            elif isinstance(node, ast.Return):
                value = (literal(None) if node.value is None
                         else compile_expression(node.value, self))
                cls = "none" if node.value is None else _RETURN[self.current]
                out.append(self._stmt("return", node, result={"class": cls, "value": value}))
            elif isinstance(node, ast.Raise):
                token, message = "Exception", literal("")
                if isinstance(node.exc, ast.Call) and isinstance(node.exc.func, ast.Name):
                    token = node.exc.func.id
                    if node.exc.args:
                        message = compile_expression(node.exc.args[0], self)
                out.append(self._stmt("raise_callback", node, exception_class=token,
                                      message=message))
            elif isinstance(node, ast.While | ast.AsyncFor | ast.For):
                self.fail("CALLBACK_PROGRAM_UNBOUNDED_CONTROL_FLOW", node, "loop is not bounded")
            else:
                self.fail("CALLBACK_PROGRAM_UNSUPPORTED_STATEMENT", node, type(node).__name__)
        return out

    def _assign(self, targets: list[ast.expr], value: ast.expr,
                node: ast.AST) -> list[dict[str, Any]]:
        if len(targets) == 1 and isinstance(targets[0], ast.Tuple) and dataframe_call(value):
            self.locals.update(x.id for x in targets[0].elts if isinstance(x, ast.Name))
            value_ir = {"op": "read_input", "name": "callback_dataframe"}
            return [self._stmt("let", node, name="dataframe", value=value_ir)]
        value_ir = compile_expression(value, self)
        out: list[dict[str, Any]] = []
        for target in targets:
            if isinstance(target, ast.Name):
                self.locals.add(target.id)
                out.append(self._stmt("let", node, name=target.id, value=value_ir))
            elif self_attribute(target):
                rid = self.register(target.attr, target)
                kind = literal_kind(value)
                if kind is not None and kind != self.register_types[target.attr]:
                    self.fail("CALLBACK_PROGRAM_REGISTER_TYPE_CONFLICT", node, target.attr)
                out.append(self._stmt("set_register", node, register_id=rid, value=value_ir))
            elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) \
                    and target.value.id == "trade":
                out.append(self._stmt("let", node, name=f"trade.{target.attr}", value=value_ir))
            elif (
                isinstance(target, ast.Subscript)
                and self_attribute(target.value)
            ):
                out.append(
                    self._stmt(
                        "set_register_item",
                        node,
                        register_id=self.register(target.value.attr, target),
                        key=compile_expression(target.slice, self),
                        value=value_ir,
                    )
                )
            else:
                self.fail("CALLBACK_PROGRAM_UNSUPPORTED_STATEMENT", target, "assignment target")
        return out

    def _augassign(self, node: ast.AugAssign) -> dict[str, Any]:
        if not self_attribute(node.target):
            self.fail("CALLBACK_PROGRAM_UNSUPPORTED_STATEMENT", node, "register target required")
        rid = self.register(node.target.attr, node)
        operator = {ast.Add: "add", ast.Sub: "sub"}.get(type(node.op))
        if operator is None:
            self.fail("CALLBACK_PROGRAM_UNSUPPORTED_EXPRESSION", node, "register operator")
        left = {"op": "read_register", "register_id": rid}
        value = dict(op="binary", operator=operator, left=left,
                     right=compile_expression(node.value, self))
        return self._stmt("set_register", node, register_id=rid, value=value)

    def _call(self, call: ast.Call) -> list[dict[str, Any]]:
        path = node_path(call.func)
        if path == ["super", "__init__"]:
            return []
        if path == ["self", "_emit"]:
            return self._emit(call)
        if path and path[0] == "trade" and path[-1] in {"set_custom_data", "delete_custom_data"}:
            key = custom_key(call, self)
            if path[-1] == "delete_custom_data":
                self.custom_types.setdefault(key, None)
                return [self._stmt("delete_custom_state", call, key=key)]
            value = call.args[1] if len(call.args) > 1 else None
            if value is None:
                self.fail("CALLBACK_PROGRAM_UNSUPPORTED_STATEMENT", call, "custom-state value")
            kind = literal_kind(value)
            old = self.custom_types.get(key)
            if old is not None and kind is not None and old != kind:
                self.fail("CALLBACK_PROGRAM_REGISTER_TYPE_CONFLICT", call, key)
            self.custom_types[key] = kind or old
            return [self._stmt("set_custom_state", call, key=key,
                               value=compile_expression(value, self))]
        self.fail("CALLBACK_PROGRAM_UNSUPPORTED_STATEMENT", call, "call statement")

    def _emit(self, call: ast.Call) -> list[dict[str, Any]]:
        helper = self.methods.get("_emit")
        if helper is None or not valid_emit(helper):
            self.fail("CALLBACK_PROGRAM_OBSERVATION_UNSUPPORTED", call, "invalid helper")
        sequence_target = next(
            (
                statement.target.attr
                for statement in helper.body
                if isinstance(statement, ast.AugAssign)
                and self_attribute(statement.target)
            ),
            None,
        )
        if sequence_target is None:
            self.fail("CALLBACK_PROGRAM_OBSERVATION_UNSUPPORTED", call, "sequence register")
        rid = self.register(sequence_target, call)
        left = {"op": "read_register", "register_id": rid}
        increment = dict(op="binary", operator="add", left=left, right=literal(1))
        fields = [dict(name="sequence", value={"op": "read_register", "register_id": rid}),
                  dict(name="callback", value=compile_expression(call.args[0], self)),
                  dict(name="timestamp_ms", value={
                      "op": "timestamp_ms", "value": compile_expression(call.args[1], self)})]
        fields.extend(dict(name=x.arg, value=compile_expression(x.value, self))
                      for x in call.keywords if x.arg is not None)
        return [self._stmt("set_register", call, register_id=rid, value=increment),
                self._stmt("emit_observation", call, channel="strategy_stdout_json",
                           payload={"op": "record", "fields": fields})]

    def inline_static(self, name: str, call: ast.Call) -> dict[str, Any]:
        helper = self.methods.get(name)
        static = helper and any(isinstance(x, ast.Name) and x.id == "staticmethod"
                                for x in helper.decorator_list)
        if not static or helper is None or len(helper.body) != 1 \
                or not isinstance(helper.body[0], ast.Return) or helper.body[0].value is None:
            self.fail("CALLBACK_PROGRAM_UNSUPPORTED_EXPRESSION", call, "helper is not pure static")
        if len(call.args) != len(helper.args.args):
            self.fail("CALLBACK_PROGRAM_UNSUPPORTED_EXPRESSION", call, "helper arguments")
        values = {x.arg: copy.deepcopy(y) for x, y in zip(helper.args.args, call.args, strict=True)}
        expression = ReplaceNames(values).visit(copy.deepcopy(helper.body[0].value))
        return compile_expression(expression, self)

    def _stmt(self, op: str, node: ast.AST, **fields: Any) -> dict[str, Any]:
        self._instruction_id += 1
        self.instructions += 1
        return dict(op=op, id=f"i{self._instruction_id}",
                    predicate_ids=list(self.predicates), **fields)

    def _collect_registers(self) -> None:
        init = self.methods.get("__init__")
        for node in [] if init is None else init.body:
            target: ast.expr | None = None
            value: ast.expr | None = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                target, value = node.target, node.value
            if target is not None and value is not None and self_attribute(target):
                kind = literal_kind(value)
                if kind is None:
                    self.fail("CALLBACK_PROGRAM_REGISTER_TYPE_UNRESOLVED", node, "initializer")
                name = target.attr
                self.register_ids[name] = f"r{len(self.register_ids) + 1}"
                self.register_types[name] = kind
                self.initials[name] = compile_expression(value, self)

    def _collect_predicates(self) -> None:
        for callback in self.execution.values():
            ids: list[str] = []
            for record in callback["source_predicates"]:
                item = dict(record)
                producer = item.pop("producer_method")
                item.update(producer_method_id=f"method:{producer}",
                            id=f"p{len(self.source_predicates) + 1}")
                self.source_predicates.append(item)
                ids.append(item["id"])
                key = (producer, item["ast_sha256"])
                self._predicate_map.setdefault(key, []).append(item["id"])
            self._callback_predicates[callback["name"]] = ids

    def _predicate_id(self, node: ast.AST) -> str:
        dumped = ast.dump(node, annotate_fields=True, include_attributes=False)
        key = (self.current, hashlib.sha256(dumped.encode()).hexdigest())
        values = self._predicate_map.get(key)
        return values[0] if values else ""
