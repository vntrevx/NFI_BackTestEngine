//! Source-resolver exit switches preserve Freqtrade's arbitration semantics.

use super::*;

fn policy() -> StrategyExitPolicy {
    StrategyExitPolicy {
        evaluate_exit_on_entry: false,
        timeframe_minutes: 5,
        use_exit_signal: true,
        exit_profit_only: false,
        exit_profit_offset: 0.0,
        ignore_roi_if_entry_signal: false,
        inherited_custom_stoploss: false,
    }
}

fn input(policy: StrategyExitPolicy) -> SimulationInput {
    let mut settings = config(1);
    settings.stoploss_ratio = -0.99;
    settings.strategy_exit_policy = Some(policy);
    let mut opening = candle(1, 100.0, 100.0);
    opening.enter_long = Some(EntrySignal {
        tag: Some("entry".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: settings,
        pairs: vec![PairSeries {
            pair: "AAA/USDT".to_owned(),
            execution_start_index: 0,
            amount_step: None,
            price_step: None,
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: BTreeMap::new(),
            candles: vec![opening, candle(2, 105.0, 105.0), candle(3, 105.0, 105.0)].into(),
        }],
    }
}

fn custom_exit(input: &mut SimulationInput) {
    input.config.custom_exit_program = Some(serde_json::from_value(serde_json::json!({
        "schema_version": "1.0.0", "entry": "custom_exit", "programs": {
            "custom_exit": {
                "schema_version": "1.1.0", "opcode": "scalar-decision-program-v1",
                "parameters": ["pair", "trade", "current_time", "current_rate", "current_profit"],
                "expressions": [["variable", "current_profit"], ["literal", 0.01],
                    ["compare", 0, [["greater", 1]]], ["literal", "native_custom_exit"],
                    ["literal", null]],
                "statements": [["if", 2, [["return", 3]], []], ["return", 4]]
            }
        }
    })).expect("valid custom exit"));
}

#[test]
fn disabled_strategy_exits_skip_custom_exit_and_preserve_risk_exits() {
    let mut settings = policy();
    settings.use_exit_signal = false;
    let mut scenario = input(settings);
    custom_exit(&mut scenario);
    assert_eq!(
        simulate(&scenario).unwrap().trades[0].exit_reason,
        "force_exit"
    );
    scenario.config.stoploss_ratio = -0.01;
    scenario.pairs[0].candles = vec![
        scenario.pairs[0].candles.get(0).unwrap().into_owned(),
        candle(2, 95.0, 94.0),
    ]
    .into();
    assert_eq!(
        simulate(&scenario).unwrap().trades[0].exit_reason,
        "stop_loss"
    );
}

#[test]
fn roi_can_be_suppressed_by_the_same_side_entry_signal() {
    let mut settings = policy();
    settings.ignore_roi_if_entry_signal = true;
    let mut scenario = input(settings);
    scenario.config.minimal_roi.insert(0, 0.01);
    let held = simulate(&scenario).unwrap();
    assert_eq!(held.trades[0].close_timestamp_ms, 2);
    scenario
        .config
        .strategy_exit_policy
        .as_mut()
        .unwrap()
        .ignore_roi_if_entry_signal = false;
    let immediate = simulate(&scenario).unwrap();
    assert_eq!(immediate.trades[0].close_timestamp_ms, 1);
    assert_eq!(immediate.trades[0].exit_reason, "roi");
}

#[test]
fn profit_only_filters_signals_but_does_not_filter_custom_exits() {
    let mut settings = policy();
    settings.exit_profit_only = true;
    settings.exit_profit_offset = 0.10;
    let mut scenario = input(settings);
    custom_exit(&mut scenario);
    let mut bar = candle(2, 105.0, 105.0);
    bar.exit_long = Some(ExitSignal {
        reason: "exit_signal".to_owned(),
    });
    scenario.pairs[0].candles = vec![
        scenario.pairs[0].candles.get(0).unwrap().into_owned(),
        bar,
        candle(3, 105.0, 105.0),
    ]
    .into();
    let filtered = simulate(&scenario).unwrap();
    assert_eq!(filtered.trades[0].close_timestamp_ms, 3);
    assert_eq!(filtered.trades[0].exit_reason, "native_custom_exit");
}

#[test]
fn source_trailing_settings_match_official_profit_and_price_vectors() {
    use crate::execution::strategy_settings::{
        source_current_profit_ratio, source_trailing_exit_rate, update_source_trailing_stop,
    };
    let document: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../benchmarks/evidence/x8-settings-2026-09-08/official-exit-settings-results.json"
    )).unwrap();
    let cases = document["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 96);
    for (index, case) in cases.iter().enumerate() {
        let short = case["short"].as_bool().unwrap();
        let mut settings = config(1);
        settings.is_futures = case["leverage"].as_f64().unwrap() > 1.0;
        settings.fee_open_rate = Some(0.0004);
        settings.fee_close_rate = Some(0.0005);
        settings.stoploss_ratio = -0.20;
        settings.trailing_stop = true;
        settings.trailing_only_offset_is_reached = case["only_offset"].as_bool().unwrap();
        settings.trailing_stop_positive_offset = Some(0.03);
        settings.trailing_stop_positive = case["positive"].as_f64();
        let mut trade = super::virtual_fees::initial_trade();
        trade.side = if short {
            TradeSide::Short
        } else {
            TradeSide::Long
        };
        trade.amount = 0.123;
        trade.leverage = case["leverage"].as_f64().unwrap();
        trade.funding_fees = case["funding"].as_f64().unwrap();
        trade.funding_fees_total = trade.funding_fees;
        let distance = 0.20 / trade.leverage;
        trade.stop_loss = if short {
            floor_step(100.0 * (1.0 + distance), 0.01).unwrap()
        } else {
            ceil_step(100.0 * (1.0 - distance), 0.01).unwrap()
        };
        trade.initial_stop_loss = trade.stop_loss;
        let elapsed = case["elapsed_minutes"].as_i64().unwrap() * 60_000;
        let mut bar = candle(
            trade.open_timestamp_ms + elapsed,
            100.0,
            case["low"].as_f64().unwrap(),
        );
        bar.high = case["high"].as_f64().unwrap();
        let bound = if short { bar.low } else { bar.high };
        let ratio = source_current_profit_ratio(&trade, bound, &settings).unwrap();
        assert_eq!(
            ratio.to_bits(),
            case["profit_ratio"].as_f64().unwrap().to_bits(),
            "ratio {index}"
        );
        update_source_trailing_stop(&mut trade, &bar, &settings).unwrap();
        assert_eq!(
            trade.stop_loss.to_bits(),
            case["stop_loss"].as_f64().unwrap().to_bits(),
            "stop {index}"
        );
        let stop_ratio = trade
            .custom_stop_loss_ratio
            .unwrap_or(settings.stoploss_ratio);
        assert_eq!(
            stop_ratio.to_bits(),
            case["stop_loss_ratio"].as_f64().unwrap().to_bits(),
            "stop ratio {index}"
        );
        if let Some(rate) = case["exit_rate"].as_f64() {
            assert_eq!(
                source_trailing_exit_rate(&trade, &bar, &settings).to_bits(),
                rate.to_bits(),
                "exit {index}"
            );
        }
    }
}

#[test]
fn inherited_stop_refresh_matches_official_vectors() {
    use crate::execution::strategy_settings::{
        refresh_inherited_stop_after_fill, update_source_trailing_stop,
    };
    let document: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../benchmarks/evidence/x8-settings-2026-09-08/official-lifecycle-settings-results.json"
    )).unwrap();
    let cases = document["stop_cases"].as_array().unwrap();
    assert_eq!(cases.len(), 192);
    for (index, case) in cases.iter().enumerate() {
        let (mut trade, mut settings) = lifecycle_case(case);
        settings.trailing_stop = case["trailing"].as_bool().unwrap();
        settings.trailing_only_offset_is_reached = case["only_offset"].as_bool().unwrap();
        settings.trailing_stop_positive_offset = Some(0.03);
        settings.trailing_stop_positive = case["positive"].as_f64();
        settings
            .strategy_exit_policy
            .as_mut()
            .unwrap()
            .inherited_custom_stoploss = true;
        let short = trade.side == TradeSide::Short;
        let mut bar = candle(
            trade.open_timestamp_ms,
            100.0,
            if short { 96.0 } else { 99.0 },
        );
        bar.high = if short { 101.0 } else { 104.0 };
        update_source_trailing_stop(&mut trade, &bar, &settings).unwrap();
        assert_stop_state(&trade, &case["before"], index);
        refresh_inherited_stop_after_fill(&mut trade, case["fill"].as_f64().unwrap(), &settings)
            .unwrap();
        assert_stop_state(&trade, &case["after"], index);
    }
}

fn assert_stop_state(
    trade: &crate::portfolio::OpenTrade,
    expected: &serde_json::Value,
    index: usize,
) {
    assert_eq!(
        trade.stop_loss.to_bits(),
        expected["stop_loss"].as_f64().unwrap().to_bits(),
        "stop {index}"
    );
    assert_eq!(
        trade.custom_stop_loss_ratio.unwrap_or(-0.20).to_bits(),
        expected["stop_loss_ratio"].as_f64().unwrap().to_bits(),
        "ratio {index}"
    );
    assert_eq!(
        trade.is_stop_loss_trailing,
        expected["trailing"].as_bool().unwrap(),
        "trailing {index}"
    );
}

fn lifecycle_case(case: &serde_json::Value) -> (crate::portfolio::OpenTrade, PortfolioConfig) {
    let mut settings = config(1);
    settings.strategy_exit_policy = Some(policy());
    settings.is_futures = case["leverage"].as_f64().unwrap() > 1.0;
    settings.fee_open_rate = Some(0.0004);
    settings.fee_close_rate = Some(0.0005);
    settings.stoploss_ratio = -0.20;
    let mut trade = super::virtual_fees::initial_trade();
    trade.side = if case["short"].as_bool().unwrap() {
        TradeSide::Short
    } else {
        TradeSide::Long
    };
    trade.amount = 0.123;
    trade.leverage = case["leverage"].as_f64().unwrap();
    // Running funding differs deliberately: callback ratios must use the
    // cumulative order-derived value after partial exits.
    trade.funding_fees = 0.025;
    trade.funding_fees_total = case["funding"].as_f64().unwrap();
    trade.stop_loss = if trade.side == TradeSide::Short {
        floor_step(100.0 * (1.0 + 0.20 / trade.leverage), 0.01).unwrap()
    } else {
        ceil_step(100.0 * (1.0 - 0.20 / trade.leverage), 0.01).unwrap()
    };
    trade.initial_stop_loss = trade.stop_loss;
    (trade, settings)
}

#[test]
fn leveraged_roi_and_opening_boundaries_match_official_vectors() {
    use crate::execution::strategy_settings::source_roi_exit_rate;
    let document: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../benchmarks/evidence/x8-settings-2026-09-08/official-lifecycle-settings-results.json"
    )).unwrap();
    let cases = document["roi_cases"].as_array().unwrap();
    assert_eq!(cases.len(), 864);
    for (index, case) in cases.iter().enumerate() {
        let (mut trade, settings) = lifecycle_case(case);
        trade.open_rate = case["open_rate"].as_f64().unwrap();
        let elapsed = case["elapsed"].as_u64().unwrap();
        let mut bar = candle(
            trade.open_timestamp_ms + i64::try_from(elapsed).unwrap() * 60_000,
            100.0,
            96.0,
        );
        bar.high = 104.0;
        bar.close = if trade.side == TradeSide::Short {
            103.0
        } else {
            97.0
        };
        let rate = source_roi_exit_rate(
            &trade,
            &bar,
            &settings,
            elapsed,
            case["entry"].as_u64().unwrap(),
            case["roi"].as_f64().unwrap(),
        )
        .unwrap();
        assert_eq!(
            rate.map(f64::to_bits),
            case["exit_rate"].as_f64().map(f64::to_bits),
            "ROI {index}: {case}"
        );
    }
}

#[test]
fn source_trailing_offset_requires_a_positive_distance() {
    let mut scenario = input(policy());
    scenario.config.trailing_stop = true;
    scenario.config.trailing_only_offset_is_reached = true;
    scenario.config.trailing_stop_positive = None;
    scenario.config.trailing_stop_positive_offset = Some(0.03);
    assert!(validate_simulator_preflight(&scenario.config).is_err());
    scenario.config.trailing_stop_positive = Some(0.0);
    assert!(validate_simulator_preflight(&scenario.config).is_err());
    scenario.config.trailing_stop_positive = Some(0.04);
    assert!(validate_simulator_preflight(&scenario.config).is_err());
    scenario.config.trailing_stop_positive = Some(0.01);
    assert_eq!(validate_simulator_preflight(&scenario.config), Ok(()));
}

#[test]
fn entry_candle_custom_exit_obeys_source_policy_and_preserves_archives() {
    let mut settings = policy();
    settings.evaluate_exit_on_entry = true;
    let mut scenario = input(settings);
    scenario.pairs[0].execution_start_index = 1;
    scenario.pairs[0].candles = vec![
        candle(0, 100.0, 100.0),
        scenario.pairs[0].candles.get(0).unwrap().into_owned(),
        candle(2, 105.0, 105.0),
        candle(3, 105.0, 105.0),
    ]
    .into();
    scenario.config.custom_exit_program = Some(serde_json::from_value(serde_json::json!({
        "schema_version": "1.0.0", "entry": "custom_exit", "programs": {
            "custom_exit": {
                "schema_version": "1.1.0", "opcode": "scalar-decision-program-v1",
                "parameters": ["pair", "trade", "current_time", "current_rate", "current_profit"],
                "expressions": [["literal", "entry_exit"]],
                "statements": [["return", 0]]
            }
        }
    })).unwrap());
    let result = simulate(&scenario).unwrap();
    assert_eq!(
        result.trades[0].open_timestamp_ms,
        result.trades[0].close_timestamp_ms
    );
    assert_eq!(result.trades[0].exit_reason, "entry_exit");
    scenario
        .config
        .strategy_exit_policy
        .as_mut()
        .unwrap()
        .evaluate_exit_on_entry = false;
    assert_eq!(simulate(&scenario).unwrap().trades[0].close_timestamp_ms, 2);
    scenario
        .config
        .strategy_exit_policy
        .as_mut()
        .unwrap()
        .evaluate_exit_on_entry = true;
    scenario
        .config
        .strategy_exit_policy
        .as_mut()
        .unwrap()
        .use_exit_signal = false;
    assert_eq!(
        simulate(&scenario).unwrap().trades[0].exit_reason,
        "force_exit"
    );
}
