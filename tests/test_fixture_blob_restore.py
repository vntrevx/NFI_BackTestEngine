"""Packed capture restoration preserves authenticated evidence."""

import gzip
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


def restorer():
    path = Path(__file__).parents[1] / ".github/scripts/restore_fixture_blobs.py"
    spec = importlib.util.spec_from_file_location("restore_fixture_blobs", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.restore


def packed(root):
    data = b"original captured state\n"
    target = root / "benchmarks/fixtures/example.nfitrace"
    target.parent.mkdir(parents=True)
    archive = target.with_suffix(".gz")
    payload = gzip.compress(data, mtime=0)
    archive.write_bytes(payload)
    record = {
        "path": str(target.relative_to(root)),
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "compressed_path": str(archive.relative_to(root)),
        "compressed_sha256": hashlib.sha256(payload).hexdigest(),
        "compressed_bytes": len(payload),
    }
    (target.parent / "compressed-blobs.json").write_text(
        json.dumps({"schema_version": "1.0.0", "files": [record]})
    )
    return target, archive, data


def test_restore_is_exact_and_idempotent(tmp_path):
    target, _, data = packed(tmp_path)
    restore = restorer()
    assert restore(tmp_path) == {"restored": 1, "already_verified": 0}
    assert target.read_bytes() == data
    assert restore(tmp_path) == {"restored": 0, "already_verified": 1}


def test_restore_refuses_to_overwrite_existing_evidence(tmp_path):
    target, _, _ = packed(tmp_path)
    target.write_bytes(b"other evidence")
    with pytest.raises(ValueError, match="refusing overwrite"):
        restorer()(tmp_path)
    assert target.read_bytes() == b"other evidence"


def test_restore_rejects_corrupt_archive_before_publication(tmp_path):
    target, archive, _ = packed(tmp_path)
    archive.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="compressed fixture identity"):
        restorer()(tmp_path)
    assert not target.exists()
