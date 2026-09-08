//! Exchange order-stake bounds, distinct from the available wallet allocation.

use crate::domain::{PairSeries, PortfolioConfig, SimError};

pub(crate) fn maximum_pair_stake(
    pair: &PairSeries,
    rate: f64,
    leverage: f64,
    config: &PortfolioConfig,
) -> Result<f64, SimError> {
    let Some(policy) = &config.order_stake_policy else {
        return Ok(f64::INFINITY);
    };
    let limits = policy
        .pair_limits
        .get(&pair.pair)
        .ok_or(SimError::InvalidPositiveConfig("order_stake_pair_limits"))?;
    let mut maximum = limits.maximum_cost.unwrap_or(f64::INFINITY);
    if let Some(amount) = limits.maximum_amount {
        maximum = maximum.min(amount * rate);
    }
    if config.is_futures {
        if let Some(notional) = config
            .liquidation_model
            .as_ref()
            .and_then(|model| model.tiers_by_pair.get(&pair.pair))
            .and_then(|tiers| {
                tiers
                    .iter()
                    .rev()
                    .find(|tier| leverage <= tier.maximum_leverage)
            })
            .and_then(|tier| tier.max_notional)
        {
            maximum = maximum.min(notional);
        }
    }
    Ok(maximum / leverage)
}

pub(crate) fn adjustment_maximum_stake(
    pair: &PairSeries,
    rate: f64,
    available: f64,
    config: &PortfolioConfig,
) -> Result<f64, SimError> {
    Ok(available.min(maximum_pair_stake(pair, rate, 1.0, config)?))
}

/// Order validation additionally reserves capacity occupied by the current trade.
pub(crate) fn additional_entry_maximum_stake(
    pair: &PairSeries,
    rate: f64,
    leverage: f64,
    available: f64,
    existing_stake: f64,
    config: &PortfolioConfig,
) -> Result<f64, SimError> {
    let maximum = maximum_pair_stake(pair, rate, leverage, config)?;
    Ok(available.min(maximum).min(maximum - existing_stake))
}
