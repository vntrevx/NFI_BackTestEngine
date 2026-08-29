use super::*;

#[test]
fn rejected_zero_stake_consumes_reserved_order_id_then_continues_to_later_pair() {
    let pairs = vec![
        pair_series(
            "TINY/USDT",
            vec![plain(-1), entry(0, "zero-sized", 10_000.0), plain(1)],
        ),
        pair_series(
            "FIT/USDT",
            vec![plain(-1), entry(0, "accepted", 100.0), plain(1)],
        ),
    ];
    let input = portfolio_input(exact_config(1), pairs);
    let mut events = Vec::new();

    let result = simulate_with_observer(&input, |event| events.push(event.clone()))
        .expect("zero-sized entry is a normal rejection");
    let mut boundaries = Vec::new();
    simulate_with_portfolio_observer(&input, |event| boundaries.push(event.clone()))
        .expect("valid rejected-stake boundary stream");
    let at_zero = events
        .iter()
        .filter(|event| event.timestamp_ms == 1)
        .collect::<Vec<_>>();

    assert_eq!(pairs_at(&at_zero), ["TINY/USDT", "FIT/USDT"]);
    assert_eq!(at_zero[0].state.quote_free, 1_000.0);
    assert_eq!(at_zero[0].state.tied_up_stake, 0.0);
    assert_eq!(at_zero[0].state.open_trade_count, 0);
    assert_eq!(at_zero[0].state.rejected_signals, 0);
    assert_eq!(at_zero[0].state.trade_id_counter, 0);
    assert_eq!(at_zero[0].state.order_id_counter, 1);
    assert!(at_zero[0].state.open_order_ids.is_empty());
    assert_eq!(
        callback_phases(at_zero[0]),
        [
            CallbackPhase::StakeSizing,
            CallbackPhase::Leverage,
            CallbackPhase::CandleAfter,
        ]
    );
    assert_eq!(at_zero[1].state.quote_free, 900.0);
    assert_eq!(at_zero[1].state.tied_up_stake, 100.0);
    assert_eq!(at_zero[1].state.open_trade_ids, [1]);
    assert_eq!(at_zero[1].state.open_order_ids, [2]);
    assert_eq!(at_zero[1].state.trade_id_counter, 1);
    assert_eq!(at_zero[1].state.order_id_counter, 2);
    let stake_rejection = boundaries
        .iter()
        .find(|event| event.rejection_reason == Some(EntryRejectionReason::StakePrecision))
        .expect("stake precision rejection");
    assert_eq!(stake_rejection.proposed_stake, Some(100.0));
    assert_eq!(stake_rejection.allocated_order_id, Some(1));
    assert_eq!(stake_rejection.allocated_trade_id, None);
    assert_eq!(stake_rejection.state_before.next_order_id, 1);
    assert_eq!(stake_rejection.state_after.next_order_id, 2);
    let accepted = boundaries
        .iter()
        .find(|event| event.boundary == PortfolioBoundary::EntryAccepted)
        .expect("later configured pair accepted");
    assert_eq!(accepted.pair, "FIT/USDT");
    assert_eq!(accepted.allocated_order_id, Some(2));
    assert_eq!(accepted.allocated_trade_id, Some(1));
    assert_eq!(result.rejected_signals, 0);
    assert_trade(
        &result.trades[0],
        1,
        "FIT/USDT",
        &[2, 3],
        "force_exit",
        100.0,
    );
}

#[test]
fn partial_exit_releases_wallet_retains_slot_and_force_exits_reverse_insertion() {
    let mut partial = candle(1, 120.0, 120.0);
    partial.high = 120.0;
    partial.adjustment = Some(AdjustmentSignal {
        stake_amount: -250.0,
        tag: "half".to_owned(),
    });
    let pairs = vec![
        pair_series(
            "NEW/USDT",
            vec![plain(-1), plain(0), entry(1, "new", 100.0), plain(2)],
        ),
        pair_series(
            "OLD/USDT",
            vec![plain(-1), entry(0, "old", 100.0), partial, plain(2)],
        ),
        pair_series(
            "BLOCKED/USDT",
            vec![plain(-1), plain(0), entry(1, "blocked", 100.0), plain(2)],
        ),
    ];
    let mut config = exact_config(2);
    config.amount_step = 0.1;
    config.unlimited_stake = true;
    let input = portfolio_input(config, pairs);
    let mut events = Vec::new();

    let result = simulate_with_observer(&input, |event| events.push(event.clone()))
        .expect("valid partial-release portfolio");
    let mut boundaries = Vec::new();
    let observed_result =
        simulate_with_portfolio_observer(&input, |event| boundaries.push(event.clone()))
            .expect("valid portfolio boundary stream");
    assert_eq!(observed_result, result);
    let at_one = events
        .iter()
        .filter(|event| event.timestamp_ms == 2)
        .collect::<Vec<_>>();

    assert_eq!(pairs_at(&at_one), ["OLD/USDT", "NEW/USDT", "BLOCKED/USDT"]);
    assert_eq!(at_one[0].state.quote_free, 800.0);
    assert_eq!(at_one[0].state.tied_up_stake, 250.0);
    assert_eq!(at_one[0].state.realized_wallet_profit, 50.0);
    assert_eq!(at_one[0].state.open_trade_count, 1);
    assert_eq!(at_one[0].state.open_trade_ids, [1]);
    assert_eq!(at_one[0].state.open_order_ids, [1, 2]);
    assert_eq!(at_one[0].state.order_id_counter, 2);
    assert_eq!(
        callback_phases(at_one[0]),
        [
            CallbackPhase::OrderFilled,
            CallbackPhase::CustomStoploss,
            CallbackPhase::CandleAfter,
        ]
    );
    assert_eq!(at_one[1].state.quote_free, 280.0);
    assert_eq!(at_one[1].state.tied_up_stake, 770.0);
    assert_eq!(at_one[1].state.open_trade_ids, [1, 2]);
    assert_eq!(at_one[1].state.open_trade_pairs, ["OLD/USDT", "NEW/USDT"]);
    assert_eq!(at_one[1].state.open_order_ids, [1, 2, 3]);
    assert_eq!(at_one[2].state.rejected_signals, 1);
    assert_eq!(at_one[2].state.open_trade_count, 2);

    assert_partial_boundaries(&boundaries);

    assert_eq!(result.maximum_concurrent_trades, 2);
    assert_eq!(result.rejected_signals, 1);
    assert_eq!(result.final_balance, 1_050.0);
    assert_eq!(result.trades.len(), 2);
    assert_trade(
        &result.trades[0],
        2,
        "NEW/USDT",
        &[3, 4],
        "force_exit",
        520.0,
    );
    assert_trade(
        &result.trades[1],
        1,
        "OLD/USDT",
        &[1, 2, 5],
        "force_exit",
        250.0,
    );
}

fn assert_partial_boundaries(boundaries: &[PortfolioBoundaryEvent]) {
    let partial = boundaries
        .iter()
        .find(|event| event.boundary == PortfolioBoundary::PartialExit)
        .expect("partial boundary");
    assert_eq!(partial.schema_version, PORTFOLIO_EVENT_SCHEMA_VERSION);
    assert_eq!(
        (
            partial.configured_pair_index,
            partial.processing_order_index
        ),
        (1, 0)
    );
    assert_eq!(
        (
            partial.state_before.wallet_free,
            partial.state_before.wallet_tied
        ),
        (500.0, 500.0)
    );
    assert_eq!(partial.state_before.open_trade_ids, [1]);
    assert_eq!(partial.state_before.next_order_id, 2);
    assert_eq!(
        (
            partial.state_after.wallet_free,
            partial.state_after.wallet_tied
        ),
        (800.0, 250.0)
    );
    assert_eq!(partial.state_after.realized_partial, 50.0);
    assert_eq!(partial.state_after.occupied_slots, 1);
    assert_eq!(partial.state_after.open_trade_ids, [1]);
    assert_eq!(partial.allocated_order_id, Some(2));
    assert_eq!(partial.partial_exit_slot_retained, Some(true));

    let entry = boundaries
        .iter()
        .find(|event| {
            event.boundary == PortfolioBoundary::EntryAccepted && event.pair == "NEW/USDT"
        })
        .expect("compounded entry boundary");
    assert_eq!(
        (entry.configured_pair_index, entry.processing_order_index),
        (0, 1)
    );
    assert_eq!(
        (entry.compounding_base, entry.proposed_stake),
        (Some(1_050.0), Some(525.0))
    );
    assert_eq!(
        (entry.allocated_trade_id, entry.allocated_order_id),
        (Some(2), Some(3))
    );
    assert_eq!(entry.state_after.open_trade_ids, [1, 2]);

    let rejected = boundaries
        .iter()
        .find(|event| event.rejection_reason == Some(EntryRejectionReason::SlotLimit))
        .expect("slot rejection boundary");
    assert_eq!(
        (
            rejected.configured_pair_index,
            rejected.processing_order_index
        ),
        (2, 2)
    );
    assert_eq!(
        (
            rejected.state_before.next_order_id,
            rejected.state_after.next_order_id
        ),
        (4, 4)
    );
    assert_eq!(rejected.state_after.rejected_signals, 1);

    let force = boundaries
        .iter()
        .filter(|event| event.boundary == PortfolioBoundary::ForceExit)
        .collect::<Vec<_>>();
    assert_eq!(force.len(), 2);
    assert_eq!(
        (force[0].pair.as_str(), force[0].force_exit_index),
        ("NEW/USDT", Some(0))
    );
    assert_eq!(force[0].state_before.open_trade_ids, [1, 2]);
    assert_eq!(force[0].state_after.open_trade_ids, [1]);
    assert_eq!(force[0].allocated_order_id, Some(4));
    assert_eq!(
        (force[1].pair.as_str(), force[1].force_exit_index),
        ("OLD/USDT", Some(1))
    );
    assert!(force[1].state_after.open_trade_ids.is_empty());
    assert_eq!(force[1].allocated_order_id, Some(5));
}
