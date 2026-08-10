//! Top-level input and configuration validation.

use std::collections::BTreeSet;

use crate::domain::{
    NfiLongGrindRoute, PortfolioConfig, ScalarDecisionProgram, ScalarProgramBundle, SimError,
    SimulationInput,
};
use crate::validate_state_machine_program;
use crate::SIMULATOR_SCHEMA_VERSION;

use super::callback::validate_callback_program;
use super::manager::validate_nfi_trade_manager;
use super::pair::validate_pair_series;

pub(crate) struct ValidationSummary {
    pub(crate) logical_timestamp_batches: u64,
}

#[allow(clippy::too_many_lines)]
pub(crate) fn validate_input(input: &SimulationInput) -> Result<ValidationSummary, SimError> {
    if input.schema_version != SIMULATOR_SCHEMA_VERSION {
        return Err(SimError::UnsupportedSchema(input.schema_version.clone()));
    }
    let config = &input.config;
    for (name, value) in [
        ("starting_balance", config.starting_balance),
        ("stake_amount", config.stake_amount),
        ("amount_step", config.amount_step),
        ("price_step", config.price_step),
    ] {
        if !value.is_finite() || value <= 0.0 {
            return Err(SimError::InvalidPositiveConfig(name));
        }
    }
    if !config.amount_reserve_percent.is_finite()
        || !(0.0..=0.5).contains(&config.amount_reserve_percent)
    {
        return Err(SimError::InvalidPositiveConfig("amount_reserve_percent"));
    }
    if !config.tradable_balance_ratio.is_finite()
        || !(0.0..=1.0).contains(&config.tradable_balance_ratio)
        || config.tradable_balance_ratio == 0.0
    {
        return Err(SimError::InvalidPositiveConfig("tradable_balance_ratio"));
    }
    for (name, value) in [
        ("fee_rate", Some(config.fee_rate)),
        ("fee_open_rate", config.fee_open_rate),
        ("fee_close_rate", config.fee_close_rate),
    ] {
        if value.is_some_and(|rate| !rate.is_finite() || rate < 0.0) {
            return Err(SimError::InvalidPositiveConfig(name));
        }
    }
    validate_leverage_contract(config)?;
    validate_liquidation_contract(config)?;
    if config
        .protection_program
        .as_ref()
        .is_some_and(|program| !program.is_valid())
    {
        return Err(SimError::InvalidPositiveConfig("protection_program"));
    }
    if !config.stoploss_ratio.is_finite()
        || config.stoploss_ratio >= 0.0
        || config.stoploss_ratio <= -1.0
    {
        return Err(SimError::InvalidStoploss);
    }
    if config.max_open_trades == 0 || u32::try_from(config.max_open_trades).is_err() {
        return Err(SimError::InvalidSlots);
    }
    if config
        .funding_fee_interval_ms
        .is_some_and(|interval| interval <= 0)
    {
        return Err(SimError::InvalidPositiveConfig("funding_fee_interval_ms"));
    }
    if let Some(duration) = config.custom_exit_after_ms {
        if duration <= 0 {
            return Err(SimError::InvalidPositiveConfig("custom_exit_after_ms"));
        }
    }
    if let Some(rule) = &config.adjustment_rule {
        if !rule.profit_below.is_finite()
            || !rule.stake_ratio.is_finite()
            || rule.stake_ratio <= 0.0
            || rule.tag.is_empty()
        {
            return Err(SimError::InvalidPositiveConfig("adjustment_rule"));
        }
    }
    if let Some(program) = &config.callback_program {
        validate_callback_program(program)?;
    }
    if config.callback_program.is_some() && config.state_machine_program.is_some() {
        return Err(SimError::InvalidStateMachineProgram);
    }
    if config.state_machine_program.is_some()
        && (config.nfi_x7_trade_manager.is_some()
            || config.adjust_trade_position_program.is_some()
            || config.custom_exit_program.is_some())
    {
        return Err(SimError::InvalidStateMachineProgram);
    }
    if config
        .state_machine_program
        .as_ref()
        .is_some_and(|program| !validate_state_machine_program(program))
    {
        return Err(SimError::InvalidStateMachineProgram);
    }
    if config
        .stake_program
        .as_ref()
        .is_some_and(|program| program.statements.is_empty())
    {
        return Err(SimError::InvalidPositiveConfig("stake_program"));
    }
    if config
        .entry_confirmation_program
        .as_ref()
        .is_some_and(|program| program.statements.is_empty())
    {
        return Err(SimError::InvalidPositiveConfig(
            "entry_confirmation_program",
        ));
    }
    if config
        .exit_confirmation_program
        .as_ref()
        .is_some_and(|program| program.statements.is_empty())
    {
        return Err(SimError::InvalidPositiveConfig("exit_confirmation_program"));
    }
    validate_scalar_callback_bundles(config)?;
    // Timestamp batches are a logical profile counter, not scheduler work.
    // Collect the distinct execution-visible timestamps while validation is
    // already reading every row. This avoids a second full scan of multi-year
    // file-backed vectors solely to preserve the profiling contract.
    let mut logical_timestamps = BTreeSet::new();
    for (pair_index, pair) in input.pairs.iter().enumerate() {
        validate_pair_series(
            pair_index,
            pair,
            config.nfi_x7_trade_manager.as_ref(),
            config.is_futures,
            &mut logical_timestamps,
        )?;
    }
    Ok(ValidationSummary {
        logical_timestamp_batches: u64::try_from(logical_timestamps.len()).unwrap_or(u64::MAX),
    })
}

fn validate_leverage_contract(config: &PortfolioConfig) -> Result<(), SimError> {
    if config
        .leverage
        .is_some_and(|leverage| !leverage.is_finite() || leverage <= 0.0)
    {
        return Err(SimError::InvalidPositiveConfig("leverage"));
    }
    if let Some(program) = &config.nfi_leverage_program {
        let invalid_rule = program.ordered_tag_overrides.iter().any(|rule| {
            !rule.leverage.is_finite()
                || rule.leverage <= 0.0
                || rule.entry_tags.is_empty()
                || rule.entry_tags.iter().any(String::is_empty)
                || rule.entry_tags.iter().collect::<BTreeSet<_>>().len() != rule.entry_tags.len()
        });
        if !program.default.is_finite()
            || program.default <= 0.0
            || program.ordered_tag_overrides.is_empty()
            || invalid_rule
        {
            return Err(SimError::InvalidPositiveConfig("nfi_leverage_program"));
        }
    }
    if config
        .maximum_leverage_by_pair
        .iter()
        .any(|(pair, value)| pair.is_empty() || !value.is_finite() || *value < 1.0)
    {
        return Err(SimError::InvalidPositiveConfig("maximum_leverage_by_pair"));
    }
    Ok(())
}

fn validate_liquidation_contract(config: &PortfolioConfig) -> Result<(), SimError> {
    let Some(model) = &config.liquidation_model else {
        return Ok(());
    };
    let valid_exchange = matches!(model.exchange.as_str(), "binance" | "binanceusdm");
    let valid_model = config.is_futures
        && valid_exchange
        && model.margin_mode == "isolated"
        && model.buffer.is_finite()
        && (0.0..=0.99).contains(&model.buffer)
        && !model.tiers_by_pair.is_empty()
        && model.tiers_by_pair.iter().all(|(pair, tiers)| {
            !pair.is_empty()
                && !tiers.is_empty()
                && tiers.first().is_some_and(|tier| tier.min_notional == 0.0)
                && tiers
                    .windows(2)
                    .all(|window| window[0].min_notional < window[1].min_notional)
                && tiers.iter().all(|tier| {
                    tier.min_notional.is_finite()
                        && tier.min_notional >= 0.0
                        && tier
                            .max_notional
                            .is_none_or(|value| value.is_finite() && value > tier.min_notional)
                        && tier.maximum_leverage.is_finite()
                        && tier.maximum_leverage >= 1.0
                        && tier.maintenance_margin_rate.is_finite()
                        && (0.0..1.0).contains(&tier.maintenance_margin_rate)
                        && tier
                            .maintenance_amount
                            .is_some_and(|value| value.is_finite() && value >= 0.0)
                })
        });
    if valid_model {
        Ok(())
    } else {
        Err(SimError::InvalidPositiveConfig("liquidation_model"))
    }
}

fn validate_scalar_callback_bundles(config: &PortfolioConfig) -> Result<(), SimError> {
    if config.max_entry_position_adjustment < -1 {
        return Err(SimError::InvalidPositiveConfig(
            "max_entry_position_adjustment",
        ));
    }
    for (name, bundle) in [
        ("custom_exit_program", config.custom_exit_program.as_ref()),
        (
            "adjust_trade_position_program",
            config.adjust_trade_position_program.as_ref(),
        ),
    ] {
        if bundle.is_some_and(|bundle| !valid_scalar_program_bundle(bundle)) {
            return Err(SimError::InvalidPositiveConfig(name));
        }
    }
    if let Some(manager) = &config.nfi_x7_trade_manager {
        validate_nfi_trade_manager(config, manager)?;
    }
    Ok(())
}

pub(crate) fn valid_scalar_program_bundle(bundle: &ScalarProgramBundle) -> bool {
    bundle.schema_version == "1.0.0"
        && !bundle.entry.is_empty()
        && bundle.programs.contains_key(&bundle.entry)
        && bundle
            .programs
            .iter()
            .all(|(name, program)| !name.is_empty() && valid_scalar_program(program))
}

pub(crate) fn valid_scalar_program(program: &ScalarDecisionProgram) -> bool {
    matches!(program.schema_version.as_str(), "1.0.0" | "1.1.0" | "1.2.0")
        && program.opcode == "scalar-decision-program-v1"
        && program
            .parameters
            .iter()
            .all(|parameter| !parameter.is_empty())
}

pub(crate) fn uses_full_futures_manager_contract(schema_version: &str) -> bool {
    matches!(
        schema_version,
        "0.15.0"
            | "0.16.0"
            | "0.17.0"
            | "0.18.0"
            | "0.19.0"
            | "0.20.0"
            | "0.21.0"
            | "0.22.0"
            | "0.23.0"
            | "0.24.0"
            | "0.25.0"
            | "0.26.0"
            | "0.27.0"
            | "0.28.0"
            | "0.29.0"
    )
}

pub(crate) fn valid_legacy_futures_fallback(
    route: &NfiLongGrindRoute,
    schema_version: &str,
) -> bool {
    route
        .futures_fallback_loss_threshold
        .is_some_and(|threshold| threshold.is_finite() && threshold < 0.0)
        || (!matches!(
            schema_version,
            "0.16.0"
                | "0.17.0"
                | "0.18.0"
                | "0.19.0"
                | "0.20.0"
                | "0.21.0"
                | "0.22.0"
                | "0.23.0"
                | "0.24.0"
                | "0.25.0"
                | "0.26.0"
                | "0.27.0"
                | "0.28.0"
                | "0.29.0"
        ) && route.futures_fallback_loss_threshold.is_none())
}
