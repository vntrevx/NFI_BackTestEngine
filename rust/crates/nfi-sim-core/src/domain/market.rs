//! Pair, candle, signal, and precision contracts.

use std::collections::BTreeMap;

use serde::Deserialize;

use crate::io::{CandleSeries, FeatureColumn};

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PairSeries {
    pub pair: String,
    /// First candle processed by the chronological trading loop.
    ///
    /// Earlier rows are immutable analyzed context for callback lookbacks.
    /// Starting the cursor here preserves those features without allowing a
    /// pre-timerange signal to consume wallet balance or a portfolio slot.
    #[serde(default)]
    pub execution_start_index: usize,
    #[serde(default)]
    pub amount_step: Option<f64>,
    #[serde(default)]
    pub price_step: Option<f64>,
    /// Sparse historical tick-size changes derived from the OHLCV archive.
    ///
    /// Freqtrade freezes the tick size visible when a trade opens. A monthly
    /// change table preserves that behavior without repeating one number on
    /// every candle.
    #[serde(default)]
    pub price_steps: Vec<PriceStepChange>,
    #[serde(default)]
    pub minimum_stake: Option<f64>,
    #[serde(default)]
    pub minimum_amount: Option<f64>,
    #[serde(default)]
    pub minimum_cost: Option<f64>,
    /// Strategy-only scalar columns stored once per pair.
    ///
    /// Each vector is aligned 1:1 with `candles`. Keeping the column name out
    /// of every candle avoids the dominant JSON/memory overhead for NFI's
    /// dozens of informative-timeframe features.
    #[serde(default)]
    pub feature_columns: BTreeMap<String, FeatureColumn>,
    pub candles: CandleSeries,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PriceStepChange {
    pub timestamp_ms: i64,
    pub step: f64,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Candle {
    pub timestamp_ms: i64,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: f64,
    #[serde(default)]
    pub previous_close: Option<f64>,
    #[serde(default)]
    pub enter_long: Option<EntrySignal>,
    #[serde(default)]
    pub enter_short: Option<EntrySignal>,
    #[serde(default)]
    pub exit_long: Option<ExitSignal>,
    #[serde(default)]
    pub exit_short: Option<ExitSignal>,
    /// Funding rate charged at this candle timestamp.
    ///
    /// This is an event, not a value to forward-fill across base candles.
    /// Freqtrade multiplies it by the funding event's mark open and the
    /// position amount. `funding_mark_price` must therefore be present on the
    /// same candle whenever this field is present.
    #[serde(default)]
    pub funding_rate: Option<f64>,
    /// Mark-price open paired with `funding_rate`.
    #[serde(default)]
    pub funding_mark_price: Option<f64>,
    #[serde(default)]
    pub adjustment: Option<AdjustmentSignal>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EntrySignal {
    pub tag: Option<String>,
    #[serde(default)]
    pub leverage: Option<f64>,
    #[serde(default)]
    pub liquidation_price: Option<f64>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExitSignal {
    pub reason: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AdjustmentSignal {
    pub stake_amount: f64,
    pub tag: String,
}
