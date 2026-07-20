"""Verified S3 transport for large fixture and certification bundles."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import BenchmarkError
from .fixture import sha256_file


def upload_artifact(
    source: str | Path,
    destination: str,
    *,
    endpoint_url: str | None = None,
) -> dict[str, Any]:
    """Upload one immutable artifact and verify its remote size and hash metadata."""
    path = Path(source).resolve()
    if not path.is_file():
        raise BenchmarkError(f"artifact does not exist: {path}")
    bucket, key = _s3_location(destination)
    digest = sha256_file(path)
    size = path.stat().st_size
    _run_aws(
        [
            "s3",
            "cp",
            str(path),
            destination,
            "--no-progress",
            "--only-show-errors",
            "--metadata",
            f"sha256={digest}",
        ],
        endpoint_url=endpoint_url,
    )
    head = _head_object(bucket, key, endpoint_url=endpoint_url)
    remote_digest = head.get("Metadata", {}).get("sha256")
    if head.get("ContentLength") != size or remote_digest != digest:
        raise BenchmarkError("uploaded S3 artifact metadata differs from the local file")
    return _storage_record(destination, size=size, digest=digest, operation="upload")


def download_artifact(
    source: str,
    destination: str | Path,
    *,
    expected_sha256: str | None = None,
    endpoint_url: str | None = None,
) -> dict[str, Any]:
    """Download to a temporary path, verify SHA-256, then publish atomically."""
    bucket, key = _s3_location(source)
    target = Path(destination).resolve()
    if target.exists():
        raise BenchmarkError(f"artifact destination already exists: {target}")
    head = _head_object(bucket, key, endpoint_url=endpoint_url)
    metadata_digest = head.get("Metadata", {}).get("sha256")
    digest = expected_sha256 or metadata_digest
    if not isinstance(digest, str) or not _is_sha256(digest):
        raise BenchmarkError("S3 download requires a canonical SHA-256 argument or object metadata")
    if expected_sha256 is not None and metadata_digest not in {None, expected_sha256}:
        raise BenchmarkError("expected SHA-256 differs from S3 object metadata")

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.partial")
    temporary.unlink(missing_ok=True)
    try:
        _run_aws(
            [
                "s3",
                "cp",
                source,
                str(temporary),
                "--no-progress",
                "--only-show-errors",
            ],
            endpoint_url=endpoint_url,
        )
        actual = sha256_file(temporary)
        if actual != digest:
            raise BenchmarkError(
                f"downloaded S3 artifact SHA-256 differs: expected {digest}, actual {actual}"
            )
        remote_size = head.get("ContentLength")
        if isinstance(remote_size, int) and temporary.stat().st_size != remote_size:
            raise BenchmarkError("downloaded S3 artifact size differs from object metadata")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return _storage_record(
        source,
        size=target.stat().st_size,
        digest=digest,
        operation="download",
        local_path=str(target),
    )


def _head_object(
    bucket: str,
    key: str,
    *,
    endpoint_url: str | None,
) -> dict[str, Any]:
    completed = _run_aws(
        [
            "s3api",
            "head-object",
            "--bucket",
            bucket,
            "--key",
            key,
            "--output",
            "json",
        ],
        endpoint_url=endpoint_url,
    )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise BenchmarkError("AWS CLI returned invalid head-object JSON") from exc
    if not isinstance(result, dict):
        raise BenchmarkError("AWS CLI head-object response must be an object")
    return result


def _run_aws(
    arguments: list[str],
    *,
    endpoint_url: str | None,
) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("aws")
    if executable is None:
        raise BenchmarkError("AWS CLI is required for S3 artifact transport")
    command = [
        executable,
        *(["--endpoint-url", endpoint_url] if endpoint_url is not None else []),
        *arguments,
    ]
    completed = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise BenchmarkError(f"AWS CLI artifact transport failed: {message[-2000:]}")
    return completed


def _s3_location(uri: str) -> tuple[str, str]:
    parsed = urlsplit(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
        raise BenchmarkError(f"invalid S3 URI: {uri!r}")
    if parsed.query or parsed.fragment:
        raise BenchmarkError("S3 artifact URI must not contain a query or fragment")
    return parsed.netloc, parsed.path.lstrip("/")


def _storage_record(
    uri: str,
    *,
    size: int,
    digest: str,
    operation: str,
    local_path: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "operation": operation,
        "uri": uri,
        "local_path": local_path,
        "bytes": size,
        "sha256": digest,
        "verified": True,
    }


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
