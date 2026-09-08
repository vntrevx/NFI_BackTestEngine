"""Official comparison honors the resource caps sealed with its Native run."""

import pytest
from nfi_backtest_engine.canonical import write_json
from nfi_backtest_engine.errors import BenchmarkError
from nfi_backtest_engine.reference_resources import reference_resource_limits


def test_reference_preserves_explicit_caps_and_legacy_absence(tmp_path):
    assert reference_resource_limits(tmp_path) == {}
    write_json(
        tmp_path / "execution-profile.json",
        {
            "limits": {"memory_cap_bytes": 4 * 1024**3, "cpu_process_limit": 2},
        },
    )
    assert reference_resource_limits(tmp_path) == {
        "memory_cap_bytes": 4 * 1024**3,
        "cpu_limit": 2,
    }


@pytest.mark.parametrize(
    "limits",
    [
        {},
        {"memory_cap_bytes": None},
        {"memory_cap_bytes": True, "cpu_process_limit": 1},
        {"memory_cap_bytes": 1024, "cpu_process_limit": 1},
        {"memory_cap_bytes": 4.0 * 1024**3, "cpu_process_limit": 1},
        {"memory_cap_bytes": None, "cpu_process_limit": True},
        {"memory_cap_bytes": None, "cpu_process_limit": 0},
        {"memory_cap_bytes": None, "cpu_process_limit": 1.5},
    ],
)
def test_invalid_reference_caps_fail_before_starting_work(tmp_path, limits):
    write_json(tmp_path / "execution-profile.json", {"limits": limits})
    with pytest.raises(BenchmarkError):
        reference_resource_limits(tmp_path)
