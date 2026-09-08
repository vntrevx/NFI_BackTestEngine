//! Optional source-compiled exits before managed route dispatch.

use std::collections::BTreeMap;

use serde_json::{Map, Value};

use crate::calculations::fee_close;
use crate::callbacks::scalar_trade_value;
use crate::domain::{Candle, NfiX7TradeManager, PairSeries, PortfolioConfig};
use crate::execution::current_profit_ratio;
use crate::portfolio::OpenTrade;
use crate::scalar_vm::{evaluate_scalar_program_bundle, number_value};

use super::exit::CustomExitDecision;

pub(super) fn evaluate(
    manager: &NfiX7TradeManager,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
) -> Option<CustomExitDecision> {
    let prefix = manager.custom_exit_prefix.as_ref()?;
    let system = trade.custom_data.get("system_version")?.as_str()?;
    if system != manager.constants.system_name_use {
        return None;
    }
    let mut row = Map::new();
    for name in &prefix.optional_columns {
        if let Some(column) = pair.feature_columns.get(name) {
            row.insert(name.clone(), column.value(candle_index)?);
        }
    }
    let entries = trade
        .orders
        .iter()
        .filter(|order| order.is_entry)
        .map(|order| Some(serde_json::json!({"safe_price": number_value(order.price)?})))
        .collect::<Option<Vec<_>>>()?;
    let elapsed_ms = candle.timestamp_ms.checked_sub(trade.open_timestamp_ms)?;
    #[allow(clippy::cast_precision_loss)] // Same float conversion as timedelta.total_seconds().
    let elapsed_seconds = elapsed_ms as f64 / 1000.0;
    let values = BTreeMap::from([
        ("trade".to_owned(), scalar_trade_value(trade)?),
        ("current_time".to_owned(), number_value(elapsed_seconds)?),
        ("current_rate".to_owned(), number_value(candle.open)?),
        (
            "current_profit".to_owned(),
            number_value(current_profit_ratio(trade, candle.open, fee_close(config)))?,
        ),
        ("last_candle".to_owned(), Value::Object(row)),
        ("filled_entries".to_owned(), Value::Array(entries)),
        (
            "enter_tag".to_owned(),
            Value::String(
                trade
                    .entry_tag
                    .clone()
                    .unwrap_or_else(|| "empty".to_owned()),
            ),
        ),
        (
            "enter_tags".to_owned(),
            serde_json::to_value(trade.entry_tag_words()).ok()?,
        ),
        (
            "system_version".to_owned(),
            Value::String(system.to_owned()),
        ),
    ]);
    match evaluate_scalar_program_bundle(&prefix.bundle.programs, &prefix.bundle.entry, &values)? {
        Value::Null => Some(CustomExitDecision::NoExit),
        Value::String(reason) if !reason.is_empty() => Some(CustomExitDecision::Exit(reason)),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::ManagedCustomExitPrefix;

    #[test]
    fn source_prefix_preserves_priority_direction_missing_data_and_rounding() {
        let prefix: ManagedCustomExitPrefix = serde_json::from_str(include_str!(
            "../../../../../benchmarks/fixtures/source-contracts/x8-system-v4/prefix-program.json"
        ))
        .expect("source prefix fixture");
        let cases = [
            (
                false,
                70.0,
                50.0,
                1.0,
                -0.4,
                "192",
                Some("exit_bad_trade_broken_30pct"),
            ),
            (
                true,
                130.0,
                150.0,
                1.0,
                -0.4,
                "501",
                Some("exit_bad_trade_broken_30pct"),
            ),
            (
                false,
                74.5,
                50.0,
                1.0,
                -0.4,
                "1",
                Some("exit_bad_trade_broken_26pct"),
            ),
            (
                false,
                90.0,
                100.0,
                14.5,
                -0.1,
                "1",
                Some("exit_bad_trade_abandon_14d"),
            ),
            (
                true,
                110.0,
                100.0,
                15.5,
                -0.1,
                "501",
                Some("exit_bad_trade_abandon_16d"),
            ),
            (false, 90.0, 100.0, 13.99, -0.1, "1", None),
            (
                false,
                90.0,
                100.0,
                1.0,
                -0.3,
                "1 192",
                Some("exit_long_normal_stoploss_doom"),
            ),
            (false, 90.0, 100.0, 1.0, -0.3, "1921", None),
            (false, 70.0, 50.0, 20.0, 0.0, "192", None),
        ];
        for (short, rate, ema, days, profit, tag, reason) in cases {
            let values = inputs(short, rate, ema, days, profit, tag);
            let actual = evaluate_scalar_program_bundle(
                &prefix.bundle.programs,
                &prefix.bundle.entry,
                &values,
            );
            assert_eq!(
                actual,
                Some(reason.map_or(Value::Null, |reason| {
                    Value::String(format!("{reason} ( {tag})"))
                })),
                "{short} {rate} {days} {tag}"
            );
        }
        let mut values = inputs(false, 70.0, 50.0, 1.0, -0.4, "192");
        values.insert("last_candle".to_owned(), serde_json::json!({}));
        assert_eq!(
            evaluate_scalar_program_bundle(&prefix.bundle.programs, &prefix.bundle.entry, &values,),
            Some(serde_json::json!("exit_long_normal_stoploss_doom ( 192)"))
        );
        values.insert(
            "last_candle".to_owned(),
            serde_json::json!({
                "EMA_50_1d": {"$float": "nan"}, "EMA_200_1d": {"$float": "inf"},
                "RANGE_PCT_14_1d": {"$float": "nan"},
            }),
        );
        values.insert("enter_tag".to_owned(), serde_json::json!("1"));
        values.insert("enter_tags".to_owned(), serde_json::json!(["1"]));
        assert_eq!(
            evaluate_scalar_program_bundle(&prefix.bundle.programs, &prefix.bundle.entry, &values,),
            Some(Value::Null)
        );
    }

    fn inputs(
        short: bool,
        rate: f64,
        ema: f64,
        days: f64,
        profit: f64,
        tag: &str,
    ) -> BTreeMap<String, Value> {
        BTreeMap::from([
            (
                "trade".to_owned(),
                serde_json::json!({"is_short": short, "open_rate": 1.0}),
            ),
            ("current_time".to_owned(), serde_json::json!(days * 86400.0)),
            ("current_rate".to_owned(), serde_json::json!(rate)),
            ("current_profit".to_owned(), serde_json::json!(profit)),
            (
                "last_candle".to_owned(),
                serde_json::json!({
                    "EMA_50_1d": ema, "EMA_200_1d": 100.0, "RANGE_PCT_14_1d": 6.0,
                }),
            ),
            (
                "filled_entries".to_owned(),
                serde_json::json!([{"safe_price": 100.0}]),
            ),
            ("enter_tag".to_owned(), serde_json::json!(tag)),
            (
                "enter_tags".to_owned(),
                serde_json::json!(tag.split_whitespace().collect::<Vec<_>>()),
            ),
            ("system_version".to_owned(), serde_json::json!("system_v4")),
        ])
    }
}
