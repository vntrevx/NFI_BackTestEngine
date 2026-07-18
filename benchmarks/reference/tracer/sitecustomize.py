"""Activate the pinned Freqtrade reference tracer when explicitly requested."""

from __future__ import annotations

import os

if any(
    os.environ.get(name)
    for name in (
        "NFI_TRACE_PATH",
        "NFI_BTE_PROFILE_EVENTS",
        "NFI_MARKET_SNAPSHOT_PATH",
        "NFI_MARKET_CAPTURE_PATH",
    )
):
    from nfi_reference_trace import install_reference_tracer

    install_reference_tracer()
