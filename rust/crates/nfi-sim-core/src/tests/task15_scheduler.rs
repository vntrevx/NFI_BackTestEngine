//! Todo 15 global scheduler permutation and tie-break certification.

use super::*;

#[path = "task15_scheduler/support.rs"]
mod support;
#[path = "task15_scheduler/wallet.rs"]
mod wallet;

use support::*;

fn assert_simultaneous_permutation(order: &[&str]) {
    let mut events = Vec::new();
    let input = portfolio_input(
        exact_config(1),
        order
            .iter()
            .map(|pair| pair_series(pair, vec![plain(-1), entry(0, pair, 100.0), plain(1)]))
            .collect(),
    );

    let result = simulate_with_observer(&input, |event| events.push(event.clone()))
        .expect("valid simultaneous portfolio");
    let mut boundaries = Vec::new();
    let boundary_result =
        simulate_with_portfolio_observer(&input, |event| boundaries.push(event.clone()))
            .expect("valid simultaneous boundary stream");
    assert_eq!(boundary_result, result);
    let timestamp_events = events
        .iter()
        .filter(|event| event.timestamp_ms == 1)
        .collect::<Vec<_>>();

    assert_eq!(
        timestamp_events
            .iter()
            .map(|event| event.pair.as_str())
            .collect::<Vec<_>>(),
        order
    );
    assert_eq!(timestamp_events.len(), order.len());
    let pair_visits = boundaries
        .iter()
        .filter(|event| event.timestamp_ms == 1 && event.boundary == PortfolioBoundary::PairVisit)
        .collect::<Vec<_>>();
    assert_eq!(pair_visits.len(), order.len());
    for (index, visit) in pair_visits.iter().enumerate() {
        assert_eq!(visit.pair, order[index]);
        assert_eq!(visit.configured_pair_index, index);
        assert_eq!(visit.processing_order_index, index);
        assert_eq!(visit.state_before, visit.state_after);
    }
    for (index, event) in timestamp_events.iter().enumerate() {
        assert_eq!(event.schema_version, SIMULATION_EVENT_SCHEMA_VERSION);
        assert_eq!(event.state.quote_free, 900.0);
        assert_eq!(event.state.tied_up_stake, 100.0);
        assert_eq!(event.state.realized_wallet_profit, 0.0);
        assert_eq!(event.state.open_trade_count, 1);
        assert_eq!(event.state.rejected_signals, index as u64);
        assert_eq!(event.state.trade_id_counter, 1);
        assert_eq!(event.state.order_id_counter, 1);
        assert_eq!(event.state.open_trade_ids, [1]);
        assert_eq!(event.state.open_trade_pairs, [order[0]]);
        assert_eq!(event.state.open_order_ids, [1]);
        assert_eq!(
            callback_phases(event),
            if index == 0 {
                vec![
                    CallbackPhase::StakeSizing,
                    CallbackPhase::Leverage,
                    CallbackPhase::EntryConfirmation,
                    CallbackPhase::OrderFilled,
                    CallbackPhase::CandleAfter,
                ]
            } else {
                vec![CallbackPhase::CandleAfter]
            }
        );
    }
    assert_eq!(result.maximum_concurrent_trades, 1);
    assert_eq!(result.rejected_signals, (order.len() - 1) as u64);
    assert_eq!(result.final_balance, 1_000.0);
    assert_eq!(result.profit_total_abs, 0.0);
    assert_eq!(result.trades.len(), 1);
    assert_trade(&result.trades[0], 1, order[0], &[1, 2], "force_exit", 100.0);
}

#[test]
fn both_two_pair_configured_permutations_are_exact() {
    for permutation in [["AAA/USDT", "BBB/USDT"], ["BBB/USDT", "AAA/USDT"]] {
        assert_simultaneous_permutation(&permutation);
    }
}

#[test]
fn all_six_three_pair_configured_permutations_are_exact() {
    for permutation in [
        ["AAA/USDT", "BBB/USDT", "CCC/USDT"],
        ["AAA/USDT", "CCC/USDT", "BBB/USDT"],
        ["BBB/USDT", "AAA/USDT", "CCC/USDT"],
        ["BBB/USDT", "CCC/USDT", "AAA/USDT"],
        ["CCC/USDT", "AAA/USDT", "BBB/USDT"],
        ["CCC/USDT", "BBB/USDT", "AAA/USDT"],
    ] {
        assert_simultaneous_permutation(&permutation);
    }
}

#[test]
fn multiple_open_trades_keep_insertion_order_before_configured_remainder() {
    let pairs = vec![
        pair_series(
            "NEW/USDT",
            vec![plain(-1), plain(0), plain(1), plain(2), plain(3)],
        ),
        pair_series(
            "SECOND/USDT",
            vec![
                plain(-1),
                plain(0),
                entry(1, "second", 100.0),
                plain(2),
                plain(3),
            ],
        ),
        pair_series(
            "FIRST/USDT",
            vec![
                plain(-1),
                entry(0, "first", 100.0),
                plain(1),
                plain(2),
                plain(3),
            ],
        ),
    ];
    let input = portfolio_input(exact_config(3), pairs);
    let mut events = Vec::new();

    let result = simulate_with_observer(&input, |event| events.push(event.clone()))
        .expect("valid insertion-prefix portfolio");
    let at_two = events
        .iter()
        .filter(|event| event.timestamp_ms == 3)
        .collect::<Vec<_>>();

    assert_eq!(pairs_at(&at_two), ["FIRST/USDT", "SECOND/USDT", "NEW/USDT"]);
    for event in at_two {
        assert_eq!(
            (event.state.quote_free, event.state.tied_up_stake),
            (800.0, 200.0)
        );
        assert_eq!(event.state.realized_wallet_profit, 0.0);
        assert_eq!(event.state.open_trade_count, 2);
        assert_eq!(event.state.open_trade_ids, [1, 2]);
        assert_eq!(event.state.open_trade_pairs, ["FIRST/USDT", "SECOND/USDT"]);
        assert_eq!(event.state.open_order_ids, [1, 2]);
        assert_eq!(
            (event.state.trade_id_counter, event.state.order_id_counter),
            (2, 2)
        );
        assert_eq!(event.state.rejected_signals, 0);
    }
    assert_trade(
        &result.trades[0],
        2,
        "SECOND/USDT",
        &[2, 3],
        "force_exit",
        100.0,
    );
    assert_trade(
        &result.trades[1],
        1,
        "FIRST/USDT",
        &[1, 4],
        "force_exit",
        100.0,
    );
}

#[test]
fn open_trade_insertion_prefix_precedes_remaining_configured_pairs_once() {
    let pairs = vec![
        pair_series(
            "AAA/USDT",
            vec![plain(-1), plain(0), entry(1, "new", 100.0), plain(2)],
        ),
        pair_series(
            "BBB/USDT",
            vec![plain(-1), entry(0, "old", 100.0), exit(1, 150.0), plain(2)],
        ),
        pair_series("CCC/USDT", vec![plain(-1), plain(0), plain(1), plain(2)]),
    ];
    let mut config = exact_config(1);
    config.unlimited_stake = true;
    let mut events = Vec::new();

    let result = simulate_with_observer(&portfolio_input(config, pairs), |event| {
        events.push(event.clone());
    })
    .expect("valid close-and-reuse portfolio");
    let at_one = events
        .iter()
        .filter(|event| event.timestamp_ms == 2)
        .collect::<Vec<_>>();

    assert_eq!(pairs_at(&at_one), ["BBB/USDT", "AAA/USDT", "CCC/USDT"]);
    assert_eq!(at_one[0].state.quote_free, 1_500.0);
    assert_eq!(at_one[0].state.tied_up_stake, 0.0);
    assert_eq!(at_one[0].state.realized_wallet_profit, 500.0);
    assert_eq!(at_one[0].state.open_trade_count, 0);
    assert!(at_one[0].state.open_trade_ids.is_empty());
    assert_eq!(at_one[0].state.order_id_counter, 2);
    assert_eq!(at_one[1].state.quote_free, 0.0);
    assert_eq!(at_one[1].state.tied_up_stake, 1_500.0);
    assert_eq!(at_one[1].state.open_trade_ids, [2]);
    assert_eq!(at_one[1].state.open_order_ids, [3]);
    assert_eq!(at_one[2].state.open_trade_pairs, ["AAA/USDT"]);
    assert_eq!(result.rejected_signals, 0);
    assert_eq!(result.final_balance, 1_500.0);
    assert_trade(
        &result.trades[0],
        1,
        "BBB/USDT",
        &[1, 2],
        "signal-exit",
        1_000.0,
    );
    assert_trade(
        &result.trades[1],
        2,
        "AAA/USDT",
        &[3, 4],
        "force_exit",
        1_500.0,
    );
}
