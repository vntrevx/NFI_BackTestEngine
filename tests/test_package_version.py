from pathlib import Path
from tomllib import load

from nfi_backtest_engine import __version__


def test_source_checkout_uses_pyproject_version() -> None:
    """Stale editable-install metadata must not alter certification identity."""

    project_file = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with project_file.open("rb") as handle:
        declared_version = load(handle)["project"]["version"]

    assert __version__ == declared_version
