"""NFI Backtest Engine public package boundary."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from tomllib import TOMLDecodeError, load

from .parity import ParityDifference, compare_surfaces, first_difference


def _source_tree_version() -> str | None:
    """Read the project version when this package is imported from a checkout.

    Editable builds can leave ignored ``*.egg-info`` directories behind.  Their
    metadata may describe an older build, so source-tree execution must use the
    same version declaration that the release builder reads.
    """

    project_file = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if not project_file.is_file():
        return None
    try:
        with project_file.open("rb") as handle:
            declared_version = load(handle)["project"]["version"]
    except (KeyError, OSError, TOMLDecodeError, TypeError):
        return None
    return declared_version if isinstance(declared_version, str) else None


def _package_version() -> str:
    source_version = _source_tree_version()
    if source_version is not None:
        return source_version
    try:
        return version("nfi-backtest-engine")
    except PackageNotFoundError:
        return "0+unknown"


__version__ = _package_version()

__all__ = ["ParityDifference", "__version__", "compare_surfaces", "first_difference"]
