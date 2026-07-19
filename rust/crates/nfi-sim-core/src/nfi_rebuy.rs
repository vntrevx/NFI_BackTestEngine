//! Exact system-v3 rebuy ladders for the bounded NFI X7 routes.
//!
//! Rebuy is not a parameterized grind level. X7 counts entry orders since the
//! latest exit, applies a dedicated loss threshold per count, and can sell
//! almost the whole trade at one de-risk boundary. Keeping this evaluator in a
//! separate module makes that order model explicit and prevents accidental
//! reuse of the more complex grind-cluster reconstruction.

use super::{
    adjustment_minimum_pair_stake, feature_bool_at, feature_number_at, fee_close, fee_open,
    nfi_profit_snapshot, AdjustmentSignal, Candle, NfiX7RebuyAdjustment, NfiX7ShortRebuyAdjustment,
    OpenTrade, PairSeries, PortfolioConfig, TradeSide,
};

/// Evaluate `long_rebuy_adjust_trade_position_v3()` for one visible candle.
///
/// The outer `Option` is the exactness boundary: `None` rejects malformed or
/// out-of-scope state. The inner `Option` is the strategy callback's normal
/// no-adjustment result.
#[allow(clippy::option_option)] // Outer None is invalid state; inner None is callback no-op.
pub(super) fn evaluate_nfi_rebuy_adjustment(
    adjustment: &NfiX7RebuyAdjustment,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    available_balance: f64,
) -> Option<Option<AdjustmentSignal>> {
    evaluate_rebuy_ladder(
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
pub(super) fn evaluate_nfi_short_rebuy_adjustment(
    adjustment: &NfiX7ShortRebuyAdjustment,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    available_balance: f64,
) -> Option<Option<AdjustmentSignal>> {
    if adjustment.execution_scope != "pre-derisk-only-v1"
        || adjustment.post_derisk_action != "fail-simulation"
        || trade.orders.iter().any(|order| !order.is_entry)
    {
        return None;
    }
    evaluate_rebuy_ladder(
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

#[allow(clippy::too_many_arguments)]
#[allow(clippy::option_option)] // Outer None is invalid state; inner None is callback no-op.
fn evaluate_rebuy_ladder(
    enabled: bool,
    system_version: &str,
    constants: &super::NfiX7RebuyConstants,
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
    _trade: &OpenTrade,
    config: &PortfolioConfig,
) -> Option<f64> {
    let has_limit = pair.minimum_stake.is_some()
        || pair.minimum_amount.is_some()
        || pair.minimum_cost.is_some();
    has_limit
        .then(|| adjustment_minimum_pair_stake(pair, candle.open, config.amount_reserve_percent))
}

fn price_distance(rate: f64, reference: f64) -> Option<f64> {
    (reference > 0.0).then_some((rate - reference) / reference)
}
