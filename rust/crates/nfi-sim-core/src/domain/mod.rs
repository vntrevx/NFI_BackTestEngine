//! Simulator domain contracts and serialized result types.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::OnceLock;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use thiserror::Error;

use super::io::{CandleSeries, FeatureColumn};
use super::protections::{PairLockState, ProtectionProgram};

pub(crate) type FeatureProjection = BTreeMap<String, BTreeSet<String>>;

const fn default_amount_reserve_percent() -> f64 {
    0.05
}

const fn default_tradable_balance_ratio() -> f64 {
    1.0
}

const fn default_max_entry_position_adjustment() -> i64 {
    -1
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
pub struct PortfolioConfig {
    pub starting_balance: f64,
    pub max_open_trades: usize,
    pub stake_amount: f64,
    pub fee_rate: f64,
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
    pub stoploss_ratio: f64,
    pub amount_step: f64,
    pub price_step: f64,
    #[serde(default)]
    pub custom_exit_after_ms: Option<i64>,
    #[serde(default)]
    pub adjustment_rule: Option<AdjustmentRule>,
    #[serde(default)]
    pub callback_program: Option<CallbackProgram>,
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

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CallbackProgram {
    #[serde(default)]
    pub order_filled: Option<OrderFilledProgram>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OrderFilledProgram {
    pub initial_successful_entry_writes: Vec<CustomDataWrite>,
    pub order_tag_actions: BTreeMap<String, Vec<CustomDataWrite>>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CustomDataWrite {
    pub key: String,
    pub value: Value,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StakeProgram {
    pub statements: Vec<StakeStatement>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum StakeStatement {
    #[serde(rename = "let")]
    Let {
        name: String,
        value: StakeExpression,
    },
    #[serde(rename = "if")]
    If {
        condition: StakeExpression,
        then: Vec<StakeStatement>,
        otherwise: Vec<StakeStatement>,
    },
    #[serde(rename = "for")]
    For {
        name: String,
        iterable: StakeExpression,
        body: Vec<StakeStatement>,
    },
    Return {
        value: StakeExpression,
    },
}

#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum StakeExpression {
    Literal {
        value: Value,
    },
    Variable {
        name: String,
    },
    Multiply {
        left: Box<Self>,
        right: Box<Self>,
    },
    And {
        values: Vec<Self>,
    },
    Or {
        values: Vec<Self>,
    },
    Equal {
        left: Box<Self>,
        right: Box<Self>,
    },
    Greater {
        left: Box<Self>,
        right: Box<Self>,
    },
    Choose {
        condition: Box<Self>,
        then: Box<Self>,
        otherwise: Box<Self>,
    },
    Index {
        value: Box<Self>,
        index: Box<Self>,
    },
    SplitWords {
        value: Box<Self>,
    },
    StakeClampMin {
        multiplier: Box<Self>,
    },
    AllIn {
        items: Box<Self>,
        container: Box<Self>,
    },
    AnyIn {
        items: Box<Self>,
        container: Box<Self>,
    },
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ConfirmProgram {
    pub statements: Vec<Value>,
    pub functions: BTreeMap<String, ConfirmFunction>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ConfirmFunction {
    pub parameters: Vec<String>,
    pub statements: Vec<Value>,
}

/// Compact arena program used by large, pure trade-decision functions.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScalarDecisionProgram {
    pub schema_version: String,
    pub opcode: String,
    pub parameters: Vec<String>,
    pub expressions: Vec<Value>,
    pub statements: Vec<Value>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScalarProgramBundle {
    pub schema_version: String,
    pub entry: String,
    pub programs: BTreeMap<String, ScalarDecisionProgram>,
}

/// Exact state needed by the currently supported NFI X7 routes.
///
/// This is intentionally narrower than a generic strategy callback. The
/// Python compiler binds these values to one strategy source hash and rejects
/// any entry tag outside the declared route before simulation starts.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NfiX7TradeManager {
    pub schema_version: String,
    pub source_sha256: String,
    /// Source order across managed routes and the two legacy grind branches.
    ///
    /// The order is observable for mixed entry tags because an earlier route
    /// may mutate the pair-level profit target even when it does not exit.
    pub route_order: Vec<String>,
    pub managed_long_routes: Vec<NfiManagedLongRoute>,
    /// Source order for the separately bounded short-side router.
    pub short_route_order: Vec<String>,
    /// The route type is shared because exit/target policy fields are
    /// identical. Validation still enforces short-only keys and tags.
    pub managed_short_routes: Vec<NfiManagedLongRoute>,
    #[serde(default)]
    pub long_grind: Option<NfiLongGrindRoute>,
    #[serde(default)]
    pub long_btc: Option<NfiLongGrindRoute>,
    pub rebuy_adjustment: NfiX7RebuyAdjustment,
    pub short_rebuy_adjustment: NfiX7ShortRebuyAdjustment,
    #[serde(default)]
    pub position_adjustment: Option<NfiX7PositionAdjustment>,
    /// System-v3.2 grind route used by ordinary short families.
    ///
    /// This is separate from the long descriptor because its source-compiled
    /// entry predicate and direction-sensitive wrapper policy are different.
    #[serde(default)]
    pub short_position_adjustment: Option<NfiX7PositionAdjustment>,
    pub constants: NfiManagedLongConstants,
    pub programs: BTreeMap<String, ScalarDecisionProgram>,
    /// Lazily derived from the source-bound scalar arenas.
    ///
    /// This is runtime-only state: serializing it would duplicate information
    /// already present in `programs` and would let an input lie about which
    /// dataframe fields a program can observe.
    #[serde(skip)]
    pub(crate) feature_projections: OnceLock<BTreeMap<String, FeatureProjection>>,
    /// Union projections for the fixed managed-exit program sequences.
    ///
    /// Like `feature_projections`, these are derived only from the immutable
    /// scalar arenas and cannot be supplied by an input document.
    #[serde(skip)]
    pub(crate) feature_projection_unions: OnceLock<BTreeMap<String, FeatureProjection>>,
}

#[derive(Debug, Clone, Copy, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum NfiManagedLongProfile {
    Normal,
    Pump,
    Quick,
    Rebuy,
    HighProfit,
    Rapid,
    TopCoins,
    Scalp,
}

/// One pure exit appended to a reviewed managed-long callback.
///
/// Python extracts these values from the supplied strategy AST. Keeping the
/// policy in the source-bound IR lets threshold and tag changes preserve their
/// exact meaning without embedding an NFI revision in the simulator.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NfiManagedTerminalExit {
    pub entry_tags: Vec<String>,
    pub minimum_age_ms: i64,
    pub minimum_profit_ratio: f64,
    pub reason: String,
}

/// One source-pinned branch in X7's managed long-side exit router.
///
/// The profile selects a closed Rust implementation. Thresholds are carried
/// only for rapid/scalp because those callbacks do not use the common
/// `long_exit_stoploss()` thresholds.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NfiManagedLongRoute {
    pub key: String,
    pub profile: NfiManagedLongProfile,
    pub mode_name: String,
    pub entry_tags: Vec<String>,
    #[serde(default)]
    pub stop_threshold_futures: Option<f64>,
    #[serde(default)]
    pub stop_threshold_spot: Option<f64>,
    #[serde(default)]
    pub terminal_exit: Option<NfiManagedTerminalExit>,
}

/// One repeated cluster in X7's legacy long-grind callback.
///
/// The callback spells out eight nearly identical branches. Keeping the tags
/// and constants typed while evaluating them in source order avoids eight
/// copies of stake arithmetic without making the order classifier generic.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NfiLegacyGrindCluster {
    pub entry_tag: String,
    pub stop_tag: String,
    pub stakes_futures: Vec<f64>,
    pub stakes_spot: Vec<f64>,
    pub thresholds_futures: Vec<f64>,
    pub thresholds_spot: Vec<f64>,
    pub stop_threshold_futures: f64,
    pub stop_threshold_spot: f64,
    pub profit_threshold_futures: f64,
    pub profit_threshold_spot: f64,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NfiLegacyGrindConstants {
    pub max_stake_multiplier: f64,
    pub stake_multipliers_futures: Vec<f64>,
    pub stake_multipliers_spot: Vec<f64>,
    pub derisk_1_reentry_futures: f64,
    pub derisk_1_reentry_spot: f64,
    pub clusters: Vec<NfiLegacyGrindCluster>,
}

/// One ``g1`` through ``g6`` cluster in tag 121's regular-mode prelude.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NfiRegularGrind {
    pub entry_tag: String,
    pub stop_tag: String,
    pub stakes_futures: Vec<f64>,
    pub stakes_spot: Vec<f64>,
    pub thresholds_futures: Vec<f64>,
    pub thresholds_spot: Vec<f64>,
    pub stop_threshold_futures: f64,
    pub stop_threshold_spot: f64,
    pub profit_threshold_futures: f64,
    pub profit_threshold_spot: f64,
}

/// Frozen constants read by ``long_adjust_trade_position_no_derisk()``.
///
/// Both market-mode branches are source-derived and stored independently. The
/// evaluator selects one complete branch from ``PortfolioConfig::is_futures``;
/// it never falls back from missing futures values to spot values.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NfiRegularAdjustmentConstants {
    pub use_grind_stops: bool,
    pub derisk_enable: bool,
    pub rebuy_stakes_futures: Vec<f64>,
    pub rebuy_stakes_spot: Vec<f64>,
    pub rebuy_thresholds_futures: Vec<f64>,
    pub rebuy_thresholds_spot: Vec<f64>,
    pub derisk_threshold_futures: f64,
    pub derisk_threshold_spot: f64,
    pub derisk_level_1_threshold_futures: f64,
    pub derisk_level_1_threshold_spot: f64,
    pub grinds: Vec<NfiRegularGrind>,
    pub policy: NfiRegularAdjustmentPolicy,
}

/// Literal gates extracted from the reviewed NFI callback body.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NfiRegularAdjustmentPolicy {
    pub entry_retry_ms: i64,
    pub grind_force_order_age_ms: i64,
    pub grind_order_age_ms: i64,
    pub rebuy_order_age_ms: i64,
    pub grind_entry_profit_gate: f64,
    pub additional_grind_profit_gate: f64,
    pub forced_age_profit_gate: f64,
    pub minimum_entry_multiplier: f64,
    pub minimum_remaining_multiplier: f64,
}

/// Source-bound X7 grind/BTC exit and adjustment route.
///
/// ``adjustment_scope`` remains explicit because tag 120's versioned
/// spot/futures state machine and tag 121's regular-mode prelude have
/// different proof boundaries even though both eventually call the same
/// Python method.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NfiLongGrindRoute {
    pub mode_name: String,
    pub entry_tags: Vec<String>,
    pub exit_profit_threshold: f64,
    pub adjustment_scope: String,
    pub grind_mode: bool,
    pub decision_program: String,
    pub first_entry_profit_threshold_spot: f64,
    pub first_entry_stop_threshold_spot: f64,
    #[serde(default)]
    pub futures_fallback_loss_threshold: Option<f64>,
    pub derisk_use_grind_stops: bool,
    pub stateful_input_contract: Value,
    pub constants: NfiLegacyGrindConstants,
    #[serde(default)]
    pub regular_decision_program: Option<String>,
    #[serde(default)]
    pub regular_constants: Option<NfiRegularAdjustmentConstants>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NfiManagedLongConstants {
    pub stops_enable: bool,
    pub stop_threshold_futures: f64,
    pub stop_threshold_spot: f64,
    pub system_name_use: String,
    pub system_v3_2_name: String,
    pub system_v3_2_stop_threshold_doom_futures: f64,
    pub system_v3_2_stop_threshold_doom_spot: f64,
    pub system_v3_2_stops_enable: bool,
    pub u_e_stops_enable: bool,
}

/// Source-bound system-v3.2 position-adjustment route.
///
/// NFI's callback rebuilds grind clusters from filled orders on every candle.
/// The Rust implementation derives that same projection and caches it only
/// until another filled order is appended; price-dependent state remains
/// candle-local.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NfiX7PositionAdjustment {
    pub enabled: bool,
    pub entry_tags: Vec<String>,
    pub system_version: String,
    pub decision_program: String,
    pub program_order: Vec<String>,
    pub stateful_input_contract: Value,
    pub constants: NfiX7AdjustmentConstants,
}

/// Source-bound system-v3 rebuy ladder used only by tags 61-65.
///
/// This remains separate from the shared grind-v3 adjustment because X7
/// counts orders and applies thresholds differently. Combining the two would
/// make the code shorter but would erase an observable strategy boundary.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NfiX7RebuyAdjustment {
    pub enabled: bool,
    pub entry_tags: Vec<String>,
    pub system_version: String,
    pub stateful_input_contract: Value,
    pub constants: NfiX7RebuyConstants,
}

/// Short-rebuy ladder before X7 transfers the trade to short-grind.
///
/// Post-de-risk grind is deliberately represented as a runtime rejection
/// boundary instead of silently reusing the long state machine.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NfiX7ShortRebuyAdjustment {
    pub enabled: bool,
    pub entry_tags: Vec<String>,
    pub system_version: String,
    pub execution_scope: String,
    pub post_derisk_action: String,
    pub stateful_input_contract: Value,
    pub constants: NfiX7RebuyConstants,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NfiX7RebuyConstants {
    pub derisk_enable: bool,
    pub stakes_futures: Vec<f64>,
    pub stakes_spot: Vec<f64>,
    pub thresholds_futures: Vec<f64>,
    pub thresholds_spot: Vec<f64>,
    pub derisk_futures: f64,
    pub derisk_spot: f64,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NfiX7AdjustmentConstants {
    pub derisk_enable: bool,
    pub max_stake_multiplier: f64,
    /// Initial stake fraction used by rebuy tags before they transfer into
    /// the shared grind-v3 state machine after a level-3 de-risk.
    ///
    /// Schema 0.9 inputs did not carry this source constant. Keeping the field
    /// optional lets old fixtures run until they reach that transition, where
    /// the evaluator fails closed instead of guessing a multiplier.
    #[serde(default)]
    pub rebuy_stake_multiplier: Option<f64>,
    pub derisk_levels: Vec<NfiX7DeriskLevel>,
    pub grinds: Vec<NfiX7GrindLevel>,
    /// Method-local retry windows and late-grind predicates extracted from
    /// the reviewed strategy source. Older inputs omit this field and fail
    /// closed only if the enabled adjustment route is reached.
    #[serde(default)]
    pub policy: Option<NfiX7AdjustmentPolicy>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NfiX7AdjustmentPolicy {
    pub entry_retry_ms: i64,
    pub stale_order_ms: i64,
    pub extra_entry_profit_condition: NfiX7AdjustmentCondition,
    pub extra_entry_derisk_levels: Vec<usize>,
    pub grind_entry_fallbacks: Vec<NfiX7GrindFallbackLevel>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NfiX7GrindFallbackLevel {
    pub level: usize,
    pub predicates: Vec<NfiX7AdjustmentPredicate>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NfiX7AdjustmentPredicate {
    pub any_derisk_levels: Vec<usize>,
    pub conditions: Vec<NfiX7AdjustmentCondition>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NfiX7AdjustmentCondition {
    pub left: NfiX7AdjustmentOperand,
    pub operator: NfiX7AdjustmentComparison,
    pub right: NfiX7AdjustmentOperand,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum NfiX7AdjustmentOperand {
    Literal { value: f64 },
    Variable { name: String },
    Feature { name: String, multiplier: f64 },
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NfiX7AdjustmentComparison {
    Lt,
    Gt,
    Eq,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NfiX7DeriskLevel {
    pub level: usize,
    pub enabled: bool,
    pub threshold_futures: f64,
    pub threshold_spot: f64,
    pub stake_futures: f64,
    pub stake_spot: f64,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NfiX7GrindLevel {
    pub level: usize,
    pub enabled: bool,
    pub use_derisk: bool,
    pub derisk_futures: f64,
    pub derisk_spot: f64,
    pub profit_threshold_futures: f64,
    pub profit_threshold_spot: f64,
    pub stakes_futures: Vec<f64>,
    pub stakes_spot: Vec<f64>,
    pub thresholds_futures: Vec<f64>,
    pub thresholds_spot: Vec<f64>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PairSeries {
    pub pair: String,
    /// First candle processed by the chronological trading loop.
    ///
    /// Earlier rows are immutable analyzed context for callback lookbacks.
    /// Starting the cursor here preserves those features without allowing a
    /// pre-timerange signal to consume wallet balance or a portfolio slot.
    #[serde(default)]
    pub execution_start_index: usize,
    #[serde(default)]
    pub amount_step: Option<f64>,
    #[serde(default)]
    pub price_step: Option<f64>,
    /// Sparse historical tick-size changes derived from the OHLCV archive.
    ///
    /// Freqtrade freezes the tick size visible when a trade opens. A monthly
    /// change table preserves that behavior without repeating one number on
    /// every candle.
    #[serde(default)]
    pub price_steps: Vec<PriceStepChange>,
    #[serde(default)]
    pub minimum_stake: Option<f64>,
    #[serde(default)]
    pub minimum_amount: Option<f64>,
    #[serde(default)]
    pub minimum_cost: Option<f64>,
    /// Strategy-only scalar columns stored once per pair.
    ///
    /// Each vector is aligned 1:1 with `candles`. Keeping the column name out
    /// of every candle avoids the dominant JSON/memory overhead for NFI's
    /// dozens of informative-timeframe features.
    #[serde(default)]
    pub feature_columns: BTreeMap<String, FeatureColumn>,
    pub candles: CandleSeries,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PriceStepChange {
    pub timestamp_ms: i64,
    pub step: f64,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Candle {
    pub timestamp_ms: i64,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: f64,
    #[serde(default)]
    pub previous_close: Option<f64>,
    #[serde(default)]
    pub enter_long: Option<EntrySignal>,
    #[serde(default)]
    pub enter_short: Option<EntrySignal>,
    #[serde(default)]
    pub exit_long: Option<ExitSignal>,
    #[serde(default)]
    pub exit_short: Option<ExitSignal>,
    /// Funding rate charged at this candle timestamp.
    ///
    /// This is an event, not a value to forward-fill across base candles.
    /// Freqtrade multiplies it by the funding event's mark open and the
    /// position amount. `funding_mark_price` must therefore be present on the
    /// same candle whenever this field is present.
    #[serde(default)]
    pub funding_rate: Option<f64>,
    /// Mark-price open paired with `funding_rate`.
    #[serde(default)]
    pub funding_mark_price: Option<f64>,
    #[serde(default)]
    pub adjustment: Option<AdjustmentSignal>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EntrySignal {
    pub tag: Option<String>,
    #[serde(default)]
    pub leverage: Option<f64>,
    #[serde(default)]
    pub liquidation_price: Option<f64>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExitSignal {
    pub reason: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AdjustmentSignal {
    pub stake_amount: f64,
    pub tag: String,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SimulationResult {
    pub schema_version: &'static str,
    pub starting_balance: f64,
    pub final_balance: f64,
    pub profit_total_abs: f64,
    pub total_volume: f64,
    pub rejected_signals: u64,
    pub maximum_concurrent_trades: usize,
    pub locks: Vec<PairLockState>,
    pub trades: Vec<ClosedTrade>,
}

/// Aggregate hot-loop measurements emitted separately from financial results.
///
/// Keeping this record outside [`SimulationResult`] preserves the exact public
/// trade-surface bytes while allowing representative runs to locate real
/// bottlenecks without per-candle logging.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct SimulationProfile {
    pub schema_version: &'static str,
    pub validation_ns: u64,
    pub event_loop_ns: u64,
    pub finalization_ns: u64,
    pub timestamp_batches: u64,
    pub pair_events: u64,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SimulationEvent {
    pub timestamp_ms: i64,
    pub pair: String,
    pub state: SimulationState,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SimulationState {
    pub quote_free: f64,
    pub base_balances: Vec<AssetBalance>,
    pub open_trade_count: usize,
    pub realized_profit: f64,
    pub closed_trade_count: usize,
    pub rejected_signals: u64,
    pub trade_id_counter: u64,
    pub order_id_counter: usize,
    pub locks: Vec<PairLockState>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct AssetBalance {
    pub currency: String,
    pub free: f64,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ClosedTrade {
    pub sequence: usize,
    pub id: u64,
    pub pair: String,
    pub is_short: bool,
    pub leverage: f64,
    pub open_timestamp_ms: i64,
    pub close_timestamp_ms: i64,
    pub open_rate: f64,
    pub close_rate: f64,
    pub amount: f64,
    pub stake_amount: f64,
    pub max_stake_amount: f64,
    pub entry_tag: Option<String>,
    pub exit_reason: String,
    pub fee_open: f64,
    pub fee_close: f64,
    pub funding_fees: f64,
    pub liquidation_price: Option<f64>,
    pub profit_abs: f64,
    pub profit_ratio: f64,
    pub initial_stop_loss: f64,
    pub stop_loss: f64,
    pub minimum_rate: f64,
    pub maximum_rate: f64,
    pub orders: Vec<FilledOrder>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct FilledOrder {
    /// Freqtrade's process-global order identifier.
    ///
    /// It participates in NFI grind tags but is not part of the normalized
    /// public trade surface, so serialization deliberately omits it.
    #[serde(skip)]
    pub id: u64,
    /// Funding accumulated since the previous filled order.
    ///
    /// Freqtrade moves the complete running funding value onto every filled
    /// order and resets the running accumulator. Replaying this hidden field
    /// is required for exact partial-exit profit accounting, but it is not
    /// part of the engine result or normalized public trade surface.
    #[serde(skip)]
    pub funding_fee: f64,
    pub sequence: usize,
    pub side: OrderSide,
    pub is_entry: bool,
    pub filled_timestamp_ms: i64,
    pub amount: f64,
    pub price: f64,
    pub cost: f64,
    pub tag: Option<String>,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum OrderSide {
    Buy,
    Sell,
}

#[derive(Debug, Error, PartialEq)]
pub enum SimError {
    #[error("unsupported simulator schema {0:?}")]
    UnsupportedSchema(String),
    #[error("configuration field {0} must be finite and positive")]
    InvalidPositiveConfig(&'static str),
    #[error("stoploss_ratio must be finite, negative, and greater than -1")]
    InvalidStoploss,
    #[error("max_open_trades must be greater than zero")]
    InvalidSlots,
    #[error("pair at index {0} is empty")]
    EmptyPair(usize),
    #[error("pair {0:?} has no candles")]
    EmptyCandles(String),
    #[error("pair {pair:?} execution_start_index {index} is outside its {rows} candle rows")]
    InvalidExecutionStart {
        pair: String,
        index: usize,
        rows: usize,
    },
    #[error("pair {pair:?} candle {index} is not strictly chronological")]
    CandleOrder { pair: String, index: usize },
    #[error("pair {pair:?} candle {index} contains invalid OHLCV")]
    InvalidCandle { pair: String, index: usize },
    #[error("pair {pair:?} feature column {column:?} is empty, misaligned, or non-numeric")]
    InvalidFeatureColumn { pair: String, column: String },
    #[error("adjustment stake must be finite, non-zero, and smaller than the position when negative at {pair:?} {timestamp_ms}")]
    InvalidAdjustment { pair: String, timestamp_ms: i64 },
    #[error("entry leverage must be finite and positive at {pair:?} {timestamp_ms}")]
    InvalidLeverage { pair: String, timestamp_ms: i64 },
    #[error("liquidation price must be finite and positive at {pair:?} {timestamp_ms}")]
    InvalidLiquidationPrice { pair: String, timestamp_ms: i64 },
    #[error("callback program contains an invalid key, tag, or value")]
    InvalidCallbackProgram,
    #[error("compiled custom stake program is invalid for {pair:?} at {timestamp_ms}")]
    InvalidStakeProgram { pair: String, timestamp_ms: i64 },
    #[error("compiled entry confirmation program is invalid for {pair:?} at {timestamp_ms}")]
    InvalidEntryConfirmation { pair: String, timestamp_ms: i64 },
    #[error("compiled exit confirmation program is invalid for {pair:?} at {timestamp_ms}")]
    InvalidExitConfirmation { pair: String, timestamp_ms: i64 },
    #[error("compiled custom exit program is invalid for {pair:?} at {timestamp_ms}")]
    InvalidCustomExit { pair: String, timestamp_ms: i64 },
    #[error("compiled position adjustment program is invalid for {pair:?} at {timestamp_ms}")]
    InvalidPositionAdjustment { pair: String, timestamp_ms: i64 },
    #[error("NFI X7 trade manager configuration or scalar program is invalid")]
    InvalidNfiTradeManager,
    #[error("NFI X7 trade manager does not support entry tag {entry_tag:?} for {pair:?}")]
    UnsupportedNfiEntryTag { pair: String, entry_tag: String },
}
