//! Fail-closed validation for simulator inputs and compiled strategy contracts.

use std::collections::BTreeSet;

use serde_json::Value;

use super::{
    CallbackProgram, Candle, CustomDataWrite, EntrySignal, NfiLegacyGrindConstants,
    NfiLongGrindRoute, NfiManagedLongProfile, NfiManagedLongRoute, NfiRegularAdjustmentConstants,
    NfiX7AdjustmentCondition, NfiX7AdjustmentConstants, NfiX7AdjustmentOperand,
    NfiX7AdjustmentPolicy, NfiX7RebuyConstants, NfiX7TradeManager, PairSeries, PortfolioConfig,
    ScalarDecisionProgram, ScalarProgramBundle, SimError, SimulationInput, TradeSide,
    SIMULATOR_SCHEMA_VERSION,
};

pub(super) struct ValidationSummary {
    pub(super) logical_timestamp_batches: u64,
}

#[allow(clippy::too_many_lines)]
pub(super) fn validate_input(input: &SimulationInput) -> Result<ValidationSummary, SimError> {
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

fn valid_scalar_program_bundle(bundle: &ScalarProgramBundle) -> bool {
    bundle.schema_version == "1.0.0"
        && !bundle.entry.is_empty()
        && bundle.programs.contains_key(&bundle.entry)
        && bundle
            .programs
            .iter()
            .all(|(name, program)| !name.is_empty() && valid_scalar_program(program))
}

fn valid_scalar_program(program: &ScalarDecisionProgram) -> bool {
    matches!(program.schema_version.as_str(), "1.0.0" | "1.1.0" | "1.2.0")
        && program.opcode == "scalar-decision-program-v1"
        && program
            .parameters
            .iter()
            .all(|parameter| !parameter.is_empty())
}

fn uses_full_futures_manager_contract(schema_version: &str) -> bool {
    matches!(schema_version, "0.15.0" | "0.16.0")
}

fn valid_legacy_futures_fallback(route: &NfiLongGrindRoute, schema_version: &str) -> bool {
    route
        .futures_fallback_loss_threshold
        .is_some_and(|threshold| threshold.is_finite() && threshold < 0.0)
        || (schema_version != "0.16.0" && route.futures_fallback_loss_threshold.is_none())
}

#[allow(clippy::too_many_lines)] // One fail-closed audit keeps all route invariants co-located.
fn validate_nfi_trade_manager(
    config: &PortfolioConfig,
    manager: &NfiX7TradeManager,
) -> Result<(), SimError> {
    const PROGRAM_ORDER: [&str; 4] = [
        "long_exit_signals",
        "long_exit_main",
        "long_exit_williams_r",
        "long_exit_dec",
    ];
    const SHORT_PROGRAM_ORDER: [&str; 4] = [
        "short_exit_signals",
        "short_exit_main",
        "short_exit_williams_r",
        "short_exit_dec",
    ];
    const ADJUSTMENT_ORDER: [&str; 18] = [
        "derisk_level_1",
        "derisk_level_2",
        "derisk_level_3",
        "grind_1_entry",
        "grind_1_exit",
        "grind_1_derisk",
        "grind_2_entry",
        "grind_2_exit",
        "grind_2_derisk",
        "grind_3_entry",
        "grind_3_exit",
        "grind_3_derisk",
        "grind_4_entry",
        "grind_4_exit",
        "grind_4_derisk",
        "grind_5_entry",
        "grind_5_exit",
        "grind_5_derisk",
    ];
    let long_grind = manager.long_grind.as_ref();
    let long_btc = manager.long_btc.as_ref();
    let adjustment = manager.position_adjustment.as_ref();
    let short_adjustment = manager.short_position_adjustment.as_ref();
    let constants = &manager.constants;
    let managed_keys = manager
        .managed_long_routes
        .iter()
        .map(|route| route.key.as_str())
        .collect::<BTreeSet<_>>();
    let expected_managed_keys = [
        "long_normal",
        "long_pump",
        "long_quick",
        "long_rebuy",
        "long_high_profit",
        "long_rapid",
        "long_top_coins",
        "long_scalp",
    ]
    .into_iter()
    .collect::<BTreeSet<_>>();
    let managed_tags = manager
        .managed_long_routes
        .iter()
        .flat_map(|route| &route.entry_tags)
        .collect::<BTreeSet<_>>();
    let total_managed_tag_count = manager
        .managed_long_routes
        .iter()
        .map(|route| route.entry_tags.len())
        .sum::<usize>();
    let short_keys = manager
        .managed_short_routes
        .iter()
        .map(|route| route.key.as_str())
        .collect::<BTreeSet<_>>();
    let short_tags = manager
        .managed_short_routes
        .iter()
        .flat_map(|route| &route.entry_tags)
        .collect::<BTreeSet<_>>();
    let total_short_tag_count = manager
        .managed_short_routes
        .iter()
        .map(|route| route.entry_tags.len())
        .sum::<usize>();
    let valid_identity = matches!(
        manager.schema_version.as_str(),
        "0.9.0" | "0.10.0" | "0.11.0" | "0.12.0" | "0.13.0" | "0.14.0" | "0.15.0" | "0.16.0"
    ) && manager.source_sha256.len() == 64
        && manager
            .source_sha256
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte));
    let valid_managed_routes = manager.managed_long_routes.len() == expected_managed_keys.len()
        && managed_keys == expected_managed_keys
        && managed_tags.len() == total_managed_tag_count
        && manager
            .managed_long_routes
            .iter()
            .all(valid_nfi_managed_long_route);
    let valid_terminal_exit_version = matches!(
        manager.schema_version.as_str(),
        "0.11.0" | "0.12.0" | "0.13.0" | "0.14.0" | "0.15.0" | "0.16.0"
    ) || manager
        .managed_long_routes
        .iter()
        .all(|route| route.terminal_exit.is_none());
    let expected_short_order = if uses_full_futures_manager_contract(&manager.schema_version) {
        vec![
            "short_normal",
            "short_pump",
            "short_quick",
            "short_rebuy",
            "short_high_profit",
            "short_rapid",
            "short_scalp",
            "short_top_coins_fallback",
        ]
    } else {
        vec!["short_rebuy"]
    };
    let expected_short_keys = expected_short_order
        .iter()
        .copied()
        .collect::<BTreeSet<_>>();
    let valid_short_routes = manager.managed_short_routes.len() == expected_short_keys.len()
        && short_keys == expected_short_keys
        && short_tags.len() == total_short_tag_count
        && short_tags.iter().all(|tag| !managed_tags.contains(*tag))
        && manager
            .managed_short_routes
            .iter()
            .all(valid_nfi_managed_short_route)
        && manager.short_route_order
            == expected_short_order
                .iter()
                .map(ToString::to_string)
                .collect::<Vec<_>>();
    let expected_route_order = [
        "long_normal",
        "long_pump",
        "long_quick",
        "long_rebuy",
        "long_high_profit",
        "long_rapid",
        "long_grind",
        "long_btc",
        "long_top_coins",
        "long_scalp",
    ]
    .into_iter()
    .filter(|key| {
        managed_keys.contains(key)
            || (*key == "long_grind" && long_grind.is_some())
            || (*key == "long_btc" && long_btc.is_some())
    })
    .map(ToOwned::to_owned)
    .collect::<Vec<_>>();
    let valid_route_order = manager.route_order == expected_route_order;
    let valid_long_grind = long_grind.is_none_or(|route| {
        let route_tags = route.entry_tags.iter().collect::<BTreeSet<_>>();
        let tags_are_disjoint = route_tags.iter().all(|tag| !managed_tags.contains(*tag));
        !route.mode_name.is_empty()
            && !route.entry_tags.is_empty()
            && route_tags.len() == route.entry_tags.len()
            && route.entry_tags.iter().all(|tag| !tag.is_empty())
            && tags_are_disjoint
            && route.exit_profit_threshold.is_finite()
            && route.exit_profit_threshold > 0.0
            && match route.adjustment_scope.as_str() {
                // Preserve the narrower replay contract of older evidence.
                "spot-grind-backtest-v1" => true,
                "grind-backtest-v2" => {
                    matches!(
                        manager.schema_version.as_str(),
                        "0.14.0" | "0.15.0" | "0.16.0"
                    )
                }
                _ => false,
            }
            && route.grind_mode
            && route.decision_program == "long_grind_entry_v3"
            && route.first_entry_profit_threshold_spot.is_finite()
            && route.first_entry_profit_threshold_spot > 0.0
            && route.first_entry_stop_threshold_spot.is_finite()
            && route.first_entry_stop_threshold_spot < 0.0
            && valid_legacy_futures_fallback(route, &manager.schema_version)
            && route.stateful_input_contract.is_object()
            && route.regular_decision_program.is_none()
            && route.regular_constants.is_none()
            && valid_nfi_legacy_grind_constants(&route.constants)
    });
    let grind_tags = long_grind
        .into_iter()
        .flat_map(|route| &route.entry_tags)
        .collect::<BTreeSet<_>>();
    let valid_long_btc = long_btc.is_none_or(|route| {
        let route_tags = route.entry_tags.iter().collect::<BTreeSet<_>>();
        let tags_are_disjoint = route_tags
            .iter()
            .all(|tag| !managed_tags.contains(*tag) && !grind_tags.contains(*tag));
        !route.mode_name.is_empty()
            && !route.entry_tags.is_empty()
            && route_tags.len() == route.entry_tags.len()
            && route.entry_tags.iter().all(|tag| !tag.is_empty())
            && tags_are_disjoint
            && route.exit_profit_threshold.is_finite()
            && route.exit_profit_threshold > 0.0
            && route.adjustment_scope == "regular-backtest-v2"
            && !route.grind_mode
            && route.decision_program == "long_grind_entry_v3"
            && route.first_entry_profit_threshold_spot.is_finite()
            && route.first_entry_profit_threshold_spot > 0.0
            && route.first_entry_stop_threshold_spot.is_finite()
            && route.first_entry_stop_threshold_spot < 0.0
            && valid_legacy_futures_fallback(route, &manager.schema_version)
            && route.stateful_input_contract.is_object()
            && route.regular_decision_program.as_deref() == Some("long_grind_entry")
            && route
                .regular_constants
                .as_ref()
                .is_some_and(valid_nfi_regular_adjustment_constants)
            && valid_nfi_legacy_grind_constants(&route.constants)
    });
    let valid_programs = manager.programs.len()
        == PROGRAM_ORDER.len()
            + SHORT_PROGRAM_ORDER.len()
            + usize::from(adjustment.is_some())
            + usize::from(short_adjustment.is_some())
            + usize::from(long_btc.is_some())
        && PROGRAM_ORDER.iter().all(|name| {
            manager
                .programs
                .get(*name)
                .is_some_and(valid_scalar_program)
        })
        && SHORT_PROGRAM_ORDER.iter().all(|name| {
            manager
                .programs
                .get(*name)
                .is_some_and(valid_scalar_program)
        })
        && long_btc.is_none_or(|route| {
            route
                .regular_decision_program
                .as_ref()
                .is_some_and(|name| manager.programs.get(name).is_some_and(valid_scalar_program))
        })
        && adjustment.is_none_or(|adjustment| {
            manager
                .programs
                .get(&adjustment.decision_program)
                .is_some_and(valid_scalar_program)
        })
        && short_adjustment.is_none_or(|adjustment| {
            manager
                .programs
                .get(&adjustment.decision_program)
                .is_some_and(valid_scalar_program)
        });
    let valid_adjustment_route = adjustment.is_none_or(|adjustment| {
        let adjustment_tags = adjustment.entry_tags.iter().collect::<BTreeSet<_>>();
        let versioned_rebuy_multiplier = match manager.schema_version.as_str() {
            "0.9.0" => {
                adjustment.constants.rebuy_stake_multiplier.is_none()
                    && adjustment.constants.policy.is_none()
            }
            "0.10.0" | "0.11.0" => {
                adjustment
                    .constants
                    .rebuy_stake_multiplier
                    .is_some_and(|value| value.is_finite() && value > 0.0)
                    && adjustment.constants.policy.is_none()
            }
            "0.12.0" | "0.13.0" | "0.14.0" | "0.15.0" | "0.16.0" => {
                adjustment
                    .constants
                    .rebuy_stake_multiplier
                    .is_some_and(|value| value.is_finite() && value > 0.0)
                    && adjustment
                        .constants
                        .policy
                        .as_ref()
                        .is_some_and(valid_nfi_adjustment_policy)
            }
            _ => false,
        };
        adjustment_tags == managed_tags
            && adjustment_tags.len() == adjustment.entry_tags.len()
            && adjustment.system_version == constants.system_v3_2_name
            && adjustment.decision_program == "long_grind_entry_v3"
            && adjustment.program_order
                == ADJUSTMENT_ORDER
                    .iter()
                    .map(ToString::to_string)
                    .collect::<Vec<_>>()
            && adjustment.stateful_input_contract.is_object()
            && versioned_rebuy_multiplier
            && valid_nfi_adjustment_constants(&adjustment.constants)
    });
    let short_rebuy_tags = manager
        .managed_short_routes
        .iter()
        .find(|route| route.key == "short_rebuy")
        .into_iter()
        .flat_map(|route| &route.entry_tags)
        .collect::<BTreeSet<_>>();
    let regular_short_tags = short_tags
        .difference(&short_rebuy_tags)
        .copied()
        .collect::<BTreeSet<_>>();
    let valid_short_adjustment_route =
        if uses_full_futures_manager_contract(&manager.schema_version) {
            short_adjustment.is_some_and(|adjustment| {
                let adjustment_tags = adjustment.entry_tags.iter().collect::<BTreeSet<_>>();
                adjustment.enabled
                    && adjustment_tags == regular_short_tags
                    && adjustment_tags.len() == adjustment.entry_tags.len()
                    && adjustment.system_version == constants.system_v3_2_name
                    && adjustment.decision_program == "short_grind_entry_v3"
                    && adjustment.program_order
                        == ADJUSTMENT_ORDER
                            .iter()
                            .map(ToString::to_string)
                            .collect::<Vec<_>>()
                    && adjustment.stateful_input_contract.is_object()
                    && adjustment
                        .constants
                        .rebuy_stake_multiplier
                        .is_some_and(|value| value.is_finite() && value > 0.0)
                    && adjustment
                        .constants
                        .policy
                        .as_ref()
                        .is_some_and(valid_nfi_adjustment_policy)
                    && valid_nfi_adjustment_constants(&adjustment.constants)
            })
        } else {
            short_adjustment.is_none()
        };
    let rebuy_route = manager
        .managed_long_routes
        .iter()
        .find(|route| route.profile == NfiManagedLongProfile::Rebuy);
    let rebuy_adjustment = &manager.rebuy_adjustment;
    let valid_rebuy_adjustment = rebuy_route.is_some_and(|route| {
        let adjustment_tags = rebuy_adjustment.entry_tags.iter().collect::<BTreeSet<_>>();
        let route_tags = route.entry_tags.iter().collect::<BTreeSet<_>>();
        rebuy_adjustment.enabled
            && adjustment_tags == route_tags
            && adjustment_tags.len() == rebuy_adjustment.entry_tags.len()
            && rebuy_adjustment.system_version == constants.system_v3_2_name
            && rebuy_adjustment.stateful_input_contract.is_object()
            && valid_nfi_rebuy_constants(&rebuy_adjustment.constants)
    });
    let short_rebuy_route = manager
        .managed_short_routes
        .iter()
        .find(|route| route.profile == NfiManagedLongProfile::Rebuy);
    let short_rebuy_adjustment = &manager.short_rebuy_adjustment;
    let valid_short_rebuy_adjustment = short_rebuy_route.is_some_and(|route| {
        let adjustment_tags = short_rebuy_adjustment
            .entry_tags
            .iter()
            .collect::<BTreeSet<_>>();
        let route_tags = route.entry_tags.iter().collect::<BTreeSet<_>>();
        let valid_scope = if uses_full_futures_manager_contract(&manager.schema_version) {
            short_rebuy_adjustment.execution_scope == "rebuy-and-grind-v2"
                && short_rebuy_adjustment.post_derisk_action == "short-position-adjustment"
        } else {
            short_rebuy_adjustment.execution_scope == "pre-derisk-only-v1"
                && short_rebuy_adjustment.post_derisk_action == "fail-simulation"
        };
        short_rebuy_adjustment.enabled
            && adjustment_tags == route_tags
            && adjustment_tags.len() == short_rebuy_adjustment.entry_tags.len()
            && short_rebuy_adjustment.system_version == constants.system_v3_2_name
            && valid_scope
            && short_rebuy_adjustment.stateful_input_contract.is_object()
            && valid_nfi_rebuy_constants(&short_rebuy_adjustment.constants)
    });
    let thresholds = [
        constants.stop_threshold_futures,
        constants.stop_threshold_spot,
        constants.system_v3_2_stop_threshold_doom_futures,
        constants.system_v3_2_stop_threshold_doom_spot,
    ];
    let valid_constants = !constants.system_name_use.is_empty()
        && constants.system_name_use == constants.system_v3_2_name
        && thresholds
            .iter()
            .all(|threshold| threshold.is_finite() && *threshold >= 0.0);
    let has_system_write = config
        .callback_program
        .as_ref()
        .and_then(|program| program.order_filled.as_ref())
        .is_some_and(|program| {
            program.initial_successful_entry_writes.iter().any(|write| {
                write.key == "system_version"
                    && write.value.as_str() == Some(constants.system_name_use.as_str())
            })
        });
    if !valid_identity
        || !valid_managed_routes
        || !valid_terminal_exit_version
        || !valid_short_routes
        || !valid_route_order
        || !valid_long_grind
        || !valid_long_btc
        || !valid_programs
        || !valid_adjustment_route
        || !valid_short_adjustment_route
        || !valid_rebuy_adjustment
        || !valid_short_rebuy_adjustment
        || !valid_constants
        || !has_system_write
        || config.custom_exit_program.is_some()
    {
        return Err(SimError::InvalidNfiTradeManager);
    }
    Ok(())
}

fn valid_nfi_managed_short_route(route: &NfiManagedLongRoute) -> bool {
    let profile_matches_key = matches!(
        (route.key.as_str(), route.profile),
        (
            "short_normal" | "short_top_coins_fallback",
            NfiManagedLongProfile::Normal
        ) | ("short_pump", NfiManagedLongProfile::Pump)
            | ("short_quick", NfiManagedLongProfile::Quick)
            | ("short_rebuy", NfiManagedLongProfile::Rebuy)
            | ("short_high_profit", NfiManagedLongProfile::HighProfit)
            | ("short_rapid", NfiManagedLongProfile::Rapid)
            | ("short_scalp", NfiManagedLongProfile::Scalp)
    );
    let route_tags = route.entry_tags.iter().collect::<BTreeSet<_>>();
    let stop_thresholds_are_valid = match route.profile {
        NfiManagedLongProfile::Rebuy
        | NfiManagedLongProfile::Rapid
        | NfiManagedLongProfile::Scalp => {
            route
                .stop_threshold_futures
                .is_some_and(|value| value.is_finite() && value >= 0.0)
                && route
                    .stop_threshold_spot
                    .is_some_and(|value| value.is_finite() && value >= 0.0)
        }
        _ => route.stop_threshold_futures.is_none() && route.stop_threshold_spot.is_none(),
    };
    profile_matches_key
        && !route.mode_name.is_empty()
        && !route.entry_tags.is_empty()
        && route_tags.len() == route.entry_tags.len()
        && route.entry_tags.iter().all(|tag| !tag.is_empty())
        && stop_thresholds_are_valid
        && route.terminal_exit.is_none()
}

pub(super) fn valid_nfi_managed_long_route(route: &NfiManagedLongRoute) -> bool {
    let profile_matches_key = matches!(
        (route.key.as_str(), route.profile),
        ("long_normal", NfiManagedLongProfile::Normal)
            | ("long_pump", NfiManagedLongProfile::Pump)
            | ("long_quick", NfiManagedLongProfile::Quick)
            | ("long_rebuy", NfiManagedLongProfile::Rebuy)
            | ("long_high_profit", NfiManagedLongProfile::HighProfit)
            | ("long_rapid", NfiManagedLongProfile::Rapid)
            | ("long_top_coins", NfiManagedLongProfile::TopCoins)
            | ("long_scalp", NfiManagedLongProfile::Scalp)
    );
    let route_tags = route.entry_tags.iter().collect::<BTreeSet<_>>();
    let terminal_exit_is_valid = route.terminal_exit.as_ref().is_none_or(|terminal| {
        let terminal_tags = terminal.entry_tags.iter().collect::<BTreeSet<_>>();
        route.profile == NfiManagedLongProfile::Rebuy
            && !terminal.entry_tags.is_empty()
            && terminal_tags.len() == terminal.entry_tags.len()
            && terminal_tags.iter().all(|tag| route_tags.contains(*tag))
            && terminal.minimum_age_ms > 0
            && terminal.minimum_profit_ratio.is_finite()
            && !terminal.reason.is_empty()
    });
    let stop_thresholds_are_valid = match route.profile {
        NfiManagedLongProfile::Rebuy
        | NfiManagedLongProfile::Rapid
        | NfiManagedLongProfile::Scalp => {
            route
                .stop_threshold_futures
                .is_some_and(|value| value.is_finite() && value >= 0.0)
                && route
                    .stop_threshold_spot
                    .is_some_and(|value| value.is_finite() && value >= 0.0)
        }
        _ => route.stop_threshold_futures.is_none() && route.stop_threshold_spot.is_none(),
    };
    profile_matches_key
        && !route.mode_name.is_empty()
        && !route.entry_tags.is_empty()
        && route_tags.len() == route.entry_tags.len()
        && route.entry_tags.iter().all(|tag| !tag.is_empty())
        && stop_thresholds_are_valid
        && terminal_exit_is_valid
}

fn valid_nfi_rebuy_constants(constants: &NfiX7RebuyConstants) -> bool {
    let vectors = [
        (&constants.stakes_futures, &constants.thresholds_futures),
        (&constants.stakes_spot, &constants.thresholds_spot),
    ];
    vectors.iter().all(|(stakes, thresholds)| {
        !stakes.is_empty()
            && stakes.len() == thresholds.len()
            && stakes
                .iter()
                .chain(thresholds.iter())
                .all(|value| value.is_finite())
            && stakes.iter().all(|value| *value > 0.0)
    }) && constants.derisk_futures.is_finite()
        && constants.derisk_spot.is_finite()
        && constants.derisk_futures < 0.0
        && constants.derisk_spot < 0.0
}

fn valid_nfi_legacy_grind_constants(constants: &NfiLegacyGrindConstants) -> bool {
    let expected_tags = [
        ("gd1", "dd1"),
        ("gd2", "dd2"),
        ("gd3", "dd3"),
        ("gd4", "dd4"),
        ("gd5", "dd5"),
        ("gd6", "dd6"),
        ("dl1", "ddl1"),
        ("dl2", "ddl2"),
    ];
    let multipliers_are_valid = [
        &constants.stake_multipliers_futures,
        &constants.stake_multipliers_spot,
    ]
    .iter()
    .all(|values| {
        !values.is_empty() && values.iter().all(|value| value.is_finite() && *value > 0.0)
    });
    let clusters_are_valid = constants.clusters.len() == expected_tags.len()
        && constants
            .clusters
            .iter()
            .zip(expected_tags)
            .all(|(cluster, expected)| {
                let vectors = [
                    &cluster.stakes_futures,
                    &cluster.stakes_spot,
                    &cluster.thresholds_futures,
                    &cluster.thresholds_spot,
                ];
                cluster.entry_tag == expected.0
                    && cluster.stop_tag == expected.1
                    && [
                        cluster.stop_threshold_futures,
                        cluster.stop_threshold_spot,
                        cluster.profit_threshold_futures,
                        cluster.profit_threshold_spot,
                    ]
                    .iter()
                    .all(|value| value.is_finite())
                    && vectors.iter().all(|values| {
                        !values.is_empty() && values.iter().all(|value| value.is_finite())
                    })
                    && cluster.stakes_futures.len() == cluster.thresholds_futures.len()
                    && cluster.stakes_spot.len() == cluster.thresholds_spot.len()
            });
    constants.max_stake_multiplier.is_finite()
        && constants.max_stake_multiplier > 0.0
        && constants.derisk_1_reentry_futures.is_finite()
        && constants.derisk_1_reentry_futures < 0.0
        && constants.derisk_1_reentry_spot.is_finite()
        && constants.derisk_1_reentry_spot < 0.0
        && multipliers_are_valid
        && clusters_are_valid
}

fn valid_nfi_regular_adjustment_constants(constants: &NfiRegularAdjustmentConstants) -> bool {
    let policy = &constants.policy;
    let policy_is_valid = policy.entry_retry_ms > 0
        && policy.grind_force_order_age_ms > policy.entry_retry_ms
        && policy.grind_order_age_ms > policy.grind_force_order_age_ms
        && policy.rebuy_order_age_ms > policy.grind_order_age_ms
        && [
            policy.grind_entry_profit_gate,
            policy.additional_grind_profit_gate,
            policy.forced_age_profit_gate,
            policy.minimum_entry_multiplier,
            policy.minimum_remaining_multiplier,
        ]
        .iter()
        .all(|value| value.is_finite())
        && policy.grind_entry_profit_gate > policy.additional_grind_profit_gate
        && policy.additional_grind_profit_gate > policy.forced_age_profit_gate
        && policy.forced_age_profit_gate < 0.0
        && policy.minimum_entry_multiplier > 1.0
        && policy.minimum_remaining_multiplier > policy.minimum_entry_multiplier;
    let rebuy_is_valid = [
        (
            &constants.rebuy_stakes_futures,
            &constants.rebuy_thresholds_futures,
        ),
        (
            &constants.rebuy_stakes_spot,
            &constants.rebuy_thresholds_spot,
        ),
    ]
    .iter()
    .all(|(stakes, thresholds)| {
        !stakes.is_empty()
            && stakes.len() == thresholds.len()
            && stakes.iter().all(|value| value.is_finite() && *value > 0.0)
            && thresholds.iter().all(|value| value.is_finite())
    });
    let grinds_are_valid = constants.grinds.len() == 6
        && constants.grinds.iter().enumerate().all(|(index, grind)| {
            let level = index + 1;
            grind.entry_tag == format!("g{level}")
                && grind.stop_tag == format!("sg{level}")
                && [
                    (&grind.stakes_futures, &grind.thresholds_futures),
                    (&grind.stakes_spot, &grind.thresholds_spot),
                ]
                .iter()
                .all(|(stakes, thresholds)| {
                    !stakes.is_empty()
                        && stakes.len() == thresholds.len()
                        && stakes.iter().all(|value| value.is_finite() && *value > 0.0)
                        && thresholds.iter().all(|value| value.is_finite())
                })
                && grind.stop_threshold_futures.is_finite()
                && grind.stop_threshold_spot.is_finite()
                && grind.profit_threshold_futures.is_finite()
                && grind.profit_threshold_spot.is_finite()
        });
    constants.derisk_threshold_futures.is_finite()
        && constants.derisk_threshold_futures < 0.0
        && constants.derisk_threshold_spot.is_finite()
        && constants.derisk_threshold_spot < 0.0
        && constants.derisk_level_1_threshold_futures.is_finite()
        && constants.derisk_level_1_threshold_futures < 0.0
        && constants.derisk_level_1_threshold_spot.is_finite()
        && constants.derisk_level_1_threshold_spot < 0.0
        && policy_is_valid
        && rebuy_is_valid
        && grinds_are_valid
}

fn valid_nfi_adjustment_constants(constants: &NfiX7AdjustmentConstants) -> bool {
    let levels = constants
        .derisk_levels
        .iter()
        .map(|level| level.level)
        .collect::<Vec<_>>();
    let grinds = constants
        .grinds
        .iter()
        .map(|grind| grind.level)
        .collect::<Vec<_>>();
    let derisk_numbers_are_valid = constants.derisk_levels.iter().all(|level| {
        [
            level.threshold_futures,
            level.threshold_spot,
            level.stake_futures,
            level.stake_spot,
        ]
        .iter()
        .all(|value| value.is_finite())
            && level.stake_futures > 0.0
            && level.stake_spot > 0.0
    });
    let grind_numbers_are_valid = constants.grinds.iter().all(|grind| {
        let scalars = [
            grind.derisk_futures,
            grind.derisk_spot,
            grind.profit_threshold_futures,
            grind.profit_threshold_spot,
        ];
        let vectors = [
            &grind.stakes_futures,
            &grind.stakes_spot,
            &grind.thresholds_futures,
            &grind.thresholds_spot,
        ];
        scalars.iter().all(|value| value.is_finite())
            && vectors
                .iter()
                .all(|values| !values.is_empty() && values.iter().all(|value| value.is_finite()))
            && grind.stakes_futures.len() == grind.thresholds_futures.len()
            && grind.stakes_spot.len() == grind.thresholds_spot.len()
    });
    constants.max_stake_multiplier.is_finite()
        && constants.max_stake_multiplier > 0.0
        && constants
            .rebuy_stake_multiplier
            .is_none_or(|value| value.is_finite() && value > 0.0)
        && levels == [1, 2, 3]
        && grinds == [1, 2, 3, 4, 5]
        && derisk_numbers_are_valid
        && grind_numbers_are_valid
}

fn valid_nfi_adjustment_policy(policy: &NfiX7AdjustmentPolicy) -> bool {
    let fallback_levels = policy
        .grind_entry_fallbacks
        .iter()
        .map(|fallback| fallback.level)
        .collect::<Vec<_>>();
    let valid_derisk_levels = |levels: &[usize]| {
        !levels.is_empty()
            && levels.windows(2).all(|pair| pair[0] < pair[1])
            && levels.iter().all(|level| (1..=3).contains(level))
    };
    let fallbacks_are_valid = policy.grind_entry_fallbacks.iter().all(|fallback| {
        fallback.predicates.iter().all(|predicate| {
            (predicate.any_derisk_levels.is_empty()
                || valid_derisk_levels(&predicate.any_derisk_levels))
                && !predicate.conditions.is_empty()
                && predicate
                    .conditions
                    .iter()
                    .all(valid_nfi_adjustment_condition)
        })
    });

    policy.entry_retry_ms > 0
        && policy.stale_order_ms > policy.entry_retry_ms
        && valid_derisk_levels(&policy.extra_entry_derisk_levels)
        && valid_nfi_adjustment_condition(&policy.extra_entry_profit_condition)
        && fallback_levels == [1, 2, 3, 4, 5]
        && fallbacks_are_valid
}

fn valid_nfi_adjustment_condition(condition: &NfiX7AdjustmentCondition) -> bool {
    valid_nfi_adjustment_operand(&condition.left) && valid_nfi_adjustment_operand(&condition.right)
}

fn valid_nfi_adjustment_operand(operand: &NfiX7AdjustmentOperand) -> bool {
    match operand {
        NfiX7AdjustmentOperand::Literal { value } => value.is_finite(),
        NfiX7AdjustmentOperand::Variable { name } => matches!(
            name.as_str(),
            "slice_profit" | "slice_profit_entry" | "num_open_grinds_and_buybacks"
        ),
        NfiX7AdjustmentOperand::Feature { name, multiplier } => {
            !name.is_empty() && multiplier.is_finite()
        }
    }
}

pub(super) fn unsupported_nfi_pair_signal(
    pair: &PairSeries,
    candle: &Candle,
    manager: &NfiX7TradeManager,
    can_short: bool,
) -> Option<SimError> {
    let (side, signal) = freqtrade_entry_signal(candle, can_short)?;
    if side != TradeSide::Short {
        return None;
    }
    (!nfi_entry_signal_is_supported(manager, TradeSide::Short, signal)).then(|| {
        SimError::UnsupportedNfiEntryTag {
            pair: pair.pair.clone(),
            entry_tag: signal.tag.clone().unwrap_or_else(|| "<short>".to_owned()),
        }
    })
}

pub(super) fn nfi_entry_signal_is_supported(
    manager: &NfiX7TradeManager,
    side: TradeSide,
    signal: &EntrySignal,
) -> bool {
    signal.tag.as_deref().is_some_and(|entry_tag| {
        let words = entry_tag.split_whitespace().collect::<Vec<_>>();
        if words.is_empty() {
            return false;
        }
        let all_words_are_compiled = words.iter().all(|tag| {
            nfi_long_tag_is_in_compiled_scope(manager, tag)
                || nfi_short_tag_is_in_compiled_scope(manager, tag)
        });
        let contains_entry_side = match side {
            TradeSide::Long => words
                .iter()
                .any(|tag| nfi_long_tag_is_in_compiled_scope(manager, tag)),
            TradeSide::Short => words
                .iter()
                .any(|tag| nfi_short_tag_is_in_compiled_scope(manager, tag)),
        };
        // X7 appends both sides to one tag column. Requiring one word for the
        // side being opened rejects malformed vectors, while the union check
        // permits only opposite-side words whose callback route was compiled
        // and reviewed from the same source snapshot.
        all_words_are_compiled && contains_entry_side
    })
}

fn nfi_long_tag_is_in_compiled_scope(manager: &NfiX7TradeManager, tag: &str) -> bool {
    manager
        .managed_long_routes
        .iter()
        .any(|route| route.entry_tags.iter().any(|supported| supported == tag))
        || manager
            .long_grind
            .as_ref()
            .is_some_and(|route| route.entry_tags.iter().any(|supported| supported == tag))
        || manager
            .long_btc
            .as_ref()
            .is_some_and(|route| route.entry_tags.iter().any(|supported| supported == tag))
}

fn nfi_short_tag_is_in_compiled_scope(manager: &NfiX7TradeManager, tag: &str) -> bool {
    manager
        .managed_short_routes
        .iter()
        .any(|route| route.entry_tags.iter().any(|supported| supported == tag))
}

pub(super) fn nfi_managed_short_route_supports_tags(
    manager: &NfiX7TradeManager,
    route: &NfiManagedLongRoute,
    words: &[&str],
) -> bool {
    if words.is_empty() {
        return false;
    }
    let contains_primary = words
        .iter()
        .any(|word| route.entry_tags.iter().any(|supported| supported == word));
    if !contains_primary {
        return false;
    }
    match route.key.as_str() {
        // Upstream uses `any(...)` for these explicit custom-exit blocks.
        "short_normal" | "short_pump" | "short_quick" | "short_high_profit" | "short_rapid" => true,
        // Rebuy is the one strict all-tags route.
        "short_rebuy" => words
            .iter()
            .all(|word| route.entry_tags.iter().any(|supported| supported == word)),
        // Scalp accepts pure scalp or a scalp/rebuy compound. Tag 620 is not
        // executable yet and is rejected by the entry-scope gate before this
        // helper can run.
        "short_scalp" => {
            let rebuy = manager
                .managed_short_routes
                .iter()
                .find(|candidate| candidate.key == "short_rebuy");
            words.iter().all(|word| {
                route.entry_tags.iter().any(|supported| supported == word)
                    || rebuy.is_some_and(|rebuy| {
                        rebuy.entry_tags.iter().any(|supported| supported == word)
                    })
            })
        }
        // Upstream omits top-coins from `short_exit_known_mode_tags`, so these
        // labels reach the final short-normal fallback only when no explicit
        // short-exit family word is present.
        "short_top_coins_fallback" => words.iter().all(|word| {
            manager.managed_short_routes.iter().all(|candidate| {
                candidate.key == "short_top_coins_fallback"
                    || candidate
                        .entry_tags
                        .iter()
                        .all(|supported| supported != word)
            })
        }),
        _ => false,
    }
}

pub(super) fn nfi_managed_route_supports_tags(
    manager: &NfiX7TradeManager,
    route: &NfiManagedLongRoute,
    words: &[&str],
) -> bool {
    let contains_primary = words
        .iter()
        .any(|word| route.entry_tags.iter().any(|tag| tag == word));
    if !contains_primary {
        return false;
    }
    match route.profile {
        NfiManagedLongProfile::Rebuy => words.iter().all(|word| {
            route.entry_tags.iter().any(|tag| tag == word)
                || manager
                    .long_grind
                    .as_ref()
                    .is_some_and(|grind| grind.entry_tags.iter().any(|tag| tag == word))
        }),
        NfiManagedLongProfile::Rapid => words.iter().all(|word| {
            route.entry_tags.iter().any(|tag| tag == word)
                || manager
                    .managed_long_routes
                    .iter()
                    .find(|candidate| candidate.profile == NfiManagedLongProfile::Rebuy)
                    .is_some_and(|rebuy| rebuy.entry_tags.iter().any(|tag| tag == word))
                || manager
                    .managed_long_routes
                    .iter()
                    .find(|candidate| candidate.profile == NfiManagedLongProfile::Scalp)
                    .is_some_and(|scalp| scalp.entry_tags.iter().any(|tag| tag == word))
                || manager
                    .long_grind
                    .as_ref()
                    .is_some_and(|grind| grind.entry_tags.iter().any(|tag| tag == word))
        }),
        NfiManagedLongProfile::Scalp => words.iter().all(|word| {
            route.entry_tags.iter().any(|tag| tag == word)
                || manager
                    .managed_long_routes
                    .iter()
                    .find(|candidate| candidate.profile == NfiManagedLongProfile::Rebuy)
                    .is_some_and(|rebuy| rebuy.entry_tags.iter().any(|tag| tag == word))
                || manager
                    .long_grind
                    .as_ref()
                    .is_some_and(|grind| grind.entry_tags.iter().any(|tag| tag == word))
        }),
        _ => true,
    }
}

fn validate_callback_program(program: &CallbackProgram) -> Result<(), SimError> {
    let Some(order_filled) = &program.order_filled else {
        return Ok(());
    };
    if order_filled.initial_successful_entry_writes.is_empty()
        || order_filled
            .initial_successful_entry_writes
            .iter()
            .any(invalid_custom_write)
        || order_filled.order_tag_actions.iter().any(|(tag, writes)| {
            tag.is_empty() || writes.is_empty() || writes.iter().any(invalid_custom_write)
        })
    {
        return Err(SimError::InvalidCallbackProgram);
    }
    Ok(())
}

fn invalid_custom_write(write: &CustomDataWrite) -> bool {
    write.key.is_empty()
        || !matches!(
            write.value,
            Value::Null | Value::Bool(_) | Value::Number(_) | Value::String(_)
        )
}

pub(super) fn freqtrade_entry_signal(
    candle: &Candle,
    can_short: bool,
) -> Option<(TradeSide, &EntrySignal)> {
    let enter_long = candle.enter_long.as_ref();
    let enter_short = can_short.then_some(candle.enter_short.as_ref()).flatten();
    if candle.exit_long.is_none() && enter_short.is_none() {
        if let Some(signal) = enter_long {
            return Some((TradeSide::Long, signal));
        }
    }
    if candle.exit_short.is_none() && enter_long.is_none() {
        if let Some(signal) = enter_short {
            return Some((TradeSide::Short, signal));
        }
    }
    None
}

fn validate_pair_series(
    pair_index: usize,
    pair: &PairSeries,
    nfi_manager: Option<&NfiX7TradeManager>,
    can_short: bool,
    logical_timestamps: &mut BTreeSet<i64>,
) -> Result<(), SimError> {
    if pair.pair.is_empty() {
        return Err(SimError::EmptyPair(pair_index));
    }
    if pair.candles.is_empty() {
        return Err(SimError::EmptyCandles(pair.pair.clone()));
    }
    if pair.execution_start_index >= pair.candles.len() {
        return Err(SimError::InvalidExecutionStart {
            pair: pair.pair.clone(),
            index: pair.execution_start_index,
            rows: pair.candles.len(),
        });
    }
    for (name, value) in [
        ("pair.amount_step", pair.amount_step),
        ("pair.price_step", pair.price_step),
    ] {
        if value.is_some_and(|step| !step.is_finite() || step <= 0.0) {
            return Err(SimError::InvalidPositiveConfig(name));
        }
    }
    let mut previous_step_timestamp = None;
    for change in &pair.price_steps {
        if change.timestamp_ms < 0
            || !change.step.is_finite()
            || change.step <= 0.0
            || previous_step_timestamp.is_some_and(|previous| change.timestamp_ms <= previous)
        {
            return Err(SimError::InvalidPositiveConfig("pair.price_steps"));
        }
        previous_step_timestamp = Some(change.timestamp_ms);
    }
    for (column, values) in &pair.feature_columns {
        if column.is_empty() || values.is_empty() || values.len() != pair.candles.len() {
            return Err(SimError::InvalidFeatureColumn {
                pair: pair.pair.clone(),
                column: column.clone(),
            });
        }
    }
    let mut previous = None;
    let mut unsupported_nfi_signal = None;
    let mut entry_indices = Vec::new();
    for (index, candle) in pair.candles.iter().enumerate() {
        if previous.is_some_and(|value| candle.timestamp_ms <= value) {
            return Err(SimError::CandleOrder {
                pair: pair.pair.clone(),
                index,
            });
        }
        previous = Some(candle.timestamp_ms);
        validate_candle(pair, index, &candle)?;
        if index >= pair.execution_start_index {
            logical_timestamps.insert(candle.timestamp_ms);
        }
        if freqtrade_entry_signal(&candle, can_short).is_some() {
            entry_indices.push(index);
        }
        // The old validator made a second full pass over every pair solely for
        // this short-tag check. Retain the first unsupported signal while the
        // general validation pass continues, preserving the prior error
        // precedence without reading a multi-year spool twice.
        if unsupported_nfi_signal.is_none() {
            unsupported_nfi_signal = nfi_manager
                .and_then(|manager| unsupported_nfi_pair_signal(pair, &candle, manager, can_short));
        }
    }
    if let Some(error) = unsupported_nfi_signal {
        return Err(error);
    }
    // File-backed scheduling can now jump by binary search instead of reading
    // every idle row again. Owned fixtures remain in memory and need no index.
    pair.candles.install_entry_indices(entry_indices);
    Ok(())
}

fn validate_candle(pair: &PairSeries, index: usize, candle: &Candle) -> Result<(), SimError> {
    let values = [
        candle.open,
        candle.high,
        candle.low,
        candle.close,
        candle.volume,
    ];
    if candle.timestamp_ms < 0
        || values.iter().any(|value| !value.is_finite())
        || candle.open <= 0.0
        || candle.high < candle.low
        || candle.low <= 0.0
        || candle.volume < 0.0
        || candle.funding_rate.is_some_and(|rate| !rate.is_finite())
        || candle
            .funding_mark_price
            .is_some_and(|price| !price.is_finite() || price <= 0.0)
        || candle.funding_rate.is_some() != candle.funding_mark_price.is_some()
    {
        return Err(SimError::InvalidCandle {
            pair: pair.pair.clone(),
            index,
        });
    }
    for signal in [&candle.enter_long, &candle.enter_short]
        .into_iter()
        .flatten()
    {
        if signal
            .leverage
            .is_some_and(|leverage| !leverage.is_finite() || leverage <= 0.0)
        {
            return Err(SimError::InvalidLeverage {
                pair: pair.pair.clone(),
                timestamp_ms: candle.timestamp_ms,
            });
        }
        if signal
            .liquidation_price
            .is_some_and(|price| !price.is_finite() || price <= 0.0)
        {
            return Err(SimError::InvalidLiquidationPrice {
                pair: pair.pair.clone(),
                timestamp_ms: candle.timestamp_ms,
            });
        }
    }
    if pair
        .minimum_stake
        .is_some_and(|stake| !stake.is_finite() || stake < 0.0)
        || pair
            .minimum_amount
            .is_some_and(|amount| !amount.is_finite() || amount < 0.0)
        || pair
            .minimum_cost
            .is_some_and(|cost| !cost.is_finite() || cost < 0.0)
    {
        return Err(SimError::InvalidPositiveConfig("pair_stake_limits"));
    }
    Ok(())
}
