from pathlib import Path
from typing import TypedDict

type NumericColumn = list[float | None]
type InformativeFrame = tuple[
    str,
    str,
    list[int],
    dict[str, NumericColumn],
]

class NativeTypedColumn(TypedDict):
    value_type: str
    values: list[float | int | bool | str | None]

class FullVectorOutput(TypedDict):
    pair: str
    timeframe: str
    timestamps_ms: list[int]
    execution_start_index: int
    columns: dict[str, NativeTypedColumn]
    enabled_indexes: dict[str, list[int]]

def schema_version() -> str: ...
def simulator_available() -> bool: ...
def source_fingerprint() -> str: ...
def scheduler_contract_json() -> str: ...
def execution_contract_json() -> str: ...
def futures_contract_json() -> str: ...
def simulate_json(input: str) -> str: ...
def recover_result_publication(
    output_path: str | Path,
    profile_path: str | Path | None = ...,
    events_path: str | Path | None = ...,
) -> bool: ...
def validate_result_publication(
    output_path: str | Path,
    profile_path: str | Path | None = ...,
    events_path: str | Path | None = ...,
) -> None: ...
def simulate_file(
    input_path: str | Path,
    output_path: str | Path,
    events_path: str | Path | None = ...,
    execution_events: bool = ...,
) -> None: ...
def simulate_vector_file(
    manifest_path: str | Path,
    output_path: str | Path,
    events_path: str | Path | None = ...,
    execution_events: bool = ...,
) -> None: ...
def simulate_vector_file_profiled(
    manifest_path: str | Path,
    output_path: str | Path,
    profile_path: str | Path,
    events_path: str | Path | None = ...,
    execution_events: bool = ...,
) -> None: ...
def simulate_full_vector_file(
    manifest_path: str | Path,
    output_path: str | Path,
    events_path: str | Path | None = ...,
    pair_worker_limit: int | None = ...,
    execution_events: bool = ...,
) -> None: ...
def simulate_full_vector_file_profiled(
    manifest_path: str | Path,
    output_path: str | Path,
    profile_path: str | Path,
    events_path: str | Path | None = ...,
    pair_worker_limit: int | None = ...,
    execution_events: bool = ...,
) -> None: ...
def execute_full_vector(
    indicator_program: str,
    signal_program: str,
    tag_program: str,
    base_pair: str,
    base_timeframe: str,
    base_timestamps_ms: list[int],
    base_columns: dict[str, NumericColumn],
    informative_frames: list[InformativeFrame],
    metadata: dict[str, str],
    requested_indicator_columns: list[str],
    execution_start_index: int,
) -> FullVectorOutput: ...
def execute_numeric_mutation_program(
    program: str,
    columns: dict[str, NumericColumn],
    metadata: dict[str, str],
    requested_outputs: list[str],
) -> dict[str, NativeTypedColumn]: ...
