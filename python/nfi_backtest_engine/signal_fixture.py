"""Generate deterministic signal evidence from pinned Freqtrade wrappers."""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pandas as pd

PINNED_SOURCE = Path(".nfi/roadmap-acceptance/M20-05/freqtrade-2026.5.1")
CONTRACT_PATH = Path("benchmarks/reference/strategies/SignalProgramContract.py")
FIXTURE_PATH = Path("benchmarks/reference/signals/freqtrade-2026.5.1.json")
_METHODS = ("advise_entry", "advise_exit")


def generate_fixture(source_root: Path | None = None) -> dict[str, object]:
    """Execute the exact pinned wrapper AST around the committed strategy contract."""
    repository = _repository_root()
    root = source_root or repository / PINNED_SOURCE
    interface = root / "freqtrade/strategy/interface.py"
    contract = repository / CONTRACT_PATH
    strategy, method_hashes = _load_strategy(interface, contract)
    input_frame = _input_frame()
    entry = strategy.advise_entry(input_frame.copy(deep=True), {"pair": "ETH/USDT"})
    output = strategy.advise_exit(entry, {"pair": "ETH/USDT"})
    fixture: dict[str, object] = {
        "schema_version": "freqtrade-signal-fixture-v1",
        "source": {
            "version": _source_version(root),
            "commit": _source_commit(root),
            "interface": "freqtrade/strategy/interface.py",
            "interface_sha256": _sha256_file(interface),
            "method_sha256": method_hashes,
            "strategy": str(CONTRACT_PATH),
            "strategy_sha256": _sha256_file(contract),
            "pandas": pd.__version__,
        },
        "call_order": ["advise_entry", "advise_exit"],
        "input": _encode_frame(input_frame),
        "output": _encode_frame(output),
    }
    fixture["fingerprint"] = canonical_sha256(fixture)
    return fixture


def write_fixture(destination: Path, source_root: Path | None = None) -> dict[str, object]:
    """Generate and persist canonical fixture evidence."""
    fixture = generate_fixture(source_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(_canonical_json(fixture) + "\n", encoding="utf-8")
    return fixture


def canonical_sha256(document: Mapping[str, object]) -> str:
    """Hash fixture contents without its self-referential fingerprint."""
    identity = {key: value for key, value in document.items() if key != "fingerprint"}
    return hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()


def decode_frame(document: Mapping[str, object]) -> pd.DataFrame:
    """Decode the compact fixture frame with its nullable dtypes."""
    raw_columns = document["columns"]
    if not isinstance(raw_columns, list):
        raise TypeError("signal fixture columns must be a list")
    columns = [str(value) for value in raw_columns]
    rows = document["rows"]
    if not isinstance(rows, list):
        raise TypeError("signal fixture rows must be a list")
    frame = pd.DataFrame(rows, columns=columns)
    dtypes = document["dtypes"]
    if not isinstance(dtypes, Mapping):
        raise TypeError("signal fixture dtypes must be an object")
    for column in columns:
        dtype = dtypes[column]
        series = cast(pd.Series, frame[column])
        if dtype == "boolean":
            frame[column] = pd.array(series.tolist(), dtype="boolean")
        elif dtype == "float64":
            numeric = cast(pd.Series, pd.to_numeric(series, errors="raise"))
            frame[column] = numeric.astype("float64")
        elif dtype == "int64":
            numeric = cast(pd.Series, pd.to_numeric(series, errors="raise"))
            frame[column] = numeric.astype("int64")
    return frame


def encode_signal_columns(frame: pd.DataFrame) -> dict[str, object]:
    """Encode only M21-01's four raw signal outputs."""
    columns = ["enter_long", "enter_short", "exit_long", "exit_short"]
    return _encode_frame(frame.loc[:, columns])


def _load_strategy(interface: Path, contract: Path) -> tuple[Any, dict[str, str]]:
    interface_source = interface.read_text(encoding="utf-8")
    interface_tree = ast.parse(interface_source, filename=str(interface))
    interface_class = next(
        node
        for node in interface_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "IStrategy"
    )
    wrapper_methods = [
        node
        for node in interface_class.body
        if isinstance(node, ast.FunctionDef) and node.name in _METHODS
    ]
    if [node.name for node in wrapper_methods] != list(_METHODS):
        raise RuntimeError("pinned Freqtrade signal wrappers changed")

    contract_source = contract.read_text(encoding="utf-8")
    contract_tree = ast.parse(contract_source, filename=str(contract))
    contract_class = next(node for node in contract_tree.body if isinstance(node, ast.ClassDef))
    populate_methods = [
        node
        for node in contract_class.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"populate_entry_trend", "populate_exit_trend"}
    ]
    if sorted(node.name for node in populate_methods) != sorted(
        ("populate_entry_trend", "populate_exit_trend")
    ):
        raise RuntimeError("signal fixture strategy contract is incomplete")
    method_hashes = {
        node.name: hashlib.sha256(
            (ast.get_source_segment(interface_source, node) or "").encode("utf-8")
        ).hexdigest()
        for node in wrapper_methods
    }
    for node in [*wrapper_methods, *populate_methods]:
        node.decorator_list = []
        node.returns = None
        for argument in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
            argument.annotation = None
    generated = ast.ClassDef(
        name="PinnedSignalStrategy",
        bases=[],
        keywords=[],
        body=[*wrapper_methods, *populate_methods],
        decorator_list=[],
        type_params=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[generated], type_ignores=[]))
    namespace: dict[str, object] = {
        "logger": SimpleNamespace(debug=lambda *args, **kwargs: None),
    }
    exec(compile(module, str(interface), "exec"), namespace)  # noqa: S102 - pinned source oracle
    strategy_class = namespace["PinnedSignalStrategy"]
    if not isinstance(strategy_class, type):
        raise TypeError("pinned signal strategy did not compile")
    return strategy_class(), method_hashes


def _input_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "score": [-2.0, -0.5, 0.5, 1.5, 2.0, 2.5, np.nan, 0.0],
            "exit_mask": pd.array(
                [pd.NA, False, True, True, False, True, False, pd.NA],
                dtype="boolean",
            ),
        }
    )


def _encode_frame(frame: pd.DataFrame) -> dict[str, object]:
    columns = [str(column) for column in frame.columns]
    return {
        "columns": columns,
        "dtypes": {column: str(frame[column].dtype) for column in columns},
        "rows": [[_json_value(value) for value in row] for row in frame.itertuples(index=False)],
    }


def _json_value(value: object) -> object:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and np.isnan(value):
        return None
    if isinstance(value, bool | int | float | str):
        return value
    raise TypeError(f"signal fixture value is not JSON-safe: {type(value).__name__}")


def _source_version(root: Path) -> str:
    version_file = root / "freqtrade/__init__.py"
    tree = ast.parse(version_file.read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    raise RuntimeError("pinned Freqtrade version is missing")


def _source_commit(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_json(document: Mapping[str, object]) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]
