//! Simulation and portfolio configuration contracts.

use std::collections::BTreeMap;

use serde::Deserialize;

use crate::protections::ProtectionProgram;

use super::{
    CallbackProgram, ConfirmProgram, ExecutableCallbackProgram, NfiX7TradeManager, PairSeries,
    ScalarProgramBundle, StakeProgram, StateMachineProgram,
};

const fn default_amount_reserve_percent() -> f64 {
    0.05
}

const fn default_tradable_balance_ratio() -> f64 {
    1.0
}

const fn default_max_entry_position_adjustment() -> i64 {
    -1
}

const fn default_order_type() -> OrderType {
    OrderType::Limit
}

#[derive(Debug, Clone, Copy, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum OrderType {
    Limit,
    Market,
}

impl OrderType {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Limit => "limit",
            Self::Market => "market",
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SimulationInput {
    pub schema_version: String,
    pub config: PortfolioConfig,
    pub pairs: Vec<PairSeries>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
#[allow(clippy::struct_excessive_bools)] // Flat fields preserve Freqtrade's sealed config wire shape.
pub struct PortfolioConfig {
    pub starting_balance: f64,
    pub max_open_trades: usize,
    pub stake_amount: f64,
    pub fee_rate: f64,
    #[serde(default = "default_order_type")]
    pub entry_order_type: OrderType,
    #[serde(default = "default_order_type")]
    pub exit_order_type: OrderType,
    #[serde(default)]
    pub entry_rates_by_pair: BTreeMap<String, BTreeMap<i64, f64>>,
    #[serde(default)]
    pub exit_rates_by_pair: BTreeMap<String, BTreeMap<i64, f64>>,
    #[serde(default)]
    pub fee_open_rate: Option<f64>,
    #[serde(default)]
    pub fee_close_rate: Option<f64>,
    #[serde(default)]
    pub leverage: Option<f64>,
    #[serde(default)]
    pub nfi_leverage_program: Option<NfiLeverageProgram>,
    #[serde(default)]
    pub maximum_leverage_by_pair: BTreeMap<String, f64>,
    #[serde(default)]
    pub liquidation_model: Option<IsolatedLiquidationModel>,
    #[serde(default)]
    pub protection_program: Option<ProtectionProgram>,
    #[serde(default)]
    pub minimal_roi: BTreeMap<u64, f64>,
    #[serde(default)]
    pub trailing_stop: bool,
    #[serde(default)]
    pub trailing_stop_positive: Option<f64>,
    #[serde(default)]
    pub trailing_stop_positive_offset: Option<f64>,
    #[serde(default)]
    pub trailing_only_offset_is_reached: bool,
    pub stoploss_ratio: f64,
    pub amount_step: f64,
    pub price_step: f64,
    #[serde(default)]
    pub custom_exit_after_ms: Option<i64>,
    #[serde(default)]
    pub adjustment_rule: Option<AdjustmentRule>,
    #[serde(default)]
    pub callback_program: Option<CallbackProgram>,
    /// Complete source-compiled callback program. Absence preserves legacy behavior.
    #[serde(default)]
    pub executable_callback_program: Option<ExecutableCallbackProgram>,
    #[serde(default)]
    pub state_machine_program: Option<StateMachineProgram>,
    #[serde(default)]
    pub stake_program: Option<StakeProgram>,
    #[serde(default = "default_amount_reserve_percent")]
    pub amount_reserve_percent: f64,
    #[serde(default)]
    pub unlimited_stake: bool,
    #[serde(default = "default_tradable_balance_ratio")]
    pub tradable_balance_ratio: f64,
    #[serde(default)]
    pub entry_confirmation_program: Option<ConfirmProgram>,
    #[serde(default)]
    pub exit_confirmation_program: Option<ConfirmProgram>,
    #[serde(default)]
    pub custom_exit_program: Option<ScalarProgramBundle>,
    #[serde(default)]
    pub adjust_trade_position_program: Option<ScalarProgramBundle>,
    #[serde(default)]
    pub nfi_x7_trade_manager: Option<NfiX7TradeManager>,
    #[serde(default = "default_max_entry_position_adjustment")]
    pub max_entry_position_adjustment: i64,
    #[serde(default)]
    pub is_futures: bool,
    /// Cadence used by Freqtrade to replace the running funding segment.
    ///
    /// Sparse funding events and refresh ticks are distinct. For example,
    /// Binance may expose an event every eight hours while the pinned exchange
    /// profile recalculates the inclusive segment every hour.
    #[serde(default)]
    pub funding_fee_interval_ms: Option<i64>,
}

/// Source-ordered X7 leverage callback.
///
/// Rules retain Python branch order. A rule matches only when every whitespace
/// separated entry-tag word belongs to that rule's reviewed tag set.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NfiLeverageProgram {
    pub default: f64,
    pub ordered_tag_overrides: Vec<NfiLeverageOverride>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NfiLeverageOverride {
    pub entry_tags: Vec<String>,
    pub leverage: f64,
}

/// Exchange-specific isolated-futures liquidation contract.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IsolatedLiquidationModel {
    pub exchange: String,
    pub margin_mode: String,
    pub buffer: f64,
    pub tiers_by_pair: BTreeMap<String, Vec<LeverageTier>>,
}

/// One Freqtrade-normalized leverage tier.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LeverageTier {
    pub min_notional: f64,
    pub max_notional: Option<f64>,
    pub maximum_leverage: f64,
    pub maintenance_margin_rate: f64,
    pub maintenance_amount: Option<f64>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AdjustmentRule {
    pub profit_below: f64,
    pub stake_ratio: f64,
    pub max_adjustments: usize,
    pub tag: String,
}
