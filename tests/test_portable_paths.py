from __future__ import annotations

from pathlib import Path

import pytest
from nfi_backtest_engine.benchmark import _validate_trusted_option_value
from nfi_backtest_engine.errors import (
    BenchmarkError,
    InputBoundaryError,
    SpecValidationError,
)
from nfi_backtest_engine.evidence_bundle import _canonical_bundle_name
from nfi_backtest_engine.fixture import _safe_fixture_path
from nfi_backtest_engine.portable_paths import (
    open_secure_directory,
    validate_portable_filesystem_path,
)

HOSTILE_PORTABLE_PATHS = [
    "CON", "con", "Con.txt", "PRN", "prn.json", "AUX", "aux.data",
    "NUL", "nul.txt", "CLOCK$", "clock$.json", "CONIN$", "conin$.x",
    "CONOUT$", "conout$.x", "COM1", "com9.log", "LPT1.bin", "lpt9.data",
    "COM¹", "com².bin", "COM³.x", "LPT¹", "lpt².cfg", "LPT³",
    "file:ads", "file.", "file ", "dir./file", "dir /file", "C:/file",
    "C:file", ".", "../file", "/file", "//server/share/file",
    "\\\\server\\share\\file", "\\\\?\\C:\\file", "\\\\.\\NUL",
    "dir\\file", "dir/../file", "./file", "dir/./file", "dir//file", "..", "",
    "bad<name", "bad>name", 'bad"name', "bad|name", "bad?name", "bad*name",
    "CON .txt", "NUL .json", "cafe\u0301.json",
]


@pytest.mark.parametrize("hostile", HOSTILE_PORTABLE_PATHS)
def test_every_public_path_boundary_rejects_complete_portable_matrix(
    tmp_path: Path, hostile: str
) -> None:
    for boundary in (
        lambda: _safe_fixture_path(tmp_path, hostile),
        lambda: _canonical_bundle_name(hostile, tmp_path),
        lambda: _validate_trusted_option_value("--config", hostile, tmp_path),
    ):
        with pytest.raises(
            (BenchmarkError, InputBoundaryError, SpecValidationError),
            match="path|portable|relative|contained|outside|invalid",
        ):
            boundary()


def test_public_secure_directory_rejects_procfs_self_magic_link() -> None:
    with pytest.raises(InputBoundaryError, match="symlink|changed"):
        open_secure_directory(Path("/proc/self"))


@pytest.mark.parametrize("spelling", ["dir//file", "dir/./file", "dir/../file"])
def test_initial_filesystem_path_rejects_normalization_aliases(spelling: str) -> None:
    with pytest.raises(InputBoundaryError, match="canonical|portable"):
        validate_portable_filesystem_path(spelling)
