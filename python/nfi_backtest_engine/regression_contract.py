"""Read-only verification for the versioned v1.1 regression contract."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from importlib.resources import files
from pathlib import Path
from typing import Any

from .canonical import read_json
from .errors import SpecValidationError
from .fixture import sha256_file, validate_fixture
from .specs import validate_regression_contract

DEFAULT_REGRESSION_CONTRACT = "regression-v1.1.0.json"


def load_regression_contract(manifest_path: str | Path | None = None) -> tuple[dict[str, Any], str]:
    """Load either an explicit manifest or the immutable manifest bundled in the wheel."""
    if manifest_path is None:
        resource = files("nfi_backtest_engine.contracts").joinpath(DEFAULT_REGRESSION_CONTRACT)
        raw = resource.read_bytes()
        label = f"package:{DEFAULT_REGRESSION_CONTRACT}"
    else:
        source = Path(manifest_path).resolve()
        raw = source.read_bytes()
        label = str(source)
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SpecValidationError(f"regression contract is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise SpecValidationError("regression contract must be a JSON object")
    return document, label


def verify_regression_contract(
    manifest_path: str | Path | None = None,
    *,
    repository_root: str | Path = ".",
    release_asset_roots: Mapping[str, Path] | None = None,
    fetch_release_assets: bool = False,
) -> dict[str, Any]:
    """Verify every repository-bound identity and, when requested, published assets."""
    manifest, manifest_label = load_regression_contract(manifest_path)
    validate_regression_contract(manifest)
    root = Path(repository_root).resolve()
    if not root.is_dir():
        raise SpecValidationError(f"repository root does not exist: {root}")

    checked_repository_files = 0
    for record in manifest["repository_files"]:
        target = _verify_repository_file(root, record)
        assertions = record.get("assertions", [])
        if assertions:
            _verify_assertions(
                read_json(target),
                assertions,
                context=record["path"],
            )
        checked_repository_files += 1

    checked_fixtures = 0
    for record in manifest["full_state_fixtures"]:
        target = _safe_repository_path(root, record["manifest_path"])
        _verify_file_identity(
            target,
            expected_bytes=record["manifest_bytes"],
            expected_sha256=record["manifest_sha256"],
            context=record["manifest_path"],
        )
        fixture = validate_fixture(target, validate_trace_semantics=False)
        _expect_equal(
            fixture.get("fixture_id"),
            record["fixture_id"],
            f"{record['manifest_path']} $.fixture_id",
        )
        artifacts = fixture.get("artifacts")
        if not isinstance(artifacts, dict):
            raise SpecValidationError(f"{record['manifest_path']} $.artifacts must be an object")
        _expect_artifact_sha(
            artifacts,
            "trade_surface",
            record["trade_surface_sha256"],
            record["manifest_path"],
        )
        _expect_artifact_sha(
            artifacts,
            "state_projection",
            record["state_projection_sha256"],
            record["manifest_path"],
        )
        checked_fixtures += 1

    actual_command_paths = _public_command_paths()
    expected_command_paths = manifest["cli"]["command_paths"]
    missing_command_paths = [
        path for path in expected_command_paths if path not in actual_command_paths
    ]
    if missing_command_paths:
        raise SpecValidationError(
            "$.cli.command_paths: frozen public commands are missing: "
            + ", ".join(missing_command_paths)
        )

    checked_error_codes = _verify_stable_error_codes(root, manifest["cli"]["error_codes"])
    checked_scenarios = _verify_scenarios(root, manifest["behavior_contracts"])

    release_roots = {
        tag: Path(path).resolve() for tag, path in (release_asset_roots or {}).items()
    }
    release_assets_verified = 0
    release_certificates_verified = 0
    for release in manifest["releases"]:
        tag = release["tag"]
        local_root = release_roots.get(tag)
        if local_root is not None and not local_root.is_dir():
            raise SpecValidationError(f"release asset root for {tag} does not exist: {local_root}")
        if local_root is None and not fetch_release_assets:
            continue
        certificate_payload: bytes | None = None
        for asset in release["assets"]:
            capture = asset["name"] == release["certificate"]["asset"]
            payload = (
                _verify_local_release_asset(local_root, asset, tag=tag, capture=capture)
                if local_root is not None
                else _verify_remote_release_asset(asset, tag=tag, capture=capture)
            )
            if capture:
                certificate_payload = payload
            release_assets_verified += 1
        if certificate_payload is None:
            raise SpecValidationError(
                f"{tag}: certificate asset {release['certificate']['asset']!r} was not verified"
            )
        try:
            certificate = json.loads(certificate_payload)
        except json.JSONDecodeError as exc:
            raise SpecValidationError(f"{tag}: certificate asset is not valid JSON: {exc}") from exc
        _verify_assertions(
            certificate,
            release["certificate"]["assertions"],
            context=f"{tag}/{release['certificate']['asset']}",
        )
        release_certificates_verified += 1

    release_mode = (
        "verified"
        if release_certificates_verified == len(manifest["releases"])
        else "identity-pinned"
    )
    return {
        "schema_version": "1.0.0",
        "contract_version": manifest["contract_version"],
        "manifest": {
            "source": manifest_label,
            "sha256": _manifest_sha256(manifest_path),
        },
        "repository_root": str(root),
        "checks": {
            "schema": "valid",
            "repository_files": checked_repository_files,
            "full_state_fixtures": checked_fixtures,
            "public_command_paths": len(expected_command_paths),
            "stable_error_codes": checked_error_codes,
            "behavior_contracts": checked_scenarios,
            "release_assets": release_assets_verified,
            "release_certificates": release_certificates_verified,
            "release_mode": release_mode,
        },
        "complete": True,
    }


def parse_release_asset_roots(values: Sequence[str]) -> dict[str, Path]:
    """Parse repeatable ``TAG=DIR`` command-line bindings without guessing."""
    roots: dict[str, Path] = {}
    for value in values:
        tag, separator, raw_path = value.partition("=")
        if not separator or not tag or not raw_path:
            raise SpecValidationError(
                f"invalid --release-assets value {value!r}; expected TAG=DIR"
            )
        if tag in roots:
            raise SpecValidationError(f"duplicate --release-assets tag: {tag}")
        roots[tag] = Path(raw_path)
    return roots


def _manifest_sha256(manifest_path: str | Path | None) -> str:
    if manifest_path is not None:
        return sha256_file(Path(manifest_path).resolve())
    resource = files("nfi_backtest_engine.contracts").joinpath(DEFAULT_REGRESSION_CONTRACT)
    return hashlib.sha256(resource.read_bytes()).hexdigest()


def _verify_repository_file(root: Path, record: Mapping[str, Any]) -> Path:
    target = _safe_repository_path(root, str(record["path"]))
    _verify_file_identity(
        target,
        expected_bytes=int(record["bytes"]),
        expected_sha256=str(record["sha256"]),
        context=str(record["path"]),
    )
    return target


def _safe_repository_path(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute():
        raise SpecValidationError(f"regression contract path must be relative: {relative}")
    target = (root / candidate).resolve()
    if not target.is_relative_to(root):
        raise SpecValidationError(f"regression contract path escapes repository: {relative}")
    if not target.is_file():
        raise SpecValidationError(f"regression contract file does not exist: {relative}")
    return target


def _verify_file_identity(
    target: Path,
    *,
    expected_bytes: int,
    expected_sha256: str,
    context: str,
) -> None:
    actual_bytes = target.stat().st_size
    if actual_bytes != expected_bytes:
        raise SpecValidationError(
            f"{context}: byte size differs; expected {expected_bytes}, actual {actual_bytes}"
        )
    actual_sha256 = sha256_file(target)
    if actual_sha256 != expected_sha256:
        raise SpecValidationError(
            f"{context}: SHA-256 differs; expected {expected_sha256}, actual {actual_sha256}"
        )


def _expect_artifact_sha(
    artifacts: Mapping[str, Any],
    name: str,
    expected: str,
    context: str,
) -> None:
    artifact = artifacts.get(name)
    if not isinstance(artifact, dict):
        raise SpecValidationError(f"{context} $.artifacts.{name} must be an object")
    _expect_equal(
        artifact.get("sha256"),
        expected,
        f"{context} $.artifacts.{name}.sha256",
    )


def _public_command_paths() -> list[str]:
    from .cli import build_parser

    def visit(parser: argparse.ArgumentParser, prefix: tuple[str, ...]) -> list[str]:
        result: list[str] = []
        for action in parser._actions:  # noqa: SLF001 - argparse exposes no public tree API.
            choices = getattr(action, "choices", None)
            if not isinstance(choices, dict):
                continue
            child_parsers = {
                name: child
                for name, child in choices.items()
                if isinstance(name, str) and isinstance(child, argparse.ArgumentParser)
            }
            if not child_parsers:
                continue
            for name, child in child_parsers.items():
                path = (*prefix, name)
                result.append(" ".join(path))
                result.extend(visit(child, path))
        return result

    return visit(build_parser(), ())


def _verify_stable_error_codes(root: Path, records: Sequence[Mapping[str, Any]]) -> int:
    strings_by_source: dict[str, set[str]] = {}
    for record in records:
        source = str(record["source"])
        if source not in strings_by_source:
            tree = ast.parse(
                _safe_repository_path(root, source).read_text(encoding="utf-8"),
                filename=source,
            )
            strings_by_source[source] = {
                node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
        code = str(record["code"])
        if code not in strings_by_source[source]:
            raise SpecValidationError(f"{source}: stable error code is missing: {code}")
    return len(records)


def _verify_scenarios(root: Path, records: Sequence[Mapping[str, Any]]) -> int:
    functions_by_source: dict[str, set[str]] = {}
    for record in records:
        nodeid = str(record["test_nodeid"])
        source, separator, test_name = nodeid.partition("::")
        if not separator or not test_name.startswith("test_"):
            raise SpecValidationError(f"invalid behavior contract test nodeid: {nodeid}")
        if source not in functions_by_source:
            tree = ast.parse(
                _safe_repository_path(root, source).read_text(encoding="utf-8"),
                filename=source,
            )
            functions_by_source[source] = {
                node.name
                for node in tree.body
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            }
        if test_name not in functions_by_source[source]:
            raise SpecValidationError(f"behavior contract test does not exist: {nodeid}")
    return len(records)


def _verify_local_release_asset(
    root: Path,
    asset: Mapping[str, Any],
    *,
    tag: str,
    capture: bool,
) -> bytes | None:
    name = str(asset["name"])
    if Path(name).name != name:
        raise SpecValidationError(f"{tag}: release asset name must be a basename: {name}")
    target = (root / name).resolve()
    if not target.is_relative_to(root):
        raise SpecValidationError(f"{tag}: release asset escapes its directory: {name}")
    _verify_file_identity(
        target,
        expected_bytes=int(asset["bytes"]),
        expected_sha256=str(asset["sha256"]),
        context=f"{tag}/{name}",
    )
    return target.read_bytes() if capture else None


def _verify_remote_release_asset(
    asset: Mapping[str, Any],
    *,
    tag: str,
    capture: bool,
) -> bytes | None:
    name = str(asset["name"])
    request = urllib.request.Request(
        str(asset["url"]),
        headers={"User-Agent": "nfi-backtest-engine-regression-contract"},
    )
    digest = hashlib.sha256()
    actual_bytes = 0
    captured = bytearray() if capture else None
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            while chunk := response.read(1024 * 1024):
                actual_bytes += len(chunk)
                digest.update(chunk)
                if captured is not None:
                    captured.extend(chunk)
    except (OSError, urllib.error.URLError) as exc:
        raise SpecValidationError(f"{tag}/{name}: release asset download failed: {exc}") from exc
    expected_bytes = int(asset["bytes"])
    if actual_bytes != expected_bytes:
        raise SpecValidationError(
            f"{tag}/{name}: byte size differs; expected {expected_bytes}, actual {actual_bytes}"
        )
    actual_sha256 = digest.hexdigest()
    expected_sha256 = str(asset["sha256"])
    if actual_sha256 != expected_sha256:
        raise SpecValidationError(
            f"{tag}/{name}: SHA-256 differs; expected {expected_sha256}, actual {actual_sha256}"
        )
    return bytes(captured) if captured is not None else None


def _verify_assertions(
    document: Any,
    assertions: Sequence[Mapping[str, Any]],
    *,
    context: str,
) -> None:
    for assertion in assertions:
        path = assertion["path"]
        actual = _lookup(document, path, context=context)
        _expect_equal(actual, assertion["equals"], f"{context} {_render_path(path)}")


def _lookup(document: Any, path: Sequence[str | int], *, context: str) -> Any:
    current = document
    traversed: list[str | int] = []
    for part in path:
        traversed.append(part)
        if isinstance(part, int):
            if not isinstance(current, list) or not 0 <= part < len(current):
                raise SpecValidationError(
                    f"{context} {_render_path(traversed)} does not exist"
                )
            current = current[part]
        else:
            if not isinstance(current, dict) or part not in current:
                raise SpecValidationError(
                    f"{context} {_render_path(traversed)} does not exist"
                )
            current = current[part]
    return current


def _render_path(parts: Sequence[str | int]) -> str:
    result = "$"
    for part in parts:
        result += f"[{part}]" if isinstance(part, int) else f".{part}"
    return result


def _expect_equal(actual: Any, expected: Any, context: str) -> None:
    if actual != expected:
        raise SpecValidationError(f"{context}: expected {expected!r}, actual {actual!r}")
