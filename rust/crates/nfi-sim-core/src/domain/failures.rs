//! Fail-closed simulator errors.

use thiserror::Error;

#[derive(Debug, Error, PartialEq)]
pub enum SimError {
    #[error("unsupported simulator schema {0:?}")]
    UnsupportedSchema(String),
    #[error("configuration field {0} must be finite and positive")]
    InvalidPositiveConfig(&'static str),
    #[error("stoploss_ratio must be finite, negative, and greater than -1")]
    InvalidStoploss,
    #[error("max_open_trades must be greater than zero")]
    InvalidSlots,
    #[error("pair at index {0} is empty")]
    EmptyPair(usize),
    #[error("pair {0:?} has no candles")]
    EmptyCandles(String),
    #[error("pair {pair:?} execution_start_index {index} is outside its {rows} candle rows")]
    InvalidExecutionStart {
        pair: String,
        index: usize,
        rows: usize,
    },
    #[error("pair {pair:?} candle {index} is not strictly chronological")]
    CandleOrder { pair: String, index: usize },
    #[error("pair {pair:?} candle {index} contains invalid OHLCV")]
    InvalidCandle { pair: String, index: usize },
    #[error("pair {pair:?} feature column {column:?} is empty, misaligned, or non-numeric")]
    InvalidFeatureColumn { pair: String, column: String },
    #[error("adjustment stake must be finite, non-zero, and smaller than the position when negative at {pair:?} {timestamp_ms}")]
    InvalidAdjustment { pair: String, timestamp_ms: i64 },
    #[error("entry leverage must be finite and positive at {pair:?} {timestamp_ms}")]
    InvalidLeverage { pair: String, timestamp_ms: i64 },
    #[error("liquidation price must be finite and positive at {pair:?} {timestamp_ms}")]
    InvalidLiquidationPrice { pair: String, timestamp_ms: i64 },
    #[error("callback program contains an invalid key, tag, or value")]
    InvalidCallbackProgram,
    #[error("generic state-machine program or runtime value is invalid")]
    InvalidStateMachineProgram,
    #[error("compiled custom stake program is invalid for {pair:?} at {timestamp_ms}")]
    InvalidStakeProgram { pair: String, timestamp_ms: i64 },
    #[error("compiled entry confirmation program is invalid for {pair:?} at {timestamp_ms}")]
    InvalidEntryConfirmation { pair: String, timestamp_ms: i64 },
    #[error("compiled exit confirmation program is invalid for {pair:?} at {timestamp_ms}")]
    InvalidExitConfirmation { pair: String, timestamp_ms: i64 },
    #[error("compiled custom exit program is invalid for {pair:?} at {timestamp_ms}")]
    InvalidCustomExit { pair: String, timestamp_ms: i64 },
    #[error("compiled position adjustment program is invalid for {pair:?} at {timestamp_ms}")]
    InvalidPositionAdjustment { pair: String, timestamp_ms: i64 },
    #[error("NFI X7 trade manager configuration or scalar program is invalid")]
    InvalidNfiTradeManager,
    #[error("NFI X7 trade manager does not support entry tag {entry_tag:?} for {pair:?}")]
    UnsupportedNfiEntryTag { pair: String, entry_tag: String },
}
