#!/bin/sh
#
# Install the latest NFI Backtest Engine release on Linux or Apple Silicon macOS.
# The release API supplies both the platform wheel URL and its SHA-256 digest. uv
# provides an isolated CLI environment and can fetch Python 3.12 when it is absent.

set -eu

repository="vntrevx/NFI_BackTestEngine"
version="${NFI_BTE_VERSION:-latest}"

case "$(uname -s):$(uname -m)" in
    Linux:x86_64)
        wheel_suffix="manylinux2014_x86_64.whl"
        ;;
    Linux:aarch64|Linux:arm64)
        wheel_suffix="manylinux2014_aarch64.whl"
        ;;
    Darwin:arm64)
        wheel_suffix="macosx_11_0_arm64.whl"
        ;;
    *)
        echo "Unsupported platform: $(uname -s) $(uname -m)" >&2
        exit 1
        ;;
esac

if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required to download the signed release assets." >&2
    exit 1
fi

if command -v uv >/dev/null 2>&1; then
    uv_bin="$(command -v uv)"
else
    echo "uv was not found; installing it from the official Astral installer..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    uv_bin="${HOME}/.local/bin/uv"
    if [ ! -x "$uv_bin" ]; then
        echo "uv installation completed but $uv_bin is not executable." >&2
        exit 1
    fi
fi

temporary_directory="$(mktemp -d "${TMPDIR:-/tmp}/nfi-bte-install.XXXXXX")"
trap 'rm -rf "$temporary_directory"' EXIT HUP INT TERM

# Use a uv-managed Python only as a portable JSON and SHA-256 helper. No project
# environment is created, and the downloaded wheel is still installed by uv tool.
wheel_path="$(
    "$uv_bin" run --no-project --python 3.12 python - \
        "$repository" "$version" "$wheel_suffix" "$temporary_directory" <<'PY'
import hashlib
import json
import pathlib
import sys
import urllib.parse
import urllib.request

repository, version, suffix, destination_text = sys.argv[1:]
if version == "latest":
    endpoint = f"https://api.github.com/repos/{repository}/releases/latest"
else:
    encoded = urllib.parse.quote(version, safe="")
    endpoint = f"https://api.github.com/repos/{repository}/releases/tags/{encoded}"
headers = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "nfi-backtest-engine-installer",
    "X-GitHub-Api-Version": "2022-11-28",
}
request = urllib.request.Request(endpoint, headers=headers)
with urllib.request.urlopen(request) as response:
    release = json.load(response)
assets = [asset for asset in release["assets"] if asset["name"].endswith(suffix)]
if len(assets) != 1:
    raise SystemExit(
        f"expected one {suffix} wheel in {release['tag_name']}; found {len(assets)}"
    )
asset = assets[0]
digest = asset.get("digest") or ""
if not digest.startswith("sha256:"):
    raise SystemExit(f"{asset['name']} has no published SHA-256 digest")
destination = pathlib.Path(destination_text, asset["name"])
download = urllib.request.Request(asset["browser_download_url"], headers=headers)
hasher = hashlib.sha256()
with urllib.request.urlopen(download) as response, destination.open("wb") as output:
    while chunk := response.read(1024 * 1024):
        output.write(chunk)
        hasher.update(chunk)
if hasher.hexdigest() != digest.removeprefix("sha256:"):
    raise SystemExit("downloaded wheel SHA-256 differs from the GitHub release digest")
print(destination)
PY
)"

if [ "${NFI_BTE_INSTALL_DRY_RUN:-0}" = "1" ]; then
    echo "verified=$wheel_path"
    exit 0
fi

"$uv_bin" tool install --force --python 3.12 "$wheel_path"
# This is safe to repeat and lets uv explain whether the current shell needs reopening.
"$uv_bin" tool update-shell || true
echo "Installed NFI Backtest Engine."
echo "Run: nfi-bte --version"
