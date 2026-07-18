"""Create a deterministic timerange slice from one Freqtrade Feather file."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import pyarrow.feather as feather


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    args = parser.parse_args()

    frame = pd.read_feather(args.source)
    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC")
    sliced = frame.loc[(frame["date"] >= start) & (frame["date"] < end)].reset_index(drop=True)
    if sliced.empty:
        raise SystemExit("requested Feather slice is empty")
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    feather.write_feather(sliced, args.destination, compression="uncompressed", version=2)
    print(
        f"wrote {len(sliced)} rows: {sliced.iloc[0]['date']} -> "
        f"{sliced.iloc[-1]['date']} ({args.destination})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
