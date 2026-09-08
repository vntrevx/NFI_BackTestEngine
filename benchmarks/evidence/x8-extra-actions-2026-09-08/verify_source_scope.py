"""Verify disclosed source transformations against the unchanged sealed X8 donor."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path


def members(path: Path):
    cls = next(n for n in ast.parse(path.read_text()).body if isinstance(n, ast.ClassDef))
    methods = {
        n.name: ast.dump(n, include_attributes=False)
        for n in cls.body
        if isinstance(n, ast.FunctionDef)
    }
    constants = {
        ast.unparse(n.targets[0]): ast.dump(n.value, include_attributes=False)
        for n in cls.body
        if isinstance(n, ast.Assign) and len(n.targets) == 1
    }
    return methods, constants


def main():
    root = Path(__file__).resolve().parents[3]
    captured = root / "benchmarks/fixtures/captured"
    donor = captured / "x8-original-futures-20220401-20220420-r1/inputs/strategy.py"
    donor_methods, donor_constants = members(donor)
    records = []
    for manifest in sorted(captured.glob("x8-extra-*-r1/manifest.json")):
        inputs = manifest.parent / "inputs"
        source = inputs / "strategy.py"
        methods, constants = members(source)
        assert methods.keys() == donor_methods.keys()
        assert constants == donor_constants, manifest
        changed = sorted(name for name in methods if methods[name] != donor_methods[name])
        assert set(changed) <= {"populate_entry_trend", "populate_indicators"}, (manifest, changed)
        config = json.loads((inputs / "config.json").read_text())
        records.append(
            {
                "fixture_id": manifest.parent.name,
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "changed_methods": changed,
                "changed_class_constants": [],
                "configuration": config.get("nfi_parameters", {}),
            }
        )
    output = {"donor_sha256": hashlib.sha256(donor.read_bytes()).hexdigest(), "scenarios": records}
    expected = Path(__file__).with_name("source-scope-final.json")
    if expected.exists():
        assert json.loads(expected.read_text()) == output
    else:
        expected.write_text(json.dumps(output, indent=2) + "\n")
    print("Verified", len(records), "sealed configuration scenarios")


if __name__ == "__main__":
    main()
