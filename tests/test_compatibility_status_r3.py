from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).parents[1]
SCHEMA = ROOT / "python/nfi_backtest_engine/schemas/compatibility-product-status-v1.schema.json"
CLI = ROOT / "scripts/compatibility_automation.py"
AUTOMATION = ROOT / "python/nfi_backtest_engine/compatibility_automation.py"


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ("{not-json", "malformed_identity"),
        ('{"schema_version":"1.1.0"', "malformed_identity"),
        ("[]", "malformed_identity"),
        ("null", "malformed_identity"),
        (None, "missing_identity"),
    ],
)
def test_cli_emits_typed_status_for_untrusted_identity_boundary(
    tmp_path: Path,
    payload: str | None,
    reason: str,
) -> None:
    # Given
    identity_path = tmp_path / "identity.json"
    if payload is not None:
        identity_path.write_text(payload, encoding="utf-8")
    decisions = tmp_path / "decisions"
    decisions.mkdir()
    output = tmp_path / "status.json"

    # When
    completed = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "--identity",
            str(identity_path),
            "--decision-dir",
            str(decisions),
            "--current-engine-sha",
            "b" * 40,
            "--current-upstream-sha",
            "a" * 40,
            "--workflow-execution",
            "succeeded",
            "--spot-discovery-execution",
            "skipped",
            "--futures-discovery-execution",
            "skipped",
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    # Then
    status = json.loads(output.read_text(encoding="utf-8"))
    Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8"))).validate(status)
    assert completed.returncode != 0
    assert status["identity"] is None
    assert status["product"] == {"state": "inconclusive", "reason": reason}
    assert status["required_status_passed"] is False
    assert "Traceback" not in completed.stderr
    assert "JSONDecodeError" not in completed.stderr


def test_compatibility_automation_has_no_size_suppression_and_fits_limit() -> None:
    text = AUTOMATION.read_text(encoding="utf-8")
    pure_lines = [
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert "SIZE_OK" not in text
    assert len(pure_lines) <= 250
