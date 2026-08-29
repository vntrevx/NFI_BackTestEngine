use super::*;

pub(super) fn exact_config(max_open_trades: usize) -> PortfolioConfig {
    let mut value = config(max_open_trades);
    value.fee_rate = 0.0;
    value.amount_step = 1.0;
    value.price_step = 1.0;
    value.stoploss_ratio = -0.99;
    value
}

pub(super) fn execution_input(config: PortfolioConfig, pairs: Vec<PairSeries>) -> SimulationInput {
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

pub(super) fn entry(timestamp_ms: i64, price: f64) -> Candle {
    let mut value = candle(timestamp_ms, price, price);
    value.high = price;
    value.enter_long = Some(EntrySignal {
        tag: Some("task16".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    value
}

pub(super) fn exit(timestamp_ms: i64, price: f64) -> Candle {
    let mut value = candle(timestamp_ms, price, price);
    value.high = price;
    value.exit_long = Some(ExitSignal {
        reason: "signal_exit".to_owned(),
    });
    value
}
