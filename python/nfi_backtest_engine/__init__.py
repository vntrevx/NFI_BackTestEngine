"""NFI Backtest Engine public package boundary."""

from importlib.metadata import PackageNotFoundError, version

from .parity import ParityDifference, compare_surfaces, first_difference

try:
    __version__ = version("nfi-backtest-engine")
except PackageNotFoundError:
    __version__ = "0.2.0"

__all__ = ["ParityDifference", "__version__", "compare_surfaces", "first_difference"]
