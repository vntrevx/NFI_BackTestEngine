from pathlib import Path

from nfi_backtest_engine.reference_assets import (
    reference_package_root,
    reference_tracer_root,
)


def test_reference_assets_are_complete_and_package_scoped() -> None:
    package_root = reference_package_root()
    tracer_root = reference_tracer_root()

    assert package_root.name == "nfi_backtest_engine"
    assert (package_root / "state_trace.py").is_file()
    assert tracer_root.parent == package_root
    assert (tracer_root / "sitecustomize.py").is_file()
    assert (tracer_root / "nfi_reference_trace.py").is_file()


def test_reference_package_root_never_exposes_site_packages() -> None:
    package_root = reference_package_root()

    assert package_root != Path(__file__).resolve().parent
    assert not (package_root / "numpy").exists()
