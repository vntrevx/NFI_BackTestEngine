"""Recheck scenario transformations using only sealed fixture sources."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path


def members(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    cls = next(node for node in ast.parse(path.read_text()).body if isinstance(node, ast.ClassDef))
    methods = {
        node.name: ast.dump(node, include_attributes=False)
        for node in cls.body
        if isinstance(node, ast.FunctionDef)
    }
    constants = {
        ast.unparse(node.targets[0]): ast.dump(node.value, include_attributes=False)
        for node in cls.body
        if isinstance(node, ast.Assign) and len(node.targets) == 1
    }
    return methods, constants


def main() -> None:
    root = Path(__file__).resolve().parents[3]
    scope = json.loads(Path(__file__).with_name("source-scope.json").read_text())
    captured = root / "benchmarks/fixtures/captured"
    donor = captured / "x8-original-futures-20220401-20220420-r1/inputs/strategy.py"
    assert hashlib.sha256(donor.read_bytes()).hexdigest() == scope["donor_sha256"]
    donor_methods, donor_constants = members(donor)
    for case in scope["scenarios"]:
        inputs = captured / f"x8-virtual-fees-{case['scenario']}-r1/inputs"
        source = inputs / "strategy.py"
        assert hashlib.sha256(source.read_bytes()).hexdigest() == case["source_sha256"]
        methods, constants = members(source)
        assert methods.keys() == donor_methods.keys()
        assert constants.keys() == donor_constants.keys()
        assert sorted(key for key in methods if methods[key] != donor_methods[key]) == sorted(
            case["changed_methods"]
        )
        assert sorted(key for key in constants if constants[key] != donor_constants[key]) == sorted(
            case["changed_class_constants"]
        )
        assert (
            json.loads((inputs / "config.json").read_text())["nfi_parameters"]
            == case["configuration"]
        )
    print(f"Verified {len(scope['scenarios'])} source/configuration scopes")


if __name__ == "__main__":
    main()
