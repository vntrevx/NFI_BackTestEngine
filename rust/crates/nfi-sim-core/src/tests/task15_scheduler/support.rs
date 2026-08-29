use super::*;

pub(super) fn exact_config(max_open_trades: usize) -> PortfolioConfig {
    let mut value = config(max_open_trades);
    value.fee_rate = 0.0;
    value.amount_step = 1.0;
    value.price_step = 1.0;
    value.stoploss_ratio = -0.99;
    value
}

pub(super) fn portfolio_input(config: PortfolioConfig, pairs: Vec<PairSeries>) -> SimulationInput {
    SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config,
        pairs,
    }
}

pub(super) fn pair_series(pair: &str, mut candles: Vec<Candle>) -> PairSeries {
    for candle in &mut candles {
        candle.timestamp_ms += 1;
    }
    PairSeries {
        pair: pair.to_owned(),
        execution_start_index: 1,
        amount_step: None,
        price_step: None,
        price_steps: Vec::new(),
        minimum_stake: None,
        minimum_amount: None,
        minimum_cost: None,
        feature_columns: BTreeMap::new(),
        candles: candles.into(),
    }
}

pub(super) fn plain(timestamp_ms: i64) -> Candle {
    let mut value = candle(timestamp_ms, 100.0, 100.0);
    value.high = 100.0;
    value
}

pub(super) fn entry(timestamp_ms: i64, tag: &str, price: f64) -> Candle {
    let mut value = candle(timestamp_ms, price, price);
    value.high = price;
    value.enter_long = Some(EntrySignal {
        tag: Some(tag.to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    value
}

pub(super) fn exit(timestamp_ms: i64, price: f64) -> Candle {
    let mut value = candle(timestamp_ms, price, price);
    value.high = price;
    value.exit_long = Some(ExitSignal {
        reason: "signal-exit".to_owned(),
    });
    value
}

pub(super) fn callback_phases(event: &SimulationEvent) -> Vec<CallbackPhase> {
    event
        .callback_events
        .iter()
        .map(|callback| callback.phase)
        .collect()
}

pub(super) fn pairs_at<'event>(events: &[&'event SimulationEvent]) -> Vec<&'event str> {
    events.iter().map(|event| event.pair.as_str()).collect()
}

pub(super) fn assert_trade(
    trade: &ClosedTrade,
    id: u64,
    pair: &str,
    order_ids: &[u64],
    reason: &str,
    stake: f64,
) {
    assert_eq!(trade.id, id);
    assert_eq!(trade.pair, pair);
    assert_eq!(trade.stake_amount, stake);
    assert_eq!(trade.exit_reason, reason);
    assert_eq!(
        trade
            .orders
            .iter()
            .map(|order| order.id)
            .collect::<Vec<_>>(),
        order_ids
    );
}
