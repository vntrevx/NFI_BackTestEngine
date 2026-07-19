"""Fast exact confirmation against an official Freqtrade JSON or ZIP export."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .errors import BenchmarkError
from .fixture import sha256_file
from .normalize import normalize_file
from .parity import first_difference


def confirm_research_run(
    run_directory: str | Path,
    freqtrade_export: str | Path,
    output_directory: str | Path,
    *,
    strategy: str | None = None,
) -> dict[str, Any]:
    root = Path(run_directory).resolve()
    run_path = root / "run.json"
    if not run_path.is_file():
        raise BenchmarkError(f"research run.json does not exist: {run_path}")
    run = read_json(run_path)
    if run.get("status") != "complete" or not isinstance(run.get("result"), dict):
        raise BenchmarkError("only a complete research run can be confirmed")
    surface_record = run["result"].get("trade_surface")
    if not isinstance(surface_record, dict) or not isinstance(surface_record.get("path"), str):
        raise BenchmarkError("research run has no trade-surface artifact")
    engine_surface_path = Path(surface_record["path"]).resolve()
    if (
        not engine_surface_path.is_relative_to(root)
        or not engine_surface_path.is_file()
        or sha256_file(engine_surface_path) != surface_record.get("sha256")
    ):
        raise BenchmarkError("research trade-surface artifact failed its hash binding")

    output = Path(output_directory).resolve()
    if output.exists() and any(output.iterdir()):
        raise BenchmarkError(f"confirmation output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    official_surface_path = output / "official-trade-surface.json"
    official = normalize_file(
        freqtrade_export,
        official_surface_path,
        strategy=strategy,
        surface_version="2",
    )
    engine = read_json(engine_surface_path)
    difference = first_difference(official, engine)
    report = {
        "schema_version": "1.0.0",
        "run_id": run["run_id"],
        "equal": difference is None,
        "engine": {
            "path": str(engine_surface_path),
            "sha256": sha256_file(engine_surface_path),
        },
        "official": {
            "export_path": str(Path(freqtrade_export).resolve()),
            "export_sha256": sha256_file(freqtrade_export),
            "surface_path": str(official_surface_path),
            "surface_sha256": sha256_file(official_surface_path),
        },
        "difference": (
            None
            if difference is None
            else {
                "path": difference.path,
                "expected": _json_value(difference.expected),
                "actual": _json_value(difference.actual),
                "reason": difference.reason,
            }
        ),
    }
    write_json(output / "confirmation.json", report)
    return report


def _json_value(value: Any) -> Any:
    if type(value).__name__ == "_Missing":
        return {"missing": True}
    return value
