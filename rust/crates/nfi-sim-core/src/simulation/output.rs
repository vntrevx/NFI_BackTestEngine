//! Force-exit ordering and canonical aggregate result assembly.

use crate::calculations::{pairwise_sum, python_float_sum};
use crate::execution::close_trade;
use crate::portfolio::{wallet_free, OpenTrade};
use crate::protections::ProtectionState;
use crate::{ClosedTrade, SimulationInput, SimulationResult, SIMULATOR_SCHEMA_VERSION};

pub(super) fn finalize_simulation(
    input: &SimulationInput,
    open_trades: Vec<OpenTrade>,
    mut closed_trades: Vec<ClosedTrade>,
    mut protection_state: ProtectionState,
    rejected_signals: u64,
    maximum_concurrent_trades: usize,
    mut next_order_id: u64,
) -> SimulationResult {
    let config = &input.config;
    // Freqtrade's LocalTrade registry exposes still-open trades newest first
    // when the backtest force-closes its remaining positions. Preserve that
    // insertion-stack order here; otherwise the trades themselves are exact,
    // but their final exported sequence numbers are reversed.
    for trade in open_trades.into_iter().rev() {
        let last = input.pairs[trade.pair_index]
            .candles
            .last()
            .expect("validated non-empty candles");
        let (closed, _) = close_trade(
            trade,
            last.timestamp_ms,
            last.open,
            "force_exit".to_owned(),
            config,
            closed_trades.len(),
            next_order_id,
        );
        next_order_id += 1;
        closed_trades.push(closed);
        if let Some(program) = &config.protection_program {
            let closed_trade = closed_trades
                .last()
                .expect("a force-closed trade was appended immediately above");
            protection_state.after_trade_close(
                program,
                closed_trade,
                &closed_trades,
                config.starting_balance,
            );
        }
    }
    let available_balance = wallet_free(config.starting_balance, &[], &closed_trades);
    // LocalTrade appends a trade to its closed-trade collection when that
    // trade closes. Freqtrade therefore exports closure order, including the
    // processing order of trades that close on the same timestamp. Sorting by
    // open time here looked deterministic but changed the public trade array
    // whenever overlapping positions closed in a different order.
    for (sequence, trade) in closed_trades.iter_mut().enumerate() {
        trade.sequence = sequence;
    }
    // Freqtrade exports `profit_total_abs` from Pandas' reduction of the
    // per-trade profit column. It is not derived from final wallet balance.
    // Pairwise summation mirrors NumPy's stable reduction and avoids the ulp
    // drift of a left-to-right iterator fold on long NFI result sets.
    let profit_total_abs = pairwise_sum(
        &closed_trades
            .iter()
            .map(|trade| trade.profit_abs)
            .collect::<Vec<_>>(),
    );
    let per_trade_volumes = closed_trades
        .iter()
        // Freqtrade calls Python `sum()` once per trade. CPython 3.14 uses a
        // compensated float accumulator, so Rust's ordinary Iterator::sum
        // differs by a few ulps on adjustment-heavy trades.
        .map(|trade| python_float_sum(trade.orders.iter().map(|order| order.cost)))
        .collect::<Vec<_>>();
    // Freqtrade then calls Python `sum()` over the per-trade subtotals. Keep
    // that second reduction boundary: flattening all orders is observably
    // different even when every order itself already matches.
    let total_volume = python_float_sum(per_trade_volumes);
    let result = SimulationResult {
        schema_version: SIMULATOR_SCHEMA_VERSION,
        starting_balance: config.starting_balance,
        final_balance: available_balance,
        profit_total_abs,
        total_volume,
        rejected_signals,
        maximum_concurrent_trades,
        locks: protection_state.locks().to_vec(),
        trades: closed_trades,
    };
    result
}
