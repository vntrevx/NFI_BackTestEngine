#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install --yes --no-install-recommends ca-certificates jq python3 python3-pip python3-venv

identity_dir=".platform-evidence/wsl-host"
mkdir -p "$identity_dir"
mapfile -t wheels < <(find dist -maxdepth 1 -type f -name '*.whl' | LC_ALL=C sort)
test "${#wheels[@]}" -eq 1
python3 -m venv .wsl-venv
.wsl-venv/bin/python -m pip install --disable-pip-version-check "${wheels[0]}"
.wsl-venv/bin/python - "$identity_dir/guest-identity.json" <<'PY'
import json
import os
import platform
import sys
from pathlib import Path

from nfi_backtest_engine.execution_platform import current_execution_platform_identity

output = Path(sys.argv[1])
os_release = {}
for raw_line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
    if "=" not in raw_line:
        continue
    key, value = raw_line.split("=", 1)
    os_release[key] = value.strip('"')
runtime_identity = current_execution_platform_identity()
if runtime_identity.get("wsl") is not True or runtime_identity.get("wsl_version") != 2:
    raise SystemExit("installed candidate did not recognize a genuine standard WSL2 guest")
document = {
    "schema_version": "wsl2-guest-identity-v1",
    "platform_slug": "windows-wsl2-x86_64",
    "architecture": platform.machine(),
    **runtime_identity,
    "kernel_version": platform.version(),
    "proc_version": Path("/proc/version").read_text(encoding="utf-8").strip(),
    "distribution": {
        "id": os_release.get("ID"),
        "version_id": os_release.get("VERSION_ID"),
        "pretty_name": os_release.get("PRETTY_NAME"),
    },
    "wsl_interop_present": bool(os.environ.get("WSL_INTEROP")),
}
output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
PY
jq -e '
  .schema_version == "wsl2-guest-identity-v1"
  and .platform_slug == "windows-wsl2-x86_64"
  and .architecture == "x86_64"
  and .wsl == true
  and .wsl_version == 2
  and .distribution.id == "ubuntu"
  and .distribution.version_id == "24.04"
' "$identity_dir/guest-identity.json" >/dev/null

.wsl-venv/bin/python .github/scripts/release_candidate_contract.py \
  --output .release-candidate-plan.json
runs="$(jq -er .platform_evidence.runs .release-candidate-plan.json)"
timeout="$(jq -er .platform_evidence.timeout_seconds .release-candidate-plan.json)"
while IFS=$'\t' read -r slug manifest; do
  .wsl-venv/bin/nfi-bte platform fixture-benchmark \
    "$manifest" \
    --wheel "${wheels[0]}" \
    --output-dir ".platform-evidence/$slug" \
    --runs "$runs" \
    --timeout "$timeout"
done < <(
  jq -er '.platform_evidence.modes[] | [.slug, .manifest] | @tsv' \
    .release-candidate-plan.json
)

(
  cd "$identity_dir"
  sha256sum guest-identity.json host-identity.json wsl-list-verbose.txt \
    wsl-status.txt wsl-version.txt > diagnostics.sha256
  sha256sum --check diagnostics.sha256
)
