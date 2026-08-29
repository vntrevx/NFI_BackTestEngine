from __future__ import annotations

import gzip
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
from nfi_backtest_engine import cli, semantic_inventory, semantic_registry
from nfi_backtest_engine.errors import (
    PackagedRegistryCurrentRefError,
    SpecValidationError,
    StrategyAnalysisError,
)
from nfi_backtest_engine.semantic_inventory import build_semantic_obligation_registry
from nfi_backtest_engine.semantic_registry import (
    load_packaged_semantic_obligation_registry,
    package_semantic_obligation_registry,
    validate_packaged_semantic_obligation_registry_integrity,
)

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_CONTRACTS = ROOT / "python/nfi_backtest_engine/contracts"
PACKAGED_REGISTRY = PACKAGE_CONTRACTS / "freqtrade-nfi-semantic-obligation-registry.json.gz"
PACKAGED_MANIFEST = PACKAGE_CONTRACTS / "freqtrade-nfi-semantic-obligation-registry.manifest.json"


def test_packaged_registry_is_complete_and_bound_to_manifest() -> None:
    assert PACKAGED_REGISTRY.is_file()
    assert PACKAGED_MANIFEST.is_file()
    manifest = json.loads(PACKAGED_MANIFEST.read_text(encoding="utf-8"))

    compressed_hash = hashlib.sha256()
    uncompressed_hash = hashlib.sha256()
    uncompressed_bytes = 0
    with PACKAGED_REGISTRY.open("rb") as compressed:
        while chunk := compressed.read(1024 * 1024):
            compressed_hash.update(chunk)
    with gzip.open(PACKAGED_REGISTRY, "rb") as payload:
        while chunk := payload.read(1024 * 1024):
            uncompressed_hash.update(chunk)
            uncompressed_bytes += len(chunk)

    integrity = validate_packaged_semantic_obligation_registry_integrity()
    assert manifest["compressed_sha256"] == compressed_hash.hexdigest()
    assert manifest["uncompressed_bytes"] == uncompressed_bytes
    assert manifest["uncompressed_sha256"] == uncompressed_hash.hexdigest()
    assert integrity == {
        "schema_version": "packaged-semantic-registry-integrity-v1",
        "integrity_valid": True,
        "compressed_sha256": manifest["compressed_sha256"],
        "uncompressed_sha256": manifest["uncompressed_sha256"],
        "registry_fingerprint": manifest["registry_fingerprint"],
        "upstream_commit": manifest["upstream_commit"],
        "native_promotion": False,
    }


def test_built_wheel_and_sdist_contain_the_complete_registry() -> None:
    wheel_value = os.environ.get("NFI_TEST_SEMANTIC_REGISTRY_WHEEL")
    sdist_value = os.environ.get("NFI_TEST_SEMANTIC_REGISTRY_SDIST")
    assert (wheel_value is None) == (sdist_value is None)
    if wheel_value is None or sdist_value is None:
        assert PACKAGED_REGISTRY.is_relative_to(ROOT / "python/nfi_backtest_engine")
        assert PACKAGED_MANIFEST.is_relative_to(ROOT / "python/nfi_backtest_engine")
        return

    wheel = Path(wheel_value)
    sdist = Path(sdist_value)
    wheel_payload = "nfi_backtest_engine/contracts/" + PACKAGED_REGISTRY.name
    wheel_manifest = "nfi_backtest_engine/contracts/" + PACKAGED_MANIFEST.name
    wheel_schema = "nfi_backtest_engine/schemas/semantic-obligation-registry-v1.schema.json"
    with zipfile.ZipFile(wheel) as archive:
        assert archive.namelist().count(wheel_schema) == 1
        packaged_payload = archive.read(wheel_payload)
        packaged_manifest = archive.read(wheel_manifest)
        packaged_schema = archive.read(wheel_schema)
    with tarfile.open(sdist, "r:gz") as archive:
        root = archive.getnames()[0].split("/", 1)[0]
        payload_member = archive.extractfile(f"{root}/python/{wheel_payload}")
        manifest_member = archive.extractfile(f"{root}/python/{wheel_manifest}")
        sdist_schema_name = f"{root}/python/{wheel_schema}"
        assert archive.getnames().count(sdist_schema_name) == 1
        schema_member = archive.extractfile(sdist_schema_name)
        assert payload_member is not None
        assert manifest_member is not None
        assert schema_member is not None
        sdist_payload = payload_member.read()
        sdist_manifest = manifest_member.read()
        sdist_schema = schema_member.read()

    source_schema = (
        ROOT / "python/nfi_backtest_engine/schemas/semantic-obligation-registry-v1.schema.json"
    ).read_bytes()
    assert packaged_payload == sdist_payload == PACKAGED_REGISTRY.read_bytes()
    assert packaged_manifest == sdist_manifest == PACKAGED_MANIFEST.read_bytes()
    assert packaged_schema == sdist_schema == source_schema
    uncompressed_payload = gzip.decompress(packaged_payload)
    manifest_document = json.loads(packaged_manifest)
    assert len(uncompressed_payload) == manifest_document["uncompressed_bytes"]
    assert (
        hashlib.sha256(uncompressed_payload).hexdigest()
        == manifest_document["uncompressed_sha256"]
    )


@pytest.mark.parametrize(
    "asset_environment",
    ["NFI_TEST_SEMANTIC_REGISTRY_WHEEL", "NFI_TEST_SEMANTIC_REGISTRY_SDIST"],
)
def test_installed_distribution_schema_mutations_fail_before_registry_counts(
    tmp_path: Path,
    asset_environment: str,
) -> None:
    asset_value = os.environ.get(asset_environment)
    if asset_value is None:
        pytest.skip("built distribution paths are supplied by the distribution lane")
    asset = Path(asset_value)
    environment = tmp_path / "venv"
    subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(environment)],
        check=True,
        capture_output=True,
        text=True,
    )
    python = environment / "bin/python"
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), str(asset)],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    located = subprocess.run(
        [
            str(python),
            "-c",
            (
                "from importlib.resources import files; "
                "print(files('nfi_backtest_engine.schemas').joinpath("
                "'semantic-obligation-registry-v1.schema.json'))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    schema = Path(located.stdout.strip())
    assert schema.is_relative_to(environment)
    original = schema.read_bytes()
    offline_cached_api = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import json; "
                "from nfi_backtest_engine.native_scorecard import "
                "_offline_nonpromotional_semantic_registry_identity as identity; "
                "print(json.dumps(identity(), sort_keys=True))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert json.loads(offline_cached_api.stdout)["native_promotion"] is False
    valid_loader = subprocess.run(
        [str(environment / "bin/nfi-bte"), "strategy", "semantic-registry-packaged"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert valid_loader.returncode == 0, valid_loader.stderr
    authoritative = load_packaged_semantic_obligation_registry()
    assert f"obligations={authoritative['summary']['total_obligations']}" in valid_loader.stdout
    assert "native_promotion=True" in valid_loader.stdout

    strategy = tmp_path / "InstalledFailureStrategy.py"
    strategy.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class InstalledFailureStrategy(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_indicators(self, dataframe, metadata): return dataframe\n"
        "    def populate_entry_trend(self, dataframe, metadata): return dataframe\n"
        "    def populate_exit_trend(self, dataframe, metadata): return dataframe\n",
        encoding="utf-8",
    )
    for name, repository, ref, blocker in (
        (
            "fetch-failure",
            str(tmp_path / "absent.git"),
            "refs/heads/main",
            "UPSTREAM_FETCH_FAILED",
        ),
        (
            "ambiguous-ref",
            "https://example.invalid/nfi.git",
            "refs/heads/*",
            "INVALID_UPSTREAM_REF",
        ),
    ):
        audit = tmp_path / f"{name}.json"
        completed = subprocess.run(
            [
                str(environment / "bin/nfi-bte"),
                "strategy",
                "semantic-registry",
                str(strategy),
                "--class",
                "InstalledFailureStrategy",
                "--source-root",
                str(tmp_path),
                "--upstream-repository",
                repository,
                "--upstream-ref",
                ref,
                "--upstream-commit",
                "a" * 40,
                "--upstream-source-path",
                strategy.name,
                "--output",
                str(audit),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode == 1, completed.stderr
        failure_document = json.loads(audit.read_text(encoding="utf-8"))
        assert {item["code"] for item in failure_document["blockers"]} == {blocker}
        assert failure_document["summary"]["native_promotion"] is False
        assert "returned non-zero exit status" not in completed.stderr

    mutations = {
        "empty": b"{}",
        "widened": json.dumps(
            {
                "$id": "mutated",
                "type": "object",
                "additionalProperties": True,
            }
        ).encode(),
        "truncated": original[: len(original) // 2],
        "byte-swapped": original[::-1],
    }
    for name, mutation in mutations.items():
        schema.write_bytes(mutation)
        completed = subprocess.run(
            [str(environment / "bin/nfi-bte"), "strategy", "semantic-registry-packaged"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode == 2, name
        assert "SEMANTIC_REGISTRY_SCHEMA_IDENTITY" in completed.stderr, name
        assert "obligations=" not in completed.stdout, name
        assert "native_promotion=" not in completed.stdout, name
        schema.write_bytes(original)

    duplicate = schema.with_name(f"{schema.name}.duplicate")
    duplicate.write_bytes(original)
    try:
        completed = subprocess.run(
            [str(environment / "bin/nfi-bte"), "strategy", "semantic-registry-packaged"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode == 2
        assert "SEMANTIC_REGISTRY_SCHEMA_IDENTITY" in completed.stderr
        assert "obligations=" not in completed.stdout
        assert "native_promotion=" not in completed.stdout
    finally:
        duplicate.unlink(missing_ok=True)

    external_backup = tmp_path / "sealed-schema.json"
    external_backup.write_bytes(original)
    schema.unlink()
    try:
        for name, configure in (
            ("missing", lambda: None),
            ("symlink", lambda: schema.symlink_to(external_backup)),
        ):
            configure()
            completed = subprocess.run(
                [str(environment / "bin/nfi-bte"), "strategy", "semantic-registry-packaged"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert completed.returncode == 2, name
            assert "SEMANTIC_REGISTRY_SCHEMA_IDENTITY" in completed.stderr, name
            assert "obligations=" not in completed.stdout, name
            assert "native_promotion=" not in completed.stdout, name
            schema.unlink(missing_ok=True)
    finally:
        schema.unlink(missing_ok=True)
        schema.write_bytes(original)


def _git(command: list[str], *, cwd: Path) -> str:
    return subprocess.run(
        ["git", *command],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_packager_rolls_back_both_assets_when_publication_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "registry.json"
    source.write_bytes(b"{}\n")
    destination = tmp_path / "registry.json.gz"
    manifest_path = tmp_path / "registry.manifest.json"
    destination.write_bytes(b"existing payload\n")
    manifest_path.write_bytes(b"existing manifest\n")
    document = {
        "strategy": {
            "upstream": {
                "repository": "https://example.invalid/nfi.git",
                "ref": "refs/heads/main",
                "configured_commit": "a" * 40,
                "observed_commit": "a" * 40,
                "observation_method": "git-fetch-depth-1-v1",
            }
        },
        "summary": {"native_promotion": True},
        "fingerprint": "b" * 64,
    }
    monkeypatch.setattr(semantic_registry, "loads_json_bytes", lambda *_args, **_kwargs: document)
    monkeypatch.setattr(
        semantic_registry,
        "validate_semantic_obligation_registry",
        lambda _document: None,
    )
    original_replace = Path.replace
    replacements = 0

    def fail_second_replace(path: Path, target: Path) -> Path:
        nonlocal replacements
        replacements += 1
        if replacements == 2:
            raise OSError("injected manifest publication failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_second_replace)
    with pytest.raises(OSError):
        package_semantic_obligation_registry(
            source,
            destination=destination,
            manifest_path=manifest_path,
        )

    assert destination.read_bytes() == b"existing payload\n"
    assert manifest_path.read_bytes() == b"existing manifest\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "registry.json",
        "registry.json.gz",
        "registry.manifest.json",
    ]


def test_packager_rejects_unobserved_registry_without_replacing_assets(
    tmp_path: Path,
) -> None:
    source = tmp_path / "OfflineStrategy.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class OfflineStrategy(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_indicators(self, dataframe, metadata): return dataframe\n"
        "    def populate_entry_trend(self, dataframe, metadata): return dataframe\n"
        "    def populate_exit_trend(self, dataframe, metadata): return dataframe\n",
        encoding="utf-8",
    )
    registry = tmp_path / "offline.json"
    build_semantic_obligation_registry(
        source,
        class_name="OfflineStrategy",
        upstream_repository="https://example.invalid/nfi.git",
        upstream_commit="0" * 40,
        upstream_source_path=source.name,
        output_path=registry,
    )
    destination = tmp_path / "packaged.json.gz"
    manifest = tmp_path / "manifest.json"
    destination.write_bytes(b"payload-sentinel")
    manifest.write_bytes(b"manifest-sentinel")

    with pytest.raises(StrategyAnalysisError, match="dynamically observed"):
        package_semantic_obligation_registry(
            registry,
            destination=destination,
            manifest_path=manifest,
        )

    assert destination.read_bytes() == b"payload-sentinel"
    assert manifest.read_bytes() == b"manifest-sentinel"


def test_packaged_loader_validates_complete_payload_and_rejects_staleness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    _git(["init", "--initial-branch=main"], cwd=work)
    _git(["config", "user.name", "Distribution Test"], cwd=work)
    _git(["config", "user.email", "distribution@example.invalid"], cwd=work)
    source = work / "Strategy.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Strategy(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_indicators(self, dataframe, metadata): return dataframe\n"
        "    def populate_entry_trend(self, dataframe, metadata): return dataframe\n"
        "    def populate_exit_trend(self, dataframe, metadata): return dataframe\n",
        encoding="utf-8",
    )
    _git(["add", "Strategy.py"], cwd=work)
    _git(["commit", "-m", "current"], cwd=work)
    commit = _git(["rev-parse", "HEAD"], cwd=work)
    remote = tmp_path / "remote.git"
    _git(["clone", "--bare", str(work), str(remote)], cwd=tmp_path)
    registry_path = tmp_path / "registry.json"
    registry = build_semantic_obligation_registry(
        source,
        class_name="Strategy",
        upstream_repository=str(remote),
        upstream_ref="refs/heads/main",
        upstream_source_path="Strategy.py",
        upstream_commit=commit,
        output_path=registry_path,
    )
    payload = tmp_path / "registry.json.gz"
    manifest = tmp_path / "registry.manifest.json"
    package_semantic_obligation_registry(
        registry_path,
        destination=payload,
        manifest_path=manifest,
    )
    monkeypatch.setattr(semantic_registry, "_PACKAGED_REGISTRY", payload)
    monkeypatch.setattr(
        semantic_registry,
        "_PACKAGED_REGISTRY_MANIFEST",
        manifest,
    )

    original_fetch = semantic_inventory._fetch_upstream_ref_once
    observations = 0

    def fetch_and_advance(*args, **kwargs):
        nonlocal observations
        result = original_fetch(*args, **kwargs)
        observations += 1
        if observations == 1:
            source.write_text(
                source.read_text(encoding="utf-8").replace(
                    "return dataframe", "return dataframe.copy()", 1
                ),
                encoding="utf-8",
            )
            _git(["add", "Strategy.py"], cwd=work)
            _git(["commit", "-m", "moved-between-observations"], cwd=work)
            _git(["push", str(remote), "main"], cwd=work)
        return result

    monkeypatch.setattr(semantic_inventory, "_fetch_upstream_ref_once", fetch_and_advance)
    with pytest.raises(PackagedRegistryCurrentRefError) as moved:
        load_packaged_semantic_obligation_registry()
    assert moved.value.evidence["code"] == "UPSTREAM_REF_MOVED_DURING_AUTHORIZATION"
    assert observations == 2

    _git(["reset", "--hard", commit], cwd=work)
    _git(["push", "--force", str(remote), "main"], cwd=work)
    monkeypatch.setattr(semantic_inventory, "_fetch_upstream_ref_once", original_fetch)
    loaded = load_packaged_semantic_obligation_registry()
    assert loaded["fingerprint"] == registry["fingerprint"]
    assert loaded["strategy"]["upstream"]["observed_commit"] == commit

    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "return dataframe", "return dataframe.copy()", 1
        ),
        encoding="utf-8",
    )
    _git(["add", "Strategy.py"], cwd=work)
    _git(["commit", "-m", "advanced"], cwd=work)
    advanced_commit = _git(["rev-parse", "HEAD"], cwd=work)
    _git(["push", str(remote), "main"], cwd=work)

    integrity = validate_packaged_semantic_obligation_registry_integrity()
    assert integrity["integrity_valid"] is True
    assert integrity["native_promotion"] is False
    with pytest.raises(PackagedRegistryCurrentRefError) as failure:
        load_packaged_semantic_obligation_registry()
    assert failure.value.evidence == {
        "schema_version": "packaged-semantic-registry-current-ref-proof-v1",
        "code": "STALE_UPSTREAM_REF",
        "observation_method": "git-fetch-depth-1-v1",
        "observation_status": "stale",
        "repository": str(remote),
        "ref": "refs/heads/main",
        "packaged_commit": commit,
        "observed_commit": advanced_commit,
        "native_promotion": False,
    }

    monkeypatch.setattr(
        "nfi_backtest_engine.semantic_inventory._fetch_upstream_ref_once",
        lambda *_args, **_kwargs: (
            None,
            {
                "blocker_code": "UPSTREAM_FETCH_FAILED",
                "observation_method": "upstream-fetch-failed-v1",
                "observation_status": "fetch-failed",
            },
        ),
    )
    with pytest.raises(PackagedRegistryCurrentRefError) as offline:
        load_packaged_semantic_obligation_registry()
    assert offline.value.evidence["code"] == "UPSTREAM_FETCH_FAILED"
    assert offline.value.evidence["native_promotion"] is False

    payload.write_bytes(payload.read_bytes() + b"tampered")
    with pytest.raises(SpecValidationError, match="compressed hash differs"):
        load_packaged_semantic_obligation_registry()


def test_installed_cli_separates_integrity_from_current_promotion() -> None:
    parser = cli.build_parser()
    current = parser.parse_args(["strategy", "semantic-registry-packaged"])
    integrity = parser.parse_args(["strategy", "semantic-registry-packaged-integrity"])

    assert current.strategy_command == "semantic-registry-packaged"
    assert integrity.strategy_command == "semantic-registry-packaged-integrity"
