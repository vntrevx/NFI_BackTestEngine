from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

mode, run_text, output_text = sys.argv[1:]
run = Path(run_text)
output = Path(output_text)
events = [
    json.loads(line.split("TASK14|", 1)[1])
    for line in (run / "stdout.log").read_text(encoding="utf-8").splitlines()
    if line.startswith("TASK14|")
]
selected = []
for event in events:
    before = event.get("before")
    after = event.get("after")
    if before is not None and after is not None:
        event["delta"] = {
            "trade": {
                key: {"before": before[key], "after": after[key]}
                for key in ("stake_amount", "entries", "exits")
                if before.get(key) != after.get(key)
            },
            "orders": {"before": before["orders"], "after": after["orders"]},
            "custom_state": {
                key: {"before": before[key], "after": after[key]}
                for key in ("system_version", "derisk_level_1")
                if before.get(key) != after.get(key)
            },
        }
    else:
        event["delta"] = {"trade": {}, "orders": None, "custom_state": {}}
    selected.append(event)
    if event["callback"] == "order_filled" and event["predicate"] == "sell":
        break

rollback = next(item for item in selected if item["predicate"] == "rollback_probe")
subsequent = selected[selected.index(rollback) + 1]
competition_times = {
    item["timestamp_ms"]
    for item in selected
    if item["callback"] == "custom_stoploss" and item["predicate"] == "competition"
} & {
    item["timestamp_ms"]
    for item in selected
    if item["callback"] == "custom_exit" and item["predicate"] == "eligible"
}
trace = {
    "schema_version": "task14-callback-trace-v1",
    "reference": {
        "version": "2026.5.1",
        "image_index_digest": "sha256:d47d7053dc07eca2ace20385575143090ba88621007e5e8b76052dca6038799a",
        "image_platform_digest": "sha256:bc5b7276118a8539d09ea797cb32c198d029a805815a29c6d27d5f610a3e0b6b",
    },
    "trading_mode": mode,
    "events": selected,
    "coverage": {
        "return_classes": ["accept", "none", "reject", "value"],
        "exception_handling": rollback["result"] == {"kind": "exception", "type": "ValueError"},
        "rollback_observed": (
            rollback["after"]["stake_amount"] == 1.0
            and subsequent["state"]["stake_amount"] == rollback["before"]["stake_amount"]
        ),
        "same_candle_competition_winner": "custom_exit" if competition_times else None,
        "state_visibility_observed": (
            rollback["after"]["derisk_level_1"] is True
            and subsequent["state"]["derisk_level_1"] is True
            and subsequent["state"]["system_version"] == "filled-visible"
        ),
        "entry_confirmation_reject_then_accept": [
            item["result"]["kind"]
            for item in selected
            if item["callback"] == "confirm_trade_entry"
        ][:2] == ["reject", "accept"],
        "startup_visibility": [
            {
                "timestamp_ms": item["timestamp_ms"],
                "visible_rows": item["visible_rows"],
                "last_visible_timestamp_ms": item["last_visible_timestamp_ms"],
            }
            for item in selected
            if item["callback"] == "bot_loop_start"
        ],
    },
}
(output / "callback-trace.json").write_text(
    json.dumps(trace, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
)
archive = next(run.glob("*.zip"))
with zipfile.ZipFile(archive) as zipped:
    result_name = next(
        name
        for name in zipped.namelist()
        if name.endswith(".json") and not name.endswith("_config.json") and not name.endswith(".meta.json")
    )
    result = json.loads(zipped.read(result_name))
strategy = result["strategy"]["Task14CallbackOracle"]
trade = strategy["trades"][0]
state = {
    "schema_version": "task14-official-trade-state-v1",
    "trading_mode": mode,
    "trade": trade,
    "result_state": {
        "final_balance": strategy["final_balance"],
        "rejected_signals": strategy["rejected_signals"],
        "timedout_entry_orders": strategy["timedout_entry_orders"],
        "timedout_exit_orders": strategy["timedout_exit_orders"],
    },
    "last_callback_state": selected[-1]["after"],
}
(output / "official-trade-state.json").write_text(
    json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
)
