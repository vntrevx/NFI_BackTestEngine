//! Deterministic global chronological portfolio simulator.
//!
//! Signals cross this boundary as complete arrays. The core never calls Python
//! per candle and never simulates pairs independently before merging results.

use std::str::FromStr;

use rust_decimal::prelude::ToPrimitive;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Normalized trade-surface contract understood by this workspace.
pub const TRADE_SURFACE_SCHEMA_VERSION: &str = "2.0.0";
/// Version of the simulator input/result contract.
pub const SIMULATOR_SCHEMA_VERSION: &str = "1.0.0";

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SimulationInput {
    pub schema_version: String,
    pub config: PortfolioConfig,
    pub pairs: Vec<PairSeries>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PortfolioConfig {
    pub starting_balance: f64,
    pub max_open_trades: usize,
    pub stake_amount: f64,
    pub fee_rate: f64,
    pub stoploss_ratio: f64,
    pub amount_step: f64,
    pub price_step: f64,
    #[serde(default)]
    pub custom_exit_after_ms: Option<i64>,
    #[serde(default)]
    pub adjustment_rule: Option<AdjustmentRule>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AdjustmentRule {
    pub profit_below: f64,
    pub stake_ratio: f64,
    pub max_adjustments: usize,
    pub tag: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PairSeries {
    pub pair: String,
    pub candles: Vec<Candle>,
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
    pub enter_long: Option<EntrySignal>,
    #[serde(default)]
    pub exit_long: Option<ExitSignal>,
    #[serde(default)]
    pub adjustment: Option<AdjustmentSignal>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EntrySignal {
    pub tag: Option<String>,
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

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SimulationResult {
    pub schema_version: &'static str,
    pub starting_balance: f64,
    pub final_balance: f64,
    pub profit_total_abs: f64,
    pub total_volume: f64,
    pub rejected_signals: u64,
    pub maximum_concurrent_trades: usize,
    pub trades: Vec<ClosedTrade>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SimulationEvent {
    pub timestamp_ms: i64,
    pub pair: String,
    pub state: SimulationState,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SimulationState {
    pub quote_free: f64,
    pub base_balances: Vec<AssetBalance>,
    pub open_trade_count: usize,
    pub realized_profit: f64,
    pub closed_trade_count: usize,
    pub rejected_signals: u64,
    pub trade_id_counter: u64,
    pub order_id_counter: usize,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct AssetBalance {
    pub currency: String,
    pub free: f64,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ClosedTrade {
    pub sequence: usize,
    pub id: u64,
    pub pair: String,
    pub open_timestamp_ms: i64,
    pub close_timestamp_ms: i64,
    pub open_rate: f64,
    pub close_rate: f64,
    pub amount: f64,
    pub stake_amount: f64,
    pub max_stake_amount: f64,
    pub entry_tag: Option<String>,
    pub exit_reason: String,
    pub fee_open: f64,
    pub fee_close: f64,
    pub profit_abs: f64,
    pub profit_ratio: f64,
    pub initial_stop_loss: f64,
    pub stop_loss: f64,
    pub minimum_rate: f64,
    pub maximum_rate: f64,
    pub orders: Vec<FilledOrder>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct FilledOrder {
    pub sequence: usize,
    pub side: OrderSide,
    pub is_entry: bool,
    pub filled_timestamp_ms: i64,
    pub amount: f64,
    pub price: f64,
    pub cost: f64,
    pub tag: Option<String>,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum OrderSide {
    Buy,
    Sell,
}

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
    #[error("pair {pair:?} candle {index} is not strictly chronological")]
    CandleOrder { pair: String, index: usize },
    #[error("pair {pair:?} candle {index} contains invalid OHLCV")]
    InvalidCandle { pair: String, index: usize },
    #[error("adjustment stake must be finite and positive at {pair:?} {timestamp_ms}")]
    InvalidAdjustment { pair: String, timestamp_ms: i64 },
}

#[derive(Debug, Clone)]
struct OpenTrade {
    id: u64,
    pair_index: usize,
    pair: String,
    open_timestamp_ms: i64,
    open_rate: f64,
    amount: f64,
    stake_amount: f64,
    max_stake_amount: f64,
    entry_cost_with_fees: f64,
    first_entry_cost_with_fees: f64,
    adjustment_count: usize,
    entry_tag: Option<String>,
    stop_loss: f64,
    minimum_rate: f64,
    maximum_rate: f64,
    orders: Vec<FilledOrder>,
}

/// Reports whether the compiled chronological simulator is present.
#[must_use]
pub const fn simulator_available() -> bool {
    true
}

/// Validate and run one global portfolio stream.
///
/// # Errors
///
/// Returns [`SimError`] when the version, configuration, candle ordering,
/// OHLCV values, or adjustment request cannot be represented exactly by this
/// supported simulator subset.
///
/// # Panics
///
/// Panics only if an internally created open trade points outside the already
/// validated immutable pair array. Public input cannot construct that state.
#[allow(clippy::too_many_lines)]
pub fn simulate(input: &SimulationInput) -> Result<SimulationResult, SimError> {
    simulate_with_observer(input, |_| {})
}

/// Run the simulator and stream one compact state projection after each
/// Freqtrade-visible pair candle. Freqtrade reserves the first row for shifted
/// signals and does expose the final row before its separate force-exit pass.
///
/// # Errors
///
/// Returns the same validation and semantic errors as [`simulate`].
///
/// # Panics
///
/// Has the same internal invariant boundary as [`simulate`].
#[allow(clippy::too_many_lines)]
pub fn simulate_with_observer<F>(
    input: &SimulationInput,
    mut observer: F,
) -> Result<SimulationResult, SimError>
where
    F: FnMut(&SimulationEvent),
{
    validate_input(input)?;
    let config = &input.config;
    let mut cursors = vec![0_usize; input.pairs.len()];
    let mut open_trades: Vec<OpenTrade> = Vec::new();
    let mut closed_trades = Vec::new();
    let mut available_balance = config.starting_balance;
    let mut rejected_signals = 0_u64;
    let mut next_trade_id = 1_u64;
    let mut maximum_concurrent_trades = 0_usize;

    while let Some(timestamp_ms) = next_timestamp(&input.pairs, &cursors) {
        for (pair_index, pair) in input.pairs.iter().enumerate() {
            let cursor = cursors[pair_index];
            let Some(candle) = pair.candles.get(cursor) else {
                continue;
            };
            if candle.timestamp_ms != timestamp_ms {
                continue;
            }

            let existing_trade_index = open_trades
                .iter()
                .position(|trade| trade.pair_index == pair_index);
            let opened_now = if candle.enter_long.is_some() && existing_trade_index.is_none() {
                if open_trades.len() >= config.max_open_trades {
                    rejected_signals += 1;
                    false
                } else if let Some(trade) = enter_trade(
                    pair_index,
                    &pair.pair,
                    candle,
                    config,
                    available_balance,
                    next_trade_id,
                ) {
                    next_trade_id += 1;
                    open_trades.push(trade);
                    maximum_concurrent_trades = maximum_concurrent_trades.max(open_trades.len());
                    available_balance =
                        wallet_free(config.starting_balance, &open_trades, &closed_trades);
                    true
                } else {
                    rejected_signals += 1;
                    false
                }
            } else {
                false
            };

            if !opened_now {
                if let Some(trade_index) = open_trades
                    .iter()
                    .position(|trade| trade.pair_index == pair_index)
                {
                    update_extrema(&mut open_trades[trade_index], candle);
                    if let Some(adjustment) = &candle.adjustment {
                        apply_adjustment(
                            &mut open_trades[trade_index],
                            candle,
                            adjustment,
                            config,
                            available_balance,
                        )?;
                        available_balance =
                            wallet_free(config.starting_balance, &open_trades, &closed_trades);
                    } else if let Some(adjustment) =
                        rule_adjustment(&open_trades[trade_index], candle, config)
                    {
                        apply_adjustment(
                            &mut open_trades[trade_index],
                            candle,
                            &adjustment,
                            config,
                            available_balance,
                        )?;
                        available_balance =
                            wallet_free(config.starting_balance, &open_trades, &closed_trades);
                    }
                    if let Some((close_rate, reason)) =
                        exit_decision(&open_trades[trade_index], candle, config)
                    {
                        let trade = open_trades.swap_remove(trade_index);
                        let (closed, _) = close_trade(
                            trade,
                            candle.timestamp_ms,
                            close_rate,
                            reason,
                            config.fee_rate,
                            closed_trades.len(),
                        );
                        closed_trades.push(closed);
                        available_balance =
                            wallet_free(config.starting_balance, &open_trades, &closed_trades);
                    }
                }
            }
            cursors[pair_index] += 1;
            if cursors[pair_index] > 1 {
                observer(&simulation_event(
                    candle.timestamp_ms,
                    &pair.pair,
                    available_balance,
                    &open_trades,
                    &closed_trades,
                    rejected_signals,
                    next_trade_id - 1,
                ));
            }
        }
    }

    for trade in open_trades {
        let last = input.pairs[trade.pair_index]
            .candles
            .last()
            .expect("validated non-empty candles");
        let (closed, _) = close_trade(
            trade,
            last.timestamp_ms,
            last.open,
            "force_exit".to_owned(),
            config.fee_rate,
            closed_trades.len(),
        );
        closed_trades.push(closed);
    }
    available_balance = wallet_free(config.starting_balance, &[], &closed_trades);
    closed_trades.sort_by_key(|trade| (trade.open_timestamp_ms, trade.id));
    for (sequence, trade) in closed_trades.iter_mut().enumerate() {
        trade.sequence = sequence;
    }
    let profit_total_abs = available_balance - config.starting_balance;
    let total_volume = closed_trades
        .iter()
        .flat_map(|trade| &trade.orders)
        .map(|order| order.cost)
        .sum();
    Ok(SimulationResult {
        schema_version: SIMULATOR_SCHEMA_VERSION,
        starting_balance: config.starting_balance,
        final_balance: available_balance,
        profit_total_abs,
        total_volume,
        rejected_signals,
        maximum_concurrent_trades,
        trades: closed_trades,
    })
}

fn simulation_event(
    timestamp_ms: i64,
    pair: &str,
    quote_free: f64,
    open_trades: &[OpenTrade],
    closed_trades: &[ClosedTrade],
    rejected_signals: u64,
    trade_id_counter: u64,
) -> SimulationEvent {
    let mut base_balances: Vec<AssetBalance> = open_trades
        .iter()
        .map(|trade| AssetBalance {
            currency: trade
                .pair
                .split_once('/')
                .map_or_else(|| trade.pair.clone(), |(base, _)| base.to_owned()),
            free: trade.amount,
        })
        .collect();
    base_balances.sort_by(|left, right| left.currency.cmp(&right.currency));
    let realized_profit = closed_trades.iter().map(|trade| trade.profit_abs).sum();
    let order_id_counter = closed_trades
        .iter()
        .map(|trade| trade.orders.len())
        .sum::<usize>()
        + open_trades
            .iter()
            .map(|trade| trade.orders.len())
            .sum::<usize>();
    SimulationEvent {
        timestamp_ms,
        pair: pair.to_owned(),
        state: SimulationState {
            quote_free,
            base_balances,
            open_trade_count: open_trades.len(),
            realized_profit,
            closed_trade_count: closed_trades.len(),
            rejected_signals,
            trade_id_counter,
            order_id_counter,
        },
    }
}

fn wallet_free(
    starting_balance: f64,
    open_trades: &[OpenTrade],
    closed_trades: &[ClosedTrade],
) -> f64 {
    let realized_profit = closed_trades
        .iter()
        .map(|trade| trade.profit_abs)
        .sum::<f64>();
    let tied_up_stake = open_trades
        .iter()
        .map(|trade| trade.stake_amount)
        .sum::<f64>();
    starting_balance + realized_profit - tied_up_stake
}

fn validate_input(input: &SimulationInput) -> Result<(), SimError> {
    if input.schema_version != SIMULATOR_SCHEMA_VERSION {
        return Err(SimError::UnsupportedSchema(input.schema_version.clone()));
    }
    let config = &input.config;
    for (name, value) in [
        ("starting_balance", config.starting_balance),
        ("stake_amount", config.stake_amount),
        ("amount_step", config.amount_step),
        ("price_step", config.price_step),
    ] {
        if !value.is_finite() || value <= 0.0 {
            return Err(SimError::InvalidPositiveConfig(name));
        }
    }
    if !config.fee_rate.is_finite() || config.fee_rate < 0.0 {
        return Err(SimError::InvalidPositiveConfig("fee_rate"));
    }
    if !config.stoploss_ratio.is_finite()
        || config.stoploss_ratio >= 0.0
        || config.stoploss_ratio <= -1.0
    {
        return Err(SimError::InvalidStoploss);
    }
    if config.max_open_trades == 0 {
        return Err(SimError::InvalidSlots);
    }
    if let Some(duration) = config.custom_exit_after_ms {
        if duration <= 0 {
            return Err(SimError::InvalidPositiveConfig("custom_exit_after_ms"));
        }
    }
    if let Some(rule) = &config.adjustment_rule {
        if !rule.profit_below.is_finite()
            || !rule.stake_ratio.is_finite()
            || rule.stake_ratio <= 0.0
            || rule.tag.is_empty()
        {
            return Err(SimError::InvalidPositiveConfig("adjustment_rule"));
        }
    }
    for (pair_index, pair) in input.pairs.iter().enumerate() {
        if pair.pair.is_empty() {
            return Err(SimError::EmptyPair(pair_index));
        }
        if pair.candles.is_empty() {
            return Err(SimError::EmptyCandles(pair.pair.clone()));
        }
        let mut previous = None;
        for (index, candle) in pair.candles.iter().enumerate() {
            if previous.is_some_and(|value| candle.timestamp_ms <= value) {
                return Err(SimError::CandleOrder {
                    pair: pair.pair.clone(),
                    index,
                });
            }
            previous = Some(candle.timestamp_ms);
            let values = [
                candle.open,
                candle.high,
                candle.low,
                candle.close,
                candle.volume,
            ];
            if candle.timestamp_ms < 0
                || values.iter().any(|value| !value.is_finite())
                || candle.open <= 0.0
                || candle.high < candle.low
                || candle.low <= 0.0
                || candle.volume < 0.0
            {
                return Err(SimError::InvalidCandle {
                    pair: pair.pair.clone(),
                    index,
                });
            }
        }
    }
    Ok(())
}

fn next_timestamp(pairs: &[PairSeries], cursors: &[usize]) -> Option<i64> {
    pairs
        .iter()
        .zip(cursors)
        .filter_map(|(pair, cursor)| pair.candles.get(*cursor))
        .map(|candle| candle.timestamp_ms)
        .min()
}

fn enter_trade(
    pair_index: usize,
    pair: &str,
    candle: &Candle,
    config: &PortfolioConfig,
    available_balance: f64,
    id: u64,
) -> Option<OpenTrade> {
    let requested = config.stake_amount.min(available_balance);
    let (amount, stake, precise_cost, order_cost) =
        entry_sizing(requested, candle.open, config.fee_rate, config.amount_step)?;
    let tag = candle
        .enter_long
        .as_ref()
        .and_then(|signal| signal.tag.clone());
    let order = FilledOrder {
        sequence: 0,
        side: OrderSide::Buy,
        is_entry: true,
        filled_timestamp_ms: candle.timestamp_ms,
        amount,
        price: candle.open,
        cost: order_cost,
        tag: tag.clone(),
    };
    Some(OpenTrade {
        id,
        pair_index,
        pair: pair.to_owned(),
        open_timestamp_ms: candle.timestamp_ms,
        open_rate: candle.open,
        amount,
        stake_amount: stake,
        max_stake_amount: stake,
        entry_cost_with_fees: precise_cost,
        first_entry_cost_with_fees: precise_cost,
        adjustment_count: 0,
        entry_tag: tag,
        stop_loss: ceil_step(
            candle.open * (1.0 + config.stoploss_ratio),
            config.price_step,
        ),
        minimum_rate: candle.low,
        maximum_rate: candle.high,
        orders: vec![order],
    })
}

fn entry_sizing(
    requested: f64,
    rate: f64,
    fee_rate: f64,
    amount_step: f64,
) -> Option<(f64, f64, f64, f64)> {
    let raw_amount = requested / (1.0 + fee_rate) / rate;
    let amount = floor_step(raw_amount, amount_step);
    if amount <= 0.0 {
        return None;
    }
    let stake = precise_product(&[amount, rate])?;
    let precise_cost = precise_product(&[amount, rate, 1.0 + fee_rate])?;
    let order_cost = (amount * rate) * (1.0 + fee_rate);
    Some((amount, stake, precise_cost, order_cost))
}

fn floor_step(value: f64, step: f64) -> f64 {
    let units = (value / step).floor();
    let inverse = (1.0 / step).round();
    if (inverse * step - 1.0).abs() < 1e-12 {
        units / inverse
    } else {
        units * step
    }
}

fn ceil_step(value: f64, step: f64) -> f64 {
    let inverse = (1.0 / step).round();
    if (inverse * step - 1.0).abs() < 1e-12 {
        (value * inverse).ceil() / inverse
    } else {
        (value / step).ceil() * step
    }
}

fn round_step(value: f64, step: f64) -> f64 {
    let inverse = (1.0 / step).round();
    if (inverse * step - 1.0).abs() < 1e-12 {
        (value * inverse).round() / inverse
    } else {
        (value / step).round() * step
    }
}

fn precise_product(values: &[f64]) -> Option<f64> {
    values
        .iter()
        .try_fold(Decimal::ONE, |product, value| {
            Decimal::from_str(&value.to_string())
                .ok()
                .map(|number| product * number)
        })?
        .to_f64()
}

fn precise_sum(values: &[f64]) -> Option<f64> {
    values
        .iter()
        .try_fold(Decimal::ZERO, |sum, value| {
            Decimal::from_str(&value.to_string())
                .ok()
                .map(|number| sum + number)
        })?
        .to_f64()
}

fn precise_quotient(numerator: f64, denominator: f64) -> Option<f64> {
    let numerator = Decimal::from_str(&numerator.to_string()).ok()?;
    let denominator = Decimal::from_str(&denominator.to_string()).ok()?;
    (numerator / denominator).to_f64()
}

fn update_extrema(trade: &mut OpenTrade, candle: &Candle) {
    trade.minimum_rate = trade.minimum_rate.min(candle.low);
    trade.maximum_rate = trade.maximum_rate.max(candle.high);
}

fn apply_adjustment(
    trade: &mut OpenTrade,
    candle: &Candle,
    adjustment: &AdjustmentSignal,
    config: &PortfolioConfig,
    available_balance: f64,
) -> Result<(), SimError> {
    if !adjustment.stake_amount.is_finite() || adjustment.stake_amount <= 0.0 {
        return Err(SimError::InvalidAdjustment {
            pair: trade.pair.clone(),
            timestamp_ms: candle.timestamp_ms,
        });
    }
    let requested = adjustment.stake_amount.min(available_balance);
    let Some((amount, stake, precise_cost, order_cost)) =
        entry_sizing(requested, candle.open, config.fee_rate, config.amount_step)
    else {
        return Ok(());
    };
    trade.amount = precise_sum(&[trade.amount, amount]).unwrap_or(trade.amount + amount);
    trade.stake_amount =
        precise_sum(&[trade.stake_amount, stake]).unwrap_or(trade.stake_amount + stake);
    trade.max_stake_amount = trade.max_stake_amount.max(trade.stake_amount);
    trade.entry_cost_with_fees = precise_sum(&[trade.entry_cost_with_fees, precise_cost])
        .unwrap_or(trade.entry_cost_with_fees + precise_cost);
    trade.adjustment_count += 1;
    trade.open_rate = round_step(
        precise_quotient(trade.stake_amount, trade.amount)
            .unwrap_or(trade.stake_amount / trade.amount),
        config.price_step,
    );
    trade.orders.push(FilledOrder {
        sequence: trade.orders.len(),
        side: OrderSide::Buy,
        is_entry: true,
        filled_timestamp_ms: candle.timestamp_ms,
        amount,
        price: candle.open,
        cost: order_cost,
        tag: Some(adjustment.tag.clone()),
    });
    Ok(())
}

fn rule_adjustment(
    trade: &OpenTrade,
    candle: &Candle,
    config: &PortfolioConfig,
) -> Option<AdjustmentSignal> {
    let rule = config.adjustment_rule.as_ref()?;
    if trade.adjustment_count >= rule.max_adjustments {
        return None;
    }
    let hypothetical_proceeds = trade.amount * candle.open * (1.0 - config.fee_rate);
    let current_profit =
        (hypothetical_proceeds - trade.entry_cost_with_fees) / trade.entry_cost_with_fees;
    (current_profit < rule.profit_below).then(|| AdjustmentSignal {
        stake_amount: trade.first_entry_cost_with_fees * rule.stake_ratio,
        tag: rule.tag.clone(),
    })
}

fn exit_decision(
    trade: &OpenTrade,
    candle: &Candle,
    config: &PortfolioConfig,
) -> Option<(f64, String)> {
    if candle.low <= trade.stop_loss {
        return Some((trade.stop_loss, "stop_loss".to_owned()));
    }
    if let Some(signal) = &candle.exit_long {
        return Some((candle.open, signal.reason.clone()));
    }
    config.custom_exit_after_ms.and_then(|duration| {
        (candle.timestamp_ms - trade.open_timestamp_ms >= duration)
            .then(|| (candle.open, "contract_timed_exit".to_owned()))
    })
}

fn close_trade(
    mut trade: OpenTrade,
    timestamp_ms: i64,
    rate: f64,
    reason: String,
    fee_rate: f64,
    sequence: usize,
) -> (ClosedTrade, f64) {
    let gross_proceeds = trade.amount * rate;
    let proceeds = precise_product(&[trade.amount, rate, 1.0 - fee_rate]).unwrap_or(gross_proceeds);
    let profit_abs = round_eight(proceeds - trade.entry_cost_with_fees);
    let profit_ratio = profit_abs / trade.entry_cost_with_fees;
    let wallet_proceeds = trade.stake_amount + profit_abs;
    trade.orders.push(FilledOrder {
        sequence: trade.orders.len(),
        side: OrderSide::Sell,
        is_entry: false,
        filled_timestamp_ms: timestamp_ms,
        amount: trade.amount,
        price: rate,
        cost: gross_proceeds * (1.0 + fee_rate),
        tag: Some(reason.clone()),
    });
    (
        ClosedTrade {
            sequence,
            id: trade.id,
            pair: trade.pair,
            open_timestamp_ms: trade.open_timestamp_ms,
            close_timestamp_ms: timestamp_ms,
            open_rate: trade.open_rate,
            close_rate: rate,
            amount: trade.amount,
            stake_amount: trade.stake_amount,
            max_stake_amount: trade.max_stake_amount,
            entry_tag: trade.entry_tag,
            exit_reason: reason,
            fee_open: fee_rate,
            fee_close: fee_rate,
            profit_abs,
            profit_ratio,
            initial_stop_loss: trade.stop_loss,
            stop_loss: trade.stop_loss,
            minimum_rate: trade.minimum_rate,
            maximum_rate: trade.maximum_rate,
            orders: trade.orders,
        },
        wallet_proceeds,
    )
}

fn round_eight(value: f64) -> f64 {
    (value * 100_000_000.0).round() / 100_000_000.0
}

#[cfg(test)]
mod tests {
    use super::*;

    fn candle(timestamp_ms: i64, open: f64, low: f64) -> Candle {
        Candle {
            timestamp_ms,
            open,
            high: open + 10.0,
            low,
            close: open,
            volume: 1.0,
            enter_long: None,
            exit_long: None,
            adjustment: None,
        }
    }

    fn config(max_open_trades: usize) -> PortfolioConfig {
        PortfolioConfig {
            starting_balance: 1_000.0,
            max_open_trades,
            stake_amount: 100.0,
            fee_rate: 0.001,
            stoploss_ratio: -0.01,
            amount_step: 0.00001,
            price_step: 0.01,
            custom_exit_after_ms: None,
            adjustment_rule: None,
        }
    }

    #[test]
    fn global_slot_competition_uses_pair_order() {
        let mut first = candle(1, 100.0, 100.0);
        first.enter_long = Some(EntrySignal {
            tag: Some("first".to_owned()),
        });
        let mut second = first.clone();
        second.enter_long = Some(EntrySignal {
            tag: Some("second".to_owned()),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(1),
            pairs: vec![
                PairSeries {
                    pair: "AAA/USDT".to_owned(),
                    candles: vec![first],
                },
                PairSeries {
                    pair: "BBB/USDT".to_owned(),
                    candles: vec![second],
                },
            ],
        };

        let result = simulate(&input).expect("valid simulation");

        assert_eq!(result.trades.len(), 1);
        assert_eq!(result.trades[0].pair, "AAA/USDT");
        assert_eq!(result.rejected_signals, 1);
    }

    #[test]
    fn entry_adjustment_stop_and_fees_are_accounted_in_order() {
        let mut entry = candle(1, 100.0, 99.5);
        entry.enter_long = Some(EntrySignal {
            tag: Some("entry".to_owned()),
        });
        let mut adjustment = candle(2, 99.5, 99.2);
        adjustment.adjustment = Some(AdjustmentSignal {
            stake_amount: 50.0,
            tag: "rebuy".to_owned(),
        });
        let stop = candle(3, 99.0, 98.0);
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(1),
            pairs: vec![PairSeries {
                pair: "AAA/USDT".to_owned(),
                candles: vec![entry, adjustment, stop],
            }],
        };

        let result = simulate(&input).expect("valid simulation");
        let trade = &result.trades[0];

        assert_eq!(trade.exit_reason, "stop_loss");
        assert_eq!(trade.orders.len(), 3);
        assert_eq!(trade.orders[1].tag.as_deref(), Some("rebuy"));
        assert!(trade.profit_abs < 0.0);
        assert!((trade.close_rate - 99.0).abs() < f64::EPSILON);
    }

    #[test]
    fn explicit_exit_is_filled_at_candle_open() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal { tag: None });
        let mut exit = candle(2, 105.0, 104.0);
        exit.exit_long = Some(ExitSignal {
            reason: "custom_exit".to_owned(),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(1),
            pairs: vec![PairSeries {
                pair: "AAA/USDT".to_owned(),
                candles: vec![entry, exit],
            }],
        };

        let result = simulate(&input).expect("valid simulation");

        assert!((result.trades[0].close_rate - 105.0).abs() < f64::EPSILON);
        assert_eq!(result.trades[0].exit_reason, "custom_exit");
        assert!(result.final_balance > result.starting_balance);
    }
}
