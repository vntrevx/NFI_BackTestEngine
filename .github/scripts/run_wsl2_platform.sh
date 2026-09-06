#!/usr/bin/env bash
set -euo pipefail

export_wsl_evidence() {
  local original_status=$?
  trap - EXIT
  set +e
  local export_status=0 source relative target
  mkdir -p "$host_workspace/.platform-evidence" || export_status=$?
  shopt -s nullglob
  local -a evidence_files=(
    "$guest_workspace"/.platform-evidence/wsl-host/*
    "$guest_workspace"/.platform-evidence/*/platform-benchmark*.json
    "$guest_workspace"/.platform-evidence/*/warmup.stdout.log
    "$guest_workspace"/.platform-evidence/*/warmup.stderr.log
    "$guest_workspace"/.platform-evidence/*/warmup/run.json
    "$guest_workspace"/.platform-evidence/*/warmup/research/run.json
    "$guest_workspace"/.platform-evidence/*/measurements/*.stdout.log
    "$guest_workspace"/.platform-evidence/*/measurements/*.stderr.log
    "$guest_workspace"/.platform-evidence/*/measurements/run-*/run.json
    "$guest_workspace"/.platform-evidence/*/measurements/run-*/research/run.json
  )
  for source in "${evidence_files[@]}"; do
    relative="${source#"$guest_workspace"/}"
    target="$host_workspace/$relative"
    mkdir -p "$(dirname "$target")" || export_status=$?
    cp -- "$source" "$target" || export_status=$?
  done
  if (( original_status == 0 && export_status != 0 )); then
    original_status=$export_status
  fi
  exit "$original_status"
}

stage_path() {
  local relative=$1
  mkdir -p "$guest_workspace/$(dirname "$relative")"
  cp -- "$host_workspace/$relative" "$guest_workspace/$relative"
}

run_wsl2_platform() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install --yes --no-install-recommends \
    ca-certificates jq python3 python3-pip python3-venv util-linux

  host_workspace="$(pwd -P)"
  local host_identity_dir="$host_workspace/.platform-evidence/wsl-host"
  mkdir -p "$host_identity_dir"
  python3 .github/scripts/release_candidate_contract.py \
    --output "$host_workspace/.release-candidate-plan.json"
  local -a host_wheels
  mapfile -t host_wheels < <(find dist -maxdepth 1 -type f -name '*.whl' | LC_ALL=C sort)
  test "${#host_wheels[@]}" -eq 1

  guest_workspace="$(mktemp -d /tmp/nfi-wsl-platform.XXXXXX)"
  chmod 700 "$guest_workspace"
  trap export_wsl_evidence EXIT
  local guest_filesystem guest_mode guest_owner
  guest_filesystem="$(findmnt --noheadings --output FSTYPE --target "$guest_workspace" | xargs)"
  guest_mode="$(stat --format '%a' "$guest_workspace")"
  guest_owner="$(stat --format '%u' "$guest_workspace")"
  if [[ "$guest_workspace" != /tmp/nfi-wsl-platform.* \
    || "$guest_filesystem" != ext4 \
    || "$guest_mode" != 700 \
    || "$guest_owner" != "$(id -u)" ]]; then
    echo "WSL platform workspace is not an owner-private Linux ext4 directory" >&2
    return 1
  fi

  stage_path .release-candidate-plan.json
  stage_path "${host_wheels[0]}"
  while IFS= read -r manifest; do
    stage_path "$manifest"
    while IFS= read -r fixture_member; do
      stage_path "$(dirname "$manifest")/$fixture_member"
    done < <(jq -er '.inputs[].path, .artifacts[].path' "$host_workspace/$manifest")
  done < <(jq -er '.platform_evidence.modes[].manifest' .release-candidate-plan.json)
  mkdir -p "$guest_workspace/.platform-evidence/wsl-host"
  cp -a -- "$host_identity_dir/." "$guest_workspace/.platform-evidence/wsl-host/"

  cd "$guest_workspace"
  local identity_dir=".platform-evidence/wsl-host"
  {
    printf 'workspace=%s\n' "$guest_workspace"
    printf 'filesystem_type=%s\n' "$guest_filesystem"
    printf 'mode=%s\n' "$guest_mode"
    printf 'owner_uid=%s\n' "$guest_owner"
  } > "$identity_dir/guest-workspace-filesystem.txt"
  local -a wheels
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

  local runs timeout
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
    sha256sum guest-identity.json guest-workspace-filesystem.txt host-identity.json \
      wsl-list-verbose-pre-start.txt wsl-list-verbose.txt wsl-status.txt \
      wsl-version.txt > diagnostics.sha256
    sha256sum --check diagnostics.sha256
  )
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  run_wsl2_platform "$@"
fi
