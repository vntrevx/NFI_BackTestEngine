//! Todo 16 exact Spot market execution contracts.

use super::*;

mod support;
use support::*;

#[test]
fn amount_precision_rejection_reserves_order_id_before_later_fill() {
    let pairs = vec![
        pair_series("TINY/USDT", vec![plain(-1), entry(0, 10_000.0), plain(1)]),
        pair_series("FIT/USDT", vec![plain(-1), entry(0, 100.0), plain(1)]),
    ];
    let input = execution_input(exact_config(1), pairs);
    let mut boundaries = Vec::new();
    let result = simulate_with_portfolio_observer(&input, |event| boundaries.push(event.clone()))
        .expect("valid precision rejection");
    let rejected = boundaries
        .iter()
        .find(|event| event.rejection_reason == Some(EntryRejectionReason::StakePrecision))
        .expect("amount precision boundary");
    assert_eq!(rejected.allocated_order_id, Some(1));
    assert_eq!(rejected.state_after.next_order_id, 2);
    assert_eq!(result.trades[0].orders[0].id, 2);
}

#[test]
fn minimum_stake_validation_precedes_order_allocation_and_amount_floor() {
    for (requested, minimum, accepted_stake) in [
        (76.92, 100.0, None),
        (76.93, 100.0, Some(100.0)),
        (100.0, 100.0, Some(100.0)),
    ] {
        let mut settings = exact_config(1);
        settings.stake_amount = requested;
        settings.amount_step = 0.01;
        let mut pair = pair_series("MIN/USDT", vec![plain(-1), entry(0, 10.0), plain(1)]);
        pair.minimum_stake = Some(minimum);
        let input = execution_input(settings, vec![pair]);
        let mut boundaries = Vec::new();
        let result =
            simulate_with_portfolio_observer(&input, |event| boundaries.push(event.clone()))
                .expect("valid minimum stake boundary");
        if let Some(stake) = accepted_stake {
            assert_eq!(result.trades[0].stake_amount, stake);
        } else {
            assert!(result.trades.is_empty());
            let rejection = boundaries
                .iter()
                .find(|event| event.boundary == PortfolioBoundary::EntryRejected)
                .expect("minimum rejection");
            assert_eq!(rejection.allocated_order_id, None);
            assert_eq!(rejection.state_after.next_order_id, 1);
        }
    }
}

#[test]
fn generated_precision_extremes_preserve_quantizer_invariants_and_fail_closed() {
    for step in [0.000_000_01, 0.000_01, 0.01, 1.0, 100.0] {
        for units in [1.0, 2.0, 17.0, 99_999.0] {
            for fraction in [0.000_000_1, 0.499_999_9, 0.5, 0.999_999_9] {
                let value = (units + fraction) * step;
                let floor = floor_step(value, step).expect("generated floor precision");
                let ceil = ceil_step(value, step).expect("generated ceil precision");
                let rounded = round_step(value, step).expect("generated round precision");
                let tolerance = step * 0.000_001;
                assert!(floor <= value + tolerance);
                assert!(ceil + tolerance >= value);
                assert!(ceil - floor <= step + tolerance);
                assert!(
                    (floor - round_step(floor, step).expect("idempotent floor")).abs() <= tolerance
                );
                assert!(
                    (ceil - round_step(ceil, step).expect("idempotent ceil")).abs() <= tolerance
                );
                assert!(floor <= rounded && rounded <= ceil);
            }
        }
    }
    assert_eq!(
        precise_product(&[f64::MAX, f64::MAX]),
        Err(SimError::ExactArithmetic {
            operation: "precise-product"
        })
    );
    assert!(floor_step(f64::NAN, 0.01).is_err());
    assert!(round_step(1.0, 0.0).is_err());
}

#[test]
fn execution_observer_reports_actual_open_fill_and_frozen_exit_rounding() {
    let mut rounded_exit = exit(1, 8.46);
    rounded_exit.high = 8.5;
    let mut pair = pair_series(
        "STEP/USDT",
        vec![plain(-1), entry(0, 8.45), rounded_exit, plain(2)],
    );
    pair.amount_step = Some(0.01);
    pair.price_steps = vec![
        PriceStepChange {
            timestamp_ms: 0,
            step: 0.1,
        },
        PriceStepChange {
            timestamp_ms: 2,
            step: 0.01,
        },
    ];
    let input = execution_input(exact_config(1), vec![pair]);
    let mut events = Vec::new();
    let result = simulate_with_execution_observer(&input, |event| events.push(event.clone()))
        .expect("valid execution observer stream");
    assert_eq!(result.trades[0].open_rate, 8.5);
    assert_eq!(result.trades[0].close_rate, 8.5);
    assert!(events.iter().any(|event| {
        event.phase == ExecutionBoundary::EntryFill && event.price_output.as_deref() == Some("8.5")
    }));
    assert!(events.iter().any(|event| {
        event.phase == ExecutionBoundary::ExitFill
            && event.price_step.as_deref() == Some("0.1")
            && event.price_output.as_deref() == Some("8.5")
            && event.order_type == "limit"
            && event.order_status == Some("filled")
            && event.timeout_checked == Some(false)
    }));
}

#[test]
fn scheduler_event_materializes_complete_trade_order_and_execution_state() {
    let mut pair = pair_series(
        "TRACE/USDT",
        vec![plain(-1), entry(0, 100.0), exit(1, 101.0), plain(2)],
    );
    pair.amount_step = Some(0.01);
    let input = execution_input(exact_config(1), vec![pair]);
    let mut events = Vec::new();

    simulate_with_observer(&input, |event| events.push(event.clone()))
        .expect("valid complete semantic event stream");

    let opened = events
        .iter()
        .find(|event| !event.state.open_trades.is_empty())
        .expect("materialized open trade");
    assert_eq!(opened.state.configured_pair_index, 0);
    assert_eq!(opened.state.processing_order_index, 0);
    assert_eq!(opened.state.occupied_slots, 1);
    assert_eq!(opened.state.slot_limit, 1);
    assert_eq!(
        opened.state.quote_total,
        opened.state.quote_free + opened.state.quote_used
    );
    assert_eq!(opened.state.open_trades[0].orders[0].id, 1);
    assert_eq!(opened.state.open_trades[0].orders[0].funding_fee, 0.0);
    assert!(opened.state.open_trades[0].custom_data.is_empty());
    assert!(opened
        .execution_events
        .iter()
        .any(|event| event.phase == ExecutionBoundary::EntryCandidate));
    assert!(opened
        .execution_events
        .iter()
        .any(|event| event.phase == ExecutionBoundary::EntryFill));

    let closed = events
        .iter()
        .find(|event| !event.state.closed_trades.is_empty())
        .expect("materialized closed trade");
    assert_eq!(closed.state.closed_trades[0].orders.len(), 2);
    assert_eq!(closed.state.closed_trades[0].orders[0].id, 1);
    assert_eq!(closed.state.closed_trades[0].orders[1].id, 2);
}

#[test]
fn rejected_higher_priority_exit_falls_through_and_all_rejected_do_not_fill() {
    for reject_all in [false, true] {
        let mut settings = exact_config(1);
        settings.stoploss_ratio = -0.01;
        settings.exit_confirmation_program = Some(
            serde_json::from_value(serde_json::json!({
                "statements": [
                    {
                        "op": "if",
                        "condition": {
                            "op": "contains",
                            "container": {
                                "op": "literal",
                                "value": if reject_all {
                                    serde_json::json!(["signal_exit", "stop_loss"])
                                } else {
                                    serde_json::json!(["signal_exit"])
                                }
                            },
                            "value": {"op": "variable", "name": "exit_reason"}
                        },
                        "then": [{
                            "op": "return",
                            "value": {"op": "literal", "value": false}
                        }],
                        "otherwise": []
                    },
                    {"op": "return", "value": {"op": "literal", "value": true}}
                ],
                "functions": {}
            }))
            .expect("valid candidate confirmation"),
        );
        let mut collision = exit(1, 100.0);
        collision.low = 98.0;
        let pair = pair_series(
            "COMPETE/USDT",
            vec![plain(-1), entry(0, 100.0), collision, plain(2)],
        );
        let input = execution_input(settings, vec![pair]);
        let mut events = Vec::new();
        let result = simulate_with_execution_observer(&input, |event| events.push(event.clone()))
            .expect("valid candidate iteration");
        assert_eq!(
            result.trades[0].exit_reason,
            if reject_all {
                "force_exit"
            } else {
                "stop_loss"
            }
        );
        let competition = events
            .iter()
            .find(|event| event.phase == ExecutionBoundary::ExitCompetition)
            .expect("direct competition event");
        assert_eq!(competition.candidates, ["signal_exit", "stop_loss"]);
        let outcomes = events
            .iter()
            .filter(|event| event.phase == ExecutionBoundary::ExitConfirmation)
            .map(|event| (event.winner.as_deref(), event.confirmation))
            .collect::<Vec<_>>();
        assert_eq!(outcomes[0], (Some("signal_exit"), Some(false)));
        assert_eq!(outcomes[1], (Some("stop_loss"), Some(!reject_all)));
        if reject_all {
            let force_fill = events
                .iter()
                .find(|event| {
                    event.phase == ExecutionBoundary::ExitFill
                        && event.winner.as_deref() == Some("force_exit")
                })
                .expect("force-exit fill remains directly observable");
            assert_eq!(force_fill.order_id, Some(2));
        }
    }
}
#[test]
fn configured_limit_rates_preserve_raw_confirmation_and_frozen_precision() {
    let mut settings = exact_config(1);
    settings.fee_rate = 0.001;
    settings.amount_step = 0.00001;
    settings.price_step = 0.01;
    settings.stoploss_ratio = -0.05;
    settings.minimal_roi.insert(0, 0.01);
    settings
        .entry_rates_by_pair
        .insert("COMPETE/USDT".to_owned(), BTreeMap::from([(1, 99.237)]));
    settings.exit_rates_by_pair.insert(
        "COMPETE/USDT".to_owned(),
        BTreeMap::from([(300_001, 101.237)]),
    );
    settings.exit_confirmation_program = Some(
        serde_json::from_value(serde_json::json!({
            "statements": [{
                "op": "return",
                "value": {
                    "op": "not",
                    "value": {
                        "op": "equal",
                        "left": {"op": "variable", "name": "exit_reason"},
                        "right": {"op": "literal", "value": "signal_exit"}
                    }
                }
            }],
            "functions": {}
        }))
        .expect("valid primary rejection"),
    );
    let mut collision = exit(300_000, 100.0);
    collision.high = 110.0;
    collision.low = 90.0;
    collision.close = 102.0;
    let input = execution_input(
        settings,
        vec![pair_series(
            "COMPETE/USDT",
            vec![plain(-1), entry(0, 100.0), collision, plain(600_000)],
        )],
    );
    let mut events = Vec::new();
    let result = simulate_with_execution_observer(&input, |event| events.push(event.clone()))
        .expect("valid exact limit competition");
    let trade = &result.trades[0];
    assert_eq!(
        (
            trade.open_rate,
            trade.amount,
            trade.close_rate,
            trade.exit_reason.as_str()
        ),
        (99.24, 1.00765, 94.28, "stop_loss")
    );
    let entry_fill = events
        .iter()
        .find(|event| event.phase == ExecutionBoundary::EntryFill)
        .expect("entry fill");
    assert_eq!(entry_fill.proposed_rate.as_deref(), Some("100"));
    assert_eq!(entry_fill.clamped_rate.as_deref(), Some("99.237"));
    assert_eq!(entry_fill.precision_rate.as_deref(), Some("99.24"));
    assert_eq!(entry_fill.amount_output.as_deref(), Some("1.00765"));
    let outcomes = events
        .iter()
        .filter(|event| event.phase == ExecutionBoundary::ExitConfirmation)
        .map(|event| {
            (
                event.winner.as_deref(),
                event.confirmation,
                event.price_input.as_deref(),
            )
        })
        .collect::<Vec<_>>();
    assert_eq!(
        outcomes,
        [
            (Some("signal_exit"), Some(false), Some("101.237")),
            (Some("stop_loss"), Some(true), Some("94.28")),
        ]
    );
}

#[test]
fn roi_is_confirmed_on_the_entry_candle_with_ftprecise_rate() {
    let mut settings = exact_config(1);
    settings.fee_rate = 0.001;
    settings.amount_step = 0.00001;
    settings.price_step = 0.01;
    settings.minimal_roi.insert(0, 0.01);
    settings
        .entry_rates_by_pair
        .insert("ROI/USDT".to_owned(), BTreeMap::from([(1, 99.237)]));
    settings.exit_confirmation_program = Some(
        serde_json::from_value(serde_json::json!({
            "statements": [{
                "op": "return",
                "value": {"op": "literal", "value": false}
            }],
            "functions": {}
        }))
        .expect("valid ROI rejection"),
    );
    let mut opening = entry(0, 99.24);
    opening.open = 100.0;
    opening.high = 101.0;
    opening.low = 98.0;
    opening.close = 100.0;
    let input = execution_input(
        settings,
        vec![pair_series(
            "ROI/USDT",
            vec![plain(-1), opening, plain(300_000)],
        )],
    );
    let mut events = Vec::new();
    let result = simulate_with_execution_observer(&input, |event| events.push(event.clone()))
        .expect("valid opening-candle ROI check");
    let roi = events
        .iter()
        .find(|event| {
            event.phase == ExecutionBoundary::ExitConfirmation
                && event.winner.as_deref() == Some("roi")
        })
        .expect("opening-candle ROI confirmation");
    assert_eq!(roi.timestamp_ms, 1);
    assert_eq!(roi.price_input.as_deref(), Some("100.43306546546548"));
    assert_eq!(roi.confirmation, Some(false));
    assert_eq!(result.trades[0].exit_reason, "force_exit");
}

#[test]
fn rejected_stop_and_roi_fall_through_to_same_candle_trailing_stop() {
    let mut settings = exact_config(1);
    settings.fee_rate = 0.001;
    settings.price_step = 0.01;
    settings.stoploss_ratio = -0.05;
    settings.minimal_roi.insert(0, 0.01);
    settings.trailing_stop = true;
    settings.trailing_stop_positive = Some(0.02);
    settings.trailing_stop_positive_offset = Some(0.03);
    settings.trailing_only_offset_is_reached = true;
    settings.exit_confirmation_program = Some(
        serde_json::from_value(serde_json::json!({
            "statements": [{
                "op": "return",
                "value": {
                    "op": "equal",
                    "left": {"op": "variable", "name": "exit_reason"},
                    "right": {"op": "literal", "value": "trailing_stop_loss"}
                }
            }],
            "functions": {}
        }))
        .expect("valid trailing acceptance"),
    );
    let mut collision = exit(300_000, 100.0);
    collision.high = 110.0;
    collision.low = 90.0;
    collision.close = 102.0;
    let mut trailing = plain(600_000);
    trailing.open = 102.0;
    trailing.high = 104.0;
    trailing.low = 99.0;
    trailing.close = 103.0;
    let input = execution_input(
        settings,
        vec![pair_series(
            "TRAIL/USDT",
            vec![
                plain(-1),
                entry(0, 99.24),
                collision,
                trailing,
                plain(900_000),
            ],
        )],
    );
    let mut events = Vec::new();
    let result = simulate_with_execution_observer(&input, |event| events.push(event.clone()))
        .expect("valid trailing competition");
    assert_eq!(result.trades[0].exit_reason, "trailing_stop_loss");
    assert_eq!(result.trades[0].close_rate, 101.92);
    let final_competition = events
        .iter()
        .filter(|event| event.phase == ExecutionBoundary::ExitCompetition)
        .next_back()
        .expect("trailing competition");
    assert_eq!(final_competition.candidates, ["roi", "trailing_stop_loss"]);
    let final_confirmation = events
        .iter()
        .filter(|event| event.phase == ExecutionBoundary::ExitConfirmation)
        .next_back()
        .expect("trailing confirmation");
    assert_eq!(
        final_confirmation.winner.as_deref(),
        Some("trailing_stop_loss")
    );
    assert_eq!(final_confirmation.price_input.as_deref(), Some("101.92"));
    assert_eq!(final_confirmation.confirmation, Some(true));
}

#[test]
fn explicit_market_configuration_is_reported_without_changing_open_fill() {
    let mut settings = exact_config(1);
    settings.entry_order_type = OrderType::Market;
    settings.exit_order_type = OrderType::Market;
    let mut market_exit = exit(1, 7.5);
    market_exit.high = 8.0;
    let input = execution_input(
        settings,
        vec![pair_series(
            "MARKET/USDT",
            vec![plain(-1), entry(0, 7.25), market_exit, plain(2)],
        )],
    );
    let mut events = Vec::new();
    let result = simulate_with_execution_observer(&input, |event| events.push(event.clone()))
        .expect("supported Spot market lane");
    assert_eq!(
        (result.trades[0].open_rate, result.trades[0].close_rate),
        (7.25, 8.0)
    );
    assert!(events
        .iter()
        .filter(|event| matches!(
            event.phase,
            ExecutionBoundary::EntryFill | ExecutionBoundary::ExitFill
        ))
        .all(|event| event.order_type == "market"));
}

#[test]
fn entry_gate_events_distinguish_minimum_stake_from_later_rejections() {
    let mut minimum_settings = exact_config(1);
    minimum_settings.stake_amount = 76.92;
    minimum_settings.amount_step = 0.01;
    let mut minimum_pair = pair_series("MINIMUM/USDT", vec![plain(-1), entry(0, 10.0), plain(1)]);
    minimum_pair.minimum_stake = Some(100.0);
    let minimum_input = execution_input(minimum_settings, vec![minimum_pair]);
    let mut minimum_events = Vec::new();
    simulate_with_execution_observer(&minimum_input, |event| {
        minimum_events.push(event.clone());
    })
    .expect("minimum-stake rejection is observable");
    let minimum_gate = minimum_events
        .iter()
        .find(|event| event.phase == ExecutionBoundary::EntryGate)
        .expect("minimum-stake gate event");
    assert_eq!(
        minimum_gate.rejection_reason.as_deref(),
        Some("minimum_stake")
    );
    assert_eq!(minimum_gate.minimum_stake_accepted, Some(false));
    assert_eq!(minimum_gate.order_id, None);

    let mut confirmation_settings = exact_config(1);
    confirmation_settings.amount_step = 0.01;
    confirmation_settings.entry_confirmation_program = Some(
        serde_json::from_value(serde_json::json!({
            "statements": [{
                "op": "return",
                "value": {"op": "literal", "value": false}
            }],
            "functions": {}
        }))
        .expect("valid rejecting confirmation"),
    );
    let confirmation_input = execution_input(
        confirmation_settings,
        vec![pair_series(
            "CONFIRM/USDT",
            vec![plain(-1), entry(0, 100.0), plain(1)],
        )],
    );
    let mut confirmation_events = Vec::new();
    simulate_with_execution_observer(&confirmation_input, |event| {
        confirmation_events.push(event.clone());
    })
    .expect("confirmation rejection is observable");
    let confirmation_gate = confirmation_events
        .iter()
        .find(|event| event.phase == ExecutionBoundary::EntryGate)
        .expect("confirmation gate event");
    assert_eq!(
        confirmation_gate.rejection_reason.as_deref(),
        Some("entry_confirmation")
    );
    assert_eq!(confirmation_gate.minimum_stake_accepted, Some(true));
    assert_eq!(confirmation_gate.order_id, Some(1));
    assert_eq!(
        confirmation_gate
            .state_after
            .as_ref()
            .map(|state| state.next_order_id),
        Some(2)
    );
}

#[test]
fn adjustment_events_bind_direct_fees_ids_precision_and_wallet_states() {
    let mut settings = exact_config(1);
    settings.amount_step = 0.1;
    settings.price_step = 0.01;
    settings.fee_open_rate = Some(0.01);
    settings.fee_close_rate = Some(0.02);
    let mut add = plain(1);
    add.adjustment = Some(AdjustmentSignal {
        stake_amount: 50.0,
        tag: "add".to_owned(),
    });
    let mut partial = plain(2);
    partial.adjustment = Some(AdjustmentSignal {
        stake_amount: -40.0,
        tag: "partial".to_owned(),
    });
    let input = execution_input(
        settings,
        vec![pair_series(
            "ADJUST/USDT",
            vec![
                plain(-1),
                entry(0, 100.0),
                add,
                partial,
                exit(3, 100.0),
                plain(4),
            ],
        )],
    );
    let mut events = Vec::new();
    let result = simulate_with_execution_observer(&input, |event| events.push(event.clone()))
        .expect("adjustment events are observable");
    let trade = &result.trades[0];
    assert_eq!(
        trade
            .orders
            .iter()
            .map(|order| order.id)
            .collect::<Vec<_>>(),
        [1, 2, 3, 4]
    );
    let added = events
        .iter()
        .find(|event| event.phase == ExecutionBoundary::AdjustmentFill)
        .expect("entry adjustment fill event");
    let reduced = events
        .iter()
        .find(|event| event.phase == ExecutionBoundary::PartialExitFill)
        .expect("partial exit fill event");
    assert_eq!((added.trade_id, added.order_id), (Some(1), Some(2)));
    assert_eq!((reduced.trade_id, reduced.order_id), (Some(1), Some(3)));
    assert_eq!(
        (
            added.amount_input.as_deref(),
            added.amount_output.as_deref()
        ),
        (Some("50"), Some("0.5"))
    );
    assert_eq!(
        (
            reduced.amount_input.as_deref(),
            reduced.amount_output.as_deref()
        ),
        (Some("-40"), Some("0.4"))
    );
    assert_eq!(
        (added.fee_open.as_deref(), added.fee_close.as_deref()),
        (Some("0.01"), Some("0.02"))
    );
    assert_eq!(
        (reduced.fee_open.as_deref(), reduced.fee_close.as_deref()),
        (Some("0.01"), Some("0.02"))
    );
    assert_eq!(added.fee_applied.as_deref(), Some("0.5"));
    assert_eq!(reduced.fee_applied.as_deref(), Some("0.8"));
    assert_ne!(
        added.state_before.as_ref().map(|state| state.wallet_tied),
        added.state_after.as_ref().map(|state| state.wallet_tied)
    );
    assert_ne!(
        reduced.state_before.as_ref().map(|state| state.wallet_tied),
        reduced.state_after.as_ref().map(|state| state.wallet_tied)
    );
}
