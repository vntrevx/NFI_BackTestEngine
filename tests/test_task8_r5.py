from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.changed_signal_proof import (
    ChangedSignalIdentity,
    validate_changed_signal_proof,
)
from nfi_backtest_engine.errors import SpecValidationError
from task8_trust_support import (
    OFFICIAL_CAPTURE,
    PROOF,
    SOURCE,
    artifact,
    attack_root,
    baseline_bindings,
    isolated_git_attack_root,
    reseal,
    reseal_role,
)


@pytest.mark.parametrize("attack", ["source", "capture", "combined"])
def test_promotion_rejects_fully_resealed_trust_anchor_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    # Given: an evidence owner changes trusted implementation bytes and refreshes every
    # locally controlled artifact, role, aggregate, normalized, and proof identity.
    root = attack_root(tmp_path, monkeypatch)
    document = read_json(PROOF)
    baseline = baseline_bindings(root)
    if attack in {"source", "combined"}:
        source = root / SOURCE
        source.write_bytes(source.read_bytes() + b"\n# attacker-controlled source bytes\n")
        reseal_role(document, root, "source_input", baseline)
    if attack in {"capture", "combined"}:
        capture = root / OFFICIAL_CAPTURE
        capture.write_bytes(capture.read_bytes() + b"\n# attacker-controlled capture bytes\n")
        reseal_role(document, root, "capture_input", baseline)
    reseal(document)

    # When / Then: promotion must consult authority outside the resealed evidence root.
    with pytest.raises(SpecValidationError, match="source|capture|git|contract"):
        validate_changed_signal_proof(
            document,
            ChangedSignalIdentity(**document["identity"]),
        )


@pytest.mark.parametrize("role", ["source", "capture"])
def test_promotion_rejects_trusted_role_symlink_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    # Given: trusted bytes are unchanged but reached through an attacker-owned symlink.
    root = attack_root(tmp_path, monkeypatch)
    relative = SOURCE if role == "source" else OFFICIAL_CAPTURE
    path = root / relative
    target = path.with_suffix(f"{path.suffix}.real")
    path.replace(target)
    path.symlink_to(target.name)

    # When / Then: lexical path identity is part of the trust boundary.
    document = read_json(PROOF)
    with pytest.raises(SpecValidationError, match="alias|path|source|capture"):
        validate_changed_signal_proof(
            document,
            ChangedSignalIdentity(**document["identity"]),
        )


def test_promotion_rejects_different_valid_upstream_git_blob(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    historical_changed_signal_upstream: Path,
) -> None:
    # Given: the replay source is another valid blob from the same immutable commit,
    # and the evidence owner refreshes all local source identities.
    root = attack_root(tmp_path, monkeypatch)
    document = read_json(PROOF)
    baseline = baseline_bindings(root)
    alternate = subprocess.run(
        (
            "git",
            "-C",
            historical_changed_signal_upstream.as_posix(),
            "show",
            "eebaf97c1434bd8f208b7cd9c417606646e1e478:NostalgiaForInfinityX6.py",
        ),
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    (root / SOURCE).write_bytes(alternate)
    reseal_role(document, root, "source_input", baseline)
    reseal(document)

    # When / Then: repository membership is insufficient; commit/path/blob bytes are exact.
    with pytest.raises(SpecValidationError, match="source|blob|path"):
        validate_changed_signal_proof(
            document,
            ChangedSignalIdentity(**document["identity"]),
        )


@pytest.mark.parametrize("git_attack", ["wrong-repository", "shallow", "missing-object"])
def test_promotion_rejects_unverifiable_upstream_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    git_attack: str,
) -> None:
    # Given: the configured Git authority cannot prove the exact repository object.
    root = isolated_git_attack_root(tmp_path, monkeypatch)
    git_root = root / ".nfi/upstream-nfi"
    if git_attack == "wrong-repository":
        subprocess.run(
            (
                "git",
                "-C",
                git_root.as_posix(),
                "remote",
                "set-url",
                "origin",
                "https://example.invalid/attacker.git",
            ),
            check=True,
        )
    elif git_attack == "shallow":
        (git_root / ".git/shallow").write_text(
            "eebaf97c1434bd8f208b7cd9c417606646e1e478\n",
            encoding="ascii",
        )
    else:
        (git_root / ".git/objects").rename(git_root / ".git/objects.missing")

    # When / Then: validation fails typed before any publication boundary.
    document = read_json(PROOF)
    with pytest.raises(SpecValidationError, match="Git|repository|shallow|object"):
        validate_changed_signal_proof(
            document,
            ChangedSignalIdentity(**document["identity"]),
        )


@pytest.mark.parametrize("field", ["version", "sha256", "path", "command"])
def test_promotion_rejects_capture_execution_contract_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    # Given: command output is resealed around a capture contract claim not emitted by
    # the validator-owned implementation identity.
    root = attack_root(tmp_path, monkeypatch)
    document = read_json(PROOF)
    signal_path = root / "benchmarks/evidence/m22/current-x7-raw/spot/official-signal.json"
    signal = json.loads(signal_path.read_text(encoding="utf-8"))
    signal["capture_contract"][field] = ["attacker"] if field == "command" else "attacker"
    signal_path.write_text(json.dumps(signal, sort_keys=True, separators=(",", ":")) + "\n")
    provenance = document["modes"]["spot"]["official_provenance"]
    record = artifact(provenance, "official_signal")
    record["sha256"] = hashlib.sha256(signal_path.read_bytes()).hexdigest()
    record["bytes"] = signal_path.stat().st_size
    reseal(document)

    # When / Then: output metadata cannot redefine the executed capture contract.
    with pytest.raises(SpecValidationError, match="capture|interface|execution"):
        validate_changed_signal_proof(
            document,
            ChangedSignalIdentity(**document["identity"]),
        )
