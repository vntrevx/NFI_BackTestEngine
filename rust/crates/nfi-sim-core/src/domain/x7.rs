//! Source-compiled NFI X7 route and adjustment contracts.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::OnceLock;

use serde::Deserialize;
use serde_json::Value;

use super::ScalarDecisionProgram;

pub(crate) type FeatureProjection = BTreeMap<String, BTreeSet<String>>;

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
