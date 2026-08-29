from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from nfi_backtest_engine.archive_security import create_deterministic_zip
from nfi_backtest_engine.canonical import write_json
from nfi_backtest_engine.fixture import sha256_file
from nfi_backtest_engine.native_scorecard import (
    PRODUCT_CANDIDATE_CREATE_OPERATION,
    PRODUCT_STABLE_CREATE_OPERATION,
)
from nfi_backtest_engine.release_provenance import candidate_distribution_identity

ROOT = Path(__file__).parents[1]


@dataclass(frozen=True, slots=True)
class WorkflowCase:
    workflow_name: str
    report_name: str
    scenario: str
    valid: bool


@pytest.mark.parametrize(
    "workflow_name",
    ["publish-product-release-candidate.yml", "promote-product-release.yml"],
)
def test_product_score_steps_install_transaction_cleanup_before_extraction(
    workflow_name: str,
) -> None:
    workflow = (ROOT / ".github" / "workflows" / workflow_name).read_text(
        encoding="utf-8"
    )
    score_step = workflow[workflow.index("extract_validated_zip") - 1200 :]

    assert "trap cleanup_private_score EXIT" in score_step
    assert score_step.index("trap cleanup_private_score EXIT") < score_step.index(
        "extract_validated_zip"
    )
    assert ".native-score.extract-" in score_step
    assert '".${score_report}.stage-"' in score_step


@pytest.mark.parametrize(
    ("workflow_name", "operation", "tag_variable"),
    [
        (
            "publish-product-release-candidate.yml",
            PRODUCT_CANDIDATE_CREATE_OPERATION,
            "RELEASE_TAG",
        ),
        (
            "promote-product-release.yml",
            PRODUCT_STABLE_CREATE_OPERATION,
            "STABLE_TAG",
        ),
    ],
)
def test_product_publication_reauthorizes_immediately_before_release_create(
    workflow_name: str,
    operation: str,
    tag_variable: str,
) -> None:
    workflow = (ROOT / ".github" / "workflows" / workflow_name).read_text(
        encoding="utf-8"
    )
    publication = workflow[workflow.index("mapfile -d '' -t assets") :]

    assert workflow.count("nfi-bte release score") == 2
    assert publication.index("nfi-bte release score") < publication.index(
        "gh release create"
    )
    between = publication[
        publication.index("nfi-bte release score") : publication.index("gh release create")
    ]
    assert "--evidence candidate/native-score/score-evidence.json" in between
    assert "--identity candidate/native-score/identity.json" in between
    assert f'--operation "{operation}:${{{tag_variable}}}:$CANDIDATE_SHA"' in between


def test_product_candidate_and_stable_publication_operations_are_distinct() -> None:
    assert PRODUCT_CANDIDATE_CREATE_OPERATION != PRODUCT_STABLE_CREATE_OPERATION


CASES = [
    WorkflowCase(workflow, report, scenario, scenario == "success")
    for workflow, report in (
        ("publish-product-release-candidate.yml", ".publish-native-score-report.json"),
        ("promote-product-release.yml", ".promote-native-score-report.json"),
    )
    for scenario in (
        "candidate-mismatch",
        "malformed-archive",
        "malformed-evidence",
        "nine-point",
        "current-ref-failure",
        "evaluator-failure",
        "success",
    )
]


@pytest.mark.parametrize("case", CASES, ids=lambda case: f"{case.workflow_name}-{case.scenario}")
def test_product_score_step_cleans_transaction_private_state_on_every_exit(
    tmp_path: Path,
    case: WorkflowCase,
) -> None:
    workflow = (ROOT / ".github" / "workflows" / case.workflow_name).read_text(
        encoding="utf-8"
    )
    lines = workflow.splitlines()
    starts = [index for index, line in enumerate(lines) if "score_root=" in line]
    start = starts[0] if case.scenario == "candidate-mismatch" else starts[-1]
    end = next(
        index
        for index in range(start, len(lines))
        if "--latest" in lines[index] or "--prerelease" in lines[index]
    )
    script = "set -euo pipefail\n" + "\n".join(
        line[10:] for line in lines[start : end + 1]
    )

    candidate = tmp_path / "candidate"
    candidate.mkdir()
    wheel = candidate / "candidate-manylinux-x86_64.whl"
    wheel.write_bytes(b"wheel")
    source_distribution = candidate / "candidate.tar.gz"
    source_distribution.write_bytes(b"sdist")
    candidate_id = candidate_distribution_identity(
        {
            wheel.name: sha256_file(wheel),
            source_distribution.name: sha256_file(source_distribution),
        }
    )
    score_tree = tmp_path / "score-input"
    score_tree.mkdir()
    identity = candidate_id if case.scenario != "candidate-mismatch" else "0" * 64
    write_json(score_tree / "identity.json", {"engine_artifact_sha256": identity})
    (score_tree / "score-evidence.json").write_text(
        "malformed" if case.scenario == "malformed-evidence" else "{}",
        encoding="utf-8",
    )
    archive = candidate / "native-score.zip"
    if case.scenario == "malformed-archive":
        archive.write_bytes(b"not-a-zip")
    else:
        create_deterministic_zip(score_tree, archive)

    venv_name = (
        ".publish-venv/bin"
        if case.report_name.startswith(".publish")
        else ".promote-venv/bin"
    )
    venv_bin = tmp_path / venv_name
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text(
        f'#!/bin/bash\nexec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    (venv_bin / "python").chmod(0o755)
    score_cli = venv_bin / "nfi-bte"
    score_cli.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "case \"${NFI_SCORE_SCENARIO}\" in\n"
        "  malformed-evidence) grep -q '^{}$' \"$4\" ;;\n"
        "  nine-point|current-ref-failure|evaluator-failure) "
        f'touch "{case.report_name}" ".{case.report_name}.stage-test"; exit 1 ;;\n'
        "esac\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = --output ]; then shift; printf '{}\\n' > \"$1\"; fi\n"
        "  shift\n"
        "done\n",
        encoding="utf-8",
    )
    score_cli.chmod(0o755)
    command_bin = tmp_path / "bin"
    command_bin.mkdir()
    gh_cli = command_bin / "gh"
    gh_cli.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "touch external-command-invoked\n",
        encoding="utf-8",
    )
    gh_cli.chmod(0o755)
    unrelated = candidate / "unrelated.txt"
    unrelated.write_text("preserve", encoding="utf-8")
    environment = os.environ.copy()
    environment.update(
        {
            "GITHUB_RUN_ID": "42",
            "GITHUB_RUN_ATTEMPT": "3",
            "NFI_SCORE_SCENARIO": case.scenario,
            "PYTHONPATH": str(ROOT / "python"),
            "PATH": f"{command_bin}:{os.environ['PATH']}",
            "GITHUB_REPOSITORY": "example/nfi",
            "RELEASE_TAG": "v1.0.0-rc.1",
            "STABLE_TAG": "v1.0.0",
            "CANDIDATE_SHA": "1" * 40,
            "RELEASE_NOTES": "release-notes.md",
        }
    )

    completed = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert (completed.returncode == 0) is case.valid, completed.stderr
    assert (tmp_path / "external-command-invoked").exists() is case.valid
    assert unrelated.read_text(encoding="utf-8") == "preserve"
    assert not (candidate / "native-score").exists()
    assert not list(candidate.glob(".native-score.extract-*"))
    assert not (tmp_path / case.report_name).exists()
    assert not list(tmp_path.glob(f".{case.report_name}.stage-*"))
