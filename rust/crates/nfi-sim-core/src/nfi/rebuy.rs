//! Exact system-v3 rebuy ladders for the bounded NFI X7 routes.
//!
//! Rebuy is not a parameterized grind level. X7 counts entry orders since the
//! latest exit, applies a dedicated loss threshold per count, and can sell
//! almost the whole trade at one de-risk boundary. Keeping this evaluator in a
//! separate module makes that order model explicit and prevents accidental
//! reuse of the more complex grind-cluster reconstruction.

use crate::calculations::{fee_close, fee_open};
use std::collections::BTreeMap;

use serde_json::Value;

use crate::callbacks::{
    feature_bool_at, feature_number_at, insert_projected_feature_window,
    scalar_program_feature_projection, scalar_trade_value,
};
use crate::domain::{
    AdjustmentSignal, Candle, CompiledAdjustmentProgram, CompiledOrderSide, NfiX7RebuyAdjustment,
    NfiX7RebuyConstants, NfiX7ShortRebuyAdjustment, OrderSide, PairSeries, PortfolioConfig,
};
use crate::execution::adjustment_minimum_pair_stake;
use crate::portfolio::{OpenTrade, TradeSide};
use crate::scalar_vm::{evaluate_scalar_decision_program, number_value};

use super::state::nfi_profit_snapshot;
/// Evaluate `long_rebuy_adjust_trade_position_v3()` for one visible candle.
///
/// The outer `Option` is the exactness boundary: `None` rejects malformed or
/// out-of-scope state. The inner `Option` is the strategy callback's normal
/// no-adjustment result.
#[allow(clippy::option_option)] // Outer None is invalid state; inner None is callback no-op.
pub(crate) fn evaluate_nfi_rebuy_adjustment(
    adjustment: &NfiX7RebuyAdjustment,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    available_balance: f64,
) -> Option<Option<AdjustmentSignal>> {
    if let Some(program) = adjustment.program.as_ref() {
        return evaluate_compiled_rebuy_program(
            program,
            TradeSide::Long,
            trade,
            pair,
            candle_index,
            candle,
            config,
            available_balance,
        );
    }
    evaluate_rebuy_ladder_legacy(
        adjustment.enabled,
        &adjustment.system_version,
        &adjustment.constants,
        TradeSide::Long,
        trade,
        pair,
        candle_index,
        candle,
        config,
        available_balance,
    )
}

/// Evaluate the pre-de-risk portion of `short_rebuy_adjust_trade_position_v3`.
#[allow(clippy::option_option)] // Outer None is invalid state; inner None is callback no-op.
pub(crate) fn evaluate_nfi_short_rebuy_adjustment(
    adjustment: &NfiX7ShortRebuyAdjustment,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    available_balance: f64,
) -> Option<Option<AdjustmentSignal>> {
    if let Some(program) = adjustment.program.as_ref() {
        return evaluate_compiled_rebuy_program(
            program,
            TradeSide::Short,
            trade,
            pair,
            candle_index,
            candle,
            config,
            available_balance,
        );
    }
    let supported_contract = matches!(
        (
            adjustment.execution_scope.as_str(),
            adjustment.post_derisk_action.as_str(),
        ),
        ("pre-derisk-only-v1", "fail-simulation")
            | ("rebuy-and-grind-v2", "short-position-adjustment")
    );
    if !supported_contract || trade.orders.iter().any(|order| !order.is_entry) {
        return None;
    }
    evaluate_rebuy_ladder_legacy(
        adjustment.enabled,
        &adjustment.system_version,
        &adjustment.constants,
        TradeSide::Short,
        trade,
        pair,
        candle_index,
        candle,
        config,
        available_balance,
    )
}

/// Whether the first filled exit selects the source-compiled delegate.
///
/// A false result means the callback must continue with its decision program.
pub(crate) fn compiled_rebuy_delegates(
    program: &CompiledAdjustmentProgram,
    trade: &OpenTrade,
) -> bool {
    let first_exit = trade.orders.iter().find(|order| !order.is_entry);
    first_exit.is_some_and(|order| order.tag.as_deref() == Some(&program.delegate.tag))
}

#[allow(clippy::too_many_arguments)]
#[allow(clippy::option_option)] // Outer None is invalid state; inner None is callback no-op.
fn evaluate_compiled_rebuy_program(
    program: &CompiledAdjustmentProgram,
    expected_side: TradeSide,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    available_balance: f64,
) -> Option<Option<AdjustmentSignal>> {
    if trade.side != expected_side || compiled_rebuy_delegates(program, trade) {
        return None;
    }
    let minimum_stake = rebuy_minimum_stake(pair, candle, trade, config)?;
    let first = trade.orders.first()?;
    if !first.is_entry {
        return None;
    }
    let aggregates = trade.filled_order_aggregates();
    let latest_entry = aggregates
        .select(crate::order_aggregates::FilledOrderSelector::Entries)
        .latest
        .as_ref()
        .and_then(|latest| trade.orders.get(latest.sequence))?;
    let sub_grind_count = compiled_cluster_count(program, trade, first.id)?;
    let snapshot = nfi_profit_snapshot(
        trade,
        candle.open,
        fee_open(config),
        fee_close(config),
        config.is_futures,
    )?;
    let raw_slice_profit_entry = price_distance(candle.open, latest_entry.price)?;
    let mut variables = BTreeMap::from([
        ("partial_sell".to_owned(), Value::Bool(false)),
        (
            "sub_grind_count".to_owned(),
            Value::Number(u64::try_from(sub_grind_count).ok()?.into()),
        ),
        (
            "slice_profit_entry".to_owned(),
            number_value(raw_slice_profit_entry)?,
        ),
        (
            "slice_amount".to_owned(),
            number_value(first.amount * first.price)?,
        ),
        ("is_futures_mode".to_owned(), Value::Bool(config.is_futures)),
        ("trade_leverage".to_owned(), number_value(trade.leverage)?),
        ("min_stake".to_owned(), number_value(minimum_stake)?),
        (
            "max_stake".to_owned(),
            number_value(available_balance / trade.leverage)?,
        ),
        ("profit_stake".to_owned(), number_value(snapshot.stake)?),
        ("trade_amount".to_owned(), number_value(trade.amount)?),
        ("exit_rate".to_owned(), number_value(candle.open)?),
        ("trade".to_owned(), scalar_trade_value(trade)?),
    ]);
    let projection = scalar_program_feature_projection(&program.decision_program);
    insert_projected_feature_window(&mut variables, pair, candle_index, &projection)?;
    let result = evaluate_scalar_decision_program(&program.decision_program, &variables)?;
    compiled_adjustment_signal(&result)
}

fn compiled_cluster_count(
    program: &CompiledAdjustmentProgram,
    trade: &OpenTrade,
    first_order_id: u64,
) -> Option<usize> {
    let mut count = 0_usize;
    for order in trade.orders.iter().rev() {
        if compiled_side_matches(program.order_scan.cluster_order_side, order.side) {
            if !program.order_scan.exclude_first_order || order.id != first_order_id {
                count = count.checked_add(1)?;
            }
        } else if compiled_side_matches(program.order_scan.boundary_order_side, order.side) {
            break;
        } else {
            return None;
        }
    }
    Some(count)
}

const fn compiled_side_matches(expected: CompiledOrderSide, actual: OrderSide) -> bool {
    matches!(
        (expected, actual),
        (CompiledOrderSide::Buy, OrderSide::Buy) | (CompiledOrderSide::Sell, OrderSide::Sell)
    )
}

#[allow(clippy::option_option)] // Outer None is invalid IR; inner None is program no-op.
fn compiled_adjustment_signal(value: &Value) -> Option<Option<AdjustmentSignal>> {
    if value.is_null() {
        return Some(None);
    }
    let values = value.as_array()?;
    if values.len() != 2 {
        return None;
    }
    let stake_amount = values.first()?.as_f64()?;
    let tag = values.get(1)?.as_str()?;
    if !stake_amount.is_finite() || stake_amount == 0.0 || tag.is_empty() {
        return None;
    }
    Some(Some(AdjustmentSignal {
        stake_amount,
        tag: tag.to_owned(),
    }))
}

#[allow(clippy::too_many_arguments)]
#[allow(clippy::option_option)] // Outer None is invalid state; inner None is callback no-op.
fn evaluate_rebuy_ladder_legacy(
    enabled: bool,
    system_version: &str,
    constants: &NfiX7RebuyConstants,
    expected_side: TradeSide,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    available_balance: f64,
) -> Option<Option<AdjustmentSignal>> {
    if !enabled || trade.side != expected_side {
        return None;
    }
    if trade.custom_data.get("system_version")?.as_str()? != system_version {
        return None;
    }
    let minimum_stake = rebuy_minimum_stake(pair, candle, trade, config)?;
    let first_entry = trade.orders.iter().find(|order| order.is_entry)?;
    let latest_entry = trade.orders.iter().rev().find(|order| order.is_entry)?;
    let sub_grind_count = entries_since_latest_exit(trade, first_entry.id);
    let stakes = if config.is_futures {
        &constants.stakes_futures
    } else {
        &constants.stakes_spot
    };
    let thresholds = if config.is_futures {
        &constants.thresholds_futures
    } else {
        &constants.thresholds_spot
    };
    let slice_amount = first_entry.amount * first_entry.price;
    let raw_price_distance = price_distance(candle.open, latest_entry.price)?;
    let loss_distance = match trade.side {
        TradeSide::Long => raw_price_distance,
        TradeSide::Short => -raw_price_distance,
    };

    if sub_grind_count < stakes.len()
        && loss_distance < *thresholds.get(sub_grind_count)?
        && rebuy_entry_features_allow(pair, candle_index, trade.side)?
    {
        let requested =
            (slice_amount * stakes[sub_grind_count] / trade.leverage).max(minimum_stake * 1.5);
        // Freqtrade passes max_stake including leverage to the callback; X7
        // divides it before comparing the returned pre-leverage stake.
        let callback_maximum = available_balance / trade.leverage;
        if requested > callback_maximum {
            return Some(None);
        }
        return Some(Some(AdjustmentSignal {
            stake_amount: requested,
            tag: "r".to_owned(),
        }));
    }

    if !constants.derisk_enable {
        return Some(None);
    }
    let snapshot = nfi_profit_snapshot(
        trade,
        candle.open,
        fee_open(config),
        fee_close(config),
        config.is_futures,
    )?;
    let derisk_threshold = if config.is_futures {
        constants.derisk_futures
    } else {
        constants.derisk_spot
    };
    if snapshot.stake >= slice_amount * derisk_threshold / trade.leverage {
        return Some(None);
    }

    // X7 leaves 1.55 exchange minimum stakes in the trade. It then converts
    // the quote exit amount back to Freqtrade stake currency using the
    // current trade stake/amount ratio, not the first-entry ratio.
    let sell_amount = trade.amount * candle.open / trade.leverage - minimum_stake * 1.55;
    let ft_sell_amount =
        sell_amount * trade.leverage * (trade.stake_amount / trade.amount) / candle.open;
    if sell_amount <= minimum_stake || ft_sell_amount <= minimum_stake {
        return Some(None);
    }
    Some(Some(AdjustmentSignal {
        stake_amount: -ft_sell_amount,
        tag: "derisk_level_3".to_owned(),
    }))
}

fn entries_since_latest_exit(trade: &OpenTrade, first_entry_id: u64) -> usize {
    let mut count = 0;
    for order in trade.orders.iter().rev() {
        if !order.is_entry {
            // The simulator exposes filled orders only. Consequently
            // Freqtrade's `safe_remaining` is zero and the source's
            // `partial_sell` flag remains false; the exit still terminates the
            // backwards cluster scan.
            break;
        }
        if order.id != first_entry_id {
            count += 1;
        }
    }
    count
}

fn rebuy_entry_features_allow(
    pair: &PairSeries,
    candle_index: usize,
    side: TradeSide,
) -> Option<bool> {
    let protections = feature_bool_at(pair, candle_index, "protections_long_global")?;
    let rsi_3 = feature_number_at(pair, candle_index, "RSI_3")?;
    let rsi_3_15m = feature_number_at(pair, candle_index, "RSI_3_15m")?;
    let close = feature_number_at(pair, candle_index, "close")?;
    let ema_26 = feature_number_at(pair, candle_index, "EMA_26")?;
    Some(match side {
        TradeSide::Long => {
            protections
                && rsi_3 > 10.0
                && rsi_3_15m > 10.0
                && feature_number_at(pair, candle_index, "AROONU_14")? < 30.0
                && feature_number_at(pair, candle_index, "AROONU_14_15m")? < 30.0
                && close < ema_26 * 0.988
        }
        TradeSide::Short => {
            protections
                && rsi_3 < 90.0
                && rsi_3_15m < 90.0
                && feature_number_at(pair, candle_index, "AROOND_14")? < 30.0
                && feature_number_at(pair, candle_index, "AROOND_14_15m")? < 30.0
                && close < ema_26 * 1.012
        }
    })
}

fn rebuy_minimum_stake(
    pair: &PairSeries,
    candle: &Candle,
    trade: &OpenTrade,
    config: &PortfolioConfig,
) -> Option<f64> {
    let has_limit = pair.minimum_stake.is_some()
        || pair.minimum_amount.is_some()
        || pair.minimum_cost.is_some();
    has_limit.then(|| {
        // Backtesting passes the exchange minimum to the callback with no
        // leverage argument. Both X7 rebuy callbacks then divide that value
        // before sizing entries and the level-3 residual. Omitting this
        // second step leaves leverage-times too much position after de-risk.
        adjustment_minimum_pair_stake(pair, candle.open, config.amount_reserve_percent)
            / trade.leverage
    })
}

fn price_distance(rate: f64, reference: f64) -> Option<f64> {
    (reference > 0.0).then_some((rate - reference) / reference)
}

#[cfg(test)]
mod tests {
    use std::sync::OnceLock;

    use serde_json::json;

    use super::*;
    use crate::domain::FilledOrder;

    fn compiled_program() -> CompiledAdjustmentProgram {
        serde_json::from_value(json!({
            "schema_version": "adjustment-transition-program-v1",
            "execution_mode": "primary",
            "source_order": ["delegate", "decision"],
            "order_scan": {
                "sequence": "reverse",
                "cluster_order_side": "buy",
                "boundary_order_side": "sell",
                "exclude_first_order": true,
                "partial_fill_policy": "filled-orders-have-zero-remaining"
            },
            "delegate": {
                "selector": "first-exit",
                "tag_operator": "equal",
                "tag": "source_derisk",
                "target": "position-adjustment",
                "source_target": "source_callback",
                "target_entry_retry_ms": 300_000,
                "location": {"line": 2, "column": 0, "end_line": 3, "end_column": 1}
            },
            "decision_program": {
                "schema_version": "1.2.0",
                "opcode": "scalar-decision-program-v1",
                "parameters": ["slice_profit_entry", "min_stake", "last_candle"],
                "expressions": [
                    ["literal", 0.08],
                    ["negative", 0],
                    ["variable", "slice_profit_entry"],
                    ["compare", 2, [["less", 1]]],
                    ["variable", "last_candle"],
                    ["literal", "gate"],
                    ["index", 4, 5],
                    ["and", [3, 6]],
                    ["variable", "min_stake"],
                    ["literal", 2.0],
                    ["multiply", 8, 9],
                    ["literal", "source_rebuy"],
                    ["tuple", [10, 11]],
                    ["literal", null]
                ],
                "statements": [
                    ["if", 7, [["return", 12]], []],
                    ["return", 13]
                ]
            },
            "input_contract": {"indexed_fields": {"last_candle": ["gate"]}},
            "location": {"line": 1, "column": 0, "end_line": 4, "end_column": 1},
            "fingerprint": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        }))
        .expect("valid compiled adjustment program")
    }

    fn open_trade() -> OpenTrade {
        OpenTrade {
            id: 1,
            pair_index: 0,
            pair: "TEST/USDT".to_owned(),
            side: TradeSide::Long,
            leverage: 1.0,
            amount_step: 0.001,
            price_step: 0.01,
            open_timestamp_ms: 0,
            open_rate: 100.0,
            amount: 1.0,
            stake_amount: 100.0,
            max_stake_amount: 100.0,
            entry_cost_with_fees: 100.1,
            first_entry_cost_with_fees: 100.1,
            adjustment_count: 0,
            entry_tag: Some("source_route".to_owned()),
            entry_tag_cache: OnceLock::new(),
            funding_fees: 0.0,
            funding_fees_total: 0.0,
            funding_sum_high: 0.0,
            funding_sum_low: 0.0,
            funding_rebase_seed: None,
            realized_partial_profit: 0.0,
            liquidation_price: None,
            liquidation_price_is_explicit: false,
            initial_stop_loss: 1.0,
            stop_loss: 1.0,
            minimum_rate: 90.0,
            maximum_rate: 100.0,
            orders: vec![FilledOrder {
                id: 1,
                funding_fee: 0.0,
                sequence: 0,
                side: OrderSide::Buy,
                is_entry: true,
                filled_timestamp_ms: 0,
                amount: 1.0,
                price: 100.0,
                cost: 100.0,
                tag: Some("source_route".to_owned()),
            }],
            filled_order_aggregates: OnceLock::new(),
            custom_data: BTreeMap::new(),
            nfi_adjustment_state: None,
        }
    }

    fn pair_and_config() -> (PairSeries, Candle, PortfolioConfig) {
        let pair: PairSeries = serde_json::from_value(json!({
            "pair": "TEST/USDT",
            "minimum_cost": 5.0,
            "feature_columns": {"gate": [true]},
            "candles": [{
                "timestamp_ms": 300_000,
                "open": 90.0,
                "high": 91.0,
                "low": 89.0,
                "close": 90.0,
                "volume": 1.0
            }]
        }))
        .expect("valid pair");
        let candle = pair.candles.get(0).expect("one candle").into_owned();
        let config: PortfolioConfig = serde_json::from_value(json!({
            "starting_balance": 1000.0,
            "max_open_trades": 1,
            "stake_amount": 100.0,
            "fee_rate": 0.001,
            "stoploss_ratio": -0.99,
            "amount_step": 0.001,
            "price_step": 0.01,
            "amount_reserve_percent": 0.0
        }))
        .expect("valid config");
        (pair, candle, config)
    }

    #[test]
    fn compiled_rebuy_stake_and_tag_are_program_data() {
        let mut program = compiled_program();
        let trade = open_trade();
        let (pair, candle, config) = pair_and_config();
        let first = evaluate_compiled_rebuy_program(
            &program,
            TradeSide::Long,
            &trade,
            &pair,
            0,
            &candle,
            &config,
            1_000.0,
        )
        .expect("valid compiled execution")
        .expect("entry action");
        assert!(first.stake_amount > 0.0);
        assert_eq!(first.tag, "source_rebuy");

        program.decision_program.expressions[9] = json!(["literal", 3.0]);
        program.decision_program.expressions[11] = json!(["literal", "changed_rebuy"]);
        let changed = evaluate_compiled_rebuy_program(
            &program,
            TradeSide::Long,
            &trade,
            &pair,
            0,
            &candle,
            &config,
            1_000.0,
        )
        .expect("valid changed program")
        .expect("changed entry action");
        assert!((changed.stake_amount - first.stake_amount * 1.5).abs() < f64::EPSILON);
        assert_eq!(changed.tag, "changed_rebuy");
    }

    #[test]
    fn compiled_rebuy_delegate_tag_is_program_data() {
        let mut program = compiled_program();
        let mut trade = open_trade();
        trade.push_filled_order(FilledOrder {
            id: 2,
            funding_fee: 0.0,
            sequence: 1,
            side: OrderSide::Sell,
            is_entry: false,
            filled_timestamp_ms: 1,
            amount: 0.5,
            price: 90.0,
            cost: 45.0,
            tag: Some("source_derisk".to_owned()),
        });
        assert!(compiled_rebuy_delegates(&program, &trade));
        program.delegate.tag = "changed_derisk".to_owned();
        assert!(!compiled_rebuy_delegates(&program, &trade));
    }

    #[test]
    fn compiled_short_futures_uses_its_directional_program() {
        let mut program = compiled_program();
        program.order_scan.cluster_order_side = CompiledOrderSide::Sell;
        program.order_scan.boundary_order_side = CompiledOrderSide::Buy;
        let source_index = program.decision_program.expressions.len();
        program
            .decision_program
            .expressions
            .push(json!(["variable", "slice_profit_entry"]));
        program.decision_program.expressions[2] = json!(["negative", source_index]);

        let mut trade = open_trade();
        trade.side = TradeSide::Short;
        trade.leverage = 3.0;
        trade.orders[0].side = OrderSide::Sell;
        let (pair, mut candle, mut config) = pair_and_config();
        candle.open = 110.0;
        config.is_futures = true;

        let signal = evaluate_compiled_rebuy_program(
            &program,
            TradeSide::Short,
            &trade,
            &pair,
            0,
            &candle,
            &config,
            1_000.0,
        )
        .expect("valid short program")
        .expect("short entry action");
        assert!(signal.stake_amount > 0.0);
        assert_eq!(signal.tag, "source_rebuy");
    }
}
