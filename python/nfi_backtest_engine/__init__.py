"""NFI Backtest Engine Phase 0/1 contracts and tooling."""

from .parity import ParityDifference, compare_surfaces, first_difference

__all__ = ["ParityDifference", "compare_surfaces", "first_difference"]
__version__ = "0.1.0"
