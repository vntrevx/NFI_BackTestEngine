from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def _module() -> ModuleType:
    path = ROOT / ".github/scripts/long_certification_contract.py"
    spec = importlib.util.spec_from_file_location("nfi_certification_identity_v2", path)
    if spec is None or spec.loader is None:
        raise AssertionError("long-certification contract module is not loadable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _inputs(tmp_path: Path) -> tuple[ModuleType, dict[str, object], Path, Path]:
    module = _module()
    contract = module.load_contract(ROOT / ".github/long-certification-contract.json")
    strategy = tmp_path / "strategy.py"
    selected_config = tmp_path / "config.json"
    release_lock = tmp_path / "release-lock.json"
    execution_profile = tmp_path / "execution-profile.json"
    engine_markets = tmp_path / "engine-markets.json"
    reference_markets = tmp_path / "reference-markets.json"
    probe = tmp_path / "probe.json"
    candidate_plan = tmp_path / "candidate-plan.json"
    oracle = tmp_path / "oracle"
    data = tmp_path / "data"
    wheel = tmp_path / "candidate.whl"
    oracle.mkdir()
    data.mkdir()
    strategy.write_text("class Strategy: pass\n", encoding="utf-8")
    wheel.write_bytes(b"candidate-one")
    _write(selected_config, {"trading_mode": "spot"})
    _write(execution_profile, {"hardware_fingerprint": "host"})
    _write(engine_markets, {"markets": {}})
    _write(reference_markets, {"markets": {}})
    _write(probe, {"fixture_id": "probe"})
    _write(
        candidate_plan,
        {
            "schema_version": "1.0.0",
            "certification_probes": {
                "modes": [
                    {
                        "slug": "spot",
                        "required_manifests": 1,
                        "manifests": [
                            {
                                "manifest": probe.name,
                                "manifest_sha256": module.sha256_file(probe),
                            }
                        ],
                    }
                ]
            },
        },
    )
    _write(
        release_lock,
        {
            "identity_sha256": "a" * 64,
            "data": {"aggregate_sha256": "b" * 64},
            "reference": {"image_platform_digest": "sha256:" + "c" * 64},
        },
    )
    run = oracle / "run.json"
    _write(run, {"complete": True})
    config: dict[str, object] = {
        "schema_version": "2.0.0",
        "mode": "spot",
        "release_lock": str(release_lock),
        "execution_profile": str(execution_profile),
        "strategy": str(strategy),
        "strategy_class": "Strategy",
        "config": str(selected_config),
        "data_directory": str(data),
        "engine_markets": str(engine_markets),
        "reference_markets": str(reference_markets),
        "oracle_index": str(tmp_path / "oracle-index.json"),
        "oracle_input_fingerprint": "0" * 64,
        "host_lock": str(tmp_path / "certification.lock"),
        "state_probes": [str(probe)],
    }
    identity = module._input_identity(config, version="oracle-input-identity-v2")
    fingerprint = module.canonical_sha256(identity)
    config["oracle_input_fingerprint"] = fingerprint
    _write(
        Path(str(config["oracle_index"])),
        {
            "schema_version": "2.0.0",
            "oracles": [
                {
                    "mode": "spot",
                    "input_fingerprint": fingerprint,
                    "input_identity": identity,
                    "directory": str(oracle),
                    "run_json_sha256": module.sha256_file(run),
                    "tree_sha256": module.directory_tree_sha256(oracle),
                    "status": "exact_parity",
                    "immutable": True,
                }
            ],
        },
    )
    config_path = tmp_path / "certification-config.json"
    _write(config_path, config)
    arguments: dict[str, object] = {
        "contract": contract,
        "config_path": config_path,
        "release_candidate_plan_path": candidate_plan,
        "mode": "spot",
        "candidate_commit": "d" * 40,
        "candidate_wheel": wheel,
        "output_directory": tmp_path / "output",
        "executable": "/installed/nfi-bte",
        "resume": False,
    }
    return module, arguments, config_path, wheel


def test_v2_separates_oracle_input_from_candidate_identity(tmp_path: Path) -> None:
    module, arguments, _config, wheel = _inputs(tmp_path)
    first = module.build_plan(**arguments)

    wheel.write_bytes(b"candidate-two")
    second = module.build_plan(**arguments)

    assert first["oracle"]["input_fingerprint"] == second["oracle"]["input_fingerprint"]
    assert (
        first["candidate_certification"]["fingerprint"]
        != second["candidate_certification"]["fingerprint"]
    )


def test_v2_candidate_identity_tracks_commit_without_rerunning_oracle(tmp_path: Path) -> None:
    module, arguments, _config, _wheel = _inputs(tmp_path)
    first = module.build_plan(**arguments)
    arguments["candidate_commit"] = "e" * 40
    second = module.build_plan(**arguments)

    assert first["oracle"]["reused"] is True
    assert first["oracle"]["input_fingerprint"] == second["oracle"]["input_fingerprint"]
    assert (
        first["candidate_certification"]["fingerprint"]
        != second["candidate_certification"]["fingerprint"]
    )


def test_v1_oracle_index_remains_readable(tmp_path: Path) -> None:
    module, arguments, config_path, _wheel = _inputs(tmp_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["schema_version"] = "1.0.0"
    config["oracle_fingerprint"] = module.canonical_sha256(module._input_identity(config))
    config.pop("oracle_input_fingerprint")
    index_path = Path(config["oracle_index"])
    index = json.loads(index_path.read_text(encoding="utf-8"))
    record = index["oracles"][0]
    record["fingerprint"] = config["oracle_fingerprint"]
    record["identity"] = module._input_identity(config)
    record.pop("input_fingerprint")
    record.pop("input_identity")
    index["schema_version"] = "1.0.0"
    _write(index_path, index)
    _write(config_path, config)

    plan = module.build_plan(**arguments)

    assert plan["schema_version"] == "2.0.0"
    assert plan["oracle"]["input_fingerprint"] == config["oracle_fingerprint"]
