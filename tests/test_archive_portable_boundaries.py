from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from nfi_backtest_engine import archive_security, cli, native_scorecard
from nfi_backtest_engine.errors import InputBoundaryError, SpecValidationError
from nfi_backtest_engine.reference.dependency_seal import safe_member
from nfi_backtest_engine.release_gate import _parse_checksum_manifest
from provenance_support import TEST_POLICY
from test_native_scorecard import _current_ref_proof, _scorecard_inputs


def test_reference_wheel_rejects_reserved_member() -> None:
    member = zipfile.ZipInfo("package/NUL.txt")
    member.external_attr = 0o100644 << 16

    with pytest.raises(ValueError, match="unsafe|portable"):
        safe_member(member)


def test_deterministic_archive_round_trip_preserves_nested_regular_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "native-score"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (source / "identity.json").write_bytes(b"identity\n")
    (nested / "score-evidence.json").write_bytes(b"evidence\n")
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"

    archive_security.create_deterministic_zip(source, first)
    (source / "identity.json").touch()
    archive_security.create_deterministic_zip(source, second)
    extracted = tmp_path / "extracted"
    archive_security.extract_validated_zip(first, extracted)

    assert first.read_bytes() == second.read_bytes()
    assert (extracted / "identity.json").read_bytes() == b"identity\n"
    assert (extracted / "nested/score-evidence.json").read_bytes() == b"evidence\n"


def test_release_score_archive_revalidates_as_an_untouched_closed_input_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        native_scorecard,
        "begin_packaged_semantic_registry_authorization",
        _current_ref_proof,
    )
    monkeypatch.setattr(
        native_scorecard,
        "finalize_packaged_semantic_registry_authorization",
        lambda _proof: None,
    )
    monkeypatch.setattr(
        native_scorecard,
        "require_fresh_current_ref_for_authorization",
        lambda _evidence, _identity, _operation: None,
    )
    source = tmp_path / "native-score"
    evidence, identity = _scorecard_inputs(source)
    original = {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }
    validation_report = tmp_path / "validation" / "score-report.json"
    validation_report.parent.mkdir()

    first = native_scorecard.evaluate_native_scorecard(
        evidence,
        expected_identity_path=identity,
        output_path=validation_report,
        provenance_policy=TEST_POLICY,
    )
    archive = tmp_path / "product-release" / "native-score.zip"
    archive.parent.mkdir()
    archive_security.create_deterministic_zip(source, archive)
    extracted = tmp_path / "installed-candidate" / "native-score"
    archive_security.extract_validated_zip(archive, extracted)
    installed_report = tmp_path / "installed-candidate-score-report.json"
    production_evaluator = native_scorecard.evaluate_native_scorecard

    def evaluate_with_test_policy(*args, **kwargs):
        kwargs["provenance_policy"] = TEST_POLICY
        return production_evaluator(*args, **kwargs)

    monkeypatch.setattr(
        native_scorecard,
        "evaluate_native_scorecard",
        evaluate_with_test_policy,
    )
    exit_code = cli.main(
        [
            "release",
            "score",
            "--evidence",
            str(extracted / "score-evidence.json"),
            "--identity",
            str(extracted / "identity.json"),
            "--output",
            str(installed_report),
        ]
    )
    second = json.loads(installed_report.read_text(encoding="utf-8"))
    archived = {
        path.relative_to(extracted).as_posix(): path.read_bytes()
        for path in extracted.rglob("*")
        if path.is_file()
    }

    assert exit_code == 0
    assert first["points_awarded"] == second["points_awarded"] == 10
    assert archived == original
    assert "score-report.json" not in archived
    with zipfile.ZipFile(archive) as release_archive:
        assert set(release_archive.namelist()) == set(original)


def test_validated_archive_extraction_rejects_parent_traversal(tmp_path: Path) -> None:
    archive_path = tmp_path / "score.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../score-evidence.json", b"tampered")

    with pytest.raises(InputBoundaryError, match="unsafe archive member path"):
        archive_security.extract_validated_zip(archive_path, tmp_path / "extracted")

    assert not (tmp_path / "score-evidence.json").exists()


def test_release_checksum_manifest_rejects_reserved_member(tmp_path: Path) -> None:
    manifest = tmp_path / "SHA256SUMS.txt"
    manifest.write_text(f"{'a' * 64}  NUL.txt\n", encoding="utf-8")

    with pytest.raises(SpecValidationError, match="invalid"):
        _parse_checksum_manifest(manifest)
