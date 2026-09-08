"""Compare captured official callback custom data with Native scheduler state.

The official audit groups samples by callback outcome. At each timestamp and trade,
select samples matching the Native open position's filled-order count, then require
their custom data to agree. Later terminal force-close samples are checked separately.
Closed positions are outside this supplemental stream; their trade surface has separate parity.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path


def read_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    return gzip.decompress(data) if path.suffix == ".gz" else data


def native_value(value):
    if not isinstance(value, float):
        return value
    decimal = Decimal(repr(value))
    if not decimal.is_finite():
        raise ValueError("unexpected non-finite custom state")
    if decimal == 0:
        return "0"
    rendered = format(decimal, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def compare(native_path: Path, official_path: Path, output: Path) -> dict:
    audit = json.loads(read_bytes(official_path))
    by_tick = defaultdict(list)
    for name in ("order_filled", "adjust_trade_position", "custom_exit"):
        for bucket in audit["callbacks"][name]["outcomes"]:
            assert bucket["signature"]["error"] is None
            for sample in bucket["samples"]:
                state = sample["after"]
                if state is not None:
                    by_tick[(sample["timestamp"], int(state["id"]))].append(state)
    native_stream, official_stream = [], []
    fields = set()
    for line in read_bytes(native_path).splitlines():
        event = json.loads(line)
        for trade in event["state"]["open_trades"]:
            key = (event["timestamp_ms"], trade["id"])
            candidates = by_tick[key]
            assert candidates, ("missing official observation", key)
            latest = len(trade["orders"])
            # Freqtrade's terminal force-close callback can share the final
            # candle timestamp. Compare the still-open scheduler boundary;
            # its later terminal fill is covered by the trade-surface fixture.
            later = [state for state in candidates if int(state["order_count"]) > latest]
            assert all(state["last_order_tag"] == "force_exit" for state in later), (
                "additional non-terminal official fill",
                key,
            )
            states = {
                json.dumps(state["custom_data"], sort_keys=True)
                for state in candidates
                if int(state["order_count"]) == latest
            }
            assert len(states) == 1, ("ambiguous official callback boundary", key)
            expected = json.loads(states.pop())
            assert set(trade["custom_data"]) <= set(expected), ("unaudited Native key", key)
            fields.update(expected)
            actual = {name: native_value(trade["custom_data"].get(name)) for name in expected}
            identity = {"timestamp_ms": key[0], "trade_id": key[1], "pair": trade["pair"]}
            official_stream.append(identity | {"custom_data": expected})
            native_stream.append(identity | {"custom_data": actual})

    def encode(stream):
        return b"".join(
            (
                json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
            ).encode()
            for row in stream
        )

    native_bytes, official_bytes = encode(native_stream), encode(official_stream)
    assert native_bytes == official_bytes, "custom-data streams differ at zero tolerance"
    output.mkdir(parents=True, exist_ok=True)
    for name, data in [("native", native_bytes), ("official", official_bytes)]:
        (output / f"{name}-custom-state.jsonl.gz").write_bytes(gzip.compress(data, mtime=0))
    result = {
        "equal": True,
        "snapshots": len(native_stream),
        "fields": sorted(fields),
        "stream_sha256": hashlib.sha256(native_bytes).hexdigest(),
        "native_input_sha256": hashlib.sha256(read_bytes(native_path)).hexdigest(),
        "official_input_sha256": hashlib.sha256(read_bytes(official_path)).hexdigest(),
        "scope": __doc__,
    }
    (output / "comparison.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--official", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare(args.native, args.official, args.output)
    print(
        json.dumps(
            {
                "equal": result["equal"],
                "snapshots": result["snapshots"],
                "stream_sha256": result["stream_sha256"],
            }
        )
    )
