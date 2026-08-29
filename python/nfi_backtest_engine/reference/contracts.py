"""Pinned Freqtrade reference identities and container scripts."""

from __future__ import annotations

from ..docker_runtime import BIND_OWNER_EXECUTABLE_FUNCTION

REFERENCE_VERSION = "2026.5.1"

REFERENCE_IMAGE = "freqtradeorg/freqtrade"

REFERENCE_INDEX_DIGEST = "sha256:d47d7053dc07eca2ace20385575143090ba88621007e5e8b76052dca6038799a"

REFERENCE_PLATFORM_DIGEST = (
    "sha256:bc5b7276118a8539d09ea797cb32c198d029a805815a29c6d27d5f610a3e0b6b"
)

REFERENCE_CONFIG_DIGEST = (
    "sha256:8615e1e2f8c429b27f57a0bcb948dfac1abe6828df8300c63ebd88a16ec6cabc"
)

# Docker's classic image store reports the config digest as ``.Id`` while the
# containerd image store reports the pinned platform-manifest digest. Both are
# immutable identities from the same digest-pinned manifest.
REFERENCE_DOCKER_IMAGE_IDS = frozenset(
    {
        REFERENCE_PLATFORM_DIGEST,
        REFERENCE_CONFIG_DIGEST,
    }
)

REFERENCE_PLATFORM = "linux/amd64"

REFERENCE_IMAGE_REF = f"{REFERENCE_IMAGE}@{REFERENCE_PLATFORM_DIGEST}"

REFERENCE_CCXT_VERSION = "4.5.55"

REFERENCE_BLAKE3_VERSION = "1.0.9"

# The tracer's only image-external dependency. The exact CPython 3.14,
# manylinux x86_64 wheel is selected for the pinned linux/amd64 Oracle image.
REFERENCE_DEPENDENCY_WHEELS = (
    (
        "blake3-1.0.9-cp314-cp314-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
        "https://files.pythonhosted.org/packages/e1/b8/1298806dd6c464a6f807df24c9640ad3bf27ee54ff4de82b2b5a823a8aba/"
        "blake3-1.0.9-cp314-cp314-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
        "f65d77eb05331495485048f6804f53885b192b998acb7e6fe1487d941bf08435",
    ),
)

REFERENCE_TRACER_VERSION = "1.1.0"

SUPPORTED_REFERENCE_TRACER_VERSIONS = frozenset({"1.0.0", REFERENCE_TRACER_VERSION})

REFERENCE_REPORT_VERSION = "1.1.0"

_BINANCE_TIER_EXPORT = """\
import json
import sys
from pathlib import Path

import freqtrade.exchange.binance as binance

source = Path(binance.__file__).with_name("binance_leverage_tiers.json")
with source.open(encoding="utf-8") as handle:
    available = json.load(handle)
pairs = sys.argv[1:]
missing = [pair for pair in pairs if pair not in available]
if missing:
    raise SystemExit("missing leverage tiers: " + ", ".join(missing))
print(
    json.dumps(
        {pair: available[pair] for pair in pairs},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
)
"""

_CGROUP_CAPTURE_SCRIPT = BIND_OWNER_EXECUTABLE_FUNCTION + """\
if [ -d /reference-deps ]; then
  /usr/local/bin/python \
    /nfi-python/nfi_backtest_engine/reference/dependency_seal.py \
    /reference-deps /nfi-deps/site \
    blake3-1.0.9-cp314-cp314-manylinux_2_17_x86_64.manylinux2014_x86_64.whl \
    f65d77eb05331495485048f6804f53885b192b998acb7e6fe1487d941bf08435 || exit 126
fi
run_as_bind_owner freqtrade "$@"
status=$?
if [ -r /sys/fs/cgroup/memory.peak ]; then
  cat /sys/fs/cgroup/memory.peak > /output/container-memory-peak.txt
elif [ -r /sys/fs/cgroup/memory/memory.max_usage_in_bytes ]; then
  cat /sys/fs/cgroup/memory/memory.max_usage_in_bytes > /output/container-memory-peak.txt
fi
if [ -r /sys/fs/cgroup/memory.events ]; then
  cat /sys/fs/cgroup/memory.events > /output/container-memory.events
fi
if [ -r /sys/fs/cgroup/memory.swap.current ]; then
  cat /sys/fs/cgroup/memory.swap.current > /output/container-memory-swap-current.txt
fi
if [ -r /sys/fs/cgroup/memory.swap.peak ]; then
  cat /sys/fs/cgroup/memory.swap.peak > /output/container-memory-swap-peak.txt
fi
if [ -r /sys/fs/cgroup/memory.swap.events ]; then
  cat /sys/fs/cgroup/memory.swap.events > /output/container-memory.swap.events
fi
if [ -r /sys/fs/cgroup/cpu.stat ]; then
  cat /sys/fs/cgroup/cpu.stat > /output/container-cpu.stat
fi
if [ -r /sys/fs/cgroup/io.stat ]; then
  cat /sys/fs/cgroup/io.stat > /output/container-io.stat
fi
exit "$status"
"""
