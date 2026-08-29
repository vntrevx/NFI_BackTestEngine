from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from nfi_backtest_engine.compatibility_qualification import qualify_compatibility
from nfi_backtest_engine.current_head_predicate import extract_changed_signal_clauses
from nfi_backtest_engine.current_head_qualification import (
    _validate_cross_mode_path_reuse,
    validate_current_head_qualification,
)
from nfi_backtest_engine.errors import SpecValidationError


def _strategy_source(clause_count: int) -> str:
    clauses = " & ".join(
        f"((score_{index} > {index}.0) | (fallback_{index} < -{index}.0))"
        for index in range(clause_count)
    )
    views = "\n".join(
        f"        score_{index} = np_view('SCORE_{index}')\n"
        f"        fallback_{index} = np_view('FALLBACK_{index}')"
        for index in range(clause_count)
    )
    return (
        "class Demo:\n"
        "    def populate_entry_trend(self, dataframe, metadata):\n"
        "        def np_view(name):\n"
        "            return dataframe[name].to_numpy(copy=False)\n"
        f"{views}\n"
        "        short_entry_condition_index = 665\n"
        "        short_entry_logic = []\n"
        "        if short_entry_condition_index == 665:\n"
        f"            short_entry_logic.append({clauses})\n"
        "        return dataframe\n"
    )


def _qualification(root: Path) -> dict[str, object]:
    artifacts = []
    lane_paths: dict[str, dict[str, str]] = {}
    sealed_roles: dict[str, dict[str, str]] = {}
    for mode in ("spot", "futures"):
        lane_paths[mode] = {}
        sealed_roles[mode] = {}
        for lane in ("official", "native"):
            for surface in ("signal_tag", "trade_surface", "full_state"):
                artifact = root / f"{lane}-{surface}-{mode}.json"
                if surface == "signal_tag":
                    value: object = {"signal": [0, 1]}
                elif surface == "trade_surface":
                    value = {"context": {"trading_mode": mode}}
                else:
                    value = [{"pair": f"PAIR-{mode}"}]
                artifact.write_text(json.dumps(value) + "\n", encoding="utf-8")
                artifacts.append(
                    {
                        "path": artifact.name,
                        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                    }
                )
                lane_paths[mode][f"{lane}_{surface}"] = artifact.name
        vector = root / f"native_vector-{mode}.json"
        vector.write_text(json.dumps({"vector": mode}) + "\n", encoding="utf-8")
        vector_digest = hashlib.sha256(vector.read_bytes()).hexdigest()
        role_documents = {
            "qualification": {"trading_mode": mode},
            "fixture_manifest": {
                "freqtrade": {"trading_mode": mode},
                "artifacts": {
                    "trade_surface": {
                        "sha256": hashlib.sha256(
                            (root / lane_paths[mode]["official_trade_surface"]).read_bytes()
                        ).hexdigest()
                    },
                    "state_projection": {"sha256": mode * 32},
                },
            },
            "native_input": {
                "config": {"is_futures": mode == "futures"},
                "pairs": [
                    {
                        "pair": f"PAIR-{mode}",
                        "can_short": mode == "futures",
                        "vector": {"sha256": vector_digest},
                    }
                ],
            },
            "native_result": {"result": mode},
            "native_vector": {"vector": mode},
        }
        for role, value in role_documents.items():
            artifact = root / f"{role}-{mode}.json"
            artifact.write_text(json.dumps(value) + "\n", encoding="utf-8")
            artifacts.append(
                {
                    "path": artifact.name,
                    "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                }
            )
            sealed_roles[mode][role] = artifact.name
    identity = {
        "upstream_commit": "a" * 40,
        "source_sha256": "b" * 64,
        "engine_commit": "c" * 40,
        "freqtrade_digest": "sha256:" + "d" * 64,
        "semantic_profile_sha256": "e" * 64,
    }
    document: dict[str, object] = {
        "schema_version": "current-head-qualification-v1",
        "identity": identity,
        "changed_target_ids": ["f" * 64],
        "artifacts": artifacts,
        "modes": {
            mode: {
                "trading_mode": mode,
                "verification_state": "quick_verified",
                "target_gaps": [],
                "unknown_compiler_constructs": [],
                "trade_surface_first_difference": None,
                "full_state_first_difference": None,
                "official": {
                    surface: lane_paths[mode][f"official_{surface}"]
                    for surface in ("signal_tag", "trade_surface", "full_state")
                },
                "native": {
                    surface: lane_paths[mode][f"native_{surface}"]
                    for surface in ("signal_tag", "trade_surface", "full_state")
                },
                **{role: path for role, path in sealed_roles[mode].items()},
                **{
                    f"{role}_sha256": hashlib.sha256((root / path).read_bytes()).hexdigest()
                    for role, path in sealed_roles[mode].items()
                },
            }
            for mode in ("spot", "futures")
        },
    }
    modes = document["modes"]
    assert isinstance(modes, dict)
    for mode_name in ("spot", "futures"):
        mode = modes[mode_name]
        assert isinstance(mode, dict)
        official = mode["official"]
        native = mode["native"]
        assert isinstance(official, dict) and isinstance(native, dict)
        roles = {
            "fixture_manifest": mode["fixture_manifest_sha256"],
            **{
                f"{lane_name}_{surface}": hashlib.sha256(
                    (root / lane[surface]).read_bytes()
                ).hexdigest()
                for lane_name, lane in (("official", official), ("native", native))
                for surface in ("signal_tag", "trade_surface", "full_state")
            },
            "native_input": mode["native_input_sha256"],
            "native_result": mode["native_result_sha256"],
            "native_vector": mode["native_vector_sha256"],
        }
        fixture = json.loads(
            (root / str(mode["fixture_manifest"])).read_text(encoding="utf-8")
        )
        qualification_path = root / str(mode["qualification"])
        qualification_path.write_text(
            json.dumps(
                {
                    "trading_mode": mode_name,
                    "mode_provenance": {
                        "roles": roles,
                        "native_run": {
                            "trading_mode": mode_name,
                            "input_sha256": roles["native_input"],
                            "result_sha256": roles["native_result"],
                            "vector_sha256": roles["native_vector"],
                            "trade_surface_sha256": roles["native_trade_surface"],
                            "full_state_sha256": roles["native_full_state"],
                            "official_full_state_sha256": roles["official_full_state"],
                            "fixture_projection_sha256": fixture["artifacts"][
                                "state_projection"
                            ]["sha256"],
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        qualification_digest = hashlib.sha256(qualification_path.read_bytes()).hexdigest()
        mode["qualification_sha256"] = qualification_digest
        next(
            item for item in artifacts if item["path"] == qualification_path.name
        )["sha256"] = qualification_digest
    document["fingerprint"] = hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return document


def test_extracts_all_41_added_clauses_with_resolved_columns_and_locations(
    tmp_path: Path,
) -> None:
    old_source = tmp_path / "old.py"
    new_source = tmp_path / "new.py"
    old_source.write_text(_strategy_source(3), encoding="utf-8")
    new_source.write_text(_strategy_source(44), encoding="utf-8")

    inventory = extract_changed_signal_clauses(
        old_source,
        new_source,
        class_name="Demo",
        signal=665,
    )

    assert len(inventory.clauses) == 41
    assert len(inventory.atomic_terms) == 82
    assert inventory.clauses[0].source.line > 0
    assert inventory.atomic_terms[0].column == "SCORE_3"
    assert inventory.atomic_terms[0].operator == "gt"
    assert inventory.atomic_terms[0].threshold == 3.0
    assert inventory.atomic_terms[-1].column == "FALLBACK_43"


def test_rejects_unsupported_changed_predicate_ast_with_source_location(
    tmp_path: Path,
) -> None:
    old_source = tmp_path / "old.py"
    new_source = tmp_path / "new.py"
    old_source.write_text(_strategy_source(3), encoding="utf-8")
    new_source.write_text(
        _strategy_source(44).replace(
            "((score_43 > 43.0) | (fallback_43 < -43.0))",
            "score_43.between(1.0, 2.0)",
        ),
        encoding="utf-8",
    )

    with pytest.raises(SpecValidationError, match=r"new\.py:\d+:\d+: unsupported"):
        extract_changed_signal_clauses(
            old_source,
            new_source,
            class_name="Demo",
            signal=665,
        )


def _identity(document: dict[str, object]) -> dict[str, str]:
    identity = document["identity"]
    assert isinstance(identity, dict)
    assert all(isinstance(key, str) and isinstance(value, str) for key, value in identity.items())
    return {str(key): str(value) for key, value in identity.items()}


def _reseal(document: dict[str, object]) -> None:
    document["fingerprint"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in document.items() if key != "fingerprint"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


@pytest.mark.parametrize(
    "field",
    [
        "upstream_commit",
        "source_sha256",
        "engine_commit",
        "freqtrade_digest",
        "semantic_profile_sha256",
    ],
)
def test_qualification_rejects_every_identity_mutation(tmp_path: Path, field: str) -> None:
    document = _qualification(tmp_path)
    expected = _identity(document)
    identity = document["identity"]
    assert isinstance(identity, dict)
    value = identity[field]
    assert isinstance(value, str)
    identity[field] = "0" * len(value)
    _reseal(document)

    with pytest.raises(SpecValidationError, match="identity"):
        validate_current_head_qualification(tmp_path, document, expected)


_CURRENT_HEAD_ROOT = Path(
    "benchmarks/evidence/m22/current-head-649890f7"
)


@pytest.mark.parametrize(
    "exchange",
    [
        "payload_preserving_anchor",
        "trade_and_full_state",
        "fixture_only",
        "native_input_and_result",
    ],
)
def test_qualification_rejects_nested_cross_mode_payloads(exchange: str) -> None:
    source = json.loads(
        (_CURRENT_HEAD_ROOT / "current-head-qualification.json").read_text(
            encoding="utf-8"
        )
    )
    document = deepcopy(source)
    modes = document["modes"]
    spot = modes["spot"]
    futures = modes["futures"]
    if exchange == "payload_preserving_anchor":
        spot_anchor = {
            field: deepcopy(spot[field])
            for field in ("trading_mode", "qualification", "qualification_sha256")
        }
        futures_anchor = {
            field: deepcopy(futures[field])
            for field in ("trading_mode", "qualification", "qualification_sha256")
        }
        modes["spot"], modes["futures"] = deepcopy(futures), deepcopy(spot)
        modes["spot"].update(spot_anchor)
        modes["futures"].update(futures_anchor)
    elif exchange == "trade_and_full_state":
        for lane in ("official", "native"):
            for surface in ("trade_surface", "full_state"):
                spot[lane][surface], futures[lane][surface] = (
                    futures[lane][surface],
                    spot[lane][surface],
                )
        spot["native_trade_surface_sha256"], futures["native_trade_surface_sha256"] = (
            futures["native_trade_surface_sha256"],
            spot["native_trade_surface_sha256"],
        )
    else:
        roles = (
            ("fixture_manifest",)
            if exchange == "fixture_only"
            else ("native_input", "native_result")
        )
        for role in roles:
            for field in (role, f"{role}_sha256"):
                spot[field], futures[field] = futures[field], spot[field]
    _reseal(document)

    expected_identity = source["identity"]
    assert isinstance(expected_identity, dict)
    with pytest.raises(SpecValidationError, match="mode provenance"):
        validate_current_head_qualification(
            _CURRENT_HEAD_ROOT,
            document,
            expected_identity,
        )


def test_qualification_accepts_byte_identical_cross_mode_signal_surface_exchange() -> None:
    source = json.loads(
        (_CURRENT_HEAD_ROOT / "current-head-qualification.json").read_text(
            encoding="utf-8"
        )
    )
    modes = source["modes"]
    for lane in ("official", "native"):
        spot_path = modes["spot"][lane]["signal_tag"]
        futures_path = modes["futures"][lane]["signal_tag"]
        assert (_CURRENT_HEAD_ROOT / spot_path).read_bytes() == (
            _CURRENT_HEAD_ROOT / futures_path
        ).read_bytes()
        modes["spot"][lane]["signal_tag"], modes["futures"][lane]["signal_tag"] = (
            futures_path,
            spot_path,
        )
    _reseal(source)
    expected_identity = source["identity"]
    assert isinstance(expected_identity, dict)

    validate_current_head_qualification(
        _CURRENT_HEAD_ROOT,
        source,
        expected_identity,
    )


def test_qualification_accepts_shared_mode_agnostic_signal_surface_path() -> None:
    source = json.loads(
        (_CURRENT_HEAD_ROOT / "current-head-qualification.json").read_text(
            encoding="utf-8"
        )
    )
    document = deepcopy(source)
    modes = document["modes"]
    assert isinstance(modes, dict)
    spot = modes["spot"]
    futures = modes["futures"]
    assert isinstance(spot, dict) and isinstance(futures, dict)
    spot_native = spot["native"]
    futures_native = futures["native"]
    assert isinstance(spot_native, dict) and isinstance(futures_native, dict)
    shared_path = spot_native["signal_tag"]
    assert isinstance(shared_path, str)
    assert (_CURRENT_HEAD_ROOT / shared_path).read_bytes() == (
        _CURRENT_HEAD_ROOT / str(futures_native["signal_tag"])
    ).read_bytes()

    futures_native["signal_tag"] = shared_path
    for mode in (spot, futures):
        qualification_path = _CURRENT_HEAD_ROOT / str(mode["qualification"])
        qualification = json.loads(qualification_path.read_text(encoding="utf-8"))
        roles = qualification["mode_provenance"]["roles"]
        assert roles["native_signal_tag"] == hashlib.sha256(
            (_CURRENT_HEAD_ROOT / shared_path).read_bytes()
        ).hexdigest()
    _reseal(document)
    expected_identity = source["identity"]
    assert isinstance(expected_identity, dict)

    validate_current_head_qualification(
        _CURRENT_HEAD_ROOT,
        document,
        expected_identity,
    )


@pytest.mark.parametrize(
    ("spot_role", "futures_role"),
    [
        ("fixture_manifest", "fixture_manifest"),
        ("native_trade_surface", "native_trade_surface"),
        ("native_full_state", "native_full_state"),
        ("native_input", "native_input"),
        ("native_result", "native_result"),
        ("native_vector", "native_vector"),
        ("qualification", "qualification"),
        ("native_signal_tag", "native_result"),
    ],
    ids=[
        "fixture",
        "trade",
        "full-state",
        "native-input",
        "native-result",
        "native-vector",
        "qualification",
        "mixed-signal-and-result",
    ],
)
def test_cross_mode_path_reuse_rejects_mode_bearing_and_mixed_roles(
    spot_role: str,
    futures_role: str,
) -> None:
    paths = {
        "spot": {"shared": {spot_role}},
        "futures": {"shared": {futures_role}},
    }

    with pytest.raises(SpecValidationError, match="cross-mode role path provenance"):
        _validate_cross_mode_path_reuse(paths)


def test_qualification_rejects_fully_crossed_mode_blocks(tmp_path: Path) -> None:
    document = _qualification(tmp_path)
    expected = _identity(document)
    modes = document["modes"]
    assert isinstance(modes, dict)
    modes["spot"], modes["futures"] = modes["futures"], modes["spot"]
    _reseal(document)

    with pytest.raises(SpecValidationError, match="trading mode"):
        validate_current_head_qualification(tmp_path, document, expected)


@pytest.mark.parametrize("value", [None, "futures"])
def test_qualification_rejects_missing_or_wrong_record_mode(
    tmp_path: Path,
    value: str | None,
) -> None:
    document = _qualification(tmp_path)
    expected = _identity(document)
    modes = document["modes"]
    assert isinstance(modes, dict)
    spot = modes["spot"]
    assert isinstance(spot, dict)
    if value is None:
        spot.pop("trading_mode")
    else:
        spot["trading_mode"] = value
    _reseal(document)

    with pytest.raises(SpecValidationError, match="trading mode"):
        validate_current_head_qualification(tmp_path, document, expected)


@pytest.mark.parametrize("field", ["qualification", "qualification_sha256"])
def test_qualification_rejects_missing_qualification_role(
    tmp_path: Path,
    field: str,
) -> None:
    document = _qualification(tmp_path)
    expected = _identity(document)
    modes = document["modes"]
    assert isinstance(modes, dict)
    spot = modes["spot"]
    assert isinstance(spot, dict)
    spot.pop(field)
    _reseal(document)

    with pytest.raises(SpecValidationError, match="qualification identity"):
        validate_current_head_qualification(tmp_path, document, expected)


def test_qualification_rejects_wrong_qualification_role_hash(tmp_path: Path) -> None:
    document = _qualification(tmp_path)
    expected = _identity(document)
    modes = document["modes"]
    assert isinstance(modes, dict)
    spot = modes["spot"]
    assert isinstance(spot, dict)
    spot["qualification_sha256"] = "0" * 64
    _reseal(document)

    with pytest.raises(SpecValidationError, match="qualification identity"):
        validate_current_head_qualification(tmp_path, document, expected)


def test_qualification_rejects_crossed_qualification_roles(tmp_path: Path) -> None:
    document = _qualification(tmp_path)
    expected = _identity(document)
    modes = document["modes"]
    assert isinstance(modes, dict)
    spot = modes["spot"]
    futures = modes["futures"]
    assert isinstance(spot, dict) and isinstance(futures, dict)
    for field in ("qualification", "qualification_sha256"):
        spot[field], futures[field] = futures[field], spot[field]
    _reseal(document)

    with pytest.raises(SpecValidationError, match="qualification trading mode"):
        validate_current_head_qualification(tmp_path, document, expected)


def test_qualification_rejects_swapped_qualification_artifacts_and_hashes(
    tmp_path: Path,
) -> None:
    document = _qualification(tmp_path)
    expected = _identity(document)
    spot_path = tmp_path / "qualification-spot.json"
    futures_path = tmp_path / "qualification-futures.json"
    spot_bytes = spot_path.read_bytes()
    futures_bytes = futures_path.read_bytes()
    spot_path.write_bytes(futures_bytes)
    futures_path.write_bytes(spot_bytes)
    artifacts = document["artifacts"]
    modes = document["modes"]
    assert isinstance(artifacts, list) and isinstance(modes, dict)
    for mode in ("spot", "futures"):
        relative = f"qualification-{mode}.json"
        digest = hashlib.sha256((tmp_path / relative).read_bytes()).hexdigest()
        record = next(
            item
            for item in artifacts
            if isinstance(item, dict) and item.get("path") == relative
        )
        record["sha256"] = digest
        mode_record = modes[mode]
        assert isinstance(mode_record, dict)
        mode_record["qualification_sha256"] = digest
    _reseal(document)

    with pytest.raises(SpecValidationError, match="qualification trading mode"):
        validate_current_head_qualification(tmp_path, document, expected)


def test_qualification_accepts_moved_mode_bound_qualification(tmp_path: Path) -> None:
    document = _qualification(tmp_path)
    expected = _identity(document)
    old_relative = "qualification-spot.json"
    new_relative = "sealed/opaque-role.json"
    destination = tmp_path / new_relative
    destination.parent.mkdir()
    (tmp_path / old_relative).rename(destination)
    artifacts = document["artifacts"]
    modes = document["modes"]
    assert isinstance(artifacts, list) and isinstance(modes, dict)
    record = next(
        item
        for item in artifacts
        if isinstance(item, dict) and item.get("path") == old_relative
    )
    record["path"] = new_relative
    spot = modes["spot"]
    assert isinstance(spot, dict)
    spot["qualification"] = new_relative
    _reseal(document)

    validate_current_head_qualification(tmp_path, document, expected)


def test_qualification_rejects_duplicate_cross_mode_qualification_role(
    tmp_path: Path,
) -> None:
    document = _qualification(tmp_path)
    expected = _identity(document)
    modes = document["modes"]
    assert isinstance(modes, dict)
    spot = modes["spot"]
    futures = modes["futures"]
    assert isinstance(spot, dict) and isinstance(futures, dict)
    futures["qualification"] = spot["qualification"]
    futures["qualification_sha256"] = spot["qualification_sha256"]
    _reseal(document)

    with pytest.raises(SpecValidationError, match="qualification trading mode"):
        validate_current_head_qualification(tmp_path, document, expected)


def test_qualification_rejects_native_result_identity_mutation(tmp_path: Path) -> None:
    document = _qualification(tmp_path)
    expected = _identity(document)
    modes = document["modes"]
    assert isinstance(modes, dict)
    futures = modes["futures"]
    assert isinstance(futures, dict)
    futures["native_result_sha256"] = "0" * 64
    _reseal(document)

    with pytest.raises(SpecValidationError, match="native_result"):
        validate_current_head_qualification(tmp_path, document, expected)


def test_qualification_rejects_artifact_and_fingerprint_mutation(tmp_path: Path) -> None:
    document = _qualification(tmp_path)
    expected = _identity(document)
    modes = document["modes"]
    assert isinstance(modes, dict)
    spot = modes["spot"]
    assert isinstance(spot, dict)
    official = spot["official"]
    assert isinstance(official, dict)
    signal_path = tmp_path / str(official["signal_tag"])
    original_signal = signal_path.read_text(encoding="utf-8")
    signal_path.write_text('{"signal":[1,1]}\n', encoding="utf-8")

    with pytest.raises(SpecValidationError, match="artifact"):
        validate_current_head_qualification(tmp_path, document, expected)

    signal_path.write_text(original_signal, encoding="utf-8")
    document["fingerprint"] = "0" * 64
    with pytest.raises(SpecValidationError, match="fingerprint"):
        validate_current_head_qualification(tmp_path, document, expected)


def test_compatibility_qualification_binds_trading_mode() -> None:
    compatibility = {
        "source": {"sha256": "a" * 64},
        "trading_mode": "futures",
        "native_compatible": True,
    }
    proof = {
        "complete": True,
        "changed_branch_reached": True,
        "trade_surface_exact": True,
        "full_state_exact": True,
    }

    qualification = qualify_compatibility(
        compatibility,
        {"classification": "vector-only"},
        branch_proof=proof,
    )

    assert qualification["trading_mode"] == "futures"
