"""Raw signal-capture validation for changed-signal producer lanes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .changed_signal_trust import expected_official_capture_attestation
from .errors import SpecValidationError


@dataclass(frozen=True, slots=True)
class RawSignalValidation:
    """Inputs required to derive one normalized signal/tag producer surface."""

    mode: str
    raw: Mapping[str, Any]
    lane: Mapping[str, Any]
    capture: Mapping[str, Any]
    official: bool


def validate_raw_signal(validation: RawSignalValidation) -> None:
    """Derive normalized signal/tag values from one raw producer capture."""
    mode = validation.mode
    raw = validation.raw
    lane = validation.lane
    capture = validation.capture
    official = validation.official
    if raw.get("trading_mode") != mode:
        raise SpecValidationError("changed signal raw mode differs")
    if official:
        if (
            raw.get("freqtrade_version") != capture["freqtrade_version"]
            or raw.get("interface_sha256") != capture["interface_sha256"]
            or raw.get("method_sha256") != capture["method_sha256"]
            or raw.get("call_order") != ["advise_entry", "advise_exit"]
            or raw.get("capture_contract")
            != expected_official_capture_attestation(mode)
        ):
            raise SpecValidationError("changed signal official interface execution differs")
        input_rows = json.loads(raw["input"])["data"]
        callback_input = {
            key: [row.get(key) for row in input_rows] for key in lane["callback_columns"]
        }
        if callback_input != lane["callback_columns"]:
            raise SpecValidationError("changed signal callback columns are not raw-derived")
        rows = json.loads(raw["output"])["data"]
        output = {key: [row.get(key) for row in rows] for key in lane["signal_tag"]}
    else:
        if raw.get("producer") != "nfi-vector-core":
            raise SpecValidationError("changed signal Native signal producer differs")
        output = {key: value["values"] for key, value in raw["output"].items()}
    if output != lane["signal_tag"]:
        raise SpecValidationError("changed signal normalized signal is not raw-derived")
