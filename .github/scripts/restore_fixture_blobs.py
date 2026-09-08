#!/usr/bin/env python3
"""Restore checksum-pinned large fixture files without replacing existing evidence."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import tempfile
from pathlib import Path


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def contained(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    if not path.is_relative_to(root / "benchmarks"):
        raise ValueError(f"fixture blob escapes benchmarks: {name}")
    return path


def restore(root: Path) -> dict[str, int]:
    index = json.loads((root / "benchmarks/fixtures/compressed-blobs.json").read_text())
    if index["schema_version"] != "1.0.0":
        raise ValueError("unsupported fixture blob index")
    restored = existing = 0
    for record in index["files"]:
        target = contained(root, record["path"])
        archive = contained(root, record["compressed_path"])
        if (
            archive.stat().st_size != record["compressed_bytes"]
            or digest(archive) != record["compressed_sha256"]
        ):
            raise ValueError(f"compressed fixture identity differs: {archive}")
        if target.exists():
            if target.stat().st_size != record["bytes"] or digest(target) != record["sha256"]:
                raise ValueError(f"existing fixture identity differs; refusing overwrite: {target}")
            existing += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(dir=target.parent, prefix=".fixture-restore-")
        temporary = Path(temporary_name)
        try:
            count = 0
            checksum = hashlib.sha256()
            with os.fdopen(descriptor, "wb") as output, gzip.open(archive, "rb") as source:
                while data := source.read(1024 * 1024):
                    count += len(data)
                    if count > record["bytes"]:
                        raise ValueError(f"expanded fixture exceeds declared size: {archive}")
                    output.write(data)
                    checksum.update(data)
            if count != record["bytes"] or checksum.hexdigest() != record["sha256"]:
                raise ValueError(f"expanded fixture identity differs: {archive}")
            # Exclusive publication keeps concurrent restorers from replacing evidence.
            with target.open("xb") as output, temporary.open("rb") as source:
                while data := source.read(1024 * 1024):
                    output.write(data)
            restored += 1
        finally:
            temporary.unlink(missing_ok=True)
    return {"restored": restored, "already_verified": existing}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    print(json.dumps(restore(args.root.resolve())))
