"""Stable callback-lowering contract identity."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | Sequence[JsonValue] | Mapping[str, JsonValue]
type JsonObject = dict[str, JsonValue]

CALLBACK_LOWERING_VERSION = "1.10.0"
