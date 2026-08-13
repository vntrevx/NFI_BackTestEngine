from pathlib import Path

import pytest
from nfi_backtest_engine import _rust


def test_full_native_file_entrypoints_fail_closed_before_writing(tmp_path: Path) -> None:
    missing = tmp_path / "missing-full-native-manifest.json"
    result = tmp_path / "result.json"
    profile = tmp_path / "profile.json"

    with pytest.raises(ValueError, match="invalid full native vector manifest"):
        _rust.simulate_full_vector_file(missing, result)
    with pytest.raises(ValueError, match="invalid full native vector manifest"):
        _rust.simulate_full_vector_file_profiled(missing, result, profile)

    assert not result.exists()
    assert not profile.exists()
