from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.changed_signal_proof import (
    ChangedSignalIdentity,
    validate_changed_signal_proof,
)
from nfi_backtest_engine.changed_signal_role_binding import (
    resolve_replay_role_bindings,
    role_bindings_sha256,
)
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.state_trace import StateTraceWriter, read_state_trace
from task8_trust_support import attack_root, reseal_publication

ROOT = Path(__file__).resolve().parents[1]
PROOF = ROOT / "benchmarks/evidence/m22/current-x7-changed-signal-boundary.json"


def _document() -> dict[str, Any]:
    return read_json(PROOF)


def _canonical_sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _reseal(document: dict[str, Any]) -> None:
    for mode in document["modes"].values():
        for lane_name in ("official", "native"):
            provenance = mode[f"{lane_name}_provenance"]
            provenance["raw_output_sha256"] = _canonical_sha(
                [artifact["sha256"] for artifact in provenance["artifacts"]]
            )
            provenance["normalized_sha256"] = _canonical_sha(mode[lane_name])
    unsigned = {key: value for key, value in document.items() if key != "fingerprint"}
    document["fingerprint"] = _canonical_sha(unsigned)


def _validate(document: dict[str, Any]) -> None:
    validate_changed_signal_proof(document, ChangedSignalIdentity(**document["identity"]))


def _artifact(provenance: dict[str, Any], role: str) -> dict[str, Any]:
    return next(item for item in provenance["artifacts"] if item["role"] == role)


@pytest.mark.parametrize(
    ("mode", "roles"),
    [
        ("spot", ("candle_input",)),
        ("spot", ("market_input",)),
        ("futures", ("funding_input",)),
        ("futures", ("mark_input",)),
        ("futures", ("leverage_input",)),
        ("spot", ("source_input",)),
        ("spot", ("capture_input",)),
        ("futures", ("candle_input", "market_input", "funding_input")),
        ("futures", ("mark_input", "leverage_input", "source_input", "capture_input")),
    ],
)
def test_promotion_rejects_resealed_replay_role_substitution(
    mode: str,
    roles: tuple[str, ...],
) -> None:
    # Given: each selected role is resealed around real but unrelated config bytes.
    document = _document()
    provenance = document["modes"][mode]["official_provenance"]
    replacement = _artifact(provenance, "config_input")
    for role in roles:
        artifact = _artifact(provenance, role)
        artifact["path"] = replacement["path"]
        artifact["sha256"] = replacement["sha256"]
        artifact["bytes"] = replacement["bytes"]
    _reseal(document)

    # When / Then: promotion proves the consumed role, not merely its self-hash.
    with pytest.raises(SpecValidationError, match="role|replay|source|capture"):
        _validate(document)


def _clean_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = attack_root(tmp_path, monkeypatch)
    return root / "benchmarks/evidence/m22/current-x7-raw"


def _rewrite_trace(path: Path, transform: str) -> None:
    trace = read_state_trace(path)
    header = trace.header
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    events = list(trace.events)
    if transform == "raw-order":
        events[0], events[1] = events[1], events[0]
    with StateTraceWriter(
        temporary,
        source=header["source"],
        run_id=header["run_id"],
        input_sha256=header["input_sha256"],
        strategy_sha256=header["strategy_sha256"],
        profile_sha256=header["profile_sha256"],
        trading_mode=header["trading_mode"],
        include_state=True,
    ) as writer:
        for event in events:
            state = deepcopy(event["state"])
            if transform == "raw-wallet":
                quote = state["wallets"]["USDT"]
                quote[1] = str(Decimal(str(quote[1])) + 1)
            elif transform == "projection-wallet":
                state["quote_free"] = str(Decimal(state["quote_free"]) + 1)
            writer.append(
                timestamp_ms=event["timestamp_ms"],
                phase=event["phase"],
                pair=event["pair"],
                callback=event["callback"],
                state=state,
            )
    temporary.replace(path)


@pytest.mark.parametrize("raw_attack", ["raw-wallet", "raw-order"])
def test_promotion_reconstructs_official_state_from_raw_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_attack: str,
) -> None:
    # Given: the authenticated raw trace is changed and the manifest is fully resealed,
    # while the normalized projection and proof rows retain the original state.
    replay_root = _clean_root(tmp_path, monkeypatch) / "spot/replay"
    raw_trace = replay_root / "artifacts/state-trace.nfitrace"
    _rewrite_trace(raw_trace, raw_attack)
    manifest_path = replay_root / "manifest.json"
    manifest = read_json(manifest_path)
    manifest["artifacts"]["state_trace"].update(
        sha256=hashlib.sha256(raw_trace.read_bytes()).hexdigest(),
        bytes=raw_trace.stat().st_size,
    )
    write_json(manifest_path, manifest)
    document = _document()
    for lane_name in ("official", "native"):
        replay = _artifact(document["modes"]["spot"][f"{lane_name}_provenance"], "replay_manifest")
        replay["sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        replay["bytes"] = manifest_path.stat().st_size
        bindings = resolve_replay_role_bindings(
            "spot", lane_name, manifest_path, tmp_path
        )
        document["modes"]["spot"][f"{lane_name}_provenance"][
            "role_bindings_sha256"
        ] = role_bindings_sha256(bindings)
    raw_artifact = _artifact(
        document["modes"]["spot"]["official_provenance"], "official_trace"
    )
    raw_artifact["sha256"] = hashlib.sha256(raw_trace.read_bytes()).hexdigest()
    raw_artifact["bytes"] = raw_trace.stat().st_size
    reseal_publication(document, tmp_path, ("spot", "official"))
    reseal_publication(document, tmp_path, ("spot", "native"))
    _reseal(document)

    # When / Then: resealing cannot detach canonical state from raw producer events.
    with pytest.raises(SpecValidationError, match="state|trace|reconstruct"):
        _validate(document)


def test_promotion_rejects_deleted_normalized_state_projection() -> None:
    # Given: raw producer traces remain authenticated but a derived cache is absent.
    document = _document()
    projection = _artifact(
        document["modes"]["spot"]["official_provenance"], "official_state"
    )
    projection["path"] = (
        "benchmarks/evidence/m22/current-x7-raw/spot/replay/artifacts/missing.nfitrace"
    )
    _reseal(document)

    # When / Then: the malformed evidence fails typed before any publication.
    with pytest.raises(SpecValidationError, match="path|projection|artifact"):
        _validate(document)


def test_promotion_rejects_resealed_native_raw_event_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: Native raw execution events are changed and completely resealed.
    replay_root = _clean_root(tmp_path, monkeypatch)
    events_path = replay_root / "futures/native-events.jsonl"
    lines = events_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["state"]["quote_free"] += 1
    lines[0] = json.dumps(first, sort_keys=True, separators=(",", ":"))
    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    document = _document()
    artifact = _artifact(
        document["modes"]["futures"]["native_provenance"], "native_events"
    )
    artifact["sha256"] = hashlib.sha256(events_path.read_bytes()).hexdigest()
    artifact["bytes"] = events_path.stat().st_size
    reseal_publication(document, tmp_path, ("futures", "native"))
    _reseal(document)

    # When / Then: reconstructed Native state no longer matches official state.
    with pytest.raises(SpecValidationError, match="state|event|reconstruct"):
        _validate(document)


def test_promotion_rejects_resealed_normalized_state_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: both stored projections and proof rows are changed in lockstep while
    # the authenticated official raw trace and Native execution remain unchanged.
    replay_root = _clean_root(tmp_path, monkeypatch)
    document = _document()
    mode = document["modes"]["futures"]
    official_path = replay_root / "futures/replay/artifacts/state-projection.nfitrace"
    native_path = replay_root / "futures/native-state.nfitrace"
    _rewrite_trace(official_path, "projection-wallet")
    _rewrite_trace(native_path, "projection-wallet")
    for lane_name, path in (("official", official_path), ("native", native_path)):
        provenance = mode[f"{lane_name}_provenance"]
        artifact = _artifact(provenance, f"{lane_name}_state")
        artifact["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        artifact["bytes"] = path.stat().st_size
        for row in mode[lane_name]["full_state"]:
            row["wallet"]["quote_free"] = str(
                Decimal(row["wallet"]["quote_free"]) + 1
            )
    reseal_publication(document, tmp_path, ("futures", "native"))
    _reseal(document)

    # When / Then: normalized views are caches and cannot become authority by resealing.
    with pytest.raises(SpecValidationError, match="state|projection|reconstruct"):
        _validate(document)
