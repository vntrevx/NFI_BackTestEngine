"""Legacy certification bundle packaging with stable byte layout."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from typing import Any

from ..fixture import sha256_file


def _bundle_files(root: Path) -> list[Path]:
    excluded = {
        root / "bundle-manifest.json",
        root / "bundle.json",
        root / "certification-bundle.zip",
    }
    return sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and path not in excluded
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )


def _write_reproducible_zip(destination: Path, root: Path, sources: list[Path]) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
        for source in sorted(sources, key=lambda path: path.relative_to(root).as_posix()):
            relative = source.relative_to(root).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100644 << 16
            with source.open("rb") as input_file, archive.open(info, "w") as output_file:
                shutil.copyfileobj(input_file, output_file, length=1024 * 1024)


def _artifact_record(path: Path, *, relative_to: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(relative_to).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }
