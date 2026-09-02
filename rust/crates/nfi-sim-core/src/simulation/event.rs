//! Observer-facing state projection for one chronological pair event.

use crate::calculations::{checked_finite, checked_float_sum};
use crate::portfolio::{OpenTrade, TradeSide};
use crate::protections::PairLockState;
use crate::{
    AssetBalance, ClosedTrade, FilledOrder, SemanticClosedTradeState, SemanticOpenTradeState,
    SemanticOrderState, SimError, SimulationEvent, SimulationState,
    SIMULATION_EVENT_SCHEMA_VERSION,
};
const NANO_QUOTE_SCALE: f64 = 1_000_000_000.0;

#[derive(Clone, Copy)]
pub(super) struct EventProjection<'a> {
    pub(super) timestamp_ms: i64,
    pub(super) pair: &'a str,
    pub(super) quote_free: f64,
    pub(super) is_futures: bool,
    pub(super) configured_pair_index: usize,
    pub(super) processing_order_index: usize,
    pub(super) candle_index: usize,
    pub(super) next_candle_index: usize,
    pub(super) slot_limit: usize,
    pub(super) open_trades: &'a [OpenTrade],
    pub(super) closed_trades: &'a [ClosedTrade],
    pub(super) rejected_signals: u64,
    pub(super) trade_id_counter: u64,
    pub(super) order_id_counter: u64,
    pub(super) locks: &'a [PairLockState],
}

pub(super) fn simulation_event(input: EventProjection<'_>) -> Result<SimulationEvent, SimError> {
    let EventProjection {
        timestamp_ms,
        pair,
        quote_free,
        is_futures,
        open_trades,
        configured_pair_index,
        processing_order_index,
        candle_index,
        next_candle_index,
        slot_limit,
        closed_trades,
        rejected_signals,
        trade_id_counter,
        order_id_counter,
        locks,
    } = input;
    let base_balances = event_base_balances(open_trades)?;
    let quote_free = event_quote_free(quote_free, is_futures)?;
    let realized_profit = checked_float_sum(
        &closed_trades
            .iter()
            .map(|trade| trade.profit_abs)
            .collect::<Vec<_>>(),
        "event-realized-profit",
    )?;
    let tied_up_stake = checked_float_sum(
        &open_trades
            .iter()
            .map(|trade| trade.stake_amount)
            .collect::<Vec<_>>(),
        "event-tied-up-stake",
    )?;
    let open_realized_profit = checked_float_sum(
        &open_trades
            .iter()
            .map(|trade| trade.realized_partial_profit)
            .collect::<Vec<_>>(),
        "event-open-realized-profit",
    )?;
    let realized_wallet_profit = checked_float_sum(
        &[realized_profit, open_realized_profit],
        "event-wallet-realized-profit",
    )?;
    let quote_total = checked_float_sum(&[quote_free, tied_up_stake], "event-quote-total")?;
    let open_trade_states = open_trades.iter().map(semantic_open_trade).collect();
    let closed_trade_states = closed_trades.iter().map(semantic_closed_trade).collect();
    Ok(SimulationEvent {
        schema_version: SIMULATION_EVENT_SCHEMA_VERSION,
        timestamp_ms,
        pair: pair.to_owned(),
        state: SimulationState {
            quote_total,
            quote_free,
            quote_used: tied_up_stake,
            tied_up_stake,
            realized_wallet_profit,
            base_balances,
            configured_pair_index,
            processing_order_index,
            candle_index,
            next_candle_index,
            occupied_slots: open_trades.len(),
            slot_limit,
            open_trade_count: open_trades.len(),
            open_trade_ids: open_trades.iter().map(|trade| trade.id).collect(),
            open_trade_pairs: open_trades.iter().map(|trade| trade.pair.clone()).collect(),
            open_order_ids: open_trades
                .iter()
                .flat_map(|trade| trade.orders.iter().map(|order| order.id))
                .collect(),
            open_trades: open_trade_states,
            realized_profit,
            closed_trade_count: closed_trades.len(),
            closed_trades: closed_trade_states,
            rejected_signals,
            trade_id_counter,
            order_id_counter,
            locks: locks.to_vec(),
        },
        callback_events: Vec::new(),
        executable_callback_events: Vec::new(),
        portfolio_events: Vec::new(),
        execution_events: Vec::new(),
    })
}

fn event_quote_free(quote_free: f64, is_futures: bool) -> Result<f64, SimError> {
    let quote_free = checked_finite(quote_free, "event-quote-balance")?;
    if !is_futures {
        return Ok(quote_free);
    }
    let scaled = checked_finite(quote_free * NANO_QUOTE_SCALE, "event-quote-balance-scale")?;
    checked_finite(
        scaled.round_ties_even() / NANO_QUOTE_SCALE,
        "event-quote-balance",
    )
}

fn event_base_balances(open_trades: &[OpenTrade]) -> Result<Vec<AssetBalance>, SimError> {
    let mut balances = open_trades
        .iter()
        .map(|trade| {
            Ok::<AssetBalance, SimError>(AssetBalance {
                currency: trade
                    .pair
                    .split_once('/')
                    .map_or_else(|| trade.pair.clone(), |(base, _)| base.to_owned()),
                free: checked_finite(
                    if trade.side == TradeSide::Short {
                        -trade.amount
                    } else {
                        trade.amount
                    },
                    "event-base-balance",
                )?,
            })
        })
        .collect::<Result<Vec<_>, SimError>>()?;
    balances.sort_by(|left, right| left.currency.cmp(&right.currency));
    Ok(balances)
}

fn semantic_order(order: &FilledOrder) -> SemanticOrderState {
    SemanticOrderState {
        id: order.id,
        funding_fee: order.funding_fee,
        sequence: order.sequence,
        side: order.side,
        is_entry: order.is_entry,
        filled_timestamp_ms: order.filled_timestamp_ms,
        amount: order.amount,
        price: order.price,
        cost: order.cost,
        tag: order.tag.clone(),
    }
}

fn semantic_open_trade(trade: &OpenTrade) -> SemanticOpenTradeState {
    SemanticOpenTradeState {
        id: trade.id,
        pair_index: trade.pair_index,
        pair: trade.pair.clone(),
        is_short: trade.side == TradeSide::Short,
        leverage: trade.leverage,
        amount_step: trade.amount_step,
        price_step: trade.price_step,
        open_timestamp_ms: trade.open_timestamp_ms,
        open_rate: trade.open_rate,
        amount: trade.amount,
        stake_amount: trade.stake_amount,
        max_stake_amount: trade.max_stake_amount,
        entry_cost_with_fees: trade.entry_cost_with_fees,
        first_entry_cost_with_fees: trade.first_entry_cost_with_fees,
        adjustment_count: trade.adjustment_count,
        entry_tag: trade.entry_tag.clone(),
        funding_fees: trade.funding_fees,
        funding_fees_total: trade.funding_fees_total,
        funding_sum_high: trade.funding_sum_high,
        funding_sum_low: trade.funding_sum_low,
        funding_rebase_seed: trade.funding_rebase_seed,
        realized_partial_profit: trade.realized_partial_profit,
        liquidation_price: trade.liquidation_price,
        liquidation_price_is_explicit: trade.liquidation_price_is_explicit,
        initial_stop_loss: trade.initial_stop_loss,
        stop_loss: trade.stop_loss,
        custom_stop_loss_ratio: trade.custom_stop_loss_ratio,
        minimum_rate: trade.minimum_rate,
        maximum_rate: trade.maximum_rate,
        orders: trade.orders.iter().map(semantic_order).collect(),
        custom_data: trade.custom_data.clone(),
    }
}

fn semantic_closed_trade(trade: &ClosedTrade) -> SemanticClosedTradeState {
    SemanticClosedTradeState {
        trade: trade.clone(),
        orders: trade.orders.iter().map(semantic_order).collect(),
    }
}

#[cfg(test)]
mod tests {
    use super::event_quote_free;

    #[test]
    fn futures_quote_balance_removes_sub_nano_float_noise() {
        assert_eq!(
            event_quote_free(8_022.005_733_333_333, true)
                .expect("finite balance")
                .to_bits(),
            8_022.005_733_333_f64.to_bits()
        );
        assert_eq!(
            event_quote_free(4_592.188_874_112_047, true)
                .expect("finite balance")
                .to_bits(),
            4_592.188_874_112_f64.to_bits()
        );
    }

    #[test]
    fn spot_quote_balance_preserves_exact_float_representation() {
        let quote_free = 4_592.188_874_112_047;

        assert_eq!(
            event_quote_free(quote_free, false)
                .expect("finite balance")
                .to_bits(),
            quote_free.to_bits()
        );
    }
}
