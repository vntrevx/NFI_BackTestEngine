"""Typed inputs for one current-HEAD changed-target proof ledger."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .errors import SpecValidationError

MODES: Final = ("futures", "spot")


@dataclass(frozen=True, slots=True)
class ChangedTargetLedgerSources:
    """Immutable paths and upstream identity joined by ledger generation."""

    strategy_diff: Path
    semantic_registry: Path
    fixture_registry: Path
    targeted_reports: dict[str, Path]
    upstream_repository: str
    upstream_ref: str
    upstream_head: str
    baseline_commit: str

    def __post_init__(self) -> None:
        if set(self.targeted_reports) != set(MODES):
            raise SpecValidationError(
                "targeted reports must contain exactly Spot and Futures paths"
            )
