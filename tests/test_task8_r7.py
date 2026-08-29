from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from threading import Event, Thread

import pytest
from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.changed_signal_proof import (
    ChangedSignalIdentity,
    validate_changed_signal_proof,
)
from nfi_backtest_engine.changed_signal_trust import (
    expected_official_capture_attestation,
)
from nfi_backtest_engine.errors import SpecValidationError
from task8_trust_support import PROOF, SOURCE, attack_root, reseal


def _validate() -> None:
    document = read_json(PROOF)
    validate_changed_signal_proof(
        document,
        ChangedSignalIdentity(**document["identity"]),
    )


def test_promotion_rejects_synchronized_atomic_trusted_source_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a synchronized worker continuously atomically replaces the trusted path
    # with canonical bytes on fresh single-link regular inodes during promotion.
    root = attack_root(tmp_path, monkeypatch)
    source = root / SOURCE
    payload = source.read_bytes()
    started = Event()
    stop = Event()
    replacements: list[int] = []
    failures: list[OSError] = []

    def rotate() -> None:
        index = 0
        try:
            while not stop.is_set():
                candidate = source.with_name(f".{source.name}.rotation-{index % 3}")
                candidate.write_bytes(payload)
                os.replace(candidate, source)
                replacements.append(source.stat().st_ino)
                started.set()
                index += 1
        except OSError as exc:
            failures.append(exc)
            started.set()

    worker = Thread(target=rotate, name="task8-r7-source-rotation")
    worker.start()
    assert started.wait(timeout=10)
    try:
        # When / Then: promotion cannot mix path identities across separate reopens.
        with pytest.raises(SpecValidationError, match="snapshot|changed|identity|source"):
            _validate()
    finally:
        stop.set()
        worker.join(timeout=10)
    assert not worker.is_alive()
    assert not failures
    assert len(set(replacements)) >= 3


def test_promotion_rejects_git_config_parameters_injection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: Git's command-level config injection carries the verifier's exact override.
    attack_root(tmp_path, monkeypatch)
    monkeypatch.setenv(
        "GIT_CONFIG_PARAMETERS",
        "'core.alternateRefsCommand=echo attacker'",
    )

    # When / Then: the original caller environment fails typed before sanitization.
    with pytest.raises(SpecValidationError, match="environment|config|Git"):
        _validate()


@pytest.mark.parametrize("attack", ["missing", "altered", "crossed", "resealed"])
def test_promotion_rejects_invalid_official_mutant_capture_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    # Given: all 24 official records and every attacker-controlled proof digest are
    # resealed around missing, altered, or cross-mode capture attestation.
    root = attack_root(tmp_path, monkeypatch)
    document = read_json(PROOF)
    for mode in ("spot", "futures"):
        for record in document["modes"][mode]["mutations"]:
            artifact = record["official_output"]
            path = root / artifact["path"]
            output = json.loads(path.read_text(encoding="utf-8"))
            if attack == "missing":
                output.pop("capture_contract")
            elif attack == "altered":
                output["capture_contract"]["sha256"] = "0" * 64
            elif attack == "crossed":
                other = "futures" if mode == "spot" else "spot"
                output["capture_contract"] = expected_official_capture_attestation(other)
            else:
                output["capture_contract"] = {
                    "version": "attacker-v1",
                    "path": "attacker.py",
                    "sha256": "a" * 64,
                    "command": ["python", "attacker.py", mode],
                }
            path.write_text(json.dumps(output, sort_keys=True, separators=(",", ":")) + "\n")
            artifact["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            artifact["bytes"] = path.stat().st_size
    reseal(document)

    # When / Then: stale official records cannot satisfy production promotion.
    with pytest.raises(SpecValidationError, match="capture|mutant|attestation"):
        validate_changed_signal_proof(
            document,
            ChangedSignalIdentity(**document["identity"]),
        )


@pytest.mark.parametrize(
    "variable",
    [
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_SYSTEM",
        "GIT_DIR",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_EXEC_PATH",
        "GIT_GRAFT_FILE",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_OBJECT_DIRECTORY",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_WORK_TREE",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
    ],
)
def test_promotion_rejects_nonempty_git_rewrite_environment_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    # Given: one installed-Git repository/object/config injection variable is active.
    attack_root(tmp_path, monkeypatch)
    monkeypatch.setenv(variable, "attacker")

    # When / Then: caller input is rejected before child environment construction.
    with pytest.raises(SpecValidationError, match="environment|config|Git"):
        _validate()


def test_promotion_accepts_unrelated_and_empty_git_environment_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: an unrelated variable is nonempty while an injection variable is empty.
    attack_root(tmp_path, monkeypatch)
    monkeypatch.setenv("TASK8_BENIGN_GIT_CONTROL", "present")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "")

    # When / Then: only active Git rewrite/config inputs are forbidden.
    _validate()
