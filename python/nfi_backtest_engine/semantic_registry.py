"""Typed, hash-bound semantic obligations for one strategy and Freqtrade contract."""

from __future__ import annotations

import ast
import gzip
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import loads_json_bytes, read_json
from .errors import (
    PackagedRegistryCurrentRefError,
    SpecValidationError,
    StrategyAnalysisError,
)
from .execution_contract import load_execution_contract
from .execution_semantic_registry import execution_semantic_obligation_rows
from .fixture import sha256_file
from .freqtrade_semantic_profile import load_freqtrade_semantic_profile
from .futures_contract import load_futures_contract
from .portfolio_semantic_registry import portfolio_semantic_obligation_rows
from .scheduler_contract import load_scheduler_contract
from .semantic_registry_callback_contract import (
    callback_semantic_contract_rows,
)
from .semantic_registry_validation import (
    semantic_registry_schema_projection,
    validate_semantic_registry_records,
)
from .specs import (
    SEMANTIC_OBLIGATION_REGISTRY_SCHEMA,
    semantic_obligation_registry_schema_identity,
    validate_schema,
    verify_semantic_obligation_registry_schema_identity,
)
from .strategy import STRATEGY_CALLBACKS

SEMANTIC_OBLIGATION_REGISTRY_VERSION = "semantic-obligation-registry-v1"
SEMANTIC_OBLIGATION_PREIMAGE_VERSION = "semantic-obligation-preimage-v1"
_MAX_SEMANTIC_REGISTRY_JSON_BYTES = 192 * 1024 * 1024

FREQTRADE_SOURCE_REPOSITORY = "https://github.com/freqtrade/freqtrade.git"
FREQTRADE_SOURCE_COMMIT = "6fa470939cc74bf0672e0e348a4d9b293072e43c"

_PACKAGE_CONTRACTS = Path(__file__).resolve().parent / "contracts"
_DEFAULT_PROFILE = _PACKAGE_CONTRACTS / "freqtrade-semantic-profile.json"
_DEFAULT_SCHEDULER = _PACKAGE_CONTRACTS / "freqtrade-scheduler-contract.json"
_DEFAULT_EXECUTION = _PACKAGE_CONTRACTS / "freqtrade-execution-contract.json"
_DEFAULT_FUTURES = _PACKAGE_CONTRACTS / "freqtrade-futures-contract.json"
_PACKAGED_REGISTRY = _PACKAGE_CONTRACTS / "freqtrade-nfi-semantic-obligation-registry.json.gz"
_PACKAGED_REGISTRY_MANIFEST = (
    _PACKAGE_CONTRACTS / "freqtrade-nfi-semantic-obligation-registry.manifest.json"
)

_MAPPING_GENERIC = "generic-runtime"


@dataclass(frozen=True, slots=True)
class CurrentRefAuthorization:
    """Identity of one operation that may emit a Native authorization result."""

    operation: str
    candidate_commit: str
    candidate_identity_sha256: str
    source_closure_sha256: str
    workload_run_nonce_sha256: str

    @property
    def digest(self) -> str:
        """Bind a live ref proof to this exact authorization candidate."""
        payload = json.dumps(
            {
                "candidate_commit": self.candidate_commit,
                "candidate_identity_sha256": self.candidate_identity_sha256,
                "operation": self.operation,
                "source_closure_sha256": self.source_closure_sha256,
                "workload_run_nonce_sha256": self.workload_run_nonce_sha256,
            },
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class PackagedRegistryCurrentRefProof:
    """First exact observation bound to one pending authorization event."""

    authorization: CurrentRefAuthorization
    authorization_digest: str
    packaged_commit: str
    packaged_source_closure_sha256: str
    initial_observed_commit: str
    document: dict[str, Any]


_MAPPING_COMPILED = "compiled-program"
_MAPPING_OFFICIAL = "official-only-blocker"
_MAPPING_UNREACHABLE = "machine-proven-unreachable"

_UPSTREAM_FAILURE_CONTRACT = {
    ("invalid-upstream-configuration-v1", "invalid-configuration"): (
        "INVALID_UPSTREAM_CONFIGURATION",
        "configured upstream observation inputs are incomplete",
    ),
    ("invalid-upstream-ref-v1", "invalid-ref"): (
        "INVALID_UPSTREAM_REF",
        "configured upstream ref is not one exact fully qualified ref",
    ),
    ("invalid-upstream-commit-v1", "invalid-commit"): (
        "INVALID_UPSTREAM_COMMIT",
        "configured upstream commit is not one lowercase 40-character object ID",
    ),
    ("invalid-upstream-source-v1", "invalid-source-path"): (
        "INVALID_UPSTREAM_SOURCE_PATH",
        "configured upstream source path is not normalized and repository-relative",
    ),
    ("upstream-fetch-failed-v1", "fetch-failed"): (
        "UPSTREAM_FETCH_FAILED",
        "configured upstream ref could not be fetched exactly once",
    ),
    ("upstream-fetch-timeout-v1", "fetch-timeout"): (
        "UPSTREAM_FETCH_TIMEOUT",
        "configured upstream ref fetch exceeded its bounded timeout",
    ),
    ("unresolved-upstream-ref-v1", "requested-object-missing"): (
        "UNOBSERVED_UPSTREAM_REF",
        "fetch completed without the exact configured upstream ref object",
    ),
    ("unresolved-upstream-ref-v1", "unresolved"): (
        "UNRESOLVED_UPSTREAM_REF",
        "configured upstream ref was not dynamically resolved",
    ),
    ("upstream-ref-not-commit-v1", "not-a-commit"): (
        "UPSTREAM_REF_NOT_COMMIT",
        "configured upstream ref does not peel to exactly one commit object",
    ),
    ("upstream-source-missing-v1", "source-missing"): (
        "UPSTREAM_SOURCE_MISSING",
        "configured upstream source is absent or not one regular non-symlink file",
    ),
}

_VECTOR_ENTRYPOINTS = {
    "informative_pairs",
    "populate_buy_trend",
    "populate_entry_trend",
    "populate_exit_trend",
    "populate_indicators",
    "populate_sell_trend",
}

# These are source forms reviewed by the current compilers. A new statement or
# expression kind is intentionally not accepted merely because Python can parse it.
_SUPPORTED_REACHABLE_AST_TYPES = frozenset(
    {
        "Module",
        "alias",
        "arg",
        "arguments",
        "AnnAssign",
        "Assert",
        "Assign",
        "Attribute",
        "AugAssign",
        "BinOp",
        "BoolOp",
        "Break",
        "Call",
        "ClassDef",
        "Compare",
        "Constant",
        "Continue",
        "Dict",
        "ExceptHandler",
        "Expr",
        "For",
        "FormattedValue",
        "FunctionDef",
        "GeneratorExp",
        "If",
        "IfExp",
        "Import",
        "ImportFrom",
        "JoinedStr",
        "Lambda",
        "List",
        "ListComp",
        "Name",
        "Nonlocal",
        "Pass",
        "Raise",
        "Return",
        "Set",
        "Slice",
        "Starred",
        "Subscript",
        "Try",
        "Tuple",
        "UnaryOp",
        "With",
        "comprehension",
        "keyword",
        "withitem",
    }
)

_KNOWN_EXTERNAL_IMPORT_ROOTS = frozenset(
    {
        "__future__",
        "copy",
        "datetime",
        "freqtrade",
        "functools",
        "logging",
        "numpy",
        "pandas",
        "pathlib",
        "rapidjson",
        "talib",
        "time",
        "typing",
        "warnings",
    }
)

_SIGNAL_COLUMNS = frozenset(
    {
        "buy",
        "enter_long",
        "enter_short",
        "exit_long",
        "exit_short",
        "sell",
    }
)
_TAG_COLUMNS = frozenset({"buy_tag", "enter_tag", "exit_tag", "sell_tag"})
_MUTATING_METHODS = frozenset(
    {
        "add",
        "append",
        "delete_custom_data",
        "discard",
        "extend",
        "pop",
        "remove",
        "set_custom_data",
        "setdefault",
        "update",
    }
)

_OPERATOR_NODE = ast.operator | ast.boolop | ast.unaryop | ast.cmpop
_SUPPORTED_OPERATOR_TYPES = frozenset(
    {
        "Add",
        "And",
        "BitAnd",
        "BitOr",
        "BitXor",
        "Div",
        "Eq",
        "FloorDiv",
        "Gt",
        "GtE",
        "In",
        "Invert",
        "Is",
        "IsNot",
        "LShift",
        "Lt",
        "LtE",
        "Mod",
        "Mult",
        "Not",
        "NotEq",
        "NotIn",
        "Or",
        "Pow",
        "RShift",
        "Sub",
        "UAdd",
        "USub",
    }
)
_REVIEWED_BUILTIN_CALLS = frozenset(
    {
        "RuntimeError",
        "ValueError",
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "enumerate",
        "float",
        "frozenset",
        "getattr",
        "hasattr",
        "id",
        "int",
        "isinstance",
        "len",
        "list",
        "max",
        "min",
        "range",
        "reversed",
        "round",
        "set",
        "setattr",
        "sorted",
        "str",
        "sum",
        "super",
        "tuple",
        "type",
        "zip",
    }
)
_REVIEWED_IMPORTED_CALLS = frozenset(
    {
        "datetime.datetime",
        "datetime.timedelta",
        "freqtrade.strategy.merge_informative_pair",
        "functools.reduce",
        "pandas.DataFrame",
        "pandas.Series",
    }
)
_REVIEWED_CALL_ATTRIBUTES = frozenset(
    {
        "ADX",
        "AROON",
        "BBANDS",
        "CCI",
        "DataFrame",
        "EMA",
        "MAX",
        "MFI",
        "MIN",
        "MINUS_DI",
        "OBV",
        "PLUS_DI",
        "Path",
        "ROC",
        "RSI",
        "SMA",
        "STDDEV",
        "STOCH",
        "STOCHF",
        "SUM",
        "Series",
        "Timedelta",
        "ULTOSC",
        "WILLR",
        "__init__",
        "__setitem__",
        "abs",
        "absolute",
        "accumulate",
        "agg",
        "append",
        "arange",
        "array",
        "asarray",
        "astype",
        "bot_loop_start",
        "calc_profit_ratio",
        "capitalize",
        "concat",
        "copy",
        "cumsum",
        "current_whitelist",
        "debug",
        "deepcopy",
        "diff",
        "divide",
        "drop",
        "dump",
        "empty_like",
        "endswith",
        "eq",
        "error",
        "extend",
        "ffill",
        "fillna",
        "floor",
        "fromisoformat",
        "full",
        "full_like",
        "get",
        "getLogger",
        "get_analyzed_dataframe",
        "get_custom_data",
        "get_open_trade_count",
        "get_pair_dataframe",
        "get_trades_proxy",
        "groupby",
        "info",
        "is_file",
        "is_numeric_dtype",
        "isfinite",
        "isinf",
        "isna",
        "isnan",
        "isoformat",
        "items",
        "load",
        "map",
        "max",
        "maximum",
        "mean",
        "min",
        "minimum",
        "nan_to_num",
        "ones_like",
        "open",
        "orderbook",
        "partition",
        "perf_counter",
        "pop",
        "reduce",
        "replace",
        "resolve",
        "roll",
        "rolling",
        "rsplit",
        "save",
        "select_filled_orders",
        "send_msg",
        "set_custom_data",
        "shift",
        "simplefilter",
        "sort_values",
        "split",
        "sqrt",
        "startswith",
        "stat",
        "sum",
        "tail",
        "ticker",
        "to_numpy",
        "to_string",
        "total_seconds",
        "transform",
        "update",
        "vstack",
        "warning",
        "where",
        "zeros_like",
    }
)
_REVIEWED_HIGHER_ORDER_PARAMETERS = frozenset({"ta_max", "ta_min", "ta_roc", "ta_sma"})
_REVIEWED_EMBEDDED_AST_NODE = _OPERATOR_NODE | ast.expr_context | ast.type_ignore

_SEMANTIC_AST_NODE = (
    ast.mod
    | ast.stmt
    | ast.expr
    | ast.comprehension
    | ast.keyword
    | ast.withitem
    | ast.ExceptHandler
    | ast.arguments
    | ast.arg
    | ast.alias
)


@dataclass
class _SourceFile:
    path: Path
    relative_path: str
    source_bytes: bytes
    tree: ast.Module
    local_imports: tuple[str, ...]
    external_imports: tuple[str, ...]
    missing_local_imports: tuple[str, ...]


@dataclass(frozen=True)
class _Callable:
    key: str
    name: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    relative_path: str
    class_name: str | None
    parent_key: str | None
    selected_method: bool
    callback_root: str | None


def _obligation_preimage(
    *,
    family: str,
    semantic_owner: str,
    subject: str,
    semantic_sha256: str,
    source_identities: Mapping[str, tuple[str, str]],
    default_source_identity: tuple[str, str] | None,
) -> dict[str, Any]:
    location_match = re.match(
        r"^(?P<path>.+?):(?P<line>\d+):(?P<column>\d+):"
        r"(?P<end_line>\d+):(?P<end_column>\d+):(?P<rest>.+)$",
        subject,
    )
    if location_match is not None:
        source_identity = source_identities.get(location_match.group("path"))
        if source_identity is None:
            source_identity = default_source_identity
        logical_path, identity_sha256 = source_identity or (
            "@semantic-contract",
            semantic_sha256,
        )
        normalized_subject = f"{logical_path}:" + location_match.group("rest")
        source = {
            "identity_sha256": identity_sha256,
            "path": logical_path,
            "span": {
                "line": int(location_match.group("line")),
                "column": int(location_match.group("column")),
                "end_line": int(location_match.group("end_line")),
                "end_column": int(location_match.group("end_column")),
            },
        }
    else:
        path_prefix, separator, remainder = subject.partition(":")
        source_identity = source_identities.get(path_prefix)
        if separator == ":" and source_identity is not None:
            logical_path, identity_sha256 = source_identity
            normalized_subject = f"{logical_path}:{remainder}"
        elif semantic_owner == "nfi-strategy" and default_source_identity is not None:
            logical_path, identity_sha256 = default_source_identity
            normalized_subject = subject
        else:
            logical_path, identity_sha256 = "@semantic-contract", semantic_sha256
            normalized_subject = subject
        source = {
            "identity_sha256": identity_sha256,
            "path": logical_path,
            "span": None,
        }
    return {
        "schema_version": SEMANTIC_OBLIGATION_PREIMAGE_VERSION,
        "family": family,
        "source": source,
        "normalized_semantics": {
            "subject": normalized_subject,
            "semantic_sha256": semantic_sha256,
        },
        "semantic_owner": semantic_owner,
    }


def _pack_obligation_preimage(preimage: Mapping[str, Any]) -> dict[str, Any]:
    source = preimage["source"]
    span = source["span"]
    packed_span = (
        None
        if span is None
        else [
            span["line"],
            span["column"],
            span["end_line"],
            span["end_column"],
        ]
    )
    return {
        "source": [source["identity_sha256"], source["path"], packed_span],
        "normalized_semantics": [
            preimage["normalized_semantics"]["subject"],
            preimage["normalized_semantics"]["semantic_sha256"],
        ],
    }


def semantic_obligation_preimage(
    group: Mapping[str, Any],
    record: Mapping[str, Any],
) -> dict[str, Any]:
    """Expand one compact, auditable obligation preimage into canonical form."""
    packed = record["preimage"]
    source_identity, source_path, packed_span = packed["source"]
    span = (
        None
        if packed_span is None
        else {
            "line": packed_span[0],
            "column": packed_span[1],
            "end_line": packed_span[2],
            "end_column": packed_span[3],
        }
    )
    return {
        "schema_version": group["preimage_version"],
        "family": group["kind"],
        "source": {
            "identity_sha256": source_identity,
            "path": source_path,
            "span": span,
        },
        "normalized_semantics": {
            "subject": packed["normalized_semantics"][0],
            "semantic_sha256": packed["normalized_semantics"][1],
        },
        "semantic_owner": group["semantic_owner"],
    }


@dataclass(frozen=True)
class _CallReview:
    approved_names: frozenset[str]
    declared_attributes: frozenset[str]


class _Catalog:
    def __init__(
        self,
        *,
        source_identities: Mapping[str, tuple[str, str]],
        default_source_identity: tuple[str, str],
    ) -> None:
        self._source_identities = source_identities
        self._default_source_identity = default_source_identity
        self._groups: dict[
            tuple[str, str, str, str, str],
            list[dict[str, Any]],
        ] = defaultdict(list)
        self._preimage_by_id: dict[str, Mapping[str, Any]] = {}
        self.blockers: list[dict[str, Any]] = []

    def add(
        self,
        *,
        kind: str,
        owner: str,
        mapping: str,
        reachability: str,
        proof: str,
        subject: str,
        semantic_sha256: str,
    ) -> str:
        if not subject:
            raise StrategyAnalysisError("semantic obligation subject must not be empty")
        preimage = _obligation_preimage(
            family=kind,
            semantic_owner=owner,
            subject=subject,
            semantic_sha256=semantic_sha256,
            source_identities=self._source_identities,
            default_source_identity=self._default_source_identity,
        )
        digest = _sha256_json(preimage)
        obligation_id = f"obl-{kind}-{digest}"
        previous = self._preimage_by_id.get(obligation_id)
        if previous is not None:
            if previous == preimage:
                raise StrategyAnalysisError(
                    f"duplicate semantic obligation preimage {obligation_id}"
                )
            raise StrategyAnalysisError(f"semantic obligation hash collision {obligation_id}")
        self._preimage_by_id[obligation_id] = preimage
        self._groups[(kind, owner, mapping, reachability, proof)].append(
            {
                "obligation_id": obligation_id,
                "preimage": _pack_obligation_preimage(preimage),
            }
        )
        return obligation_id

    def block(
        self,
        *,
        code: str,
        obligation_id: str,
        message: str,
        location: dict[str, Any] | None,
    ) -> None:
        if any(
            item["code"] == code and item["obligation_id"] == obligation_id
            for item in self.blockers
        ):
            return
        self.blockers.append(
            {
                "code": code,
                "obligation_id": obligation_id,
                "message": message,
                "source": location,
            }
        )

    def groups(self) -> list[dict[str, Any]]:
        return [
            {
                "kind": kind,
                "semantic_owner": owner,
                "mapping": mapping,
                "reachability": reachability,
                "proof": proof,
                "preimage_version": SEMANTIC_OBLIGATION_PREIMAGE_VERSION,
                "obligations": sorted(
                    obligations,
                    key=lambda item: item["obligation_id"],
                ),
            }
            for (kind, owner, mapping, reachability, proof), obligations in sorted(
                self._groups.items()
            )
        ]


def write_semantic_obligation_registry(
    path: str | Path,
    document: Mapping[str, Any],
) -> None:
    """Write the large linear registry in deterministic bounded compact JSON."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise StrategyAnalysisError(
            "semantic obligation registry destination must be a non-symlink path"
        )
    serialized = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=False,
        separators=(",", ":"),
    )
    encoded = f"{serialized}\n".encode()
    if len(encoded) > _MAX_SEMANTIC_REGISTRY_JSON_BYTES:
        raise StrategyAnalysisError(
            "semantic obligation registry exceeds its bounded artifact size "
            f"({len(encoded)} > {_MAX_SEMANTIC_REGISTRY_JSON_BYTES})"
        )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
        if os.name == "posix":
            directory_descriptor = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def build_semantic_obligation_registry(
    source: str | Path,
    *,
    class_name: str | None = None,
    analysis: Mapping[str, Any] | None = None,
    strategy: Mapping[str, Any] | None = None,
    runtime_inventory: Mapping[str, Any] | None = None,
    trading_mode: str | None = None,
    config_path: str | Path | None = None,
    source_root: str | Path | None = None,
    upstream_repository: str | None = None,
    upstream_commit: str | None = None,
    upstream_ref: str | None = None,
    upstream_source_path: str | None = None,
    upstream_fetch_timeout_seconds: int = 180,
    upstream_observation: Mapping[str, str] | None = None,
    semantic_profile_path: str | Path = _DEFAULT_PROFILE,
    scheduler_contract_path: str | Path = _DEFAULT_SCHEDULER,
    execution_contract_path: str | Path = _DEFAULT_EXECUTION,
    futures_contract_path: str | Path = _DEFAULT_FUTURES,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build every statically reachable source and pinned-kernel obligation."""
    if analysis is None and strategy is None:
        from .semantic_inventory import build_semantic_inventory

        inventory = build_semantic_inventory(
            source,
            class_name=class_name,
            trading_mode=trading_mode,
            config_path=config_path,
            fixtures_root=Path(source).resolve(),
            source_root=source_root,
            upstream_repository=upstream_repository,
            upstream_commit=upstream_commit,
            upstream_ref=upstream_ref,
            upstream_source_path=upstream_source_path,
            upstream_fetch_timeout_seconds=upstream_fetch_timeout_seconds,
        )
        registry = inventory["obligation_registry"]
        if output_path is not None:
            write_semantic_obligation_registry(output_path, registry)
        return registry
    if analysis is None or strategy is None:
        raise StrategyAnalysisError(
            "semantic registry analyzed inputs must provide analysis and strategy together"
        )
    if class_name is None:
        class_name = str(strategy["name"])

    source_input = Path(source)
    if source_input.is_symlink():
        raise StrategyAnalysisError(f"strategy source must not be a symbolic link: {source_input}")
    source_path = source_input.resolve()
    closure, source_files = _build_source_closure(source_path, source_root=source_root)
    freqtrade, contracts = _load_freqtrade_contract(
        semantic_profile_path=semantic_profile_path,
        scheduler_contract_path=scheduler_contract_path,
        execution_contract_path=execution_contract_path,
        futures_contract_path=futures_contract_path,
    )

    source_identities = {
        source_file.relative_path: (
            "@strategy"
            if source_file.path == source_path
            else f"@source/{source_file.relative_path}",
            hashlib.sha256(source_file.source_bytes).hexdigest(),
        )
        for source_file in source_files
    }
    default_source_identity = source_identities[
        next(
            source_file.relative_path
            for source_file in source_files
            if source_file.path == source_path
        )
    ]
    catalog = _Catalog(
        source_identities=source_identities,
        default_source_identity=default_source_identity,
    )
    callable_state = _callable_reachability(
        source_files,
        source_path=source_path,
        class_name=class_name,
    )
    _add_source_obligations(
        catalog,
        source_files,
        class_name=class_name,
        callable_state=callable_state,
    )
    _add_contract_obligations(catalog, freqtrade, contracts, strategy)
    if runtime_inventory is not None:
        _add_runtime_inventory_obligations(catalog, runtime_inventory)
    _add_closure_blockers(catalog, source_files)
    upstream_identity = _add_upstream_ref_obligation(
        catalog,
        repository=upstream_repository,
        configured_commit=upstream_commit,
        ref=upstream_ref,
        source_path=upstream_source_path or str(closure["root_path"]),
        observation=upstream_observation,
    )

    groups = catalog.groups()
    blockers = sorted(
        catalog.blockers,
        key=lambda item: (
            item["source"]["path"] if item["source"] else "",
            item["source"]["line"] if item["source"] else 0,
            item["source"]["column"] if item["source"] else 0,
            item["code"],
            item["obligation_id"],
        ),
    )
    summary = _registry_summary(groups, blockers, closure_complete=closure["complete"])
    source_record = next(item for item in closure["files"] if item["role"] == "strategy-root")
    report: dict[str, Any] = {
        "schema_version": SEMANTIC_OBLIGATION_REGISTRY_VERSION,
        "identity": {
            "obligation_id_algorithm": "sha256-canonical-semantic-preimage-v1",
            "registry_fingerprint_algorithm": ("sha256-canonical-semantic-content-v1"),
            "source_closure_algorithm": "sha256-merkle-source-closure-v1",
        },
        "freqtrade": freqtrade,
        "strategy": {
            "selected_class": class_name,
            "source": {
                "path": source_record["path"],
                "bytes": int(analysis["source"]["bytes"]),
                "sha256": str(analysis["source"]["sha256"]),
            },
            "upstream": upstream_identity,
        },
        "source_closure": closure,
        "obligation_groups": groups,
        "blockers": blockers,
        "summary": summary,
    }
    report["fingerprint"] = _registry_fingerprint(report)
    validate_semantic_obligation_registry(report)
    if output_path is not None:
        write_semantic_obligation_registry(output_path, report)
    return report


def validate_semantic_obligation_registry(document: Any) -> None:
    """Validate schema, IDs, totals, blocker references, and content fingerprint."""
    validate_schema(
        semantic_registry_schema_projection(document),
        SEMANTIC_OBLIGATION_REGISTRY_SCHEMA,
    )
    if not isinstance(document, Mapping):  # pragma: no cover - schema owns it
        return
    validate_semantic_registry_records(document["obligation_groups"])
    if document["fingerprint"] != _registry_fingerprint(document):
        raise SpecValidationError(
            "semantic obligation registry fingerprint differs from canonical content"
        )

    expected_freqtrade, expected_contracts = _load_freqtrade_contract(
        semantic_profile_path=_DEFAULT_PROFILE,
        scheduler_contract_path=_DEFAULT_SCHEDULER,
        execution_contract_path=_DEFAULT_EXECUTION,
        futures_contract_path=_DEFAULT_FUTURES,
    )
    if document["freqtrade"] != expected_freqtrade:
        raise SpecValidationError(
            "semantic obligation registry does not match the pinned Freqtrade contract"
        )
    _validate_callback_contract_obligations(document)
    _validate_portfolio_contract_obligations(document, expected_contracts["scheduler"])
    _validate_execution_contract_obligations(document, expected_contracts["execution"])
    _validate_source_closure_identity(document)

    groups = document["obligation_groups"]
    group_order = [
        (
            group["kind"],
            group["semantic_owner"],
            group["mapping"],
            group["reachability"],
            group["proof"],
        )
        for group in groups
    ]
    if group_order != sorted(group_order):
        raise SpecValidationError("semantic obligation groups must be canonically sorted")
    all_ids: list[str] = []
    mapping_by_id: dict[str, str] = {}
    canonical_preimages: set[str] = set()
    for group in groups:
        excluded = group["reachability"] == "machine-proven-unreachable"
        if (group["mapping"] == _MAPPING_UNREACHABLE) != excluded:
            raise SpecValidationError(
                "machine-proven-unreachable mapping and reachability must agree"
            )
        if group["preimage_version"] != SEMANTIC_OBLIGATION_PREIMAGE_VERSION:
            raise SpecValidationError("semantic obligation preimage version is unsupported")
        records = group["obligations"]
        obligation_ids = [record["obligation_id"] for record in records]
        if obligation_ids != sorted(obligation_ids):
            raise SpecValidationError("semantic obligation IDs must be sorted in every group")
        for record in records:
            obligation_id = record["obligation_id"]
            preimage = semantic_obligation_preimage(group, record)
            expected_id = f"obl-{group['kind']}-{_sha256_json(preimage)}"
            if obligation_id != expected_id:
                raise SpecValidationError(
                    f"semantic obligation {obligation_id} preimage does not match its ID"
                )
            canonical_preimage = json.dumps(
                preimage,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if canonical_preimage in canonical_preimages:
                raise SpecValidationError("duplicate semantic obligation preimage")
            canonical_preimages.add(canonical_preimage)
            if obligation_id in mapping_by_id:
                raise SpecValidationError(
                    f"semantic obligation {obligation_id} is mapped more than once"
                )
            mapping_by_id[obligation_id] = group["mapping"]
            all_ids.append(obligation_id)
    listed_blocker_ids = [item["obligation_id"] for item in document["blockers"]]
    blocker_ids = set(listed_blocker_ids)
    if len(listed_blocker_ids) != len(blocker_ids):
        raise SpecValidationError("semantic blocker obligation IDs must be unique")
    if document["blockers"] != sorted(
        document["blockers"],
        key=lambda item: (
            item["source"]["path"] if item["source"] else "",
            item["source"]["line"] if item["source"] else 0,
            item["source"]["column"] if item["source"] else 0,
            item["code"],
            item["obligation_id"],
        ),
    ):
        raise SpecValidationError("semantic blockers must be canonically sorted")
    missing = blocker_ids - mapping_by_id.keys()
    if missing:
        raise SpecValidationError(
            f"semantic blocker references unknown obligation {sorted(missing)[0]}"
        )
    for obligation_id in blocker_ids:
        if mapping_by_id[obligation_id] != _MAPPING_OFFICIAL:
            raise SpecValidationError(
                f"semantic blocker {obligation_id} is not mapped to official-only"
            )
    expected = _registry_summary(
        groups,
        document["blockers"],
        closure_complete=bool(document["source_closure"]["complete"]),
    )
    if document["summary"] != expected:
        raise SpecValidationError("semantic obligation registry summary is not derived from groups")
    _validate_upstream_promotion_identity(document)
    if len(all_ids) != len(set(all_ids)):
        raise SpecValidationError("semantic obligation IDs are not globally unique")


def _validate_upstream_promotion_identity(document: Mapping[str, Any]) -> None:
    upstream = document["strategy"]["upstream"]
    blocker_codes = {item["code"] for item in document["blockers"]}
    ref = upstream["ref"]
    configured_commit = upstream["configured_commit"]
    observed_commit = upstream["observed_commit"]
    observed_timestamp = upstream["observed_commit_timestamp"]
    observation_method = upstream["observation_method"]
    observation_status = upstream.get("observation_status")
    native_promotion = document["summary"]["native_promotion"]

    if ref is None:
        expected_method = (
            "offline-unverified-commit-v1"
            if configured_commit is not None
            else "offline-unverified-source-v1"
            if upstream["repository"] is not None
            else "unconfigured-local-source-v1"
        )
        expected_blocker = (
            "UNOBSERVED_UPSTREAM_COMMIT"
            if configured_commit is not None
            else "UNOBSERVED_UPSTREAM_REF"
        )
        if (
            observed_commit is not None
            or observed_timestamp is not None
            or observation_method != expected_method
            or observation_status is not None
            or expected_blocker not in blocker_codes
            or native_promotion
        ):
            raise SpecValidationError(
                "Native promotion requires a configured upstream ref observed "
                "through one immutable commit"
            )
        return

    failure_contract = _UPSTREAM_FAILURE_CONTRACT.get((observation_method, observation_status))
    if failure_contract is not None:
        if (
            observed_commit is not None
            or observed_timestamp is not None
            or failure_contract[0] not in blocker_codes
            or native_promotion
        ):
            raise SpecValidationError(
                "failed upstream ref observation cannot permit Native promotion"
            )
        return
    if (
        upstream["repository"] is None
        or upstream["source_path"] != document["strategy"]["source"]["path"]
    ):
        raise SpecValidationError(
            "configured upstream ref identity does not match the strategy source"
        )
    if (
        observation_method != "git-fetch-depth-1-v1"
        or observation_status is not None
        or observed_commit is None
        or observed_timestamp is None
        or configured_commit is None
    ):
        raise SpecValidationError(
            "configured upstream ref was not dynamically resolved to an immutable commit"
        )
    stale = configured_commit != observed_commit
    if stale != ("STALE_UPSTREAM_REF" in blocker_codes):
        raise SpecValidationError("configured and observed upstream commit status is inconsistent")
    if native_promotion and stale:
        raise SpecValidationError(
            "Native promotion requires the configured and observed commits to match"
        )


def _validate_source_closure_identity(document: Mapping[str, Any]) -> None:
    closure = document["source_closure"]
    files = closure["files"]
    paths = [item["path"] for item in files]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise SpecValidationError("semantic source closure files must be uniquely sorted")
    roots = [item for item in files if item["role"] == "strategy-root"]
    if len(roots) != 1:
        raise SpecValidationError("semantic source closure must contain one strategy root")
    strategy_source = document["strategy"]["source"]
    root = roots[0]
    if any(strategy_source[field] != root[field] for field in ("path", "bytes", "sha256")):
        raise SpecValidationError(
            "strategy identity does not match the semantic source closure root"
        )
    if closure["file_count"] != len(files):
        raise SpecValidationError("semantic source closure file count is not derived")
    if closure["complete"] != (not closure["missing_local_imports"]):
        raise SpecValidationError("semantic source closure completeness is not derived")
    path_set = set(paths)
    for item in files:
        if item["imports"] != sorted(item["imports"]):
            raise SpecValidationError("semantic source closure imports must be sorted")
        unknown_import_targets = set(item["imports"]) - path_set
        if unknown_import_targets:
            raise SpecValidationError(
                "semantic source closure import target is absent from the closure"
            )
    if closure["external_imports"] != sorted(closure["external_imports"]):
        raise SpecValidationError("semantic source closure external imports must be sorted")
    if closure["missing_local_imports"] != sorted(closure["missing_local_imports"]):
        raise SpecValidationError("semantic source closure missing imports must be sorted")
    expected_merkle = _source_closure_merkle(files)
    if closure["merkle_root"] != expected_merkle:
        raise SpecValidationError("semantic source closure Merkle root is not derived")


def load_semantic_obligation_registry(
    source: str | Path,
    *,
    strategy_source: str | Path | None = None,
    source_root: str | Path | None = None,
) -> dict[str, Any]:
    """Load a registry and optionally reject a stale transitive source closure."""
    verify_semantic_obligation_registry_schema_identity()
    document = read_json(
        source,
        max_bytes=_MAX_SEMANTIC_REGISTRY_JSON_BYTES,
    )
    validate_semantic_obligation_registry(document)
    if strategy_source is not None:
        strategy_path = Path(strategy_source).resolve()
        closure, _ = _build_source_closure(strategy_path, source_root=source_root)
        expected_source = document["strategy"]["source"]
        source_bytes = strategy_path.read_bytes()
        if (
            expected_source["bytes"] != len(source_bytes)
            or expected_source["sha256"] != hashlib.sha256(source_bytes).hexdigest()
            or _semantic_closure_identity(document["source_closure"])
            != _semantic_closure_identity(closure)
        ):
            raise SpecValidationError("semantic obligation registry source closure is stale")
    return dict(document)


def packaged_semantic_obligation_registry_identity() -> dict[str, Any]:
    """Verify and return the identity of the complete packaged registry payload."""
    schema_identity = verify_semantic_obligation_registry_schema_identity()
    if (
        not _PACKAGED_REGISTRY.is_file()
        or _PACKAGED_REGISTRY.is_symlink()
        or not _PACKAGED_REGISTRY_MANIFEST.is_file()
        or _PACKAGED_REGISTRY_MANIFEST.is_symlink()
    ):
        raise SpecValidationError(
            "packaged semantic obligation registry payload or manifest is missing"
        )
    manifest = read_json(_PACKAGED_REGISTRY_MANIFEST)
    expected_fields = {
        "schema_version",
        "compression",
        "registry_schema_id",
        "registry_schema_version",
        "registry_schema_bytes",
        "registry_schema_sha256",
        "compressed_sha256",
        "uncompressed_bytes",
        "uncompressed_sha256",
        "registry_fingerprint",
        "upstream_repository",
        "upstream_ref",
        "upstream_commit",
    }
    if not isinstance(manifest, Mapping) or set(manifest) != expected_fields:
        raise SpecValidationError("packaged semantic obligation registry manifest fields differ")
    if manifest["schema_version"] != "packaged-semantic-obligation-registry-v1":
        raise SpecValidationError("packaged semantic obligation registry manifest version differs")
    if manifest["compression"] != "gzip-mtime-zero-v1":
        raise SpecValidationError("packaged semantic obligation registry compression differs")
    if any(
        manifest[field] != schema_identity[identity_field]
        for field, identity_field in (
            ("registry_schema_id", "$id"),
            ("registry_schema_version", "schema_version"),
            ("registry_schema_bytes", "bytes"),
            ("registry_schema_sha256", "sha256"),
        )
    ):
        raise SpecValidationError(
            "SEMANTIC_REGISTRY_SCHEMA_IDENTITY: packaged manifest differs from compiled identity"
        )
    if manifest["compressed_sha256"] != sha256_file(_PACKAGED_REGISTRY):
        raise SpecValidationError("packaged semantic obligation registry compressed hash differs")
    if (
        not isinstance(manifest["uncompressed_bytes"], int)
        or isinstance(manifest["uncompressed_bytes"], bool)
        or not 0 < manifest["uncompressed_bytes"] <= _MAX_SEMANTIC_REGISTRY_JSON_BYTES
    ):
        raise SpecValidationError("packaged semantic obligation registry byte count is invalid")
    for field in ("uncompressed_sha256", "registry_fingerprint"):
        value = manifest[field]
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise SpecValidationError(f"packaged semantic obligation registry {field} is invalid")
    upstream_commit = manifest["upstream_commit"]
    if (
        not isinstance(upstream_commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", upstream_commit) is None
    ):
        raise SpecValidationError(
            "packaged semantic obligation registry upstream_commit is invalid"
        )
    for field in ("upstream_repository", "upstream_ref"):
        if not isinstance(manifest[field], str) or not manifest[field]:
            raise SpecValidationError(f"packaged semantic obligation registry {field} is invalid")
    return dict(manifest)


def _load_packaged_semantic_obligation_registry_integrity() -> tuple[
    dict[str, Any], dict[str, Any]
]:
    """Decompress, hash, parse, and fully validate the immutable shipped registry."""
    manifest = packaged_semantic_obligation_registry_identity()
    payload = bytearray()
    try:
        with gzip.open(_PACKAGED_REGISTRY, "rb") as compressed:
            while chunk := compressed.read(1024 * 1024):
                payload.extend(chunk)
                if len(payload) > _MAX_SEMANTIC_REGISTRY_JSON_BYTES:
                    raise SpecValidationError(
                        "packaged semantic obligation registry exceeds its size bound"
                    )
    except OSError as exc:
        raise SpecValidationError(
            f"packaged semantic obligation registry cannot be decompressed: {exc}"
        ) from exc
    payload_bytes = bytes(payload)
    if len(payload_bytes) != manifest["uncompressed_bytes"]:
        raise SpecValidationError("packaged semantic obligation registry uncompressed size differs")
    if hashlib.sha256(payload_bytes).hexdigest() != manifest["uncompressed_sha256"]:
        raise SpecValidationError("packaged semantic obligation registry uncompressed hash differs")
    document = loads_json_bytes(
        payload_bytes,
        max_bytes=_MAX_SEMANTIC_REGISTRY_JSON_BYTES,
    )
    validate_semantic_obligation_registry(document)
    upstream = document["strategy"]["upstream"]
    if (
        document["fingerprint"] != manifest["registry_fingerprint"]
        or upstream["repository"] != manifest["upstream_repository"]
        or upstream["ref"] != manifest["upstream_ref"]
        or upstream["observed_commit"] != manifest["upstream_commit"]
        or upstream["configured_commit"] != manifest["upstream_commit"]
        or upstream["observation_method"] != "git-fetch-depth-1-v1"
        or not document["summary"]["native_promotion"]
    ):
        raise SpecValidationError(
            "packaged semantic obligation registry identity or promotion differs"
        )
    return dict(document), manifest


def validate_packaged_semantic_obligation_registry_integrity() -> dict[str, Any]:
    """Validate immutable package integrity without claiming current promotion."""
    document, manifest = _load_packaged_semantic_obligation_registry_integrity()
    return {
        "schema_version": "packaged-semantic-registry-integrity-v1",
        "integrity_valid": True,
        "compressed_sha256": manifest["compressed_sha256"],
        "uncompressed_sha256": manifest["uncompressed_sha256"],
        "registry_fingerprint": document["fingerprint"],
        "upstream_commit": manifest["upstream_commit"],
        "native_promotion": False,
    }


def load_immutable_packaged_semantic_registry_for_offline_audit() -> dict[str, Any]:
    """Load immutable package contents without making a live promotion claim."""
    document, _manifest = _load_packaged_semantic_obligation_registry_integrity()
    return document


def begin_packaged_semantic_registry_authorization(
    authorization: CurrentRefAuthorization,
    *,
    upstream_fetch_timeout_seconds: int = 180,
) -> PackagedRegistryCurrentRefProof:
    """Obtain the first exact current-ref/source observation for one authorization."""
    document, manifest = _load_packaged_semantic_obligation_registry_integrity()
    observed_commit = _observe_packaged_registry_current_ref(
        document,
        manifest,
        timeout_seconds=upstream_fetch_timeout_seconds,
        initial_observed_commit=None,
    )
    return PackagedRegistryCurrentRefProof(
        authorization=authorization,
        authorization_digest=authorization.digest,
        packaged_commit=str(manifest["upstream_commit"]),
        packaged_source_closure_sha256=str(document["source_closure"]["merkle_root"]),
        initial_observed_commit=observed_commit,
        document=document,
    )


def finalize_packaged_semantic_registry_authorization(
    proof: PackagedRegistryCurrentRefProof,
    *,
    upstream_fetch_timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Reobserve the exact ref/source immediately before authorization output."""
    manifest = packaged_semantic_obligation_registry_identity()
    if (
        proof.authorization.digest != proof.authorization_digest
        or proof.packaged_commit != manifest["upstream_commit"]
        or proof.packaged_source_closure_sha256 != proof.document["source_closure"]["merkle_root"]
        or not proof.authorization.digest
    ):
        message = (
            "packaged current-ref proof authorization digest differs"
            if proof.authorization.digest != proof.authorization_digest
            else "packaged current-ref proof identity differs"
        )
        raise SpecValidationError(message)
    _observe_packaged_registry_current_ref(
        proof.document,
        manifest,
        timeout_seconds=upstream_fetch_timeout_seconds,
        initial_observed_commit=proof.initial_observed_commit,
    )
    return proof.document


def _observe_packaged_registry_current_ref(
    document: dict[str, Any],
    manifest: dict[str, Any],
    *,
    timeout_seconds: int,
    initial_observed_commit: str | None,
) -> str:
    repository = str(manifest["upstream_repository"])
    ref = str(manifest["upstream_ref"])
    source_path = str(document["strategy"]["upstream"]["source_path"])
    from .semantic_inventory import _fetch_upstream_ref_once

    with tempfile.TemporaryDirectory(prefix="nfi-packaged-current-ref-") as temporary:
        checkout, observation = _fetch_upstream_ref_once(
            Path(temporary),
            repository=repository,
            ref=ref,
            source_path=source_path,
            timeout_seconds=timeout_seconds,
        )
        if checkout is None:
            raise PackagedRegistryCurrentRefError(
                code=observation["blocker_code"],
                observation_method=observation["observation_method"],
                observation_status=observation["observation_status"],
                repository=repository,
                ref=ref,
                packaged_commit=str(manifest["upstream_commit"]),
                observed_commit=None,
            )
        observed_commit = str(observation["observed_commit"])
        if observed_commit != manifest["upstream_commit"]:
            code = (
                "UPSTREAM_REF_MOVED_DURING_AUTHORIZATION"
                if initial_observed_commit is not None
                else "STALE_UPSTREAM_REF"
            )
            raise PackagedRegistryCurrentRefError(
                code=code,
                observation_method=observation["observation_method"],
                observation_status="moved" if initial_observed_commit is not None else "stale",
                repository=repository,
                ref=ref,
                packaged_commit=str(manifest["upstream_commit"]),
                observed_commit=observed_commit,
            )
        observed_source = checkout.joinpath(*Path(source_path).parts)
        closure, _missing = _build_source_closure(observed_source, source_root=checkout)
        source_bytes = observed_source.read_bytes()
        expected_source = document["strategy"]["source"]
        if (
            expected_source["bytes"] != len(source_bytes)
            or expected_source["sha256"] != hashlib.sha256(source_bytes).hexdigest()
            or _semantic_closure_identity(document["source_closure"])
            != _semantic_closure_identity(closure)
        ):
            raise PackagedRegistryCurrentRefError(
                code="UPSTREAM_SOURCE_IDENTITY_MISMATCH",
                observation_method=observation["observation_method"],
                observation_status="source-identity-mismatch",
                repository=repository,
                ref=ref,
                packaged_commit=str(manifest["upstream_commit"]),
                observed_commit=observed_commit,
            )
    return observed_commit


def load_packaged_semantic_obligation_registry(
    *,
    upstream_fetch_timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Load after two fresh observations bound to this standalone operation."""
    document, manifest = _load_packaged_semantic_obligation_registry_integrity()
    authorization = CurrentRefAuthorization(
        operation="semantic-registry-packaged",
        candidate_commit=str(manifest["upstream_commit"]),
        candidate_identity_sha256=str(manifest["uncompressed_sha256"]),
        source_closure_sha256=str(document["source_closure"]["merkle_root"]),
        workload_run_nonce_sha256=hashlib.sha256(
            f"semantic-registry-packaged:{manifest['registry_fingerprint']}".encode()
        ).hexdigest(),
    )
    proof = begin_packaged_semantic_registry_authorization(
        authorization,
        upstream_fetch_timeout_seconds=upstream_fetch_timeout_seconds,
    )
    return finalize_packaged_semantic_registry_authorization(
        proof,
        upstream_fetch_timeout_seconds=upstream_fetch_timeout_seconds,
    )


def package_semantic_obligation_registry(
    source: str | Path,
    *,
    destination: str | Path = _PACKAGED_REGISTRY,
    manifest_path: str | Path = _PACKAGED_REGISTRY_MANIFEST,
) -> dict[str, Any]:
    """Create the deterministic complete gzip payload and identity manifest."""
    verify_semantic_obligation_registry_schema_identity()
    source_path = Path(source)
    if not source_path.is_file() or source_path.is_symlink():
        raise StrategyAnalysisError(
            f"semantic obligation registry source is not a regular file: {source_path}"
        )
    payload = source_path.read_bytes()
    if len(payload) > _MAX_SEMANTIC_REGISTRY_JSON_BYTES:
        raise StrategyAnalysisError("semantic obligation registry exceeds the packaged size bound")
    document = loads_json_bytes(
        payload,
        max_bytes=_MAX_SEMANTIC_REGISTRY_JSON_BYTES,
    )
    validate_semantic_obligation_registry(document)
    upstream = document["strategy"]["upstream"]
    if (
        not all(
            isinstance(upstream[field], str) and upstream[field]
            for field in (
                "repository",
                "ref",
                "configured_commit",
                "observed_commit",
            )
        )
        or upstream["configured_commit"] != upstream["observed_commit"]
        or upstream["observation_method"] != "git-fetch-depth-1-v1"
        or document["summary"]["native_promotion"] is not True
    ):
        raise StrategyAnalysisError("only a dynamically observed upstream registry can be packaged")
    destination_path = Path(destination)
    manifest_destination = Path(manifest_path)
    if destination_path.absolute() == manifest_destination.absolute():
        raise StrategyAnalysisError(
            "packaged semantic registry payload and manifest destinations must differ"
        )
    for target in (destination_path, manifest_destination):
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise StrategyAnalysisError(
                "packaged semantic registry destinations must be regular non-symlink files"
            )

    payload_descriptor, payload_temporary_name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.",
        suffix=".tmp",
        dir=destination_path.parent,
    )
    os.close(payload_descriptor)
    payload_temporary = Path(payload_temporary_name)
    manifest_temporary: Path | None = None
    payload_backup: Path | None = None
    manifest_backup: Path | None = None
    payload_published = False
    manifest_published = False
    try:
        with payload_temporary.open("wb") as raw:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=9,
                fileobj=raw,
                mtime=0,
            ) as compressed:
                compressed.write(payload)
            raw.flush()
            os.fsync(raw.fileno())
        schema_identity = semantic_obligation_registry_schema_identity()
        manifest = {
            "schema_version": "packaged-semantic-obligation-registry-v1",
            "compression": "gzip-mtime-zero-v1",
            "registry_schema_id": schema_identity["$id"],
            "registry_schema_version": schema_identity["schema_version"],
            "registry_schema_bytes": schema_identity["bytes"],
            "registry_schema_sha256": schema_identity["sha256"],
            "compressed_sha256": sha256_file(payload_temporary),
            "uncompressed_bytes": len(payload),
            "uncompressed_sha256": hashlib.sha256(payload).hexdigest(),
            "registry_fingerprint": document["fingerprint"],
            "upstream_repository": upstream["repository"],
            "upstream_ref": upstream["ref"],
            "upstream_commit": upstream["observed_commit"],
        }
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
        ).encode()
        manifest_descriptor, manifest_temporary_name = tempfile.mkstemp(
            prefix=f".{manifest_destination.name}.",
            suffix=".tmp",
            dir=manifest_destination.parent,
        )
        manifest_temporary = Path(manifest_temporary_name)
        with os.fdopen(manifest_descriptor, "wb") as handle:
            handle.write(manifest_bytes)
            handle.flush()
            os.fsync(handle.fileno())

        def backup(target: Path) -> Path | None:
            if not target.exists():
                return None
            descriptor, backup_name = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".backup",
                dir=target.parent,
            )
            os.close(descriptor)
            backup_path = Path(backup_name)
            try:
                shutil.copyfile(target, backup_path)
            except BaseException:
                backup_path.unlink(missing_ok=True)
                raise
            return backup_path

        payload_backup = backup(destination_path)
        manifest_backup = backup(manifest_destination)
        payload_temporary.replace(destination_path)
        payload_published = True
        manifest_temporary.replace(manifest_destination)
        manifest_published = True
        if os.name == "posix":
            for directory in {destination_path.parent, manifest_destination.parent}:
                directory_descriptor = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
        return manifest
    except BaseException:
        if manifest_published:
            if manifest_backup is None:
                manifest_destination.unlink(missing_ok=True)
            else:
                manifest_backup.replace(manifest_destination)
                manifest_backup = None
        if payload_published:
            if payload_backup is None:
                destination_path.unlink(missing_ok=True)
            else:
                payload_backup.replace(destination_path)
                payload_backup = None
        raise
    finally:
        payload_temporary.unlink(missing_ok=True)
        if manifest_temporary is not None:
            manifest_temporary.unlink(missing_ok=True)
        if payload_backup is not None:
            payload_backup.unlink(missing_ok=True)
        if manifest_backup is not None:
            manifest_backup.unlink(missing_ok=True)


def _build_source_closure(
    source: Path,
    *,
    source_root: str | Path | None,
) -> tuple[dict[str, Any], list[_SourceFile]]:
    root = Path(source_root).resolve() if source_root is not None else source.parent.resolve()
    if not source.is_file():
        raise StrategyAnalysisError(f"strategy source does not exist: {source}")
    if source.is_symlink():
        raise StrategyAnalysisError(f"strategy source must not be a symbolic link: {source}")
    source = source.resolve()
    if not source.is_relative_to(root):
        raise StrategyAnalysisError(f"strategy source is outside its source root: {source}")

    queued: deque[Path] = deque([source])
    seen: set[Path] = set()
    parsed: list[_SourceFile] = []
    while queued:
        path = queued.popleft().resolve()
        if path in seen:
            continue
        if not path.is_relative_to(root):
            raise StrategyAnalysisError(f"local strategy import escapes its source root: {path}")
        if path.is_symlink() or not path.is_file():
            raise StrategyAnalysisError(f"local strategy source is not a regular file: {path}")
        source_bytes = path.read_bytes()
        try:
            text = source_bytes.decode("utf-8")
            tree = ast.parse(text, filename=str(path), type_comments=True)
        except UnicodeDecodeError as exc:
            raise StrategyAnalysisError(f"local strategy source is not UTF-8: {path}") from exc
        except SyntaxError as exc:
            raise StrategyAnalysisError(
                f"local strategy source does not parse: {path}:{exc.lineno or 1}: {exc.msg}"
            ) from exc
        local, external, missing = _resolve_imports(tree, path=path, root=root)
        relative = path.relative_to(root).as_posix()
        parsed.append(
            _SourceFile(
                path=path,
                relative_path=relative,
                source_bytes=source_bytes,
                tree=tree,
                local_imports=tuple(sorted(item.relative_to(root).as_posix() for item in local)),
                external_imports=tuple(sorted(external)),
                missing_local_imports=tuple(sorted(missing)),
            )
        )
        seen.add(path)
        queued.extend(sorted(local - seen, key=lambda item: item.as_posix()))

    parsed.sort(key=lambda item: item.relative_path)
    records = [
        {
            "path": item.relative_path,
            "role": "strategy-root" if item.path == source else "transitive-local-source",
            "bytes": len(item.source_bytes),
            "sha256": hashlib.sha256(item.source_bytes).hexdigest(),
            "imports": list(item.local_imports),
        }
        for item in parsed
    ]
    missing_imports = sorted(
        {
            f"{item.relative_path}:{missing}"
            for item in parsed
            for missing in item.missing_local_imports
        }
    )
    external_imports = sorted({name for item in parsed for name in item.external_imports})
    merkle_root = _source_closure_merkle(records)
    closure = {
        "algorithm": "sha256-merkle-source-closure-v1",
        "root_path": ".",
        "complete": not missing_imports,
        "file_count": len(records),
        "files": records,
        "external_imports": external_imports,
        "missing_local_imports": missing_imports,
        "merkle_root": merkle_root,
    }
    return closure, parsed


def _source_closure_merkle(files: Sequence[Mapping[str, Any]]) -> str:
    root_sha256 = next(item["sha256"] for item in files if item["role"] == "strategy-root")
    content_leaves = sorted(
        _sha256_json(
            {
                "algorithm": "sha256-merkle-source-content-v1",
                "bytes": item["bytes"],
                "sha256": item["sha256"],
            }
        )
        for item in files
    )
    return hashlib.sha256(
        b"semantic-source-closure-v1\0"
        + bytes.fromhex(root_sha256)
        + b"".join(bytes.fromhex(leaf) for leaf in content_leaves)
    ).hexdigest()


def _resolve_imports(
    tree: ast.Module,
    *,
    path: Path,
    root: Path,
) -> tuple[set[Path], set[str], set[str]]:
    local: set[Path] = set()
    external: set[str] = set()
    missing: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                candidate = _resolve_module(root, alias.name.split("."), root=root)
                if candidate is None:
                    external.add(alias.name.split(".", 1)[0])
                else:
                    local.add(candidate)
        elif isinstance(node, ast.ImportFrom):
            anchor = root
            if node.level:
                anchor = path.parent
                for _ in range(node.level - 1):
                    anchor = anchor.parent
            module_parts = node.module.split(".") if node.module else []
            module_path = _resolve_module(anchor, module_parts, root=root)
            if module_path is not None:
                local.add(module_path)
            member_found = False
            for alias in node.names:
                if alias.name == "*":
                    continue
                candidate = _resolve_module(
                    anchor,
                    [*module_parts, *alias.name.split(".")],
                    root=root,
                )
                if candidate is not None:
                    local.add(candidate)
                    member_found = True
            if module_path is None and not member_found:
                rendered = "." * node.level + (node.module or "")
                if node.level:
                    missing.add(rendered or ".")
                elif node.module:
                    external.add(node.module.split(".", 1)[0])
    return local, external, missing


def _resolve_module(anchor: Path, parts: Sequence[str], *, root: Path) -> Path | None:
    if not parts:
        init = anchor / "__init__.py"
        return init.resolve() if init.is_file() and init.resolve().is_relative_to(root) else None
    base = anchor.joinpath(*parts)
    candidates = (base.with_suffix(".py"), base / "__init__.py")
    for candidate in candidates:
        if candidate.is_symlink():
            raise StrategyAnalysisError(
                f"local strategy source must not be a symbolic link: {candidate}"
            )
        if candidate.is_file() and candidate.resolve().is_relative_to(root):
            return candidate.resolve()
    return None


def _load_freqtrade_contract(
    *,
    semantic_profile_path: str | Path,
    scheduler_contract_path: str | Path,
    execution_contract_path: str | Path,
    futures_contract_path: str | Path,
) -> tuple[dict[str, Any], dict[str, Mapping[str, Any]]]:
    profile_path = Path(semantic_profile_path)
    scheduler_path = Path(scheduler_contract_path)
    execution_path = Path(execution_contract_path)
    futures_path = Path(futures_contract_path)
    profile = load_freqtrade_semantic_profile(profile_path)
    scheduler = load_scheduler_contract(
        scheduler_path,
        semantic_profile_path=profile_path,
    )
    execution = load_execution_contract(
        execution_path,
        semantic_profile_path=profile_path,
        scheduler_contract_path=scheduler_path,
    )
    futures = load_futures_contract(
        futures_path,
        semantic_profile_path=profile_path,
        scheduler_contract_path=scheduler_path,
        execution_contract_path=execution_path,
    )
    observed_methods = profile["observer"]["observed_methods"]
    method_leaves = [
        _sha256_json(method)
        for method in sorted(
            observed_methods,
            key=lambda item: (item["owner"], item["method"]),
        )
    ]
    source_merkle = hashlib.sha256(
        b"freqtrade-observed-method-source-v1\0"
        + b"".join(bytes.fromhex(leaf) for leaf in method_leaves)
    ).hexdigest()
    freqtrade = {
        "semantic_profile": {
            "schema_version": profile["schema_version"],
            "fingerprint": profile["fingerprint"],
            "file_sha256": sha256_file(profile_path),
        },
        "reference": dict(profile["reference"]),
        "source": {
            "repository": FREQTRADE_SOURCE_REPOSITORY,
            "commit": FREQTRADE_SOURCE_COMMIT,
            "identity_kind": "git-commit-and-observed-method-merkle-v1",
            "observed_method_count": len(observed_methods),
            "observed_method_merkle_root": source_merkle,
        },
        "contracts": [
            {
                "name": name,
                "schema_version": contract["schema_version"],
                "fingerprint": contract["fingerprint"],
                "file_sha256": sha256_file(path),
            }
            for name, contract, path in (
                ("scheduler", scheduler, scheduler_path),
                ("execution", execution, execution_path),
                ("futures", futures, futures_path),
            )
        ],
    }
    return freqtrade, {
        "profile": profile,
        "scheduler": scheduler,
        "execution": execution,
        "futures": futures,
    }


def _callable_reachability(
    source_files: Sequence[_SourceFile],
    *,
    source_path: Path,
    class_name: str,
) -> dict[str, Any]:
    callables, node_keys = _collect_callables(
        source_files,
        source_path=source_path,
        class_name=class_name,
    )
    by_file_name: dict[tuple[str, str], list[str]] = defaultdict(list)
    by_class_method: dict[tuple[str, str, str], str] = {}
    nested_by_parent_name: dict[tuple[str, str], str] = {}
    classes: dict[tuple[str, str], list[str]] = defaultdict(list)
    for key, record in callables.items():
        by_file_name[(record.relative_path, record.name)].append(key)
        if record.class_name is not None:
            by_class_method[(record.relative_path, record.class_name, record.name)] = key
            classes[(record.relative_path, record.class_name)].append(key)
        if record.parent_key is not None:
            nested_by_parent_name[(record.parent_key, record.name)] = key

    edges: dict[str, set[str]] = defaultdict(set)
    activated_classes: dict[str, set[tuple[str, str]]] = defaultdict(set)
    source_by_relative = {item.relative_path: item for item in source_files}
    for key, record in callables.items():
        owner_file = source_by_relative[record.relative_path]
        for node in _walk_callable_body(record.node, node_keys):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in {"self", "cls"}
                and record.class_name is not None
            ):
                target_key = by_class_method.get(
                    (record.relative_path, record.class_name, node.attr)
                )
                if target_key is not None:
                    edges[key].add(target_key)
            if isinstance(node, ast.Name):
                nested_key = nested_by_parent_name.get((key, node.id))
                if nested_key is not None:
                    edges[key].add(nested_key)
            if not isinstance(node, ast.Call):
                continue
            target = _resolve_local_call(
                node,
                record=record,
                owner_file=owner_file,
                by_file_name=by_file_name,
                by_class_method=by_class_method,
                nested_by_parent_name=nested_by_parent_name,
                classes=classes,
            )
            if isinstance(target, str):
                edges[key].add(target)
            elif target:
                activated_classes[key].update(target)

    roots: set[str] = set()
    unknown_callbacks: set[str] = set()
    actual_root_relative = next(
        item.relative_path for item in source_files if item.path == source_path
    )
    for key, record in callables.items():
        if record.parent_key is not None:
            continue
        if record.relative_path == actual_root_relative and record.selected_method:
            # Python reflection and framework lifecycle hooks make every method on
            # the selected strategy conservatively reachable. Only nested/local
            # callables and methods on uninstantiated helper classes may be excluded.
            roots.add(key)
            if (
                record.name not in STRATEGY_CALLBACKS | _VECTOR_ENTRYPOINTS
                and not _has_property_decorator(record.node)
                and _looks_like_unknown_callback(record.name)
            ):
                unknown_callbacks.add(key)
        elif record.class_name is None:
            # A local module-level helper imported by the strategy may be passed as
            # a value, so absence of a direct Call node is not an unreachability proof.
            roots.add(key)

    reachable: set[str] = set()
    queue: deque[str] = deque(sorted(roots))
    while queue:
        key = queue.popleft()
        if key in reachable:
            continue
        reachable.add(key)
        record = callables[key]
        queue.extend(sorted(edges.get(key, set()) - reachable))
        for class_identity in activated_classes.get(key, set()):
            queue.extend(sorted(set(classes[class_identity]) - reachable))
        for nested_key, nested in callables.items():
            if nested.parent_key == key and nested_key in edges.get(key, set()):
                queue.append(nested_key)
        if record.parent_key is not None and record.parent_key not in reachable:
            # A nested callable cannot execute unless its defining scope executed.
            queue.append(record.parent_key)

    return {
        "callables": callables,
        "node_keys": node_keys,
        "reachable": reachable,
        "unknown_callbacks": unknown_callbacks,
        "roots": roots,
    }


def _collect_callables(
    source_files: Sequence[_SourceFile],
    *,
    source_path: Path,
    class_name: str,
) -> tuple[dict[str, _Callable], dict[int, str]]:
    callables: dict[str, _Callable] = {}
    node_keys: dict[int, str] = {}

    def visit(
        node: ast.AST,
        *,
        relative_path: str,
        parent_key: str | None,
        current_class: str | None,
        selected_class: bool,
        callback_root: str | None,
    ) -> None:
        next_parent = parent_key
        next_callback = callback_root
        if isinstance(node, ast.ClassDef):
            current_class = node.name
            selected_class = selected_class or (
                relative_path == root_relative and node.name == class_name
            )
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            key = (
                f"{relative_path}:{current_class + '.' if current_class else ''}{node.name}"
                f"@{node.lineno}:{node.col_offset}"
            )
            is_selected_method = (
                selected_class and current_class == class_name and parent_key is None
            )
            if is_selected_method and (
                node.name in STRATEGY_CALLBACKS or _looks_like_unknown_callback(node.name)
            ):
                next_callback = node.name
            record = _Callable(
                key=key,
                name=node.name,
                node=node,
                relative_path=relative_path,
                class_name=current_class,
                parent_key=parent_key,
                selected_method=is_selected_method,
                callback_root=next_callback,
            )
            callables[key] = record
            node_keys[id(node)] = key
            next_parent = key
        for child in ast.iter_child_nodes(node):
            visit(
                child,
                relative_path=relative_path,
                parent_key=next_parent,
                current_class=current_class,
                selected_class=selected_class,
                callback_root=next_callback,
            )

    root_relative = next(item.relative_path for item in source_files if item.path == source_path)
    for source_file in source_files:
        visit(
            source_file.tree,
            relative_path=source_file.relative_path,
            parent_key=None,
            current_class=None,
            selected_class=False,
            callback_root=None,
        )
    return callables, node_keys


def _walk_callable_body(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    node_keys: Mapping[int, str],
) -> Iterable[ast.AST]:
    stack: list[ast.AST] = list(reversed(function.body))
    while stack:
        node = stack.pop()
        yield node
        for child in reversed(list(ast.iter_child_nodes(node))):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) and id(child) in node_keys:
                continue
            stack.append(child)


def _resolve_local_call(
    node: ast.Call,
    *,
    record: _Callable,
    owner_file: _SourceFile,
    by_file_name: Mapping[tuple[str, str], list[str]],
    by_class_method: Mapping[tuple[str, str, str], str],
    nested_by_parent_name: Mapping[tuple[str, str], str],
    classes: Mapping[tuple[str, str], list[str]],
) -> str | set[tuple[str, str]] | None:
    function = node.func
    if isinstance(function, ast.Attribute):
        if isinstance(function.value, ast.Name) and function.value.id in {"self", "cls"}:
            if record.class_name is None:
                return None
            return by_class_method.get((record.relative_path, record.class_name, function.attr))
        if isinstance(function.value, ast.Name):
            matches = [
                key
                for imported_path in owner_file.local_imports
                for key in by_file_name.get((imported_path, function.attr), [])
            ]
            return matches[0] if len(matches) == 1 else None
        return None
    if not isinstance(function, ast.Name):
        return None
    nested = nested_by_parent_name.get((record.key, function.id))
    if nested is not None:
        return nested
    local = by_file_name.get((record.relative_path, function.id), [])
    if len(local) == 1:
        return local[0]
    imported = [
        key
        for imported_path in owner_file.local_imports
        for key in by_file_name.get((imported_path, function.id), [])
    ]
    if len(imported) == 1:
        return imported[0]
    activated = {
        class_identity
        for class_identity in classes
        if class_identity[0] == record.relative_path and class_identity[1] == function.id
    }
    return activated or None


def _add_source_obligations(
    catalog: _Catalog,
    source_files: Sequence[_SourceFile],
    *,
    class_name: str,
    callable_state: Mapping[str, Any],
) -> None:
    del class_name
    callables: Mapping[str, _Callable] = callable_state["callables"]
    node_keys: Mapping[int, str] = callable_state["node_keys"]
    reachable: set[str] = callable_state["reachable"]
    unknown_callbacks: set[str] = callable_state["unknown_callbacks"]
    closure_declarations = frozenset(
        node.name
        for source_file in source_files
        for node in ast.walk(source_file.tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    )

    for source_file in source_files:
        source_content_hash = hashlib.sha256(source_file.source_bytes).hexdigest()
        call_review = _build_call_review(
            source_file.tree,
            closure_declarations=closure_declarations,
        )
        structural_hashes = _structural_hashes(source_file.tree)
        parent_nodes = {
            id(child): parent
            for parent in ast.walk(source_file.tree)
            for child in ast.iter_child_nodes(parent)
        }
        node_callable = _node_callable_map(source_file.tree, node_keys)
        semantic_nodes = [
            node for node in ast.walk(source_file.tree) if isinstance(node, _SEMANTIC_AST_NODE)
        ]
        embedded_unknown_nodes = [
            node
            for node in ast.walk(source_file.tree)
            if not isinstance(
                node,
                _SEMANTIC_AST_NODE | _REVIEWED_EMBEDDED_AST_NODE,
            )
        ]
        for node in embedded_unknown_nodes:
            callable_key = node_callable.get(id(node))
            mapping = _source_mapping(
                node,
                callable_key=callable_key,
                reachable=reachable,
                unknown_callbacks=unknown_callbacks,
                call_review=call_review,
            )
            parent = parent_nodes.get(id(node), source_file.tree)
            location = _source_location(source_file.relative_path, parent)
            context_hash = (
                structural_hashes[id(callables[callable_key].node)]
                if callable_key is not None and callable_key in callables
                else structural_hashes[id(source_file.tree)]
            )
            obligation_id = catalog.add(
                kind="ast-node",
                owner="nfi-strategy",
                mapping=mapping[0],
                reachability=mapping[1],
                proof=mapping[2],
                subject=_node_locator(location, type(node).__name__),
                semantic_sha256=_source_node_semantic_hash(
                    source_content_hash=source_content_hash,
                    context_hash=context_hash,
                    node=node,
                    structural_hash=structural_hashes[id(node)],
                    parent_nodes=parent_nodes,
                ),
            )
            if mapping[1] == "reachable":
                catalog.block(
                    code="UNKNOWN_REACHABLE_AST_NODE",
                    obligation_id=obligation_id,
                    message=f"reachable AST node {type(node).__name__} is not reviewed",
                    location=location,
                )
        for node in semantic_nodes:
            callable_key = node_callable.get(id(node))
            mapping = _source_mapping(
                node,
                callable_key=callable_key,
                reachable=reachable,
                unknown_callbacks=unknown_callbacks,
                call_review=call_review,
            )
            location = _source_location(
                source_file.relative_path,
                node,
                parent_nodes=parent_nodes,
            )
            locator = _node_locator(location, type(node).__name__)
            structural_hash = structural_hashes[id(node)]
            context_hash = (
                structural_hashes[id(callables[callable_key].node)]
                if callable_key is not None and callable_key in callables
                else structural_hashes[id(source_file.tree)]
            )
            node_semantic_hash = _source_node_semantic_hash(
                source_content_hash=source_content_hash,
                context_hash=context_hash,
                node=node,
                structural_hash=structural_hash,
                parent_nodes=parent_nodes,
            )
            node_kind = "call" if isinstance(node, ast.Call) else "ast-node"
            obligation_subject = (
                f"{locator}:callee={_normalized_call_target(node.func)}"
                if isinstance(node, ast.Call)
                else locator
            )
            obligation_id = catalog.add(
                kind=node_kind,
                owner="nfi-strategy",
                mapping=mapping[0],
                reachability=mapping[1],
                proof=mapping[2],
                subject=obligation_subject,
                semantic_sha256=node_semantic_hash,
            )
            if mapping[1] == "reachable":
                if type(node).__name__ not in _SUPPORTED_REACHABLE_AST_TYPES:
                    catalog.block(
                        code="UNKNOWN_REACHABLE_AST_NODE",
                        obligation_id=obligation_id,
                        message=(f"reachable AST node {type(node).__name__} is not reviewed"),
                        location=location,
                    )
                elif isinstance(node, ast.Call) and not _is_supported_call(node, call_review):
                    catalog.block(
                        code="UNKNOWN_REACHABLE_CALL",
                        obligation_id=obligation_id,
                        message=(
                            f"reachable call target {type(node.func).__name__} is not reviewed"
                        ),
                        location=location,
                    )
            _add_node_operator_obligations(
                catalog,
                node,
                locator=locator,
                context_sha256=node_semantic_hash,
                mapping=mapping,
                location=location,
            )
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                callable_record = callables.get(node_keys.get(id(node), ""))
                callable_id = catalog.add(
                    kind="callable",
                    owner="nfi-strategy",
                    mapping=mapping[0],
                    reachability=mapping[1],
                    proof=mapping[2],
                    subject=f"{locator}:{node.name}",
                    semantic_sha256=node_semantic_hash,
                )
                if callable_record is not None and callable_record.key in unknown_callbacks:
                    catalog.block(
                        code="UNKNOWN_STRATEGY_CALLBACK",
                        obligation_id=callable_id,
                        message=f"strategy callback {node.name!r} has no reviewed contract",
                        location=location,
                    )
            _add_decision_obligations(
                catalog,
                node,
                locator=locator,
                structural_hashes=structural_hashes,
                context_sha256=node_semantic_hash,
                mapping=mapping,
            )
            _add_threshold_obligations(
                catalog,
                node,
                locator=locator,
                structural_hashes=structural_hashes,
                context_sha256=node_semantic_hash,
                mapping=mapping,
            )
            _add_signal_tag_obligations(
                catalog,
                node,
                locator=locator,
                structural_hash=structural_hash,
                context_sha256=node_semantic_hash,
                mapping=mapping,
                callback_root=(
                    callables[callable_key].callback_root
                    if callable_key is not None and callable_key in callables
                    else None
                ),
            )
            callback_root = (
                callables[callable_key].callback_root
                if callable_key is not None and callable_key in callables
                else None
            )
            if callback_root is not None:
                _add_callback_obligations(
                    catalog,
                    node,
                    callback_root=callback_root,
                    locator=locator,
                    structural_hash=structural_hash,
                    context_sha256=node_semantic_hash,
                    mapping=mapping,
                )

        for callable_key, callable_record in sorted(callables.items()):
            if callable_record.relative_path != source_file.relative_path:
                continue
            callable_mapping = _source_mapping(
                callable_record.node,
                callable_key=callable_key,
                reachable=reachable,
                unknown_callbacks=unknown_callbacks,
            )
            if callable_record.callback_root is None or callable_record.parent_key is not None:
                continue
            callback_location = _source_location(
                source_file.relative_path,
                callable_record.node,
            )
            for outcome in ("normal-completion", "raised-error"):
                catalog.add(
                    kind="callback-exception",
                    owner="nfi-strategy",
                    mapping=callable_mapping[0],
                    reachability=callable_mapping[1],
                    proof=callable_mapping[2],
                    subject=(
                        f"{_node_locator(callback_location, 'callback')}:"
                        f"{callable_record.name}:{outcome}"
                    ),
                    semantic_sha256=_sha256_json(
                        {
                            "callback": structural_hashes[id(callable_record.node)],
                            "outcome": outcome,
                        }
                    ),
                )
            _add_state_machine_paths(
                catalog,
                callable_record,
                source_file.relative_path,
                mapping=callable_mapping,
                structural_hashes=structural_hashes,
            )


def _add_node_operator_obligations(
    catalog: _Catalog,
    node: ast.AST,
    *,
    locator: str,
    context_sha256: str,
    mapping: tuple[str, str, str],
    location: dict[str, Any],
) -> None:
    occurrences: list[tuple[str, int, ast.AST]] = []
    for field, value in ast.iter_fields(node):
        if isinstance(value, _OPERATOR_NODE):
            occurrences.append((field, 0, value))
        elif isinstance(value, list):
            occurrences.extend(
                (field, index, item)
                for index, item in enumerate(value)
                if isinstance(item, _OPERATOR_NODE)
            )
    for field, index, operator in occurrences:
        operator_name = type(operator).__name__
        supported = operator_name in _SUPPORTED_OPERATOR_TYPES
        operator_mapping = mapping
        if mapping[1] == "reachable" and not supported:
            operator_mapping = (
                _MAPPING_OFFICIAL,
                "reachable",
                "typed-unsupported-construct",
            )
        obligation_id = catalog.add(
            kind="operator",
            owner="nfi-strategy",
            mapping=operator_mapping[0],
            reachability=operator_mapping[1],
            proof=operator_mapping[2],
            subject=f"{locator}:{field}-{index}:{operator_name}",
            semantic_sha256=_sha256_json(
                {
                    "parent": context_sha256,
                    "field": field,
                    "operator_index": index,
                    "operator": operator_name,
                }
            ),
        )
        if operator_mapping[1] == "reachable" and not supported:
            catalog.block(
                code="UNKNOWN_REACHABLE_OPERATOR",
                obligation_id=obligation_id,
                message=f"reachable operator {operator_name} is not reviewed",
                location=location,
            )


def _node_callable_map(tree: ast.Module, node_keys: Mapping[int, str]) -> dict[int, str]:
    result: dict[int, str] = {}
    stack: list[tuple[ast.AST, str | None]] = [(tree, None)]
    while stack:
        node, current = stack.pop()
        current = node_keys.get(id(node), current)
        if current is not None:
            result[id(node)] = current
        children = list(ast.iter_child_nodes(node))
        stack.extend((child, current) for child in reversed(children))
    return result


def _build_call_review(
    tree: ast.Module,
    *,
    closure_declarations: frozenset[str],
) -> _CallReview:
    approved = set(_REVIEWED_BUILTIN_CALLS)
    approved.update(closure_declarations)
    approved.update(_REVIEWED_HIGHER_ORDER_PARAMETERS)
    attributes = set(_REVIEWED_CALL_ATTRIBUTES)
    attributes.update(closure_declarations)

    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        for imported in node.names:
            qualified = f"{node.module}.{imported.name}"
            if qualified in _REVIEWED_IMPORTED_CALLS or imported.name in closure_declarations:
                approved.add(imported.asname or imported.name)

    def reviewed_value(value: ast.expr) -> bool:
        if isinstance(value, ast.Lambda):
            return True
        if isinstance(value, ast.Name):
            return value.id in approved
        if isinstance(value, ast.Attribute):
            return value.attr in attributes
        if isinstance(value, ast.IfExp):
            branches = (value.body, value.orelse)
            callable_branches = [
                branch
                for branch in branches
                if not (isinstance(branch, ast.Constant) and branch.value is None)
            ]
            return bool(callable_branches) and all(
                reviewed_value(branch) for branch in callable_branches
            )
        return False

    callable_sequences: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            value: ast.expr | None = None
            targets: Sequence[ast.expr] = ()
            if isinstance(node, ast.Assign):
                value = node.value
                targets = node.targets
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                value = node.value
                targets = (node.target,)
            if value is None:
                continue
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                if reviewed_value(value) and target.id not in approved:
                    approved.add(target.id)
                    changed = True
                if (
                    isinstance(value, ast.Tuple | ast.List | ast.Set)
                    and value.elts
                    and all(reviewed_value(item) for item in value.elts)
                    and target.id not in callable_sequences
                ):
                    callable_sequences.add(target.id)
                    changed = True
        for node in ast.walk(tree):
            if not isinstance(node, ast.For | ast.AsyncFor):
                continue
            if not isinstance(node.target, ast.Name):
                continue
            reviewed_iterable = (
                isinstance(node.iter, ast.Tuple | ast.List | ast.Set)
                and bool(node.iter.elts)
                and all(reviewed_value(item) for item in node.iter.elts)
            ) or (isinstance(node.iter, ast.Name) and node.iter.id in callable_sequences)
            if reviewed_iterable and node.target.id not in approved:
                approved.add(node.target.id)
                changed = True

    return _CallReview(
        approved_names=frozenset(approved),
        declared_attributes=frozenset(attributes),
    )


def _is_supported_call(node: ast.Call, review: _CallReview | None) -> bool:
    if review is None:
        return False
    if isinstance(node.func, ast.Name):
        return node.func.id in review.approved_names
    if isinstance(node.func, ast.Attribute):
        return node.func.attr in review.declared_attributes
    return False


def _normalized_call_target(function: ast.expr) -> str:
    return ast.dump(function, annotate_fields=True, include_attributes=False)


def _source_mapping(
    node: ast.AST,
    *,
    callable_key: str | None,
    reachable: set[str],
    unknown_callbacks: set[str],
    call_review: _CallReview | None = None,
) -> tuple[str, str, str]:
    if callable_key is not None and callable_key not in reachable:
        return (
            _MAPPING_UNREACHABLE,
            "machine-proven-unreachable",
            "static-call-graph-no-path",
        )
    if (
        callable_key in unknown_callbacks
        or type(node).__name__ not in _SUPPORTED_REACHABLE_AST_TYPES
        or (isinstance(node, ast.Call) and not _is_supported_call(node, call_review))
    ):
        return (_MAPPING_OFFICIAL, "reachable", "typed-unsupported-construct")
    return (_MAPPING_COMPILED, "reachable", "source-and-compiler-inventory")


def _add_decision_obligations(
    catalog: _Catalog,
    node: ast.AST,
    *,
    locator: str,
    structural_hashes: Mapping[int, str],
    context_sha256: str,
    mapping: tuple[str, str, str],
) -> None:
    tests: list[tuple[str, ast.AST]] = []
    if isinstance(node, ast.If | ast.IfExp | ast.Assert):
        tests.append(("condition", node.test))
    elif isinstance(node, ast.BoolOp):
        tests.append(("short-circuit", node))
    elif isinstance(node, ast.comprehension):
        tests.extend((f"filter-{index}", value) for index, value in enumerate(node.ifs))
    elif isinstance(node, ast.For):
        for outcome in ("iterates", "empty-or-complete"):
            catalog.add(
                kind="decision-outcome",
                owner="nfi-strategy",
                mapping=mapping[0],
                reachability=mapping[1],
                proof=mapping[2],
                subject=f"{locator}:loop:{outcome}",
                semantic_sha256=_sha256_json(
                    {
                        "context": context_sha256,
                        "loop": structural_hashes[id(node)],
                        "outcome": outcome,
                    }
                ),
            )
        return
    if not tests:
        return
    for label, test in tests:
        for outcome in ("false", "true"):
            catalog.add(
                kind="decision-outcome",
                owner="nfi-strategy",
                mapping=mapping[0],
                reachability=mapping[1],
                proof=mapping[2],
                subject=f"{locator}:{label}:{outcome}",
                semantic_sha256=_sha256_json(
                    {
                        "context": context_sha256,
                        "decision": structural_hashes[id(test)],
                        "label": label,
                        "outcome": outcome,
                    }
                ),
            )
        for index, term_hash in enumerate(_atomic_boolean_term_hashes(test, structural_hashes)):
            catalog.add(
                kind="mcdc-term",
                owner="nfi-strategy",
                mapping=mapping[0],
                reachability=mapping[1],
                proof=mapping[2],
                subject=f"{locator}:{label}:term-{index}",
                semantic_sha256=_sha256_json(
                    {
                        "context": context_sha256,
                        "decision": structural_hashes[id(test)],
                        "label": label,
                        "term": term_hash,
                    }
                ),
            )


def _atomic_boolean_term_hashes(
    node: ast.AST,
    structural_hashes: Mapping[int, str],
) -> list[str]:
    result: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, ast.BoolOp):
            stack.extend(reversed(current.values))
        elif isinstance(current, ast.UnaryOp) and isinstance(current.op, ast.Not):
            stack.append(current.operand)
        elif isinstance(current, ast.Compare) and len(current.ops) > 1:
            values = [current.left, *current.comparators]
            result.extend(
                _sha256_json(
                    {
                        "left": structural_hashes[id(left)],
                        "left_span": _ast_span_identity(left),
                        "operator": type(operator).__name__,
                        "right": structural_hashes[id(right)],
                        "right_span": _ast_span_identity(right),
                    }
                )
                for left, operator, right in zip(values[:-1], current.ops, values[1:], strict=True)
            )
        else:
            result.append(
                _sha256_json(
                    {
                        "term": structural_hashes[id(current)],
                        "source_span": _ast_span_identity(current),
                    }
                )
            )
    return result


def _add_threshold_obligations(
    catalog: _Catalog,
    node: ast.AST,
    *,
    locator: str,
    structural_hashes: Mapping[int, str],
    context_sha256: str,
    mapping: tuple[str, str, str],
) -> None:
    if isinstance(node, ast.Compare):
        values = [node.left, *node.comparators]
        for index, (left, operator, right) in enumerate(
            zip(values[:-1], node.ops, values[1:], strict=True)
        ):
            boundary = _numeric_literal(left)
            if boundary is None:
                boundary = _numeric_literal(right)
            semantic_hash = _sha256_json(
                {
                    "context": context_sha256,
                    "comparison": structural_hashes[id(node)],
                    "left": structural_hashes[id(left)],
                    "left_span": _ast_span_identity(left),
                    "operator": type(operator).__name__,
                    "right": structural_hashes[id(right)],
                    "right_span": _ast_span_identity(right),
                    "boundary": repr(boundary),
                }
            )
            for boundary_case in _comparison_boundary_cases(
                left,
                operator,
                right,
                numeric_boundary=boundary,
            ):
                catalog.add(
                    kind="threshold-boundary",
                    owner="nfi-strategy",
                    mapping=mapping[0],
                    reachability=mapping[1],
                    proof=mapping[2],
                    subject=f"{locator}:comparison-{index}:{boundary_case}",
                    semantic_sha256=_sha256_json(
                        {"threshold": semantic_hash, "case": boundary_case}
                    ),
                )
        return
    if not isinstance(node, ast.Call):
        return
    function_name = (
        node.func.id
        if isinstance(node.func, ast.Name)
        else node.func.attr
        if isinstance(node.func, ast.Attribute)
        else None
    )
    if function_name not in {"ceil", "floor", "int", "round", "trunc"}:
        return
    semantic_hash = _sha256_json(
        {
            "context": context_sha256,
            "rounding_call": structural_hashes[id(node)],
            "function": function_name,
        }
    )
    for boundary_case in ("just-below", "at-boundary-or-tie", "just-above"):
        catalog.add(
            kind="threshold-boundary",
            owner="nfi-strategy",
            mapping=mapping[0],
            reachability=mapping[1],
            proof=mapping[2],
            subject=f"{locator}:rounding:{function_name}:{boundary_case}",
            semantic_sha256=_sha256_json({"threshold": semantic_hash, "case": boundary_case}),
        )


def _comparison_boundary_cases(
    left: ast.AST,
    operator: ast.cmpop,
    right: ast.AST,
    *,
    numeric_boundary: int | float | None,
) -> tuple[str, ...]:
    if numeric_boundary is not None:
        return ("just-below", "at-equality", "just-above")
    constants = {item.value for item in (left, right) if isinstance(item, ast.Constant)}
    if None in constants:
        return ("none", "not-none")
    if isinstance(operator, ast.In | ast.NotIn):
        return ("contained", "not-contained")
    if isinstance(operator, ast.Eq | ast.NotEq | ast.Is | ast.IsNot):
        return ("equal-or-identical", "not-equal-or-identical")
    return ("just-below", "at-equality", "just-above")


def _numeric_literal(node: ast.AST) -> int | float | None:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
    ):
        return node.value
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub | ast.UAdd)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, int | float)
        and not isinstance(node.operand.value, bool)
    ):
        return -node.operand.value if isinstance(node.op, ast.USub) else node.operand.value
    return None


def _add_signal_tag_obligations(
    catalog: _Catalog,
    node: ast.AST,
    *,
    locator: str,
    structural_hash: str,
    context_sha256: str,
    mapping: tuple[str, str, str],
    callback_root: str | None,
) -> None:
    target: ast.AST | None = None
    if isinstance(node, ast.Assign) and node.targets:
        target = node.targets[0]
    elif isinstance(node, ast.AnnAssign | ast.AugAssign):
        target = node.target
    if target is not None:
        columns = set(_literal_strings(target))
        for column in sorted(columns & _SIGNAL_COLUMNS):
            for outcome in ("creation", "suppression"):
                catalog.add(
                    kind="signal",
                    owner="nfi-strategy",
                    mapping=mapping[0],
                    reachability=mapping[1],
                    proof=mapping[2],
                    subject=f"{locator}:{column}:{outcome}",
                    semantic_sha256=_sha256_json(
                        {
                            "context": context_sha256,
                            "assignment": structural_hash,
                            "column": column,
                            "outcome": outcome,
                        }
                    ),
                )
        for column in sorted(columns & _TAG_COLUMNS):
            for outcome in ("selection", "suppression"):
                catalog.add(
                    kind="tag",
                    owner="nfi-strategy",
                    mapping=mapping[0],
                    reachability=mapping[1],
                    proof=mapping[2],
                    subject=f"{locator}:{column}:{outcome}",
                    semantic_sha256=_sha256_json(
                        {
                            "context": context_sha256,
                            "assignment": structural_hash,
                            "column": column,
                            "outcome": outcome,
                        }
                    ),
                )
    if callback_root is not None and isinstance(node, ast.Return):
        for value in _literal_strings(node.value) if node.value is not None else []:
            for outcome in ("selection", "suppression"):
                catalog.add(
                    kind="tag",
                    owner="nfi-strategy",
                    mapping=mapping[0],
                    reachability=mapping[1],
                    proof=mapping[2],
                    subject=(f"{locator}:{callback_root}:return:{value}:{outcome}"),
                    semantic_sha256=_sha256_json(
                        {
                            "context": context_sha256,
                            "return": structural_hash,
                            "callback": callback_root,
                            "tag": value,
                            "outcome": outcome,
                        }
                    ),
                )


def _add_callback_obligations(
    catalog: _Catalog,
    node: ast.AST,
    *,
    callback_root: str,
    locator: str,
    structural_hash: str,
    context_sha256: str,
    mapping: tuple[str, str, str],
) -> None:
    if isinstance(node, ast.Return):
        action = _return_action(node)
        catalog.add(
            kind="callback-action",
            owner="nfi-strategy",
            mapping=mapping[0],
            reachability=mapping[1],
            proof=mapping[2],
            subject=f"{locator}:{callback_root}:{action}",
            semantic_sha256=_sha256_json(
                {
                    "context": context_sha256,
                    "return": structural_hash,
                    "callback": callback_root,
                    "action": action,
                }
            ),
        )
    if _is_state_mutation(node):
        catalog.add(
            kind="callback-state-mutation",
            owner="nfi-strategy",
            mapping=mapping[0],
            reachability=mapping[1],
            proof=mapping[2],
            subject=f"{locator}:{callback_root}",
            semantic_sha256=_sha256_json(
                {
                    "context": context_sha256,
                    "mutation": structural_hash,
                    "callback": callback_root,
                }
            ),
        )
    if isinstance(node, ast.Raise | ast.ExceptHandler):
        catalog.add(
            kind="callback-exception",
            owner="nfi-strategy",
            mapping=mapping[0],
            reachability=mapping[1],
            proof=mapping[2],
            subject=f"{locator}:{callback_root}:{type(node).__name__}",
            semantic_sha256=_sha256_json(
                {
                    "context": context_sha256,
                    "exception": structural_hash,
                    "callback": callback_root,
                    "node_type": type(node).__name__,
                }
            ),
        )


def _return_action(node: ast.Return) -> str:
    if node.value is None or (isinstance(node.value, ast.Constant) and node.value.value is None):
        return "no-action"
    if isinstance(node.value, ast.Tuple):
        return "position-adjustment"
    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
        return "tagged-exit"
    return "typed-return"


def _is_state_mutation(node: ast.AST) -> bool:
    target: ast.AST | None = None
    if isinstance(node, ast.Assign) and node.targets:
        target = node.targets[0]
    elif isinstance(node, ast.AnnAssign | ast.AugAssign):
        target = node.target
    if target is not None:
        root = target
        while isinstance(root, ast.Attribute | ast.Subscript):
            root = root.value
        if isinstance(root, ast.Name) and root.id in {"self", "trade", "order", "wallet"}:
            return True
    if isinstance(node, ast.Call):
        leaf = node.func.attr if isinstance(node.func, ast.Attribute) else None
        return leaf in _MUTATING_METHODS
    return False


def _add_state_machine_paths(
    catalog: _Catalog,
    callable_record: _Callable,
    relative_path: str,
    *,
    mapping: tuple[str, str, str],
    structural_hashes: Mapping[int, str],
) -> None:
    edges = _statement_edges(callable_record.node.body)
    edge_records: list[tuple[str, str, str, str]] = []
    for source, target, label in sorted(
        edges,
        key=lambda item: (
            _statement_key(item[0]),
            item[2],
            _statement_key(item[1]),
        ),
    ):
        source_key = _statement_key(source)
        target_key = _statement_key(target)
        source_statement = _source_statement(source)
        target_statement = _source_statement(target)
        semantic_hash = _sha256_json(
            {
                "callback": structural_hashes[id(callable_record.node)],
                "source": structural_hashes[id(source_statement)],
                "source_span": source_key,
                "target": structural_hashes[id(target_statement)],
                "target_span": target_key,
                "label": label,
            }
        )
        subject = f"{relative_path}:{callable_record.name}:{source_key}:{label}:{target_key}"
        catalog.add(
            kind="state-machine-edge",
            owner="nfi-strategy",
            mapping=mapping[0],
            reachability=mapping[1],
            proof=mapping[2],
            subject=subject,
            semantic_sha256=semantic_hash,
        )
        edge_records.append((source_key, target_key, label, semantic_hash))
    by_source: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
    for edge in edge_records:
        by_source[edge[0]].append(edge)
    for first in edge_records:
        for second in sorted(by_source.get(first[1], [])):
            catalog.add(
                kind="state-machine-two-edge-sequence",
                owner="nfi-strategy",
                mapping=mapping[0],
                reachability=mapping[1],
                proof=mapping[2],
                subject=(
                    f"{relative_path}:{callable_record.name}:{first[0]}:{first[2]}:"
                    f"{first[1]}:{second[2]}:{second[1]}"
                ),
                semantic_sha256=_sha256_json({"first": first[3], "second": second[3]}),
            )


@dataclass(frozen=True)
class _CfgNode:
    statement: ast.stmt
    context: tuple[str, ...]

    @property
    def lineno(self) -> int:
        identity = f"{_source_statement_key(self.statement)}|{'+'.join(self.context)}"
        return -(int(hashlib.sha256(identity.encode()).hexdigest()[:8], 16) + 1)


_CfgStatement = ast.stmt | _CfgNode


@dataclass(frozen=True)
class _CfgExit:
    node: _CfgStatement
    label: str


@dataclass
class _CfgFlow:
    entry: _CfgStatement
    normal: list[_CfgExit]
    breaks: list[_CfgExit]
    continues: list[_CfgExit]
    returns: list[_CfgExit]
    raises: list[_CfgExit]


def _statement_edges(
    nodes: Sequence[ast.stmt],
) -> set[tuple[_CfgStatement, _CfgStatement, str]]:
    """Build a continuation-sensitive reachable statement CFG for one callable."""
    edges: set[tuple[_CfgStatement, _CfgStatement, str]] = set()

    def cfg_node(statement: ast.stmt, context: tuple[str, ...]) -> _CfgStatement:
        return statement if not context else _CfgNode(statement, context)

    def empty_flow(entry: _CfgStatement) -> _CfgFlow:
        return _CfgFlow(entry, [], [], [], [], [])

    def relabel(exits: Sequence[_CfgExit], label: str) -> list[_CfgExit]:
        return [_CfgExit(item.node, label) for item in exits]

    def connect(exits: Sequence[_CfgExit], target: _CfgStatement) -> None:
        edges.update((item.node, target, item.label) for item in exits)

    def merge_abrupt(target: _CfgFlow, *sources: _CfgFlow) -> None:
        for source in sources:
            target.breaks.extend(source.breaks)
            target.continues.extend(source.continues)
            target.returns.extend(source.returns)
            target.raises.extend(source.raises)

    def build_block(
        statements: Sequence[ast.stmt],
        context: tuple[str, ...],
    ) -> _CfgFlow | None:
        if not statements:
            return None
        flow = build_statement(statements[0], context)
        for statement in statements[1:]:
            if not flow.normal:
                break
            following = build_statement(statement, context)
            connect(flow.normal, following.entry)
            combined = _CfgFlow(
                flow.entry,
                following.normal,
                list(flow.breaks),
                list(flow.continues),
                list(flow.returns),
                list(flow.raises),
            )
            merge_abrupt(combined, following)
            flow = combined
        return flow

    def build_if(statement: ast.If, context: tuple[str, ...]) -> _CfgFlow:
        entry = cfg_node(statement, context)
        flow = empty_flow(entry)
        body = build_block(statement.body, context)
        if body is None:
            flow.normal.append(_CfgExit(entry, "if-true"))
        else:
            edges.add((entry, body.entry, "if-true"))
            flow.normal.extend(relabel(body.normal, "if-join"))
            merge_abrupt(flow, body)
        alternative = build_block(statement.orelse, context)
        if alternative is None:
            flow.normal.append(_CfgExit(entry, "if-false"))
        else:
            edges.add((entry, alternative.entry, "if-false"))
            flow.normal.extend(relabel(alternative.normal, "if-join"))
            merge_abrupt(flow, alternative)
        return flow

    def build_loop(
        statement: ast.For | ast.AsyncFor | ast.While,
        context: tuple[str, ...],
    ) -> _CfgFlow:
        entry = cfg_node(statement, context)
        flow = empty_flow(entry)
        body = build_block(statement.body, context)
        if body is not None:
            edges.add((entry, body.entry, "loop-body"))
            connect(relabel(body.normal, "loop-back"), entry)
            connect(relabel(body.continues, "continue"), entry)
            flow.normal.extend(relabel(body.breaks, "break"))
            flow.returns.extend(body.returns)
            flow.raises.extend(body.raises)
        has_static_infinite_condition = (
            isinstance(statement, ast.While)
            and isinstance(statement.test, ast.Constant)
            and statement.test.value is True
        )
        alternative = build_block(statement.orelse, context)
        if alternative is not None and not has_static_infinite_condition:
            edges.add((entry, alternative.entry, "loop-exit"))
            flow.normal.extend(relabel(alternative.normal, "loop-else-join"))
            flow.normal.extend(relabel(alternative.breaks, "break"))
            flow.continues.extend(alternative.continues)
            flow.returns.extend(alternative.returns)
            flow.raises.extend(alternative.raises)
        elif not has_static_infinite_condition:
            flow.normal.append(_CfgExit(entry, "loop-exit"))
        return flow

    def build_try(
        statement: ast.Try | ast.TryStar,
        context: tuple[str, ...],
    ) -> _CfgFlow:
        entry = cfg_node(statement, context)
        flow = empty_flow(entry)
        body = build_block(statement.body, context)
        if body is not None:
            edges.add((entry, body.entry, "try-body"))

        handler_flows: list[_CfgFlow] = []
        for index, handler in enumerate(statement.handlers):
            handler_flow = build_block(handler.body, context)
            if handler_flow is not None:
                edges.add((entry, handler_flow.entry, f"except-{index}"))
                handler_flows.append(handler_flow)

        body_normal = [] if body is None else body.normal
        body_breaks = [] if body is None else body.breaks
        body_continues = [] if body is None else body.continues
        body_returns = [] if body is None else body.returns
        body_raises = [] if body is None else body.raises

        alternative = build_block(statement.orelse, context) if body_normal else None
        if alternative is not None:
            connect(relabel(body_normal, "try-else"), alternative.entry)
            body_normal = alternative.normal
            body_breaks = [*body_breaks, *alternative.breaks]
            body_continues = [*body_continues, *alternative.continues]
            body_returns = [*body_returns, *alternative.returns]
            body_raises = [*body_raises, *alternative.raises]

        normal = [*body_normal]
        breaks = [*body_breaks]
        continues = [*body_continues]
        returns = [*body_returns]
        raises = [*body_raises]
        for handler_flow in handler_flows:
            normal.extend(handler_flow.normal)
            breaks.extend(handler_flow.breaks)
            continues.extend(handler_flow.continues)
            returns.extend(handler_flow.returns)
            raises.extend(handler_flow.raises)

        if not statement.finalbody:
            flow.normal = relabel(normal, "try-join")
            flow.breaks = breaks
            flow.continues = continues
            flow.returns = returns
            flow.raises = raises
            return flow

        incoming = {
            "normal": normal,
            "break": breaks,
            "continue": continues,
            "return": returns,
            "raise": raises,
        }
        try_identity = _source_statement_key(statement).replace(":", "-")
        for kind, exits in incoming.items():
            if not exits:
                continue
            final_context = (*context, f"finally-{kind}-{try_identity}")
            final = build_block(statement.finalbody, final_context)
            if final is None:  # pragma: no cover - finalbody was checked above
                continue
            connect(relabel(exits, f"finally-{kind}"), final.entry)
            if kind == "normal":
                flow.normal.extend(relabel(final.normal, "finally-resume-normal"))
            elif kind == "break":
                flow.breaks.extend(relabel(final.normal, "break"))
            elif kind == "continue":
                flow.continues.extend(relabel(final.normal, "continue"))
            elif kind == "return":
                flow.returns.extend(relabel(final.normal, "return"))
            else:
                flow.raises.extend(relabel(final.normal, "raise"))
            merge_abrupt(flow, final)
        return flow

    def build_with(
        statement: ast.With | ast.AsyncWith,
        context: tuple[str, ...],
    ) -> _CfgFlow:
        entry = cfg_node(statement, context)
        flow = empty_flow(entry)
        body = build_block(statement.body, context)
        if body is None:
            flow.normal.append(_CfgExit(entry, "with-exit"))
            return flow
        edges.add((entry, body.entry, "with-body"))
        flow.normal = relabel(body.normal, "with-exit")
        merge_abrupt(flow, body)
        return flow

    def build_match(statement: ast.Match, context: tuple[str, ...]) -> _CfgFlow:
        entry = cfg_node(statement, context)
        flow = empty_flow(entry)
        exhaustive = False
        for index, case in enumerate(statement.cases):
            case_flow = build_block(case.body, context)
            if case_flow is not None:
                edges.add((entry, case_flow.entry, f"match-case-{index}"))
                flow.normal.extend(relabel(case_flow.normal, "match-join"))
                merge_abrupt(flow, case_flow)
            exhaustive = exhaustive or (
                case.guard is None
                and isinstance(case.pattern, ast.MatchAs)
                and case.pattern.pattern is None
                and case.pattern.name is None
            )
        if not exhaustive:
            flow.normal.append(_CfgExit(entry, "match-unmatched"))
        return flow

    def build_statement(
        statement: ast.stmt,
        context: tuple[str, ...],
    ) -> _CfgFlow:
        if isinstance(statement, ast.If):
            return build_if(statement, context)
        if isinstance(statement, ast.For | ast.AsyncFor | ast.While):
            return build_loop(statement, context)
        if isinstance(statement, ast.Try | ast.TryStar):
            return build_try(statement, context)
        if isinstance(statement, ast.With | ast.AsyncWith):
            return build_with(statement, context)
        if isinstance(statement, ast.Match):
            return build_match(statement, context)
        entry = cfg_node(statement, context)
        flow = empty_flow(entry)
        if isinstance(statement, ast.Break):
            flow.breaks.append(_CfgExit(entry, "break"))
        elif isinstance(statement, ast.Continue):
            flow.continues.append(_CfgExit(entry, "continue"))
        elif isinstance(statement, ast.Return):
            flow.returns.append(_CfgExit(entry, "return"))
        elif isinstance(statement, ast.Raise):
            flow.raises.append(_CfgExit(entry, "raise"))
        else:
            flow.normal.append(_CfgExit(entry, "next"))
        return flow

    build_block(nodes, ())
    return edges


def _source_statement(node: _CfgStatement) -> ast.stmt:
    return node.statement if isinstance(node, _CfgNode) else node


def _source_statement_key(node: ast.stmt) -> str:
    return f"{getattr(node, 'lineno', 1)}:{getattr(node, 'col_offset', 0)}:{type(node).__name__}"


def _statement_key(node: _CfgStatement) -> str:
    statement = _source_statement(node)
    key = _source_statement_key(statement)
    if isinstance(node, _CfgNode):
        key += f"@{'+'.join(node.context)}"
    return key


def _add_contract_obligations(
    catalog: _Catalog,
    freqtrade: Mapping[str, Any],
    contracts: Mapping[str, Mapping[str, Any]],
    strategy: Mapping[str, Any],
) -> None:
    source = freqtrade["source"]
    catalog.add(
        kind="freqtrade-source",
        owner="freqtrade",
        mapping=_MAPPING_GENERIC,
        reachability="reachable",
        proof="pinned-contract-source",
        subject=f"freqtrade-source:{source['commit']}",
        semantic_sha256=source["observed_method_merkle_root"],
    )
    profile = contracts["profile"]
    for method in profile["observer"]["observed_methods"]:
        catalog.add(
            kind="freqtrade-source",
            owner="freqtrade",
            mapping=_MAPPING_GENERIC,
            reachability="reachable",
            proof="pinned-contract-source",
            subject=f"{method['owner']}.{method['method']}",
            semantic_sha256=method["source_sha256"],
        )
    for row in callback_semantic_contract_rows(freqtrade):
        catalog.add(
            kind=_callback_contract_kind(row["interaction"]),
            owner="freqtrade",
            mapping=_MAPPING_GENERIC,
            reachability="reachable",
            proof="pinned-contract-source",
            subject=(
                f"callback-contract:{row['callback']}:{row['interaction']}:{row['boundary_row']}"
            ),
            semantic_sha256=_sha256_json(row),
        )
    for row in portfolio_semantic_obligation_rows(freqtrade, contracts["scheduler"]):
        catalog.add(
            kind=_portfolio_contract_kind(row["dimension"]),
            owner="freqtrade",
            mapping=_MAPPING_GENERIC,
            reachability="reachable",
            proof="pinned-contract-source",
            subject=f"portfolio-contract:{row['dimension']}:{row['boundary_row']}",
            semantic_sha256=_sha256_json(row),
        )
    for row in execution_semantic_obligation_rows(freqtrade, contracts["execution"]):
        catalog.add(
            kind=_execution_contract_kind(row["dimension"]),
            owner="freqtrade",
            mapping=_MAPPING_GENERIC,
            reachability="reachable",
            proof="pinned-contract-source",
            subject=f"execution-contract:{row['dimension']}:{row['boundary_row']}",
            semantic_sha256=_sha256_json(row),
        )
    _add_contract_two_edge_sequences(
        catalog,
        contracts["scheduler"],
        contract_name="scheduler",
        owner="freqtrade",
    )
    for path, value in _contract_leaves(contracts["scheduler"]):
        _add_contract_leaf(
            catalog,
            kind="scheduler-transition",
            owner="freqtrade",
            contract_name="scheduler",
            path=path,
            value=value,
        )
    _add_contract_two_edge_sequences(
        catalog,
        contracts["execution"],
        contract_name="execution",
        owner="freqtrade",
    )
    for path, value in _contract_leaves(contracts["execution"]):
        head = path.split(".", 1)[0].split("[", 1)[0]
        kind = "wallet-transition" if head == "wallet" else "order-transition"
        _add_contract_leaf(
            catalog,
            kind=kind,
            owner="freqtrade",
            contract_name="execution",
            path=path,
            value=value,
        )
    _add_contract_two_edge_sequences(
        catalog,
        contracts["futures"],
        contract_name="futures",
        owner="freqtrade-and-exchange",
    )
    for path, value in _contract_leaves(contracts["futures"]):
        kind = "protection" if path.startswith("protections") else "futures-path"
        _add_contract_leaf(
            catalog,
            kind=kind,
            owner="freqtrade-and-exchange",
            contract_name="futures",
            path=path,
            value=value,
        )
    protections = strategy.get("protections")
    if isinstance(protections, list):
        for index, protection in enumerate(protections):
            catalog.add(
                kind="protection",
                owner="nfi-strategy",
                mapping=_MAPPING_COMPILED,
                reachability="reachable",
                proof="source-and-compiler-inventory",
                subject=f"strategy.protections[{index}]",
                semantic_sha256=_sha256_json(protection),
            )
    elif strategy.get("protections_static") is False:
        obligation_id = catalog.add(
            kind="protection",
            owner="nfi-strategy",
            mapping=_MAPPING_OFFICIAL,
            reachability="reachable",
            proof="typed-unsupported-construct",
            subject="strategy.protections:dynamic",
            semantic_sha256=_sha256_json("dynamic-protection-property"),
        )
        catalog.block(
            code="UNKNOWN_DYNAMIC_PROTECTION",
            obligation_id=obligation_id,
            message="strategy protections cannot be resolved without executing Python",
            location=None,
        )


def _portfolio_contract_kind(dimension: str) -> str:
    if dimension.startswith("wallet-") or dimension in {
        "partial-exit-release",
        "compounding-base",
    }:
        return "wallet-transition"
    if dimension in {"trade-id", "order-id", "final-trades"}:
        return "order-transition"
    return "scheduler-transition"


def _execution_contract_kind(dimension: str) -> str:
    if dimension.startswith(("fee-", "stake-", "basis-", "profit-", "partial-", "min-stake")):
        return "wallet-transition"
    if dimension.startswith(("trade-id", "order-id", "fill-", "precision-")):
        return "order-transition"
    return "scheduler-transition"


def _callback_contract_kind(interaction: str) -> str:
    kinds = {
        "order": "scheduler-transition",
        "predicate": "decision-outcome",
        "return": "callback-action",
        "rollback": "callback-exception",
        "state-delta": "callback-state-mutation",
        "visibility": "callback-state-mutation",
    }
    try:
        return kinds[interaction]
    except KeyError:
        raise SpecValidationError(
            "CALLBACK_SEMANTIC_CONTRACT: unknown callback interaction"
        ) from None


def _validate_callback_contract_obligations(document: Mapping[str, Any]) -> None:
    expected = {
        (
            _callback_contract_kind(row["interaction"]),
            f"callback-contract:{row['callback']}:{row['interaction']}:{row['boundary_row']}",
            _sha256_json(row),
        )
        for row in callback_semantic_contract_rows(document["freqtrade"])
    }
    actual: list[tuple[str, str, str]] = []
    for group in document["obligation_groups"]:
        for record in group["obligations"]:
            subject, semantic_sha256 = record["preimage"]["normalized_semantics"]
            if subject.startswith("callback-contract:"):
                actual.append((group["kind"], subject, semantic_sha256))
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise SpecValidationError(
            "CALLBACK_SEMANTIC_CONTRACT: registry callback obligations differ"
        )


def _validate_portfolio_contract_obligations(
    document: Mapping[str, Any], scheduler_contract: Mapping[str, Any]
) -> None:
    actual: list[tuple[str, str, str]] = []
    for group in document["obligation_groups"]:
        for record in group["obligations"]:
            subject, semantic_sha256 = record["preimage"]["normalized_semantics"]
            if subject.startswith("portfolio-contract:"):
                actual.append((group["kind"], subject, semantic_sha256))
    # Registry v1 remains readable before its intentional additive migration.
    # Once any Todo 15 row is present, the matrix is closed and exhaustive.
    if not actual:
        return
    expected = {
        (
            _portfolio_contract_kind(row["dimension"]),
            f"portfolio-contract:{row['dimension']}:{row['boundary_row']}",
            _sha256_json(row),
        )
        for row in portfolio_semantic_obligation_rows(document["freqtrade"], scheduler_contract)
    }
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise SpecValidationError("PORTFOLIO_SEMANTIC_REGISTRY: registry obligations differ")


def _validate_execution_contract_obligations(
    document: Mapping[str, Any], execution_contract: Mapping[str, Any]
) -> None:
    actual: list[tuple[str, str, str]] = []
    for group in document["obligation_groups"]:
        for record in group["obligations"]:
            subject, semantic_sha256 = record["preimage"]["normalized_semantics"]
            if subject.startswith("execution-contract:"):
                actual.append((group["kind"], subject, semantic_sha256))
    # Registry v1 remains readable before Todo 16's additive migration.
    if not actual:
        return
    expected = {
        (
            _execution_contract_kind(row["dimension"]),
            f"execution-contract:{row['dimension']}:{row['boundary_row']}",
            _sha256_json(row),
        )
        for row in execution_semantic_obligation_rows(document["freqtrade"], execution_contract)
    }
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise SpecValidationError("EXECUTION_SEMANTIC_REGISTRY: registry obligations differ")


def _add_contract_two_edge_sequences(
    catalog: _Catalog,
    value: Any,
    *,
    contract_name: str,
    owner: str,
    path: str = "",
) -> None:
    if isinstance(value, Mapping):
        for key in sorted(value):
            if key == "fingerprint":
                continue
            child = f"{path}.{key}" if path else str(key)
            _add_contract_two_edge_sequences(
                catalog,
                value[key],
                contract_name=contract_name,
                owner=owner,
                path=child,
            )
        return
    if not isinstance(value, list):
        return
    for index, (first, middle, last) in enumerate(zip(value, value[1:], value[2:], strict=False)):
        catalog.add(
            kind="state-machine-two-edge-sequence",
            owner=owner,
            mapping=_MAPPING_GENERIC,
            reachability="reachable",
            proof="pinned-contract-source",
            subject=f"{contract_name}:{path}:two-edge-{index}",
            semantic_sha256=_sha256_json({"from": first, "through": middle, "to": last}),
        )
    for index, item in enumerate(value):
        _add_contract_two_edge_sequences(
            catalog,
            item,
            contract_name=contract_name,
            owner=owner,
            path=f"{path}[{index}]",
        )


def _contract_leaves(value: Any, path: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key in sorted(value):
            if key == "fingerprint":
                continue
            child = f"{path}.{key}" if path else str(key)
            yield from _contract_leaves(value[key], child)
    elif isinstance(value, list):
        if not value:
            yield path, []
        for index, item in enumerate(value):
            yield from _contract_leaves(item, f"{path}[{index}]")
    else:
        yield path, value


def _add_contract_leaf(
    catalog: _Catalog,
    *,
    kind: str,
    owner: str,
    contract_name: str,
    path: str,
    value: Any,
) -> None:
    catalog.add(
        kind=kind,
        owner=owner,
        mapping=_MAPPING_GENERIC,
        reachability="reachable",
        proof="pinned-contract-source",
        subject=f"{contract_name}:{path}",
        semantic_sha256=_sha256_json(
            {
                "contract": contract_name,
                "semantic_path": re.sub(r"\\[\\d+\\]", "[]", path),
                "value": value,
            }
        ),
    )


def _add_runtime_inventory_obligations(
    catalog: _Catalog,
    runtime_inventory: Mapping[str, Any],
) -> None:
    for method in runtime_inventory.get("vector_methods", []):
        catalog.add(
            kind="ir-node",
            owner="nfi-strategy",
            mapping=_MAPPING_COMPILED,
            reachability="reachable",
            proof="source-and-compiler-inventory",
            subject=f"vector-method:{method['name']}",
            semantic_sha256=method["source_sha256"],
        )
    for callback in runtime_inventory.get("callbacks", []):
        boundary = callback.get("native_boundary")
        mapping = (
            _MAPPING_COMPILED
            if boundary in {"generic-rust-ir", "source-bound-rust-adapter"}
            else _MAPPING_OFFICIAL
        )
        catalog.add(
            kind="ir-node",
            owner="nfi-strategy",
            mapping=mapping,
            reachability="reachable",
            proof=(
                "source-and-compiler-inventory"
                if mapping == _MAPPING_COMPILED
                else "typed-unsupported-construct"
            ),
            subject=f"callback-ir:{callback['name']}:{callback.get('backend', 'unknown')}",
            semantic_sha256=callback["source_sha256"],
        )
    for route in runtime_inventory.get("routes", []):
        route_hash = _sha256_json(route)
        for outcome in ("creation", "suppression"):
            catalog.add(
                kind="signal",
                owner="nfi-strategy",
                mapping=_MAPPING_COMPILED,
                reachability="reachable",
                proof="source-and-compiler-inventory",
                subject=f"route:{route['side']}:{route['key']}:{outcome}",
                semantic_sha256=_sha256_json({"route": route_hash, "outcome": outcome}),
            )
        for index, tag in enumerate(route.get("entry_tags", [])):
            for outcome in ("selection", "suppression"):
                catalog.add(
                    kind="tag",
                    owner="nfi-strategy",
                    mapping=_MAPPING_COMPILED,
                    reachability="reachable",
                    proof="source-and-compiler-inventory",
                    subject=(f"route:{route['side']}:{route['key']}:tag-{index}:{tag}:{outcome}"),
                    semantic_sha256=_sha256_json(
                        {
                            "route": route_hash,
                            "tag": tag,
                            "outcome": outcome,
                        }
                    ),
                )
    for index, error in enumerate(runtime_inventory.get("compilation_errors", [])):
        obligation_id = catalog.add(
            kind="ir-node",
            owner="nfi-strategy",
            mapping=_MAPPING_OFFICIAL,
            reachability="reachable",
            proof="typed-unsupported-construct",
            subject=f"lowering-error:{index}:{error['code']}",
            semantic_sha256=_sha256_json(error),
        )
        catalog.block(
            code=str(error["code"]),
            obligation_id=obligation_id,
            message=str(error["message"]),
            location=None,
        )


def _add_closure_blockers(catalog: _Catalog, source_files: Sequence[_SourceFile]) -> None:
    for source_file in source_files:
        for missing in source_file.missing_local_imports:
            obligation_id = catalog.add(
                kind="source-closure",
                owner="nfi-strategy",
                mapping=_MAPPING_OFFICIAL,
                reachability="reachable",
                proof="typed-unsupported-construct",
                subject=f"{source_file.relative_path}:missing:{missing}",
                semantic_sha256=_sha256_json(missing),
            )
            catalog.block(
                code="MISSING_TRANSITIVE_LOCAL_SOURCE",
                obligation_id=obligation_id,
                message=f"relative local import {missing!r} cannot be resolved",
                location=None,
            )
        for imported_root in source_file.external_imports:
            if imported_root in _KNOWN_EXTERNAL_IMPORT_ROOTS:
                continue
            obligation_id = catalog.add(
                kind="source-closure",
                owner="nfi-strategy",
                mapping=_MAPPING_OFFICIAL,
                reachability="reachable",
                proof="typed-unsupported-construct",
                subject=f"{source_file.relative_path}:external:{imported_root}",
                semantic_sha256=_sha256_json(imported_root),
            )
            catalog.block(
                code="UNKNOWN_EXTERNAL_IMPORT",
                obligation_id=obligation_id,
                message=f"external import root {imported_root!r} is not reviewed",
                location=None,
            )


def _add_upstream_ref_obligation(
    catalog: _Catalog,
    *,
    repository: str | None,
    configured_commit: str | None,
    ref: str | None,
    source_path: str,
    observation: Mapping[str, str] | None,
) -> dict[str, Any]:
    if ref is None:
        observation_method = (
            "offline-unverified-commit-v1"
            if configured_commit is not None
            else "offline-unverified-source-v1"
            if repository is not None
            else "unconfigured-local-source-v1"
        )
        identity = {
            "repository": repository,
            "ref": None,
            "configured_commit": configured_commit,
            "observed_commit": None,
            "observed_commit_timestamp": None,
            "source_path": source_path,
            "observation_method": observation_method,
        }
        code = (
            "UNOBSERVED_UPSTREAM_COMMIT"
            if configured_commit is not None
            else "UNOBSERVED_UPSTREAM_REF"
        )
        obligation_id = catalog.add(
            kind="source-closure",
            owner="nfi-strategy",
            mapping=_MAPPING_OFFICIAL,
            reachability="reachable",
            proof="typed-unsupported-construct",
            subject=(f"unobserved-upstream-ref:{repository}:{configured_commit}:{source_path}"),
            semantic_sha256=_sha256_json(identity),
        )
        catalog.block(
            code=code,
            obligation_id=obligation_id,
            message=(
                "upstream source identity was not independently observed through "
                "a configured ref; local and offline analysis is audit-only"
            ),
            location=None,
        )
        return identity

    observed_commit = observation.get("observed_commit") if observation else None
    observed_timestamp = observation.get("observed_commit_timestamp") if observation else None
    observation_method = (
        observation.get("observation_method") if observation else None
    ) or "unresolved-upstream-ref-v1"
    observation_status = (
        observation.get("observation_status") if observation else None
    ) or "unresolved"
    failure_contract = _UPSTREAM_FAILURE_CONTRACT.get((observation_method, observation_status))
    if observed_commit is None and failure_contract is None:
        observation_method = "unresolved-upstream-ref-v1"
        observation_status = "unresolved"
        failure_contract = _UPSTREAM_FAILURE_CONTRACT[(observation_method, observation_status)]
    pinned_commit = configured_commit or observed_commit
    stale = (
        configured_commit is not None
        and observed_commit is not None
        and configured_commit != observed_commit
    )
    unresolved = observed_commit is None
    mapping = _MAPPING_OFFICIAL if stale or unresolved else _MAPPING_GENERIC
    proof = (
        "typed-unsupported-construct" if stale or unresolved else "source-and-compiler-inventory"
    )
    semantic_identity = {
        "repository": repository,
        "ref": ref,
        "configured_commit": pinned_commit,
        "observed_commit": observed_commit,
        "observed_commit_timestamp": observed_timestamp,
        "source_path": source_path,
        "observation_method": observation_method,
    }
    if unresolved:
        semantic_identity["observation_status"] = observation_status
    semantic_hash_identity = (
        {
            "ref": ref,
            "configured_commit": pinned_commit,
            "observation_method": observation_method,
            "observation_status": observation_status,
            "blocker_code": failure_contract[0] if failure_contract else None,
        }
        if unresolved
        else semantic_identity
    )
    subject = (
        f"upstream-ref-failure:{ref}:{pinned_commit}:{observation_method}:"
        f"{observation_status}:{failure_contract[0] if failure_contract else ''}"
        if unresolved
        else (f"upstream-ref:{repository}:{ref}:{pinned_commit}:{observed_commit}:{source_path}")
    )
    obligation_id = catalog.add(
        kind="source-closure",
        owner="nfi-strategy",
        mapping=mapping,
        reachability="reachable",
        proof=proof,
        subject=subject,
        semantic_sha256=_sha256_json(semantic_hash_identity),
    )
    if stale:
        catalog.block(
            code="STALE_UPSTREAM_REF",
            obligation_id=obligation_id,
            message=(
                f"configured upstream commit {configured_commit} differs from "
                f"observed {ref} commit {observed_commit}"
            ),
            location=None,
        )
    elif unresolved and failure_contract is not None:
        catalog.block(
            code=failure_contract[0],
            obligation_id=obligation_id,
            message=failure_contract[1],
            location=None,
        )
    return semantic_identity


def _registry_summary(
    groups: Sequence[Mapping[str, Any]],
    blockers: Sequence[Mapping[str, Any]],
    *,
    closure_complete: bool,
) -> dict[str, Any]:
    total = sum(len(group["obligations"]) for group in groups)
    mapping_counts: dict[str, int] = defaultdict(int)
    kind_counts: dict[str, int] = defaultdict(int)
    active = 0
    unreachable = 0
    for group in groups:
        count = len(group["obligations"])
        mapping_counts[str(group["mapping"])] += count
        kind_counts[str(group["kind"])] += count
        if group["reachability"] == "reachable":
            active += count
        else:
            unreachable += count
    unknown = len(blockers)
    active_accounted = active == (
        sum(
            len(group["obligations"])
            for group in groups
            if group["reachability"] == "reachable"
            and group["mapping"] in {_MAPPING_GENERIC, _MAPPING_COMPILED, _MAPPING_OFFICIAL}
        )
    )
    return {
        "total_obligations": total,
        "mapped_obligations": total,
        "active_obligations": active,
        "statically_witnessed_obligations": active,
        "machine_proven_unreachable_obligations": unreachable,
        "generic_runtime_obligations": mapping_counts[_MAPPING_GENERIC],
        "compiled_program_obligations": mapping_counts[_MAPPING_COMPILED],
        "official_only_blocker_obligations": mapping_counts[_MAPPING_OFFICIAL],
        "unknown_obligations": unknown,
        "source_node_obligations": (kind_counts["ast-node"] + kind_counts["call"]),
        "ast_node_obligations": kind_counts["ast-node"],
        "operator_obligations": kind_counts["operator"],
        "call_obligations": kind_counts["call"],
        "callable_obligations": kind_counts["callable"],
        "ir_node_obligations": kind_counts["ir-node"],
        "decision_outcomes": kind_counts["decision-outcome"],
        "mcdc_terms": kind_counts["mcdc-term"],
        "threshold_boundaries": kind_counts["threshold-boundary"],
        "signal_obligations": kind_counts["signal"],
        "tag_obligations": kind_counts["tag"],
        "callback_action_obligations": kind_counts["callback-action"],
        "callback_state_mutation_obligations": kind_counts["callback-state-mutation"],
        "callback_exception_obligations": kind_counts["callback-exception"],
        "state_machine_edges": kind_counts["state-machine-edge"],
        "state_machine_two_edge_sequences": kind_counts["state-machine-two-edge-sequence"],
        "scheduler_transitions": kind_counts["scheduler-transition"],
        "wallet_transitions": kind_counts["wallet-transition"],
        "order_transitions": kind_counts["order-transition"],
        "futures_paths": kind_counts["futures-path"],
        "protection_obligations": kind_counts["protection"],
        "historical_non_observation_exclusions": 0,
        "duplicate_obligation_ids": 0,
        "every_obligation_mapped_once": True,
        "all_obligations_accounted": total == active + unreachable,
        "active_obligations_accounted": active_accounted,
        "source_closure_complete": closure_complete,
        "native_promotion": not blockers and closure_complete and active_accounted,
    }


def _structural_hashes(tree: ast.AST) -> dict[int, str]:
    hashes: dict[int, str] = {}
    stack: list[tuple[ast.AST, bool]] = [(tree, False)]
    while stack:
        node, visited = stack.pop()
        identity = id(node)
        if identity in hashes:
            continue
        if not visited:
            stack.append((node, True))
            stack.extend(
                (child, False)
                for child in reversed(list(ast.iter_child_nodes(node)))
                if id(child) not in hashes
            )
            continue
        fields: list[Any] = []
        for name, value in ast.iter_fields(node):
            if isinstance(value, ast.AST):
                rendered: Any = ["ast", hashes[id(value)]]
            elif isinstance(value, list):
                rendered = [
                    ["ast", hashes[id(item)]]
                    if isinstance(item, ast.AST)
                    else _primitive_identity(item)
                    for item in value
                ]
            else:
                rendered = _primitive_identity(value)
            fields.append([name, rendered])
        hashes[identity] = _sha256_json({"node_type": type(node).__name__, "fields": fields})
    return hashes


def _primitive_identity(value: Any) -> list[str]:
    return [type(value).__name__, repr(value)]


def _literal_strings(node: ast.AST | None) -> list[str]:
    if node is None:
        return []
    return [
        item.value
        for item in ast.walk(node)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    ]


def _source_node_semantic_hash(
    *,
    source_content_hash: str,
    context_hash: str,
    node: ast.AST,
    structural_hash: str,
    parent_nodes: Mapping[int, ast.AST],
) -> str:
    return _sha256_json(
        {
            "source_sha256": source_content_hash,
            "context": context_hash,
            "node": structural_hash,
            "source_span": _semantic_span_identity(node, parent_nodes),
        }
    )


def _semantic_span_identity(
    node: ast.AST,
    parent_nodes: Mapping[int, ast.AST],
) -> dict[str, int | bool]:
    current = node
    inherited = False
    while not hasattr(current, "lineno") and id(current) in parent_nodes:
        current = parent_nodes[id(current)]
        inherited = True
    return {**_ast_span_identity(current), "inherited_from_parent": inherited}


def _ast_span_identity(node: ast.AST) -> dict[str, int]:
    return {
        "line": getattr(node, "lineno", 1),
        "column": getattr(node, "col_offset", 0),
        "end_line": getattr(node, "end_lineno", getattr(node, "lineno", 1)),
        "end_column": getattr(
            node,
            "end_col_offset",
            getattr(node, "col_offset", 0),
        ),
    }


def _source_location(
    relative_path: str,
    node: ast.AST,
    *,
    parent_nodes: Mapping[int, ast.AST] | None = None,
) -> dict[str, Any]:
    if parent_nodes is None:
        span = _ast_span_identity(node)
    else:
        inherited_span = _semantic_span_identity(node, parent_nodes)
        span = {
            key: value for key, value in inherited_span.items() if key != "inherited_from_parent"
        }
    return {"path": relative_path, **span}


def _node_locator(location: Mapping[str, Any], node_type: str) -> str:
    return (
        f"{location['path']}:{location['line']}:{location['column']}:"
        f"{location['end_line']}:{location['end_column']}:{node_type}"
    )


def _has_property_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(decorator, ast.Name) and decorator.id in {"property", "cached_property"}
        for decorator in node.decorator_list
    )


def _looks_like_unknown_callback(name: str) -> bool:
    return (
        "callback" in name
        or name.startswith(("adjust_trade_", "bot_", "check_trade_", "confirm_trade_"))
        or name.startswith("custom_")
        or name.endswith("_callback")
    )


def _sha256_json(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _semantic_closure_identity(closure: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "algorithm": closure["algorithm"],
        "root_path": closure["root_path"],
        "complete": closure["complete"],
        "file_count": closure["file_count"],
        "files": sorted(
            (
                {
                    "role": item["role"],
                    "bytes": item["bytes"],
                    "sha256": item["sha256"],
                }
                for item in closure["files"]
            ),
            key=lambda item: (item["role"], item["sha256"], item["bytes"]),
        ),
        "external_imports": closure["external_imports"],
        "missing_local_imports": sorted(
            item.split(":", 1)[-1] for item in closure["missing_local_imports"]
        ),
        "merkle_root": closure["merkle_root"],
    }


def _registry_fingerprint(report: Mapping[str, Any]) -> str:
    identity = {key: value for key, value in report.items() if key != "fingerprint"}
    strategy = dict(report["strategy"])
    strategy_source = dict(strategy["source"])
    strategy_source.pop("path", None)
    strategy["source"] = strategy_source
    identity["strategy"] = strategy

    identity["source_closure"] = _semantic_closure_identity(report["source_closure"])
    identity["blockers"] = sorted(
        ({**item, "source": None} for item in report["blockers"]),
        key=lambda item: (item["code"], item["obligation_id"]),
    )
    return _sha256_json(identity)
