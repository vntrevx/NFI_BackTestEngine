"""Executable source mutants for the current changed Signal 562 predicate."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, assert_never

from . import _rust
from .errors import SpecValidationError
from .signal_program import compile_signal_program

_COLUMNS: Final = ("RSI_3_15m", "RSI_3_1h", "RSI_3_4h", "AROONU_14_1h")
_THRESHOLDS: Final = ("15.0", "20.0", "25.0", "0.0")
_REPLACEMENT_THRESHOLDS: Final = (
    "15.000000000000002",
    "20.000000000000004",
    "25.000000000000004",
    "5e-324",
)
MutationKind = Literal["comparator", "threshold", "source"]


@dataclass(frozen=True, slots=True)
class SourceMutant:
    """One uniquely identified executable predicate source mutation."""

    identifier: str
    kind: MutationKind
    term_index: int
    source: str


def source_mutants(source: str) -> tuple[SourceMutant, ...]:
    """Return every comparator, threshold, and source-term mutant."""
    mutants: list[SourceMutant] = []
    for index, (column, threshold) in enumerate(zip(_COLUMNS, _THRESHOLDS, strict=True)):
        needle = f'dataframe["{column}"] > {threshold}'
        comparator = source.replace(needle, needle.replace(">", ">="), 1)
        threshold_source = source.replace(
            needle,
            needle.replace(threshold, _REPLACEMENT_THRESHOLDS[index]),
            1,
        )
        replacement_column = _COLUMNS[(index + 1) % len(_COLUMNS)]
        source_column = source.replace(
            needle,
            needle.replace(column, replacement_column),
            1,
        )
        mutants.extend(
            (
                SourceMutant(f"comparator-{index}", "comparator", index, comparator),
                SourceMutant(f"threshold-{index}", "threshold", index, threshold_source),
                SourceMutant(f"source-{index}", "source", index, source_column),
            )
        )
    if any(mutant.source == source for mutant in mutants):
        raise SpecValidationError("changed signal mutant did not alter source")
    return tuple(mutants)


def execute_native_mutant_snapshot(
    source: bytes,
    mode: Literal["spot", "futures"],
    columns: dict[str, list[float | None]],
) -> list[int]:
    """Compile Native from one private immutable authenticated source snapshot."""
    with tempfile.TemporaryDirectory(prefix="task8-r7-native-mutant-") as name:
        source_path = Path(name) / "CurrentChangedPredicateContract.py"
        source_path.write_bytes(source)
        source_path.chmod(0o444)
        return execute_native_mutant(source_path, mode, columns)


def execute_native_mutant(
    source_path: Path,
    mode: Literal["spot", "futures"],
    columns: dict[str, list[float | None]],
) -> list[int]:
    """Compile and execute one mutant through the shipped Rust runtime."""
    program = compile_signal_program(
        source_path,
        class_name="CurrentChangedPredicateContract",
        trading_mode=mode,
        config={"trading_mode": mode},
    )
    output = _rust.execute_numeric_mutation_program(
        json.dumps(program, separators=(",", ":")),
        columns,
        {},
        ["enter_short"],
    )
    values: list[int] = []
    for value in output["enter_short"]["values"]:
        match value:
            case bool():
                raise SpecValidationError("mutant enter_short output is Boolean")
            case int():
                values.append(value)
            case float() | str() | None:
                raise SpecValidationError("mutant enter_short output is not integer")
            case unreachable:
                assert_never(unreachable)
    return values
