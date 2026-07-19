//! Deterministic global chronological portfolio simulator.
//!
//! Signals cross this boundary as complete arrays. The core never calls Python
//! per candle and never simulates pairs independently before merging results.

use std::borrow::Cow;
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::rc::Rc;
use std::str::FromStr;
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use num_bigint::BigInt;
use num_rational::BigRational;
use num_traits::{One, ToPrimitive, Zero};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use thiserror::Error;

mod nfi_adjustment;
use nfi_adjustment::evaluate_nfi_position_adjustment as evaluate_nfi_system_v3_adjustment;
mod nfi_legacy_grind;
use nfi_legacy_grind::evaluate_nfi_legacy_grind_adjustment;
mod nfi_rebuy;
use nfi_rebuy::{evaluate_nfi_rebuy_adjustment, evaluate_nfi_short_rebuy_adjustment};

/// Normalized trade-surface contract understood by this workspace.
pub const TRADE_SURFACE_SCHEMA_VERSION: &str = "2.0.0";
/// Version of the simulator input/result contract.
pub const SIMULATOR_SCHEMA_VERSION: &str = "1.0.0";
/// Bytes before the first feature in one file-backed vector row.
///
/// This is a transport boundary shared with `nfi-vector-io`, not a tuning
/// value. Changing it requires a new row schema and an explicit decoder.
pub const FILE_BACKED_ROW_HEADER_BYTES: usize = 81;
/// Width of one normalized numeric or boolean feature in a file-backed row.
pub const FILE_BACKED_FEATURE_BYTES: usize = std::mem::size_of::<f64>();

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
    pub constants: NfiManagedLongConstants,
    pub programs: BTreeMap<String, ScalarDecisionProgram>,
    /// Lazily derived from the source-bound scalar arenas.
    ///
    /// This is runtime-only state: serializing it would duplicate information
    /// already present in `programs` and would let an input lie about which
    /// dataframe fields a program can observe.
    #[serde(skip)]
    feature_projections: OnceLock<BTreeMap<String, FeatureProjection>>,
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

/// Source-bound X7 legacy grind/BTC exit and adjustment route.
///
/// ``adjustment_scope`` remains explicit because tag 120's spot/backtest
/// state machine and tag 121's regular-mode prelude have different proof
/// boundaries even though both eventually call the same Python method.
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
    pub derisk_use_grind_stops: bool,
    pub stateful_input_contract: Value,
    pub constants: NfiLegacyGrindConstants,
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
/// The Rust implementation keeps that observable behavior instead of caching
/// a second, potentially divergent trade model.
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
    pub derisk_levels: Vec<NfiX7DeriskLevel>,
    pub grinds: Vec<NfiX7GrindLevel>,
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

/// One homogeneous strategy dataframe column.
///
/// The legacy JSON transport represents every scalar as `serde_json::Value`.
/// That costs roughly three times as much memory as the underlying `f64` for
/// X7's 100+ callback columns.  The simulator only needs numeric and boolean
/// dataframe scalars, so this enum keeps the hot data compact while its custom
/// deserializer continues accepting the existing JSON array contract.
#[derive(Debug, Clone)]
pub enum FeatureColumn {
    Numbers(Vec<f64>),
    Booleans(Vec<bool>),
    FileBacked {
        rows: Rc<FileBackedRows>,
        feature_index: usize,
        kind: FileBackedFeatureKind,
    },
}

impl FeatureColumn {
    #[must_use]
    pub fn numbers(values: Vec<f64>) -> Self {
        Self::Numbers(values)
    }

    #[must_use]
    pub fn booleans(values: Vec<bool>) -> Self {
        Self::Booleans(values)
    }

    #[must_use]
    pub fn file_backed(
        rows: Rc<FileBackedRows>,
        feature_index: usize,
        kind: FileBackedFeatureKind,
    ) -> Self {
        Self::FileBacked {
            rows,
            feature_index,
            kind,
        }
    }

    #[must_use]
    pub fn len(&self) -> usize {
        match self {
            Self::Numbers(values) => values.len(),
            Self::Booleans(values) => values.len(),
            Self::FileBacked { rows, .. } => rows.len(),
        }
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    fn value(&self, index: usize) -> Option<Value> {
        match self {
            Self::Numbers(values) => scalar_number_value(*values.get(index)?),
            Self::Booleans(values) => values.get(index).copied().map(Value::Bool),
            Self::FileBacked {
                rows,
                feature_index,
                kind,
            } => match kind {
                FileBackedFeatureKind::Number => {
                    scalar_number_value(rows.feature_number(index, *feature_index)?)
                }
                FileBackedFeatureKind::Boolean => {
                    rows.feature_boolean(index, *feature_index).map(Value::Bool)
                }
            },
        }
    }

    fn number(&self, index: usize) -> Option<f64> {
        match self {
            Self::Numbers(values) => values.get(index).copied(),
            Self::Booleans(_)
            | Self::FileBacked {
                kind: FileBackedFeatureKind::Boolean,
                ..
            } => None,
            Self::FileBacked {
                rows,
                feature_index,
                kind: FileBackedFeatureKind::Number,
            } => rows.feature_number(index, *feature_index),
        }
    }

    fn boolean(&self, index: usize) -> Option<bool> {
        match self {
            Self::Booleans(values) => values.get(index).copied(),
            Self::Numbers(_)
            | Self::FileBacked {
                kind: FileBackedFeatureKind::Number,
                ..
            } => None,
            Self::FileBacked {
                rows,
                feature_index,
                kind: FileBackedFeatureKind::Boolean,
            } => rows.feature_boolean(index, *feature_index),
        }
    }
}

impl<'de> Deserialize<'de> for FeatureColumn {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let values = Vec::<Value>::deserialize(deserializer)?;
        if values.iter().all(Value::is_boolean) {
            return Ok(Self::Booleans(
                values
                    .into_iter()
                    .map(|value| {
                        value
                            .as_bool()
                            .expect("all feature values were checked as boolean")
                    })
                    .collect(),
            ));
        }
        if values.iter().all(|value| scalar_number(value).is_some()) {
            return Ok(Self::Numbers(
                values
                    .iter()
                    .map(|value| {
                        scalar_number(value)
                            .expect("all feature values were checked as numeric scalars")
                    })
                    .collect(),
            ));
        }
        Err(serde::de::Error::custom(
            "feature column must contain only numbers or only booleans",
        ))
    }
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

/// Type of one normalized feature in the row-oriented file backing.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FileBackedFeatureKind {
    Number,
    Boolean,
}

/// Candle storage accepted by the simulator.
///
/// JSON fixtures remain ordinary owned vectors. Feather input is normalized
/// into a private, file-backed row spool so five-year workloads retain only
/// one decoded row per pair in heap memory.
#[derive(Debug, Clone)]
pub enum CandleSeries {
    Owned(Vec<Candle>),
    FileBacked(Rc<FileBackedRows>),
}

impl CandleSeries {
    #[must_use]
    pub fn file_backed(rows: Rc<FileBackedRows>) -> Self {
        Self::FileBacked(rows)
    }

    #[must_use]
    pub fn len(&self) -> usize {
        match self {
            Self::Owned(candles) => candles.len(),
            Self::FileBacked(rows) => rows.len(),
        }
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    #[must_use]
    pub fn get(&self, index: usize) -> Option<Cow<'_, Candle>> {
        match self {
            Self::Owned(candles) => candles.get(index).map(Cow::Borrowed),
            Self::FileBacked(rows) => rows.candle(index).map(Cow::Owned),
        }
    }

    #[must_use]
    pub fn timestamp_ms(&self, index: usize) -> Option<i64> {
        match self {
            Self::Owned(candles) => candles.get(index).map(|candle| candle.timestamp_ms),
            Self::FileBacked(rows) => rows.timestamp_ms(index),
        }
    }

    #[must_use]
    pub fn last(&self) -> Option<Cow<'_, Candle>> {
        self.len().checked_sub(1).and_then(|index| self.get(index))
    }

    #[must_use]
    pub fn iter(&self) -> CandleSeriesIter<'_> {
        CandleSeriesIter {
            series: self,
            index: 0,
        }
    }
}

impl From<Vec<Candle>> for CandleSeries {
    fn from(value: Vec<Candle>) -> Self {
        Self::Owned(value)
    }
}

impl<'de> Deserialize<'de> for CandleSeries {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        Vec::<Candle>::deserialize(deserializer).map(Self::Owned)
    }
}

pub struct CandleSeriesIter<'a> {
    series: &'a CandleSeries,
    index: usize,
}

impl<'a> Iterator for CandleSeriesIter<'a> {
    type Item = Cow<'a, Candle>;

    fn next(&mut self) -> Option<Self::Item> {
        let candle = self.series.get(self.index)?;
        self.index += 1;
        Some(candle)
    }

    fn size_hint(&self) -> (usize, Option<usize>) {
        let remaining = self.series.len().saturating_sub(self.index);
        (remaining, Some(remaining))
    }
}

impl ExactSizeIterator for CandleSeriesIter<'_> {}

impl<'a> IntoIterator for &'a CandleSeries {
    type Item = Cow<'a, Candle>;
    type IntoIter = CandleSeriesIter<'a>;

    fn into_iter(self) -> Self::IntoIter {
        self.iter()
    }
}

struct FileBackedState {
    file: File,
    cached_index: Option<usize>,
    row: Vec<u8>,
}

/// Shared safe file reader for one normalized pair.
///
/// The file is created privately by the verified Arrow boundary and remains
/// open for this object's lifetime. `RefCell` is deliberate: the simulator's
/// chronological event loop is single-threaded, while pair preparation is
/// parallelized before this boundary.
pub struct FileBackedRows {
    state: RefCell<FileBackedState>,
    row_count: usize,
    row_stride: usize,
    feature_count: usize,
    tags: Vec<String>,
}

impl fmt::Debug for FileBackedRows {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("FileBackedRows")
            .field("row_count", &self.row_count)
            .field("row_stride", &self.row_stride)
            .field("feature_count", &self.feature_count)
            .field("tag_count", &self.tags.len())
            .finish_non_exhaustive()
    }
}

impl FileBackedRows {
    /// Open a verified fixed-width pair spool.
    ///
    /// # Errors
    ///
    /// Returns an I/O error if the spool length does not match the declared
    /// row and feature counts or if its length cannot be represented safely.
    pub fn new(
        mut file: File,
        row_count: usize,
        feature_count: usize,
        tags: Vec<String>,
    ) -> Result<Rc<Self>, std::io::Error> {
        let feature_bytes = feature_count
            .checked_mul(FILE_BACKED_FEATURE_BYTES)
            .ok_or_else(|| {
                std::io::Error::new(std::io::ErrorKind::InvalidData, "feature row is too wide")
            })?;
        let row_stride = FILE_BACKED_ROW_HEADER_BYTES
            .checked_add(feature_bytes)
            .ok_or_else(|| {
                std::io::Error::new(std::io::ErrorKind::InvalidData, "pair row is too wide")
            })?;
        let expected_bytes = row_count.checked_mul(row_stride).ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::InvalidData, "pair spool is too large")
        })?;
        let actual_bytes = file.seek(SeekFrom::End(0))?;
        if actual_bytes != u64::try_from(expected_bytes).unwrap_or(u64::MAX) {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!(
                    "pair spool length mismatch: expected {expected_bytes}, got {actual_bytes}"
                ),
            ));
        }
        file.seek(SeekFrom::Start(0))?;
        Ok(Rc::new(Self {
            state: RefCell::new(FileBackedState {
                file,
                cached_index: None,
                row: vec![0; row_stride],
            }),
            row_count,
            row_stride,
            feature_count,
            tags,
        }))
    }

    #[must_use]
    pub const fn len(&self) -> usize {
        self.row_count
    }

    #[must_use]
    pub const fn is_empty(&self) -> bool {
        self.row_count == 0
    }

    fn with_row<T>(&self, index: usize, read: impl FnOnce(&[u8]) -> T) -> Option<T> {
        if index >= self.row_count {
            return None;
        }
        let mut state = self.state.borrow_mut();
        if state.cached_index != Some(index) {
            let offset = index
                .checked_mul(self.row_stride)
                .and_then(|value| u64::try_from(value).ok())
                .expect("validated pair spool offset remains representable");
            {
                let FileBackedState { file, row, .. } = &mut *state;
                file.seek(SeekFrom::Start(offset))
                    .and_then(|_| file.read_exact(row))
                    .expect("private verified pair spool remains readable");
            }
            state.cached_index = Some(index);
        }
        Some(read(&state.row))
    }

    fn candle(&self, index: usize) -> Option<Candle> {
        self.with_row(index, |row| {
            let flags = row[72];
            let entry_tag = self.tag(read_u32(row, 73));
            let exit_tag = self.tag(read_u32(row, 77));
            let exit_reason = || exit_tag.clone().unwrap_or_else(|| "exit_signal".to_owned());
            Candle {
                timestamp_ms: read_i64(row, 0),
                open: read_f64(row, 8),
                high: read_f64(row, 16),
                low: read_f64(row, 24),
                close: read_f64(row, 32),
                volume: read_f64(row, 40),
                previous_close: flag(flags, 0).then(|| read_f64(row, 48)),
                enter_long: flag(flags, 3).then(|| EntrySignal {
                    tag: entry_tag.clone(),
                    leverage: None,
                    liquidation_price: None,
                }),
                enter_short: flag(flags, 4).then_some(EntrySignal {
                    tag: entry_tag,
                    leverage: None,
                    liquidation_price: None,
                }),
                exit_long: flag(flags, 5).then(|| ExitSignal {
                    reason: exit_reason(),
                }),
                exit_short: flag(flags, 6).then(|| ExitSignal {
                    reason: exit_reason(),
                }),
                funding_rate: flag(flags, 1).then(|| read_f64(row, 56)),
                funding_mark_price: flag(flags, 2).then(|| read_f64(row, 64)),
                adjustment: None,
            }
        })
    }

    fn timestamp_ms(&self, index: usize) -> Option<i64> {
        self.with_row(index, |row| read_i64(row, 0))
    }

    fn feature_number(&self, row_index: usize, feature_index: usize) -> Option<f64> {
        if feature_index >= self.feature_count {
            return None;
        }
        self.with_row(row_index, |row| {
            read_f64(
                row,
                FILE_BACKED_ROW_HEADER_BYTES + feature_index * FILE_BACKED_FEATURE_BYTES,
            )
        })
    }

    fn feature_boolean(&self, row_index: usize, feature_index: usize) -> Option<bool> {
        self.feature_number(row_index, feature_index)
            .map(|value| value != 0.0)
    }

    fn tag(&self, encoded: u32) -> Option<String> {
        encoded
            .checked_sub(1)
            .and_then(|index| usize::try_from(index).ok())
            .and_then(|index| self.tags.get(index))
            .cloned()
    }
}

const fn flag(flags: u8, bit: u8) -> bool {
    flags & (1 << bit) != 0
}

fn read_i64(row: &[u8], offset: usize) -> i64 {
    i64::from_le_bytes(
        row[offset..offset + 8]
            .try_into()
            .expect("validated row scalar width"),
    )
}

fn read_u32(row: &[u8], offset: usize) -> u32 {
    u32::from_le_bytes(
        row[offset..offset + 4]
            .try_into()
            .expect("validated row scalar width"),
    )
}

fn read_f64(row: &[u8], offset: usize) -> f64 {
    f64::from_bits(u64::from_le_bytes(
        row[offset..offset + 8]
            .try_into()
            .expect("validated row scalar width"),
    ))
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
    #[error("pair {pair:?} candle {index} enters long and short simultaneously")]
    ConflictingEntrySignals { pair: String, index: usize },
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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum TradeSide {
    Long,
    Short,
}

#[derive(Debug, Clone)]
struct OpenTrade {
    id: u64,
    pair_index: usize,
    pair: String,
    side: TradeSide,
    leverage: f64,
    amount_step: f64,
    price_step: f64,
    open_timestamp_ms: i64,
    open_rate: f64,
    amount: f64,
    stake_amount: f64,
    max_stake_amount: f64,
    entry_cost_with_fees: f64,
    first_entry_cost_with_fees: f64,
    adjustment_count: usize,
    entry_tag: Option<String>,
    funding_fees: f64,
    funding_fees_total: f64,
    /// High and correction words for `CPython`'s compensated `sum(float)` path.
    ///
    /// Freqtrade recomputes the funding accrued since the most recent filled
    /// order with Python `sum()` on every funding tick. Keeping both words
    /// avoids losing the correction before that running value is attached to
    /// the next order.
    funding_sum_high: f64,
    funding_sum_low: f64,
    realized_partial_profit: f64,
    liquidation_price: Option<f64>,
    initial_stop_loss: f64,
    stop_loss: f64,
    minimum_rate: f64,
    maximum_rate: f64,
    orders: Vec<FilledOrder>,
    custom_data: BTreeMap<String, Value>,
}

#[derive(Debug, Clone, Copy, PartialEq)]
struct NfiProfitSnapshot {
    stake: f64,
    ratio: f64,
    current_stake_ratio: f64,
    initial_stake_ratio: f64,
}

#[derive(Debug, Clone, PartialEq)]
struct ProfitTarget {
    rate: f64,
    profit: f64,
    sell_reason: String,
    time_profit_reached_ms: i64,
}

/// Reports whether the compiled chronological simulator is present.
#[must_use]
pub const fn simulator_available() -> bool {
    true
}

/// Parse one simulator document for both native frontends.
///
/// The IR compiler flattens Python `elif` chains, so the normal JSON recursion
/// limit remains an input-safety boundary. Keeping this parser in the core
/// prevents the CLI and Python extension from drifting to different
/// acceptance rules.
///
/// # Errors
///
/// Returns the original JSON/type error, including trailing input.
pub fn parse_simulation_input(encoded: &[u8]) -> Result<SimulationInput, serde_json::Error> {
    serde_json::from_slice(encoded)
}

/// Validate and run one global portfolio stream.
///
/// # Errors
///
/// Returns [`SimError`] when the version, configuration, candle ordering,
/// OHLCV values, or adjustment request cannot be represented exactly by this
/// supported simulator subset.
///
/// # Panics
///
/// Panics only if an internally created open trade points outside the already
/// validated immutable pair array. Public input cannot construct that state.
#[allow(clippy::too_many_lines)]
pub fn simulate(input: &SimulationInput) -> Result<SimulationResult, SimError> {
    simulate_with_observer(input, |_| {})
}

/// Run the simulator and stream one compact state projection after each
/// Freqtrade-visible pair candle. Freqtrade reserves the first row for shifted
/// signals and does expose the final row before its separate force-exit pass.
///
/// # Errors
///
/// Returns the same validation and semantic errors as [`simulate`].
///
/// # Panics
///
/// Has the same internal invariant boundary as [`simulate`].
#[allow(clippy::if_not_else, clippy::too_many_lines)]
pub fn simulate_with_observer<F>(
    input: &SimulationInput,
    observer: F,
) -> Result<SimulationResult, SimError>
where
    F: FnMut(&SimulationEvent),
{
    simulate_with_observer_profiled(input, observer).map(|(result, _)| result)
}

/// Run the simulator and return aggregate phase timings beside the result.
///
/// # Errors
///
/// Returns the same validation and semantic errors as [`simulate`].
pub fn simulate_profiled(
    input: &SimulationInput,
) -> Result<(SimulationResult, SimulationProfile), SimError> {
    simulate_with_observer_profiled(input, |_| {})
}

/// Run with an observer and return aggregate phase timings.
///
/// # Errors
///
/// Returns the same validation and semantic errors as [`simulate`].
///
/// # Panics
///
/// Has the same internal invariant boundary as [`simulate`].
#[allow(clippy::if_not_else, clippy::too_many_lines)]
pub fn simulate_with_observer_profiled<F>(
    input: &SimulationInput,
    mut observer: F,
) -> Result<(SimulationResult, SimulationProfile), SimError>
where
    F: FnMut(&SimulationEvent),
{
    let validation_started = Instant::now();
    validate_input(input)?;
    let validation_ns = duration_ns(validation_started.elapsed());
    let event_loop_started = Instant::now();
    let mut timestamp_batches = 0_u64;
    let mut pair_events = 0_u64;
    let config = &input.config;
    // Each pair may retain a different amount of startup context. Initializing
    // from the sealed boundary excludes those rows from global time ordering,
    // order IDs, shared-wallet accounting, and observer traces.
    let mut cursors = input
        .pairs
        .iter()
        .map(|pair| pair.execution_start_index)
        .collect::<Vec<_>>();
    let mut open_trades: Vec<OpenTrade> = Vec::new();
    let mut closed_trades = Vec::new();
    let mut available_balance = config.starting_balance;
    let mut rejected_signals = 0_u64;
    let mut next_trade_id = 1_u64;
    let mut next_order_id = 1_u64;
    let mut maximum_concurrent_trades = 0_usize;
    let mut profit_targets: BTreeMap<String, ProfitTarget> = BTreeMap::new();

    while let Some(timestamp_ms) = next_timestamp(&input.pairs, &cursors) {
        timestamp_batches += 1;
        for (pair_index, pair) in input.pairs.iter().enumerate() {
            let cursor = cursors[pair_index];
            let Some(candle_storage) = pair.candles.get(cursor) else {
                continue;
            };
            let candle = candle_storage.as_ref();
            if candle.timestamp_ms != timestamp_ms {
                continue;
            }
            pair_events += 1;

            let existing_trade_index = open_trades
                .iter()
                .position(|trade| trade.pair_index == pair_index);
            // Freqtrade includes the timerange stop-boundary row so callbacks
            // and force exits see its open price, but passes `can_enter=false`
            // for that row. Without this gate a shifted signal at the boundary
            // would create and immediately force-close a trade that Freqtrade
            // never opens.
            let can_enter = cursor + 1 < pair.candles.len();
            let entry_request = can_enter
                .then(|| {
                    candle
                        .enter_long
                        .as_ref()
                        .map(|signal| (TradeSide::Long, signal))
                        .or_else(|| {
                            candle
                                .enter_short
                                .as_ref()
                                .map(|signal| (TradeSide::Short, signal))
                        })
                })
                .flatten();
            let opened_now = if let (Some((side, signal)), None) =
                (entry_request, existing_trade_index)
            {
                if open_trades.len() >= config.max_open_trades {
                    rejected_signals += 1;
                    false
                } else {
                    if config.nfi_x7_trade_manager.as_ref().is_some_and(|manager| {
                        !nfi_entry_signal_is_supported(manager, side, signal)
                    }) {
                        return Err(SimError::UnsupportedNfiEntryTag {
                            pair: pair.pair.clone(),
                            entry_tag: signal.tag.clone().unwrap_or_else(|| "<none>".to_owned()),
                        });
                    }
                    let tied_up_stake = open_trades
                        .iter()
                        .map(|trade| trade.stake_amount)
                        .sum::<f64>();
                    let stake_available = available_stake_amount(
                        available_balance,
                        tied_up_stake,
                        config.tradable_balance_ratio,
                    );
                    let proposed_stake = if config.unlimited_stake {
                        let slot_divisor = f64::from(
                            u32::try_from(config.max_open_trades)
                                .expect("validated max_open_trades fits u32"),
                        );
                        ((stake_available + tied_up_stake) / slot_divisor).min(stake_available)
                    } else {
                        config.stake_amount.min(stake_available)
                    };
                    if let Some(trade) = enter_trade(
                        EntryRequest {
                            pair_index,
                            pair,
                            candle,
                            side,
                            signal,
                            stake: EntryStake {
                                proposed: proposed_stake,
                                maximum: stake_available,
                            },
                            open_trades: &open_trades,
                            id: next_trade_id,
                            order_id: next_order_id,
                        },
                        config,
                    )? {
                        next_trade_id += 1;
                        next_order_id += 1;
                        open_trades.push(trade);
                        maximum_concurrent_trades =
                            maximum_concurrent_trades.max(open_trades.len());
                        available_balance =
                            wallet_free(config.starting_balance, &open_trades, &closed_trades);
                        true
                    } else {
                        rejected_signals += 1;
                        false
                    }
                }
            } else {
                false
            };

            if !opened_now {
                if let Some(trade_index) = open_trades
                    .iter()
                    .position(|trade| trade.pair_index == pair_index)
                {
                    update_extrema(&mut open_trades[trade_index], candle);
                    apply_funding(&mut open_trades[trade_index], candle);
                    // Freqtrade exposes `wallets.get_available_stake_amount()`
                    // as the callback's max_stake. This is smaller than raw
                    // free balance when tradable_balance_ratio keeps a wallet
                    // reserve, and NFI intentionally rejects an adjustment
                    // that exceeds this boundary instead of clamping it.
                    let tied_up_stake = open_trades
                        .iter()
                        .map(|trade| trade.stake_amount)
                        .sum::<f64>();
                    let adjustment_available = available_stake_amount(
                        available_balance,
                        tied_up_stake,
                        config.tradable_balance_ratio,
                    );
                    let adjustment = if let Some(adjustment) = &candle.adjustment {
                        Some(adjustment.clone())
                    } else if let Some(manager) = &config.nfi_x7_trade_manager {
                        let feature_index = callback_feature_index(cursor).ok_or_else(|| {
                            SimError::InvalidPositionAdjustment {
                                pair: pair.pair.clone(),
                                timestamp_ms: candle.timestamp_ms,
                            }
                        })?;
                        evaluate_nfi_position_adjustment(
                            manager,
                            &mut open_trades[trade_index],
                            pair,
                            feature_index,
                            candle,
                            config,
                            adjustment_available,
                        )
                        .ok_or_else(|| {
                            SimError::InvalidPositionAdjustment {
                                pair: pair.pair.clone(),
                                timestamp_ms: candle.timestamp_ms,
                            }
                        })?
                    } else if let Some(bundle) = &config.adjust_trade_position_program {
                        let feature_index = callback_feature_index(cursor).ok_or_else(|| {
                            SimError::InvalidPositionAdjustment {
                                pair: pair.pair.clone(),
                                timestamp_ms: candle.timestamp_ms,
                            }
                        })?;
                        evaluate_adjustment_bundle(
                            bundle,
                            &open_trades[trade_index],
                            pair,
                            feature_index,
                            candle,
                            config,
                            adjustment_available,
                        )
                        .map_err(|()| {
                            SimError::InvalidPositionAdjustment {
                                pair: pair.pair.clone(),
                                timestamp_ms: candle.timestamp_ms,
                            }
                        })?
                    } else {
                        rule_adjustment(&open_trades[trade_index], candle, config)
                    };
                    if let Some(adjustment) = adjustment {
                        let order_count = open_trades[trade_index].orders.len();
                        apply_adjustment(
                            &mut open_trades[trade_index],
                            candle,
                            &adjustment,
                            config,
                            adjustment_available,
                            next_order_id,
                        )?;
                        if open_trades[trade_index].orders.len() > order_count {
                            next_order_id += 1;
                        }
                        available_balance =
                            wallet_free(config.starting_balance, &open_trades, &closed_trades);
                    }
                    if let Some(exit) = exit_decision(
                        &open_trades[trade_index],
                        pair,
                        cursor,
                        candle,
                        config,
                        &mut profit_targets,
                    )? {
                        let (confirmed, clear_profit_target) = if exit.requires_confirmation {
                            if let Some(program) = &config.exit_confirmation_program {
                                evaluate_exit_confirm_program(
                                    program,
                                    &open_trades[trade_index],
                                    candle.timestamp_ms,
                                    exit.rate,
                                    &exit.reason,
                                    config,
                                )
                                .ok_or_else(|| {
                                    SimError::InvalidExitConfirmation {
                                        pair: pair.pair.clone(),
                                        timestamp_ms: candle.timestamp_ms,
                                    }
                                })?
                            } else {
                                (true, false)
                            }
                        } else {
                            // Freqtrade deliberately bypasses
                            // confirm_trade_exit for liquidation orders.
                            (true, false)
                        };
                        if confirmed {
                            if clear_profit_target {
                                profit_targets.remove(&pair.pair);
                            }
                            let trade = open_trades.swap_remove(trade_index);
                            let (closed, _) = close_trade(
                                trade,
                                candle.timestamp_ms,
                                exit.rate,
                                exit.reason,
                                config,
                                closed_trades.len(),
                                next_order_id,
                            );
                            next_order_id += 1;
                            closed_trades.push(closed);
                            available_balance =
                                wallet_free(config.starting_balance, &open_trades, &closed_trades);
                        }
                    }
                }
            }
            cursors[pair_index] += 1;
            if cursors[pair_index] > 1 {
                observer(&simulation_event(
                    candle.timestamp_ms,
                    &pair.pair,
                    available_balance,
                    &open_trades,
                    &closed_trades,
                    rejected_signals,
                    next_trade_id - 1,
                ));
            }
        }
    }

    let event_loop_ns = duration_ns(event_loop_started.elapsed());
    let finalization_started = Instant::now();
    for trade in open_trades {
        let last = input.pairs[trade.pair_index]
            .candles
            .last()
            .expect("validated non-empty candles");
        let (closed, _) = close_trade(
            trade,
            last.timestamp_ms,
            last.open,
            "force_exit".to_owned(),
            config,
            closed_trades.len(),
            next_order_id,
        );
        next_order_id += 1;
        closed_trades.push(closed);
    }
    available_balance = wallet_free(config.starting_balance, &[], &closed_trades);
    closed_trades.sort_by_key(|trade| (trade.open_timestamp_ms, trade.id));
    for (sequence, trade) in closed_trades.iter_mut().enumerate() {
        trade.sequence = sequence;
    }
    // Freqtrade exports `profit_total_abs` from Pandas' reduction of the
    // per-trade profit column. It is not derived from final wallet balance.
    // Pairwise summation mirrors NumPy's stable reduction and avoids the ulp
    // drift of a left-to-right iterator fold on long NFI result sets.
    let profit_total_abs = pairwise_sum(
        &closed_trades
            .iter()
            .map(|trade| trade.profit_abs)
            .collect::<Vec<_>>(),
    );
    let per_trade_volumes = closed_trades
        .iter()
        // Freqtrade calls Python `sum()` once per trade. CPython 3.14 uses a
        // compensated float accumulator, so Rust's ordinary Iterator::sum
        // differs by a few ulps on adjustment-heavy trades.
        .map(|trade| python_float_sum(trade.orders.iter().map(|order| order.cost)))
        .collect::<Vec<_>>();
    // Freqtrade then calls Python `sum()` over the per-trade subtotals. Keep
    // that second reduction boundary: flattening all orders is observably
    // different even when every order itself already matches.
    let total_volume = python_float_sum(per_trade_volumes);
    let result = SimulationResult {
        schema_version: SIMULATOR_SCHEMA_VERSION,
        starting_balance: config.starting_balance,
        final_balance: available_balance,
        profit_total_abs,
        total_volume,
        rejected_signals,
        maximum_concurrent_trades,
        trades: closed_trades,
    };
    let profile = SimulationProfile {
        schema_version: "1.0.0",
        validation_ns,
        event_loop_ns,
        finalization_ns: duration_ns(finalization_started.elapsed()),
        timestamp_batches,
        pair_events,
    };
    Ok((result, profile))
}

fn duration_ns(duration: Duration) -> u64 {
    u64::try_from(duration.as_nanos()).unwrap_or(u64::MAX)
}

fn simulation_event(
    timestamp_ms: i64,
    pair: &str,
    quote_free: f64,
    open_trades: &[OpenTrade],
    closed_trades: &[ClosedTrade],
    rejected_signals: u64,
    trade_id_counter: u64,
) -> SimulationEvent {
    let mut base_balances: Vec<AssetBalance> = open_trades
        .iter()
        .map(|trade| AssetBalance {
            currency: trade
                .pair
                .split_once('/')
                .map_or_else(|| trade.pair.clone(), |(base, _)| base.to_owned()),
            free: if trade.side == TradeSide::Short {
                -trade.amount
            } else {
                trade.amount
            },
        })
        .collect();
    base_balances.sort_by(|left, right| left.currency.cmp(&right.currency));
    let realized_profit = closed_trades.iter().map(|trade| trade.profit_abs).sum();
    let order_id_counter = closed_trades
        .iter()
        .map(|trade| trade.orders.len())
        .sum::<usize>()
        + open_trades
            .iter()
            .map(|trade| trade.orders.len())
            .sum::<usize>();
    SimulationEvent {
        timestamp_ms,
        pair: pair.to_owned(),
        state: SimulationState {
            quote_free,
            base_balances,
            open_trade_count: open_trades.len(),
            realized_profit,
            closed_trade_count: closed_trades.len(),
            rejected_signals,
            trade_id_counter,
            order_id_counter,
        },
    }
}

fn wallet_free(
    starting_balance: f64,
    open_trades: &[OpenTrade],
    closed_trades: &[ClosedTrade],
) -> f64 {
    let realized_profit = closed_trades
        .iter()
        .map(|trade| trade.profit_abs)
        .sum::<f64>();
    let tied_up_stake = open_trades
        .iter()
        .map(|trade| trade.stake_amount)
        .sum::<f64>();
    let open_realized_profit = open_trades
        .iter()
        .map(|trade| trade.realized_partial_profit)
        .sum::<f64>();
    // Freqtrade does not settle a running funding value into its backtest
    // wallet. It becomes available only through a realized partial exit or a
    // closed trade, both of which are already included above.
    starting_balance + realized_profit + open_realized_profit - tied_up_stake
}

fn available_stake_amount(free: f64, tied_up_stake: f64, ratio: f64) -> f64 {
    let total_stake_amount = (tied_up_stake + free) * ratio;
    (total_stake_amount - tied_up_stake).min(free).max(0.0)
}

fn validate_input(input: &SimulationInput) -> Result<(), SimError> {
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
    if config
        .leverage
        .is_some_and(|leverage| !leverage.is_finite() || leverage <= 0.0)
    {
        return Err(SimError::InvalidPositiveConfig("leverage"));
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
    for (pair_index, pair) in input.pairs.iter().enumerate() {
        validate_pair_series(pair_index, pair)?;
        if let Some(manager) = &config.nfi_x7_trade_manager {
            validate_nfi_pair_signals(pair, manager)?;
        }
    }
    Ok(())
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
    let valid_identity = manager.schema_version == "0.8.0"
        && manager.source_sha256.len() == 64
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
    let valid_short_routes = manager.managed_short_routes.len() == 1
        && short_keys == BTreeSet::from(["short_rebuy"])
        && short_tags.len() == total_short_tag_count
        && short_tags.iter().all(|tag| !managed_tags.contains(*tag))
        && manager
            .managed_short_routes
            .iter()
            .all(valid_nfi_managed_short_route)
        && manager.short_route_order == ["short_rebuy"];
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
            && route.adjustment_scope == "spot-grind-backtest-v1"
            && route.grind_mode
            && route.decision_program == "long_grind_entry_v3"
            && route.first_entry_profit_threshold_spot.is_finite()
            && route.first_entry_profit_threshold_spot > 0.0
            && route.first_entry_stop_threshold_spot.is_finite()
            && route.first_entry_stop_threshold_spot < 0.0
            && route.stateful_input_contract.is_object()
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
            && route.adjustment_scope == "exit-only-v1"
            && !route.grind_mode
            && route.decision_program == "long_grind_entry_v3"
            && route.first_entry_profit_threshold_spot.is_finite()
            && route.first_entry_profit_threshold_spot > 0.0
            && route.first_entry_stop_threshold_spot.is_finite()
            && route.first_entry_stop_threshold_spot < 0.0
            && route.stateful_input_contract.is_object()
            && valid_nfi_legacy_grind_constants(&route.constants)
    });
    let valid_programs = manager.programs.len()
        == PROGRAM_ORDER.len() + SHORT_PROGRAM_ORDER.len() + usize::from(adjustment.is_some())
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
        && adjustment.is_none_or(|adjustment| {
            manager
                .programs
                .get(&adjustment.decision_program)
                .is_some_and(valid_scalar_program)
        });
    let valid_adjustment_route = adjustment.is_none_or(|adjustment| {
        let adjustment_tags = adjustment.entry_tags.iter().collect::<BTreeSet<_>>();
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
            && valid_nfi_adjustment_constants(&adjustment.constants)
    });
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
        short_rebuy_adjustment.enabled
            && adjustment_tags == route_tags
            && adjustment_tags.len() == short_rebuy_adjustment.entry_tags.len()
            && short_rebuy_adjustment.system_version == constants.system_v3_2_name
            && short_rebuy_adjustment.execution_scope == "pre-derisk-only-v1"
            && short_rebuy_adjustment.post_derisk_action == "fail-simulation"
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
        || !valid_short_routes
        || !valid_route_order
        || !valid_long_grind
        || !valid_long_btc
        || !valid_programs
        || !valid_adjustment_route
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
    let route_tags = route.entry_tags.iter().collect::<BTreeSet<_>>();
    route.key == "short_rebuy"
        && route.profile == NfiManagedLongProfile::Rebuy
        && route.mode_name == "short_rebuy"
        && !route.entry_tags.is_empty()
        && route_tags.len() == route.entry_tags.len()
        && route.entry_tags.iter().all(|tag| !tag.is_empty())
        && route
            .stop_threshold_futures
            .is_some_and(|value| value.is_finite() && value >= 0.0)
        && route
            .stop_threshold_spot
            .is_some_and(|value| value.is_finite() && value >= 0.0)
}

fn valid_nfi_managed_long_route(route: &NfiManagedLongRoute) -> bool {
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
        && levels == [1, 2, 3]
        && grinds == [1, 2, 3, 4, 5]
        && derisk_numbers_are_valid
        && grind_numbers_are_valid
}

#[allow(clippy::too_many_arguments)]
#[allow(clippy::option_option)] // Outer None rejects invalid state; inner None is a valid no-op.
fn evaluate_nfi_position_adjustment(
    manager: &NfiX7TradeManager,
    trade: &mut OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    available_balance: f64,
) -> Option<Option<AdjustmentSignal>> {
    if trade.side == TradeSide::Short {
        let words = trade
            .entry_tag
            .as_deref()
            .unwrap_or("")
            .split_whitespace()
            .collect::<Vec<_>>();
        let route = manager
            .managed_short_routes
            .iter()
            .find(|route| nfi_short_route_supports_tags(route, &words))?;
        if route.profile != NfiManagedLongProfile::Rebuy {
            return None;
        }
        return evaluate_nfi_short_rebuy_adjustment(
            &manager.short_rebuy_adjustment,
            trade,
            pair,
            candle_index,
            candle,
            config,
            available_balance,
        );
    }
    if let Some(route) = manager
        .managed_long_routes
        .iter()
        .find(|route| route.profile == NfiManagedLongProfile::Rebuy)
    {
        let words = trade
            .entry_tag
            .as_deref()
            .unwrap_or("")
            .split_whitespace()
            .collect::<Vec<_>>();
        if nfi_managed_route_supports_tags(manager, route, &words) {
            let first_exit_is_level_three = trade
                .orders
                .iter()
                .find(|order| !order.is_entry)
                .and_then(|order| order.tag.as_deref())
                == Some("derisk_level_3");
            if !first_exit_is_level_three {
                return evaluate_nfi_rebuy_adjustment(
                    &manager.rebuy_adjustment,
                    trade,
                    pair,
                    candle_index,
                    candle,
                    config,
                    available_balance,
                );
            }
            // X7 permanently transfers a rebuy trade to the shared grind-v3
            // state machine after its first level-3 de-risk fill.
        }
    }
    if let Some(route) = manager.long_grind.as_ref() {
        if nfi_long_grind_supports_trade(route, trade) {
            return evaluate_nfi_legacy_grind_adjustment(
                manager,
                route,
                trade,
                pair,
                candle_index,
                candle,
                config,
                available_balance,
            );
        }
    }
    if let Some(route) = manager.long_btc.as_ref() {
        if nfi_long_grind_supports_trade(route, trade) {
            return evaluate_nfi_legacy_grind_adjustment(
                manager,
                route,
                trade,
                pair,
                candle_index,
                candle,
                config,
                available_balance,
            );
        }
    }
    evaluate_nfi_system_v3_adjustment(
        manager,
        trade,
        pair,
        candle_index,
        candle,
        config,
        available_balance,
    )
}

fn nfi_long_grind_supports_trade(route: &NfiLongGrindRoute, trade: &OpenTrade) -> bool {
    let words = trade
        .entry_tag
        .as_deref()
        .unwrap_or("")
        .split_whitespace()
        .collect::<Vec<_>>();
    // X7 uses ``all(c in long_grind_mode_tags for c in enter_tags)`` for
    // this route. Requiring every word matters for mixed NFI tags: top-coins
    // intentionally uses a different, any-tag routing rule.
    !words.is_empty()
        && words
            .iter()
            .all(|word| route.entry_tags.iter().any(|supported| supported == word))
}

fn validate_nfi_pair_signals(
    pair: &PairSeries,
    manager: &NfiX7TradeManager,
) -> Result<(), SimError> {
    for candle in &pair.candles {
        if let Some(signal) = &candle.enter_short {
            if !nfi_entry_signal_is_supported(manager, TradeSide::Short, signal) {
                return Err(SimError::UnsupportedNfiEntryTag {
                    pair: pair.pair.clone(),
                    entry_tag: signal.tag.clone().unwrap_or_else(|| "<short>".to_owned()),
                });
            }
        }
    }
    Ok(())
}

fn nfi_entry_signal_is_supported(
    manager: &NfiX7TradeManager,
    side: TradeSide,
    signal: &EntrySignal,
) -> bool {
    signal.tag.as_deref().is_some_and(|entry_tag| {
        let words = entry_tag.split_whitespace().collect::<Vec<_>>();
        if words.is_empty() {
            return false;
        }
        match side {
            TradeSide::Long => {
                // Reject mixed tags containing a route that has not been
                // lowered. Checking only one recognized word can skip an
                // earlier source branch and silently change callback state.
                words
                    .iter()
                    .all(|tag| nfi_long_tag_is_in_compiled_scope(manager, tag))
                    && nfi_any_route_matches(manager, &words)
            }
            TradeSide::Short => {
                words.iter().all(|tag| {
                    manager
                        .managed_short_routes
                        .iter()
                        .any(|route| route.entry_tags.iter().any(|supported| supported == tag))
                }) && manager
                    .managed_short_routes
                    .iter()
                    .any(|route| nfi_short_route_supports_tags(route, &words))
            }
        }
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

fn nfi_short_route_supports_tags(route: &NfiManagedLongRoute, words: &[&str]) -> bool {
    !words.is_empty()
        && words
            .iter()
            .all(|word| route.entry_tags.iter().any(|supported| supported == word))
}

fn nfi_any_route_matches(manager: &NfiX7TradeManager, words: &[&str]) -> bool {
    manager
        .managed_long_routes
        .iter()
        .any(|route| nfi_managed_route_supports_tags(manager, route, words))
        || manager
            .long_grind
            .as_ref()
            .is_some_and(|route| nfi_legacy_route_supports_tags(route, words))
        || manager
            .long_btc
            .as_ref()
            .is_some_and(|route| nfi_legacy_route_supports_tags(route, words))
}

fn nfi_managed_route_supports_tags(
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

fn nfi_legacy_route_supports_tags(route: &NfiLongGrindRoute, words: &[&str]) -> bool {
    !words.is_empty()
        && words
            .iter()
            .all(|word| route.entry_tags.iter().any(|supported| supported == word))
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

fn validate_pair_series(pair_index: usize, pair: &PairSeries) -> Result<(), SimError> {
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
    for (index, candle) in pair.candles.iter().enumerate() {
        if previous.is_some_and(|value| candle.timestamp_ms <= value) {
            return Err(SimError::CandleOrder {
                pair: pair.pair.clone(),
                index,
            });
        }
        previous = Some(candle.timestamp_ms);
        validate_candle(pair, index, &candle)?;
    }
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
    if candle.enter_long.is_some() && candle.enter_short.is_some() {
        return Err(SimError::ConflictingEntrySignals {
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

fn next_timestamp(pairs: &[PairSeries], cursors: &[usize]) -> Option<i64> {
    pairs
        .iter()
        .zip(cursors)
        .filter_map(|(pair, cursor)| pair.candles.timestamp_ms(*cursor))
        .min()
}

fn enter_trade(
    request: EntryRequest<'_>,
    config: &PortfolioConfig,
) -> Result<Option<OpenTrade>, SimError> {
    let leverage = entry_leverage(request.signal, config, request.pair, request.candle)?;
    let requested = requested_entry_stake(&request, config, leverage)?;
    let EntryRequest {
        pair_index,
        pair,
        candle,
        side,
        signal,
        stake: _,
        open_trades: _,
        id,
        order_id,
    } = request;
    let Some((amount, stake, precise_cost, order_cost)) = entry_sizing(
        requested,
        candle.open,
        fee_open(config),
        pair.amount_step.unwrap_or(config.amount_step),
        leverage,
    ) else {
        return Ok(None);
    };
    if !entry_is_confirmed(&request, config, amount)? {
        return Ok(None);
    }
    let tag = signal.tag.clone();
    let order = FilledOrder {
        id: order_id,
        funding_fee: 0.0,
        sequence: 0,
        side: entry_order_side(side),
        is_entry: true,
        filled_timestamp_ms: candle.timestamp_ms,
        amount,
        price: candle.open,
        cost: order_cost,
        tag: tag.clone(),
    };
    let amount_step = pair.amount_step.unwrap_or(config.amount_step);
    let price_step = pair_price_step(pair, candle, config.price_step);
    let stop_loss = initial_stop_loss(
        side,
        candle.open,
        config.stoploss_ratio,
        leverage,
        price_step,
    );
    let mut trade = OpenTrade {
        id,
        pair_index,
        pair: pair.pair.clone(),
        side,
        leverage,
        amount_step,
        price_step,
        open_timestamp_ms: candle.timestamp_ms,
        open_rate: candle.open,
        amount,
        stake_amount: stake,
        max_stake_amount: stake,
        entry_cost_with_fees: precise_cost,
        first_entry_cost_with_fees: precise_cost,
        adjustment_count: 0,
        entry_tag: tag,
        funding_fees: 0.0,
        funding_fees_total: 0.0,
        funding_sum_high: 0.0,
        funding_sum_low: 0.0,
        realized_partial_profit: 0.0,
        liquidation_price: signal.liquidation_price,
        initial_stop_loss: stop_loss,
        stop_loss,
        minimum_rate: candle.low,
        maximum_rate: candle.high,
        orders: vec![order],
        custom_data: BTreeMap::new(),
    };
    apply_order_filled(&mut trade, signal.tag.as_deref(), config);
    Ok(Some(trade))
}

fn requested_entry_stake(
    request: &EntryRequest<'_>,
    config: &PortfolioConfig,
    leverage: f64,
) -> Result<f64, SimError> {
    let Some(program) = &config.stake_program else {
        return Ok(request.stake.proposed);
    };
    evaluate_stake_program(
        program,
        &StakeInputs {
            proposed_stake: request.stake.proposed,
            minimum_stake: minimum_pair_stake(
                request.pair,
                request.candle.open,
                config.stoploss_ratio,
                leverage,
                config.amount_reserve_percent,
            ),
            maximum_stake: request.stake.maximum,
            current_rate: request.candle.open,
            leverage,
            entry_tag: request.signal.tag.as_deref(),
            side: request.side,
        },
    )
    .ok_or_else(|| SimError::InvalidStakeProgram {
        pair: request.pair.pair.clone(),
        timestamp_ms: request.candle.timestamp_ms,
    })
    .map(|stake| stake.min(request.stake.maximum))
}

fn initial_stop_loss(
    side: TradeSide,
    open_rate: f64,
    stoploss_ratio: f64,
    leverage: f64,
    price_step: f64,
) -> f64 {
    let leveraged_stoploss = stoploss_ratio / leverage;
    match side {
        TradeSide::Long => ceil_step(open_rate * (1.0 + leveraged_stoploss), price_step),
        TradeSide::Short => floor_step(open_rate * (1.0 - leveraged_stoploss), price_step),
    }
}

fn pair_price_step(pair: &PairSeries, candle: &Candle, default: f64) -> f64 {
    let changes_before_or_at_candle = pair
        .price_steps
        .partition_point(|change| change.timestamp_ms <= candle.timestamp_ms);
    changes_before_or_at_candle
        .checked_sub(1)
        .and_then(|index| pair.price_steps.get(index))
        .map_or_else(|| pair.price_step.unwrap_or(default), |change| change.step)
}

fn entry_leverage(
    signal: &EntrySignal,
    config: &PortfolioConfig,
    pair: &PairSeries,
    candle: &Candle,
) -> Result<f64, SimError> {
    let leverage = signal.leverage.or(config.leverage).unwrap_or(1.0);
    if leverage.is_finite() && leverage > 0.0 {
        Ok(leverage)
    } else {
        Err(SimError::InvalidLeverage {
            pair: pair.pair.clone(),
            timestamp_ms: candle.timestamp_ms,
        })
    }
}

fn entry_is_confirmed(
    request: &EntryRequest<'_>,
    config: &PortfolioConfig,
    amount: f64,
) -> Result<bool, SimError> {
    let Some(program) = &config.entry_confirmation_program else {
        return Ok(true);
    };
    evaluate_confirm_program(
        program,
        ConfirmInputs {
            pair: &request.pair.pair,
            timestamp_ms: request.candle.timestamp_ms,
            amount,
            rate: request.candle.open,
            entry_tag: request.signal.tag.as_deref(),
            side: request.side,
            previous_close: request.candle.previous_close,
            open_trades: request.open_trades,
            max_open_trades: config.max_open_trades,
        },
    )
    .ok_or_else(|| SimError::InvalidEntryConfirmation {
        pair: request.pair.pair.clone(),
        timestamp_ms: request.candle.timestamp_ms,
    })
}

fn minimum_pair_stake(
    pair: &PairSeries,
    rate: f64,
    stoploss_ratio: f64,
    leverage: f64,
    reserve_percent: f64,
) -> f64 {
    if let Some(stake) = pair.minimum_stake {
        return stake;
    }
    let margin_reserve = 1.0 + reserve_percent;
    let denominator = 1.0 - stoploss_ratio.abs();
    let stoploss_reserve = if denominator > 0.0 {
        (margin_reserve / denominator).clamp(1.0, 1.5)
    } else {
        1.5
    };
    let cost_stake = pair
        .minimum_cost
        .map_or(0.0, |cost| cost * stoploss_reserve);
    let amount_stake = pair
        .minimum_amount
        .map_or(0.0, |amount| amount * rate * margin_reserve);
    cost_stake.max(amount_stake) / leverage
}

/// Return the minimum stake exposed to `adjust_trade_position`.
///
/// Freqtrade's backtester asks the exchange for this value with a fixed
/// `-10%` stop-loss reserve and does not pass the trade leverage. Entry-order
/// validation is different: it passes leverage explicitly. Keeping this
/// distinction in one helper prevents the generic callback path and the
/// optimized NFI managers from drifting apart.
fn adjustment_minimum_pair_stake(pair: &PairSeries, rate: f64, reserve_percent: f64) -> f64 {
    minimum_pair_stake(pair, rate, -0.1, 1.0, reserve_percent)
}

fn apply_order_filled(trade: &mut OpenTrade, order_tag: Option<&str>, config: &PortfolioConfig) {
    let Some(program) = config
        .callback_program
        .as_ref()
        .and_then(|program| program.order_filled.as_ref())
    else {
        return;
    };
    let successful_entries = trade.orders.iter().filter(|order| order.is_entry).count();
    if successful_entries == 1 {
        apply_custom_writes(
            &mut trade.custom_data,
            &program.initial_successful_entry_writes,
        );
    }
    let Some(mode) = order_tag.and_then(|tag| tag.split(' ').next()) else {
        return;
    };
    if let Some(writes) = program.order_tag_actions.get(mode) {
        apply_custom_writes(&mut trade.custom_data, writes);
    }
}

fn apply_custom_writes(custom_data: &mut BTreeMap<String, Value>, writes: &[CustomDataWrite]) {
    for write in writes {
        custom_data.insert(write.key.clone(), write.value.clone());
    }
}

struct StakeInputs<'a> {
    proposed_stake: f64,
    minimum_stake: f64,
    maximum_stake: f64,
    current_rate: f64,
    leverage: f64,
    entry_tag: Option<&'a str>,
    side: TradeSide,
}

#[derive(Clone, Copy)]
struct EntryStake {
    proposed: f64,
    maximum: f64,
}

#[derive(Clone, Copy)]
struct EntryRequest<'a> {
    pair_index: usize,
    pair: &'a PairSeries,
    candle: &'a Candle,
    side: TradeSide,
    signal: &'a EntrySignal,
    stake: EntryStake,
    open_trades: &'a [OpenTrade],
    id: u64,
    order_id: u64,
}

enum StakeControl {
    Continue,
    Return(Value),
}

fn evaluate_stake_program(program: &StakeProgram, inputs: &StakeInputs<'_>) -> Option<f64> {
    let mut variables = BTreeMap::from([
        (
            "proposed_stake".to_owned(),
            number_value(inputs.proposed_stake)?,
        ),
        ("min_stake".to_owned(), number_value(inputs.minimum_stake)?),
        ("max_stake".to_owned(), number_value(inputs.maximum_stake)?),
        (
            "current_rate".to_owned(),
            number_value(inputs.current_rate)?,
        ),
        ("leverage".to_owned(), number_value(inputs.leverage)?),
        (
            "entry_tag".to_owned(),
            Value::String(inputs.entry_tag?.to_owned()),
        ),
        (
            "side".to_owned(),
            Value::String(
                match inputs.side {
                    TradeSide::Long => "long",
                    TradeSide::Short => "short",
                }
                .to_owned(),
            ),
        ),
    ]);
    let StakeControl::Return(result) =
        evaluate_stake_statements(&program.statements, &mut variables)?
    else {
        return None;
    };
    let stake = result.as_f64()?;
    (stake.is_finite() && stake > 0.0).then_some(stake)
}

fn evaluate_stake_statements(
    statements: &[StakeStatement],
    variables: &mut BTreeMap<String, Value>,
) -> Option<StakeControl> {
    for statement in statements {
        match statement {
            StakeStatement::Let { name, value } => {
                let result = evaluate_stake_expression(value, variables)?;
                variables.insert(name.clone(), result);
            }
            StakeStatement::If {
                condition,
                then,
                otherwise,
            } => {
                let branch = if evaluate_stake_expression(condition, variables)?.as_bool()? {
                    then
                } else {
                    otherwise
                };
                if let control @ StakeControl::Return(_) =
                    evaluate_stake_statements(branch, variables)?
                {
                    return Some(control);
                }
            }
            StakeStatement::For {
                name,
                iterable,
                body,
            } => {
                let values = evaluate_stake_expression(iterable, variables)?
                    .as_array()?
                    .clone();
                for value in values {
                    variables.insert(name.clone(), value);
                    if let control @ StakeControl::Return(_) =
                        evaluate_stake_statements(body, variables)?
                    {
                        return Some(control);
                    }
                }
            }
            StakeStatement::Return { value } => {
                return Some(StakeControl::Return(evaluate_stake_expression(
                    value, variables,
                )?));
            }
        }
    }
    Some(StakeControl::Continue)
}

fn evaluate_stake_expression(
    expression: &StakeExpression,
    variables: &BTreeMap<String, Value>,
) -> Option<Value> {
    match expression {
        StakeExpression::Literal { value } => valid_vm_value(value).then(|| value.clone()),
        StakeExpression::Variable { name } => variables.get(name).cloned(),
        StakeExpression::Multiply { left, right } => number_value(
            evaluate_stake_expression(left, variables)?.as_f64()?
                * evaluate_stake_expression(right, variables)?.as_f64()?,
        ),
        StakeExpression::And { values } => {
            for value in values {
                if !evaluate_stake_expression(value, variables)?.as_bool()? {
                    return Some(Value::Bool(false));
                }
            }
            Some(Value::Bool(true))
        }
        StakeExpression::Or { values } => {
            for value in values {
                if evaluate_stake_expression(value, variables)?.as_bool()? {
                    return Some(Value::Bool(true));
                }
            }
            Some(Value::Bool(false))
        }
        StakeExpression::Equal { left, right } => Some(Value::Bool(
            evaluate_stake_expression(left, variables)?
                == evaluate_stake_expression(right, variables)?,
        )),
        StakeExpression::Greater { left, right } => Some(Value::Bool(
            evaluate_stake_expression(left, variables)?.as_f64()?
                > evaluate_stake_expression(right, variables)?.as_f64()?,
        )),
        StakeExpression::Choose {
            condition,
            then,
            otherwise,
        } => {
            if evaluate_stake_expression(condition, variables)?.as_bool()? {
                evaluate_stake_expression(then, variables)
            } else {
                evaluate_stake_expression(otherwise, variables)
            }
        }
        StakeExpression::Index { value, index } => {
            let values = evaluate_stake_expression(value, variables)?;
            let index = evaluate_stake_expression(index, variables)?.as_u64()?;
            values
                .as_array()?
                .get(usize::try_from(index).ok()?)
                .cloned()
        }
        StakeExpression::SplitWords { value } => {
            let value = evaluate_stake_expression(value, variables)?;
            Some(Value::Array(
                value
                    .as_str()?
                    .split_whitespace()
                    .map(|word| Value::String(word.to_owned()))
                    .collect(),
            ))
        }
        StakeExpression::StakeClampMin { multiplier } => {
            let stake = variables.get("proposed_stake")?.as_f64()?
                * evaluate_stake_expression(multiplier, variables)?.as_f64()?;
            let minimum = variables.get("min_stake")?.as_f64()?;
            number_value(if stake > minimum { stake } else { minimum })
        }
        StakeExpression::AllIn { items, container } => {
            let items = evaluate_stake_expression(items, variables)?;
            let container = evaluate_stake_expression(container, variables)?;
            Some(Value::Bool(items.as_array()?.iter().all(|item| {
                container
                    .as_array()
                    .is_some_and(|values| values.contains(item))
            })))
        }
        StakeExpression::AnyIn { items, container } => {
            let items = evaluate_stake_expression(items, variables)?;
            let container = evaluate_stake_expression(container, variables)?;
            Some(Value::Bool(items.as_array()?.iter().any(|item| {
                container
                    .as_array()
                    .is_some_and(|values| values.contains(item))
            })))
        }
    }
}

fn valid_vm_value(value: &Value) -> bool {
    match value {
        Value::Bool(_) | Value::Number(_) | Value::String(_) => true,
        Value::Array(values) => values
            .iter()
            .all(|item| matches!(item, Value::Bool(_) | Value::Number(_) | Value::String(_))),
        Value::Null | Value::Object(_) => false,
    }
}

fn number_value(value: f64) -> Option<Value> {
    serde_json::Number::from_f64(value).map(Value::Number)
}

#[allow(clippy::float_cmp)] // A VM index is valid only when its float token is exactly integral.
fn integer_value(value: &Value) -> Option<i64> {
    if let Some(integer) = value.as_i64() {
        return Some(integer);
    }
    // Arithmetic expressions such as unary minus are serialized through
    // `Number::from_f64`, so JSON `-1.0` no longer answers `as_i64()` even
    // though Python treats it as the exact list index -1. Accept only finite,
    // integral values inside i64's exactly checked conversion range.
    let numeric = value.as_f64()?;
    if !numeric.is_finite() || numeric.fract() != 0.0 {
        return None;
    }
    numeric.to_i64()
}

#[derive(Clone, Copy)]
struct ConfirmInputs<'a> {
    pair: &'a str,
    timestamp_ms: i64,
    amount: f64,
    rate: f64,
    entry_tag: Option<&'a str>,
    side: TradeSide,
    previous_close: Option<f64>,
    open_trades: &'a [OpenTrade],
    max_open_trades: usize,
}

enum ConfirmControl {
    Continue,
    Return(Value),
}

fn evaluate_confirm_program(program: &ConfirmProgram, inputs: ConfirmInputs<'_>) -> Option<bool> {
    let side = match inputs.side {
        TradeSide::Long => "long",
        TradeSide::Short => "short",
    };
    let open_trades = Value::Array(
        inputs
            .open_trades
            .iter()
            .map(|trade| {
                Value::Object(serde_json::Map::from_iter([
                    (
                        "trade_direction".to_owned(),
                        Value::String(
                            match trade.side {
                                TradeSide::Long => "long",
                                TradeSide::Short => "short",
                            }
                            .to_owned(),
                        ),
                    ),
                    (
                        "enter_tag".to_owned(),
                        trade
                            .entry_tag
                            .as_ref()
                            .map_or(Value::Null, |tag| Value::String(tag.clone())),
                    ),
                ]))
            })
            .collect(),
    );
    let analyzed_frame = Value::Array(
        inputs
            .previous_close
            .and_then(number_value)
            .map(|close| Value::Object(serde_json::Map::from_iter([("close".to_owned(), close)])))
            .into_iter()
            .collect(),
    );
    let mut variables = BTreeMap::from([
        ("pair".to_owned(), Value::String(inputs.pair.to_owned())),
        ("order_type".to_owned(), Value::String("limit".to_owned())),
        ("amount".to_owned(), number_value(inputs.amount)?),
        ("rate".to_owned(), number_value(inputs.rate)?),
        ("time_in_force".to_owned(), Value::String("gtc".to_owned())),
        (
            "current_time".to_owned(),
            Value::Number(inputs.timestamp_ms.into()),
        ),
        (
            "entry_tag".to_owned(),
            Value::String(inputs.entry_tag?.to_owned()),
        ),
        ("side".to_owned(), Value::String(side.to_owned())),
        ("open_trades".to_owned(), open_trades),
        ("analyzed_frame".to_owned(), analyzed_frame),
        (
            "config.max_open_trades".to_owned(),
            Value::Number(u64::try_from(inputs.max_open_trades).ok()?.into()),
        ),
    ]);
    let ConfirmControl::Return(value) =
        evaluate_confirm_statements(&program.statements, &mut variables, program, 0)?
    else {
        return None;
    };
    value.as_bool()
}

fn evaluate_exit_confirm_program(
    program: &ConfirmProgram,
    trade: &OpenTrade,
    timestamp_ms: i64,
    rate: f64,
    exit_reason: &str,
    config: &PortfolioConfig,
) -> Option<(bool, bool)> {
    let liquidation_price = trade
        .liquidation_price
        .and_then(number_value)
        .unwrap_or(Value::Null);
    let profit_snapshot = nfi_profit_snapshot(
        trade,
        rate,
        fee_open(config),
        fee_close(config),
        config.is_futures,
    );
    let snapshot_value = |value: Option<f64>| value.and_then(number_value).unwrap_or(Value::Null);
    let trade_value = Value::Object(serde_json::Map::from_iter([
        (
            "realized_profit".to_owned(),
            number_value(trade.realized_partial_profit)?,
        ),
        ("stake_amount".to_owned(), number_value(trade.stake_amount)?),
        (
            "is_short".to_owned(),
            Value::Bool(trade.side == TradeSide::Short),
        ),
        ("liquidation_price".to_owned(), liquidation_price),
        ("open_rate".to_owned(), number_value(trade.open_rate)?),
        ("fee_close".to_owned(), number_value(fee_close(config))?),
        (
            "nfi_profit_stake".to_owned(),
            snapshot_value(profit_snapshot.map(|snapshot| snapshot.stake)),
        ),
        (
            "nfi_profit_ratio".to_owned(),
            snapshot_value(profit_snapshot.map(|snapshot| snapshot.ratio)),
        ),
        (
            "nfi_profit_current_stake_ratio".to_owned(),
            snapshot_value(profit_snapshot.map(|snapshot| snapshot.current_stake_ratio)),
        ),
        (
            "nfi_profit_initial_stake_ratio".to_owned(),
            snapshot_value(profit_snapshot.map(|snapshot| snapshot.initial_stake_ratio)),
        ),
    ]));
    let mut variables = BTreeMap::from([
        ("pair".to_owned(), Value::String(trade.pair.clone())),
        ("trade".to_owned(), trade_value),
        ("order_type".to_owned(), Value::String("limit".to_owned())),
        ("amount".to_owned(), number_value(trade.amount)?),
        ("rate".to_owned(), number_value(rate)?),
        ("time_in_force".to_owned(), Value::String("gtc".to_owned())),
        (
            "exit_reason".to_owned(),
            Value::String(exit_reason.to_owned()),
        ),
        (
            "current_time".to_owned(),
            Value::Number(timestamp_ms.into()),
        ),
        (
            "trade_profit_ratio".to_owned(),
            number_value(current_profit_ratio(trade, rate, fee_close(config)))?,
        ),
        ("clear_profit_target".to_owned(), Value::Bool(false)),
    ]);
    let ConfirmControl::Return(value) =
        evaluate_confirm_statements(&program.statements, &mut variables, program, 0)?
    else {
        return None;
    };
    Some((
        value.as_bool()?,
        variables.get("clear_profit_target")?.as_bool()?,
    ))
}

fn evaluate_confirm_statements(
    statements: &[Value],
    variables: &mut BTreeMap<String, Value>,
    program: &ConfirmProgram,
    depth: usize,
) -> Option<ConfirmControl> {
    if depth > 128 {
        return None;
    }
    for statement in statements {
        let object = statement.as_object()?;
        match object.get("op")?.as_str()? {
            "let" => {
                let name = object.get("name")?.as_str()?;
                let value = evaluate_confirm_expression(
                    object.get("value")?,
                    variables,
                    program,
                    depth + 1,
                )?;
                variables.insert(name.to_owned(), value);
            }
            "if" => {
                let condition = evaluate_confirm_expression(
                    object.get("condition")?,
                    variables,
                    program,
                    depth + 1,
                )?
                .as_bool()?;
                let branch = if condition {
                    object.get("then")?
                } else {
                    object.get("otherwise")?
                };
                if let control @ ConfirmControl::Return(_) =
                    evaluate_confirm_statements(branch.as_array()?, variables, program, depth + 1)?
                {
                    return Some(control);
                }
            }
            "return" => {
                return Some(ConfirmControl::Return(evaluate_confirm_expression(
                    object.get("value")?,
                    variables,
                    program,
                    depth + 1,
                )?));
            }
            "log_noop" => {}
            "clear_profit_target" => {
                let pair = evaluate_confirm_expression(
                    object.get("pair")?,
                    variables,
                    program,
                    depth + 1,
                )?;
                pair.as_str()?;
                variables.insert("clear_profit_target".to_owned(), Value::Bool(true));
            }
            _ => return None,
        }
    }
    Some(ConfirmControl::Continue)
}

#[allow(clippy::too_many_lines)]
fn evaluate_confirm_expression(
    expression: &Value,
    variables: &mut BTreeMap<String, Value>,
    program: &ConfirmProgram,
    depth: usize,
) -> Option<Value> {
    if depth > 128 {
        return None;
    }
    let object = expression.as_object()?;
    let op = object.get("op")?.as_str()?;
    match op {
        "literal" => object.get("value").cloned(),
        "variable" => variables.get(object.get("name")?.as_str()?).cloned(),
        "field" => {
            let value =
                evaluate_confirm_expression(object.get("value")?, variables, program, depth + 1)?;
            value
                .as_object()?
                .get(object.get("name")?.as_str()?)
                .cloned()
        }
        "config_value" => variables
            .get(&format!("config.{}", object.get("name")?.as_str()?))
            .cloned(),
        "index" => {
            let value =
                evaluate_confirm_expression(object.get("value")?, variables, program, depth + 1)?;
            let index =
                evaluate_confirm_expression(object.get("index")?, variables, program, depth + 1)?;
            if let Some(values) = value.as_array() {
                let raw_index = integer_value(&index)?;
                let length = i64::try_from(values.len()).ok()?;
                let resolved = if raw_index < 0 {
                    length.checked_add(raw_index)?
                } else {
                    raw_index
                };
                values.get(usize::try_from(resolved).ok()?).cloned()
            } else {
                value.as_object()?.get(index.as_str()?).cloned()
            }
        }
        "negative" => number_value(
            -evaluate_confirm_expression(object.get("value")?, variables, program, depth + 1)?
                .as_f64()?,
        ),
        "not" => Some(Value::Bool(
            !evaluate_confirm_expression(object.get("value")?, variables, program, depth + 1)?
                .as_bool()?,
        )),
        "add" | "subtract" | "multiply" | "divide" => {
            let left =
                evaluate_confirm_expression(object.get("left")?, variables, program, depth + 1)?
                    .as_f64()?;
            let right =
                evaluate_confirm_expression(object.get("right")?, variables, program, depth + 1)?
                    .as_f64()?;
            number_value(match op {
                "add" => left + right,
                "subtract" => left - right,
                "multiply" => left * right,
                "divide" if right != 0.0 => left / right,
                _ => return None,
            })
        }
        "and" | "or" => {
            let values = object.get("values")?.as_array()?;
            if op == "and" {
                for value in values {
                    if !evaluate_confirm_expression(value, variables, program, depth + 1)?
                        .as_bool()?
                    {
                        return Some(Value::Bool(false));
                    }
                }
                Some(Value::Bool(true))
            } else {
                for value in values {
                    if evaluate_confirm_expression(value, variables, program, depth + 1)?
                        .as_bool()?
                    {
                        return Some(Value::Bool(true));
                    }
                }
                Some(Value::Bool(false))
            }
        }
        "equal" | "not_equal" => {
            let left =
                evaluate_confirm_expression(object.get("left")?, variables, program, depth + 1)?;
            let right =
                evaluate_confirm_expression(object.get("right")?, variables, program, depth + 1)?;
            Some(Value::Bool(if op == "equal" {
                left == right
            } else {
                left != right
            }))
        }
        "greater" | "greater_equal" | "less" | "less_equal" => {
            let left =
                evaluate_confirm_expression(object.get("left")?, variables, program, depth + 1)?
                    .as_f64()?;
            let right =
                evaluate_confirm_expression(object.get("right")?, variables, program, depth + 1)?
                    .as_f64()?;
            Some(Value::Bool(match op {
                "greater" => left > right,
                "greater_equal" => left >= right,
                "less" => left < right,
                "less_equal" => left <= right,
                _ => return None,
            }))
        }
        "contains" => {
            let container = evaluate_confirm_expression(
                object.get("container")?,
                variables,
                program,
                depth + 1,
            )?;
            let value =
                evaluate_confirm_expression(object.get("value")?, variables, program, depth + 1)?;
            Some(Value::Bool(
                container
                    .as_array()
                    .is_some_and(|values| values.contains(&value))
                    || container
                        .as_str()
                        .zip(value.as_str())
                        .is_some_and(|(text, needle)| text.contains(needle)),
            ))
        }
        "all_in" | "any_in" => {
            let items =
                evaluate_confirm_expression(object.get("items")?, variables, program, depth + 1)?;
            let container = evaluate_confirm_expression(
                object.get("container")?,
                variables,
                program,
                depth + 1,
            )?;
            let items = items.as_array()?;
            let container = container.as_array()?;
            Some(Value::Bool(if op == "all_in" {
                items.iter().all(|item| container.contains(item))
            } else {
                items.iter().any(|item| container.contains(item))
            }))
        }
        "length" => {
            let value =
                evaluate_confirm_expression(object.get("value")?, variables, program, depth + 1)?;
            let length = value
                .as_array()
                .map(Vec::len)
                .or_else(|| value.as_str().map(str::len))?;
            Some(Value::Number(u64::try_from(length).ok()?.into()))
        }
        "open_trades" => variables.get("open_trades").cloned(),
        "open_trade_count" => {
            let count = variables.get("open_trades")?.as_array()?.len();
            Some(Value::Number(u64::try_from(count).ok()?.into()))
        }
        "analyzed_frame" => variables.get("analyzed_frame").cloned(),
        "trade_profit_ratio" => variables.get("trade_profit_ratio").cloned(),
        "split_words" => {
            let value =
                evaluate_confirm_expression(object.get("value")?, variables, program, depth + 1)?;
            Some(Value::Array(
                value
                    .as_str()?
                    .split_whitespace()
                    .map(|word| Value::String(word.to_owned()))
                    .collect(),
            ))
        }
        "partition" => {
            let value =
                evaluate_confirm_expression(object.get("value")?, variables, program, depth + 1)?;
            let text = value.as_str()?;
            let separator = object.get("separator")?.as_str()?;
            let (before, found, after) = text.find(separator).map_or((text, "", ""), |index| {
                (&text[..index], separator, &text[index + separator.len()..])
            });
            Some(Value::Array(
                [before, found, after]
                    .into_iter()
                    .map(|item| Value::String(item.to_owned()))
                    .collect(),
            ))
        }
        "count" => {
            let iterable = evaluate_confirm_expression(
                object.get("iterable")?,
                variables,
                program,
                depth + 1,
            )?;
            let values = iterable.as_array()?.clone();
            let name = object.get("name")?.as_str()?;
            let filters = object.get("filters")?.as_array()?;
            let previous = variables.get(name).cloned();
            let mut count = 0_u64;
            for value in values {
                variables.insert(name.to_owned(), value);
                let mut accepted = true;
                for filter in filters {
                    if !evaluate_confirm_expression(filter, variables, program, depth + 1)?
                        .as_bool()?
                    {
                        accepted = false;
                        break;
                    }
                }
                count += u64::from(accepted);
            }
            if let Some(previous) = previous {
                variables.insert(name.to_owned(), previous);
            } else {
                variables.remove(name);
            }
            Some(Value::Number(count.into()))
        }
        "call" => {
            let function = program.functions.get(object.get("name")?.as_str()?)?;
            let argument_nodes = object.get("arguments")?.as_array()?;
            if argument_nodes.len() != function.parameters.len() {
                return None;
            }
            let arguments = argument_nodes
                .iter()
                .map(|argument| {
                    evaluate_confirm_expression(argument, variables, program, depth + 1)
                })
                .collect::<Option<Vec<_>>>()?;
            let mut local = variables.clone();
            for (parameter, argument) in function.parameters.iter().zip(arguments) {
                local.insert(parameter.clone(), argument);
            }
            let ConfirmControl::Return(value) =
                evaluate_confirm_statements(&function.statements, &mut local, program, depth + 1)?
            else {
                return None;
            };
            Some(value)
        }
        _ => None,
    }
}

enum ScalarControl {
    Continue,
    Return(Value),
}

/// Evaluate a compact scalar-decision program without entering Python.
///
/// Inputs are the already-normalized method arguments. The function returns
/// `None` when either the program contract or a runtime value is invalid.
#[must_use]
pub fn evaluate_scalar_decision_program(
    program: &ScalarDecisionProgram,
    variables: BTreeMap<String, Value>,
) -> Option<Value> {
    evaluate_scalar_program(program, variables, None, 0)
}

/// Evaluate one entry method in a hash-bound scalar program bundle.
///
/// Calls are resolved only inside `programs`; missing methods, arity drift,
/// recursive overflow, and malformed values all fail closed.
#[must_use]
pub fn evaluate_scalar_program_bundle(
    programs: &BTreeMap<String, ScalarDecisionProgram>,
    entry: &str,
    variables: BTreeMap<String, Value>,
) -> Option<Value> {
    evaluate_scalar_program(programs.get(entry)?, variables, Some(programs), 0)
}

fn evaluate_scalar_program(
    program: &ScalarDecisionProgram,
    mut variables: BTreeMap<String, Value>,
    programs: Option<&BTreeMap<String, ScalarDecisionProgram>>,
    depth: usize,
) -> Option<Value> {
    if depth > 64
        || !matches!(program.schema_version.as_str(), "1.0.0" | "1.1.0" | "1.2.0")
        || program.opcode != "scalar-decision-program-v1"
    {
        return None;
    }
    if program
        .parameters
        .iter()
        .any(|parameter| !variables.contains_key(parameter))
    {
        return None;
    }
    let ScalarControl::Return(value) = evaluate_scalar_statements(
        &program.statements,
        &mut variables,
        program,
        programs,
        depth,
    )?
    else {
        return None;
    };
    Some(value)
}

fn evaluate_scalar_statements(
    statements: &[Value],
    variables: &mut BTreeMap<String, Value>,
    program: &ScalarDecisionProgram,
    programs: Option<&BTreeMap<String, ScalarDecisionProgram>>,
    depth: usize,
) -> Option<ScalarControl> {
    if depth > 256 {
        return None;
    }
    for statement in statements {
        let fields = statement.as_array()?;
        match fields.first()?.as_str()? {
            "set" if fields.len() == 3 => {
                let value = evaluate_scalar_expression(
                    value_index(fields.get(2)?)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?;
                variables.insert(fields.get(1)?.as_str()?.to_owned(), value);
            }
            "ephemeral-set" if fields.len() == 3 => {
                let value = evaluate_scalar_expression(
                    value_index(fields.get(2)?)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?;
                variables.insert(format!("$ephemeral.{}", fields.get(1)?.as_str()?), value);
            }
            "unpack" if fields.len() == 3 => {
                let names = fields.get(1)?.as_array()?;
                let values = evaluate_scalar_expression(
                    value_index(fields.get(2)?)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?;
                let values = values.as_array()?;
                if names.len() != values.len() {
                    return None;
                }
                for (name, value) in names.iter().zip(values) {
                    variables.insert(name.as_str()?.to_owned(), value.clone());
                }
            }
            "if" if fields.len() == 4 => {
                let condition = evaluate_scalar_expression(
                    value_index(fields.get(1)?)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?;
                let branch = if scalar_truthy(&condition) {
                    fields.get(2)?
                } else {
                    fields.get(3)?
                };
                if let control @ ScalarControl::Return(_) = evaluate_scalar_statements(
                    branch.as_array()?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )? {
                    return Some(control);
                }
            }
            "if-chain" if fields.len() == 3 => {
                if let control @ ScalarControl::Return(_) =
                    evaluate_scalar_if_chain(fields, variables, program, programs, depth)?
                {
                    return Some(control);
                }
            }
            "return" if fields.len() == 2 => {
                return Some(ScalarControl::Return(evaluate_scalar_expression(
                    value_index(fields.get(1)?)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?));
            }
            "pass" if fields.len() == 1 => {}
            _ => return None,
        }
    }
    Some(ScalarControl::Continue)
}

fn evaluate_scalar_if_chain(
    fields: &[Value],
    variables: &mut BTreeMap<String, Value>,
    program: &ScalarDecisionProgram,
    programs: Option<&BTreeMap<String, ScalarDecisionProgram>>,
    depth: usize,
) -> Option<ScalarControl> {
    let mut selected = None;
    for branch in fields.get(1)?.as_array()? {
        let branch = branch.as_array()?;
        if branch.len() != 2 {
            return None;
        }
        let condition = evaluate_scalar_expression(
            value_index(branch.first()?)?,
            variables,
            program,
            programs,
            depth + 1,
        )?;
        if scalar_truthy(&condition) {
            selected = Some(branch.get(1)?);
            break;
        }
    }
    let branch = selected.unwrap_or(fields.get(2)?);
    evaluate_scalar_statements(branch.as_array()?, variables, program, programs, depth + 1)
}

#[allow(clippy::too_many_lines)]
fn evaluate_scalar_expression(
    index: usize,
    variables: &mut BTreeMap<String, Value>,
    program: &ScalarDecisionProgram,
    programs: Option<&BTreeMap<String, ScalarDecisionProgram>>,
    depth: usize,
) -> Option<Value> {
    if depth > 256 {
        return None;
    }
    let fields = program.expressions.get(index)?.as_array()?;
    let opcode = fields.first()?.as_str()?;
    match opcode {
        "literal" if fields.len() == 2 => fields.get(1).cloned(),
        "variable" if fields.len() == 2 => variables.get(fields.get(1)?.as_str()?).cloned(),
        "attribute" if fields.len() == 3 => {
            let value = scalar_operand(fields, 1, variables, program, programs, depth)?;
            value.as_object()?.get(fields.get(2)?.as_str()?).cloned()
        }
        "index" if fields.len() == 3 => {
            let value = scalar_operand(fields, 1, variables, program, programs, depth)?;
            let index = scalar_operand(fields, 2, variables, program, programs, depth)?;
            scalar_index(&value, &index)
        }
        "not" if fields.len() == 2 => Some(Value::Bool(!scalar_truthy(&scalar_operand(
            fields, 1, variables, program, programs, depth,
        )?))),
        "negative" | "positive" if fields.len() == 2 => {
            let value = scalar_number(&scalar_operand(
                fields, 1, variables, program, programs, depth,
            )?)?;
            scalar_number_value(if opcode == "negative" { -value } else { value })
        }
        "add" | "subtract" | "multiply" | "divide" | "floor-divide" | "modulo" | "power"
            if fields.len() == 3 =>
        {
            let left = scalar_operand(fields, 1, variables, program, programs, depth)?;
            let right = scalar_operand(fields, 2, variables, program, programs, depth)?;
            scalar_binary(opcode, &left, &right)
        }
        "and" | "or" if fields.len() == 2 => {
            let operands = fields.get(1)?.as_array()?;
            let mut last = Value::Bool(opcode == "and");
            for operand in operands {
                last = evaluate_scalar_expression(
                    value_index(operand)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?;
                if (opcode == "and" && !scalar_truthy(&last))
                    || (opcode == "or" && scalar_truthy(&last))
                {
                    break;
                }
            }
            Some(last)
        }
        "compare" if fields.len() == 3 => {
            let mut left = scalar_operand(fields, 1, variables, program, programs, depth)?;
            for comparison in fields.get(2)?.as_array()? {
                let comparison = comparison.as_array()?;
                if comparison.len() != 2 {
                    return None;
                }
                let right = evaluate_scalar_expression(
                    value_index(comparison.get(1)?)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?;
                if !scalar_compare(comparison.first()?.as_str()?, &left, &right)? {
                    return Some(Value::Bool(false));
                }
                left = right;
            }
            Some(Value::Bool(true))
        }
        "if-expression" if fields.len() == 4 => {
            let condition = scalar_operand(fields, 1, variables, program, programs, depth)?;
            scalar_operand(
                fields,
                if scalar_truthy(&condition) { 2 } else { 3 },
                variables,
                program,
                programs,
                depth,
            )
        }
        "tuple" | "list" | "set-literal" if fields.len() == 2 => Some(Value::Array(
            fields
                .get(1)?
                .as_array()?
                .iter()
                .map(|item| {
                    evaluate_scalar_expression(
                        value_index(item)?,
                        variables,
                        program,
                        programs,
                        depth + 1,
                    )
                })
                .collect::<Option<Vec<_>>>()?,
        )),
        "dict" if fields.len() == 2 => {
            let mut result = serde_json::Map::new();
            for item in fields.get(1)?.as_array()? {
                let item = item.as_array()?;
                if item.len() != 2 {
                    return None;
                }
                let key = evaluate_scalar_expression(
                    value_index(item.first()?)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?;
                let value = evaluate_scalar_expression(
                    value_index(item.get(1)?)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?;
                result.insert(scalar_string(&key), value);
            }
            Some(Value::Object(result))
        }
        "format" if fields.len() == 2 => {
            let mut result = String::new();
            for part in fields.get(1)?.as_array()? {
                let part = part.as_array()?;
                match part.first()?.as_str()? {
                    "text" if part.len() == 2 => result.push_str(part.get(1)?.as_str()?),
                    "value" if part.len() == 2 => {
                        let value = evaluate_scalar_expression(
                            value_index(part.get(1)?)?,
                            variables,
                            program,
                            programs,
                            depth + 1,
                        )?;
                        result.push_str(&scalar_string(&value));
                    }
                    _ => return None,
                }
            }
            Some(Value::String(result))
        }
        "call-program" if fields.len() == 3 => {
            let programs = programs?;
            let callee = programs.get(fields.get(1)?.as_str()?)?;
            let arguments = fields.get(2)?.as_array()?;
            if arguments.len() != callee.parameters.len() {
                return None;
            }
            let mut callee_variables = BTreeMap::new();
            for (parameter, argument) in callee.parameters.iter().zip(arguments) {
                let value = evaluate_scalar_expression(
                    value_index(argument)?,
                    variables,
                    program,
                    Some(programs),
                    depth + 1,
                )?;
                callee_variables.insert(parameter.clone(), value);
            }
            evaluate_scalar_program(callee, callee_variables, Some(programs), depth + 1)
        }
        "is-instance" if fields.len() == 3 => {
            let value = scalar_operand(fields, 1, variables, program, programs, depth)?;
            let matches = match fields.get(2)?.as_str()? {
                "bool" => value.is_boolean(),
                "float" | "np.float64" => scalar_number(&value).is_some(),
                "int" => value
                    .as_i64()
                    .or_else(|| value.as_u64().and_then(|item| i64::try_from(item).ok()))
                    .is_some(),
                "str" => value.is_string(),
                _ => return None,
            };
            Some(Value::Bool(matches))
        }
        "length" if fields.len() == 2 => {
            let value = scalar_operand(fields, 1, variables, program, programs, depth)?;
            let length = match value {
                Value::Array(values) => values.len(),
                Value::Object(values) => values.len(),
                Value::String(value) => value.chars().count(),
                _ => return None,
            };
            Some(Value::Number(u64::try_from(length).ok()?.into()))
        }
        _ => None,
    }
}

fn scalar_operand(
    fields: &[Value],
    position: usize,
    variables: &mut BTreeMap<String, Value>,
    program: &ScalarDecisionProgram,
    programs: Option<&BTreeMap<String, ScalarDecisionProgram>>,
    depth: usize,
) -> Option<Value> {
    evaluate_scalar_expression(
        value_index(fields.get(position)?)?,
        variables,
        program,
        programs,
        depth + 1,
    )
}

fn value_index(value: &Value) -> Option<usize> {
    usize::try_from(value.as_u64()?).ok()
}

fn scalar_number(value: &Value) -> Option<f64> {
    if let Some(value) = value.as_f64() {
        return Some(value);
    }
    let marker = value.as_object()?.get("$float")?.as_str()?;
    match marker {
        "nan" => Some(f64::NAN),
        "inf" | "infinity" => Some(f64::INFINITY),
        "-inf" | "-infinity" => Some(f64::NEG_INFINITY),
        _ => None,
    }
}

fn scalar_number_value(value: f64) -> Option<Value> {
    if value.is_finite() {
        return number_value(value);
    }
    let marker = if value.is_nan() {
        "nan"
    } else if value.is_sign_positive() {
        "inf"
    } else {
        "-inf"
    };
    Some(serde_json::json!({"$float": marker}))
}

fn scalar_binary(opcode: &str, left: &Value, right: &Value) -> Option<Value> {
    if opcode == "add" {
        if let (Some(left), Some(right)) = (left.as_str(), right.as_str()) {
            return Some(Value::String(format!("{left}{right}")));
        }
        if let (Some(left), Some(right)) = (left.as_array(), right.as_array()) {
            return Some(Value::Array(
                left.iter().chain(right).cloned().collect::<Vec<_>>(),
            ));
        }
    }
    let left = scalar_number(left)?;
    let right = scalar_number(right)?;
    let result = match opcode {
        "add" => left + right,
        "subtract" => left - right,
        "multiply" => left * right,
        "divide" => left / right,
        "floor-divide" => (left / right).floor(),
        "modulo" => left - (left / right).floor() * right,
        "power" => left.powf(right),
        _ => return None,
    };
    scalar_number_value(result)
}

fn scalar_compare(opcode: &str, left: &Value, right: &Value) -> Option<bool> {
    match opcode {
        "equal" | "is" => Some(scalar_equal(left, right)),
        "not-equal" | "is-not" => Some(!scalar_equal(left, right)),
        "less" | "less-equal" | "greater" | "greater-equal" => {
            if let (Some(left), Some(right)) = (scalar_number(left), scalar_number(right)) {
                return Some(match opcode {
                    "less" => left < right,
                    "less-equal" => left <= right,
                    "greater" => left > right,
                    "greater-equal" => left >= right,
                    _ => unreachable!(),
                });
            }
            let (left, right) = (left.as_str()?, right.as_str()?);
            Some(match opcode {
                "less" => left < right,
                "less-equal" => left <= right,
                "greater" => left > right,
                "greater-equal" => left >= right,
                _ => unreachable!(),
            })
        }
        "in" | "not-in" => {
            let included = match right {
                Value::Array(values) => values.iter().any(|value| scalar_equal(left, value)),
                Value::Object(values) => left.as_str().is_some_and(|key| values.contains_key(key)),
                Value::String(value) => left.as_str().is_some_and(|item| value.contains(item)),
                _ => return None,
            };
            Some(if opcode == "in" { included } else { !included })
        }
        _ => None,
    }
}

#[allow(clippy::float_cmp)]
fn scalar_equal(left: &Value, right: &Value) -> bool {
    match (scalar_number(left), scalar_number(right)) {
        (Some(left), Some(right)) => left == right,
        (Some(left), None) if right.is_boolean() => {
            left == f64::from(u8::from(right.as_bool().unwrap_or(false)))
        }
        (None, Some(right)) if left.is_boolean() => {
            f64::from(u8::from(left.as_bool().unwrap_or(false))) == right
        }
        _ => left == right,
    }
}

fn scalar_index(value: &Value, index: &Value) -> Option<Value> {
    match value {
        Value::Object(values) => values.get(index.as_str()?).cloned(),
        Value::Array(values) => {
            let raw = integer_value(index)?;
            let normalized = if raw < 0 {
                i64::try_from(values.len()).ok()?.checked_add(raw)?
            } else {
                raw
            };
            values.get(usize::try_from(normalized).ok()?).cloned()
        }
        Value::String(value) => {
            let characters = value.chars().collect::<Vec<_>>();
            let raw = integer_value(index)?;
            let normalized = if raw < 0 {
                i64::try_from(characters.len()).ok()?.checked_add(raw)?
            } else {
                raw
            };
            Some(Value::String(
                characters
                    .get(usize::try_from(normalized).ok()?)?
                    .to_string(),
            ))
        }
        _ => None,
    }
}

fn scalar_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().is_some_and(|value| value != 0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => scalar_number(&Value::Object(value.clone()))
            .map_or(!value.is_empty(), |number| number != 0.0),
    }
}

fn scalar_string(value: &Value) -> String {
    match value {
        Value::Null => "None".to_owned(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        Value::String(value) => value.clone(),
        Value::Number(value) => value.to_string(),
        Value::Object(_) if scalar_number(value).is_some() => {
            let number = scalar_number(value).unwrap_or(f64::NAN);
            if number.is_nan() {
                "nan".to_owned()
            } else if number.is_sign_positive() {
                "inf".to_owned()
            } else {
                "-inf".to_owned()
            }
        }
        Value::Array(_) | Value::Object(_) => value.to_string(),
    }
}

fn entry_sizing(
    requested: f64,
    rate: f64,
    fee_rate: f64,
    amount_step: f64,
    leverage: f64,
) -> Option<(f64, f64, f64, f64)> {
    // Freqtrade treats the callback's stake as collateral/notional and derives
    // amount before accounting for fees (`stake / rate * leverage`). Fees
    // affect profit and wallet proceeds, but do not shrink the requested base
    // amount at entry.
    let raw_amount = requested * leverage / rate;
    let amount = floor_step(raw_amount, amount_step);
    if amount <= 0.0 {
        return None;
    }
    let notional = precise_product(&[amount, rate])?;
    let stake = if (leverage - 1.0).abs() < f64::EPSILON {
        notional
    } else {
        precise_quotient(notional, leverage)?
    };
    let precise_cost = if (leverage - 1.0).abs() < f64::EPSILON {
        precise_product(&[amount, rate, 1.0 + fee_rate])?
    } else {
        let entry_fee = precise_product(&[notional, fee_rate])?;
        precise_sum(&[stake, entry_fee])?
    };
    let order_cost = (amount * rate) * (1.0 + fee_rate);
    Some((amount, stake, precise_cost, order_cost))
}

fn fee_open(config: &PortfolioConfig) -> f64 {
    config.fee_open_rate.unwrap_or(config.fee_rate)
}

fn fee_close(config: &PortfolioConfig) -> f64 {
    config.fee_close_rate.unwrap_or(config.fee_rate)
}

const fn entry_order_side(side: TradeSide) -> OrderSide {
    match side {
        TradeSide::Long => OrderSide::Buy,
        TradeSide::Short => OrderSide::Sell,
    }
}

const fn exit_order_side(side: TradeSide) -> OrderSide {
    match side {
        TradeSide::Long => OrderSide::Sell,
        TradeSide::Short => OrderSide::Buy,
    }
}

fn floor_step(value: f64, step: f64) -> f64 {
    exact_step_quantize(value, step, StepQuantize::Floor).unwrap_or_else(|| {
        let units = (value / step).floor();
        units * step
    })
}

fn ceil_step(value: f64, step: f64) -> f64 {
    exact_step_quantize(value, step, StepQuantize::Ceil)
        .unwrap_or_else(|| (value / step).ceil() * step)
}

fn round_step(value: f64, step: f64) -> f64 {
    exact_step_quantize(value, step, StepQuantize::Round)
        .unwrap_or_else(|| (value / step).round() * step)
}

#[derive(Clone, Copy)]
enum StepQuantize {
    Floor,
    Ceil,
    Round,
}

/// Apply exchange tick precision without dividing binary floats.
///
/// Values such as `8.45 / 0.01` can become `844.999...` in f64 and lose a
/// full market step. CCXT precision works on decimal text, so the simulator
/// must choose the integer number of ticks in that same domain.
fn exact_step_quantize(value: f64, step: f64, mode: StepQuantize) -> Option<f64> {
    let value = exact_rational(value)?;
    let step = exact_rational(step)?;
    if value < BigRational::zero() || step <= BigRational::zero() {
        return None;
    }
    let quotient = &value / &step;
    let floor = quotient.to_integer();
    let remainder = quotient - BigRational::from_integer(floor.clone());
    let units = match mode {
        StepQuantize::Floor => floor,
        StepQuantize::Ceil => {
            if remainder.is_zero() {
                floor
            } else {
                floor + BigInt::from(1_u8)
            }
        }
        StepQuantize::Round => {
            if remainder * BigRational::from_integer(BigInt::from(2_u8)) >= BigRational::one() {
                floor + BigInt::from(1_u8)
            } else {
                floor
            }
        }
    };
    (step * BigRational::from_integer(units)).to_f64()
}

fn precise_product(values: &[f64]) -> Option<f64> {
    values
        .iter()
        .try_fold(BigRational::one(), |product, value| {
            exact_rational(*value).map(|number| product * number)
        })?
        .to_f64()
}

fn precise_sum(values: &[f64]) -> Option<f64> {
    values
        .iter()
        .try_fold(BigRational::zero(), |sum, value| {
            exact_rational(*value).map(|number| sum + number)
        })?
        .to_f64()
}

fn precise_quotient(numerator: f64, denominator: f64) -> Option<f64> {
    let denominator = exact_rational(denominator)?;
    if denominator.is_zero() {
        return None;
    }
    (exact_rational(numerator)? / denominator).to_f64()
}

fn precise_product_quotient(left: f64, right: f64, denominator: f64) -> Option<f64> {
    let denominator = exact_rational(denominator)?;
    if denominator.is_zero() {
        return None;
    }
    ft_precise_division(
        &(exact_rational(left)? * exact_rational(right)?),
        &denominator,
    )?
    .to_f64()
}

/// Reproduce CCXT `Precise.div(..., precision=18)`.
///
/// `FtPrecise` truncates every division toward zero to eighteen decimal
/// places. Keeping divisions as unlimited rationals looks more accurate, but
/// diverges from Freqtrade after a long sequence of weighted-basis updates.
fn ft_precise_division(numerator: &BigRational, denominator: &BigRational) -> Option<BigRational> {
    if denominator.is_zero() {
        return None;
    }
    let value = numerator / denominator;
    let scale = BigInt::from(10_u8).pow(18);
    let scaled_integer = (value.numer() * &scale) / value.denom();
    Some(BigRational::new(scaled_integer, scale))
}

/// Convert the shortest round-trippable float text into an exact rational.
///
/// CCXT's `Precise`, and therefore Freqtrade's `FtPrecise`, performs decimal
/// string arithmetic. `rust_decimal` is used only to parse one f64 string
/// (at most 17 significant digits); multiplication and addition stay exact,
/// while `ft_precise_division` applies CCXT's explicit division boundary.
fn exact_rational(value: f64) -> Option<BigRational> {
    if !value.is_finite() {
        return None;
    }
    let encoded = value.to_string();
    let decimal = Decimal::from_str(&encoded)
        .or_else(|_| Decimal::from_scientific(&encoded))
        .ok()?;
    let numerator = BigInt::from(decimal.mantissa());
    let denominator = BigInt::from(10_u8).pow(decimal.scale());
    Some(BigRational::new(numerator, denominator))
}

fn update_extrema(trade: &mut OpenTrade, candle: &Candle) {
    trade.minimum_rate = trade.minimum_rate.min(candle.low);
    trade.maximum_rate = trade.maximum_rate.max(candle.high);
}

fn apply_adjustment(
    trade: &mut OpenTrade,
    candle: &Candle,
    adjustment: &AdjustmentSignal,
    config: &PortfolioConfig,
    available_balance: f64,
    order_id: u64,
) -> Result<(), SimError> {
    if !adjustment.stake_amount.is_finite() || adjustment.stake_amount == 0.0 {
        return Err(SimError::InvalidAdjustment {
            pair: trade.pair.clone(),
            timestamp_ms: candle.timestamp_ms,
        });
    }
    if adjustment.stake_amount < 0.0 {
        return apply_partial_exit(trade, candle, adjustment, config, order_id);
    }
    let requested = adjustment.stake_amount.min(available_balance);
    let Some((amount, _, _, order_cost)) = entry_sizing(
        requested,
        candle.open,
        fee_open(config),
        trade.amount_step,
        trade.leverage,
    ) else {
        return Ok(());
    };
    let funding_fee = take_running_funding(trade);
    trade.orders.push(FilledOrder {
        id: order_id,
        funding_fee,
        sequence: trade.orders.len(),
        side: entry_order_side(trade.side),
        is_entry: true,
        filled_timestamp_ms: candle.timestamp_ms,
        amount,
        price: candle.open,
        cost: order_cost,
        tag: Some(adjustment.tag.clone()),
    });
    recalculate_order_funding_total(trade);
    // Freqtrade does not update these fields incrementally. Its
    // `LocalTrade.recalc_trade_from_orders()` replays every filled order after
    // each adjustment. Replaying here preserves weighted-basis exits and the
    // all-time entry stake even after a cluster has been sold.
    recalculate_open_trade_from_orders(trade, config).ok_or_else(|| {
        SimError::InvalidAdjustment {
            pair: trade.pair.clone(),
            timestamp_ms: candle.timestamp_ms,
        }
    })?;
    trade.adjustment_count += 1;
    apply_order_filled(trade, Some(&adjustment.tag), config);
    Ok(())
}

fn apply_partial_exit(
    trade: &mut OpenTrade,
    candle: &Candle,
    adjustment: &AdjustmentSignal,
    config: &PortfolioConfig,
    order_id: u64,
) -> Result<(), SimError> {
    let requested_stake = -adjustment.stake_amount;
    if requested_stake >= trade.stake_amount {
        return Err(SimError::InvalidAdjustment {
            pair: trade.pair.clone(),
            timestamp_ms: candle.timestamp_ms,
        });
    }
    // Freqtrade performs this multiplication with `FtPrecise` before amount
    // precision is applied. A mathematically exact 0.46 can therefore become
    // 0.459999... and correctly truncate to 0.45 on a 0.01 market step.
    let raw_amount = precise_product_quotient(requested_stake, trade.amount, trade.stake_amount)
        .ok_or_else(|| SimError::InvalidAdjustment {
            pair: trade.pair.clone(),
            timestamp_ms: candle.timestamp_ms,
        })?;
    let amount = floor_step(raw_amount, trade.amount_step);
    if amount <= 0.0 || amount >= trade.amount {
        return Err(SimError::InvalidAdjustment {
            pair: trade.pair.clone(),
            timestamp_ms: candle.timestamp_ms,
        });
    }
    let funding_fee = take_running_funding(trade);
    trade.orders.push(FilledOrder {
        id: order_id,
        funding_fee,
        sequence: trade.orders.len(),
        side: exit_order_side(trade.side),
        is_entry: false,
        filled_timestamp_ms: candle.timestamp_ms,
        amount,
        price: candle.open,
        cost: amount * candle.open * (1.0 + fee_close(config)),
        tag: Some(adjustment.tag.clone()),
    });
    recalculate_order_funding_total(trade);
    recalculate_open_trade_from_orders(trade, config).ok_or_else(|| {
        SimError::InvalidAdjustment {
            pair: trade.pair.clone(),
            timestamp_ms: candle.timestamp_ms,
        }
    })?;
    trade.realized_partial_profit = if is_unleveraged_spot(trade, config) {
        replay_spot_profit(trade, config)
            .map(|replay| replay.profit_abs)
            .ok_or_else(|| SimError::InvalidAdjustment {
                pair: trade.pair.clone(),
                timestamp_ms: candle.timestamp_ms,
            })?
    } else {
        replay_leveraged_profit(trade, config).ok_or_else(|| SimError::InvalidAdjustment {
            pair: trade.pair.clone(),
            timestamp_ms: candle.timestamp_ms,
        })?
    };
    trade.adjustment_count += 1;
    apply_order_filled(trade, Some(&adjustment.tag), config);
    Ok(())
}

/// Rebuild Freqtrade's order-derived open-position fields.
///
/// Exit orders remove stake at the weighted entry price, not at their fill
/// price. `max_stake_amount` is the sum of every successful entry and never
/// shrinks after partial exits. Decimal replay also prevents accumulated
/// binary-float drift across the hundreds of X7 grind orders.
fn recalculate_open_trade_from_orders(
    trade: &mut OpenTrade,
    config: &PortfolioConfig,
) -> Option<()> {
    let mut current_amount = BigRational::zero();
    let mut current_stake = BigRational::zero();
    let mut maximum_stake = BigRational::zero();
    let mut average_price = BigRational::zero();

    for order in &trade.orders {
        let amount = exact_rational(order.amount)?;
        let price = exact_rational(order.price)?;
        if amount <= BigRational::zero() || price <= BigRational::zero() {
            return None;
        }
        if order.is_entry {
            current_amount += &amount;
            current_stake += &price * &amount;
            maximum_stake += &price * &amount;
            average_price = ft_precise_division(&current_stake, &current_amount)?;
        } else {
            current_amount -= &amount;
            current_stake -= &average_price * &amount;
        }
    }
    if current_amount <= BigRational::zero() || current_stake <= BigRational::zero() {
        return None;
    }

    let raw_amount = current_amount.to_f64()?;
    let raw_stake = current_stake.to_f64()?;
    trade.amount = floor_step(raw_amount, trade.amount_step);
    trade.stake_amount = raw_stake / trade.leverage;
    trade.max_stake_amount = maximum_stake.to_f64()? / trade.leverage;
    trade.open_rate = round_step(
        (&current_stake / &current_amount).to_f64()?,
        trade.price_step,
    );
    let leveraged_stoploss = config.stoploss_ratio / trade.leverage;
    let adjusted_stop = match trade.side {
        TradeSide::Long => ceil_step(
            trade.open_rate * (1.0 + leveraged_stoploss),
            trade.price_step,
        ),
        TradeSide::Short => floor_step(
            trade.open_rate * (1.0 - leveraged_stoploss),
            trade.price_step,
        ),
    };
    trade.stop_loss = match trade.side {
        // `adjust_stop_loss()` is monotonic: position adjustment may protect
        // more profit, but it must never loosen an already established stop.
        TradeSide::Long => trade.stop_loss.max(adjusted_stop),
        TradeSide::Short => trade.stop_loss.min(adjusted_stop),
    };

    let notional = precise_product(&[trade.amount, trade.open_rate])?;
    trade.entry_cost_with_fees = if (trade.leverage - 1.0).abs() < f64::EPSILON {
        precise_product(&[trade.amount, trade.open_rate, 1.0 + fee_open(config)])?
    } else {
        let entry_fee = precise_product(&[notional, fee_open(config)])?;
        precise_sum(&[trade.stake_amount, entry_fee])?
    };
    Some(())
}

struct ProfitReplay {
    profit_abs: f64,
    total_entry_value: f64,
}

fn is_unleveraged_spot(trade: &OpenTrade, config: &PortfolioConfig) -> bool {
    !config.is_futures && (trade.leverage - 1.0).abs() < f64::EPSILON
}

/// Replay Freqtrade's spot `recalc_trade_from_orders()` profit path.
///
/// Each partial exit is valued against the weighted entry price at that point
/// and rounded to eight decimals before it is added to cumulative profit.
/// The denominator includes entry fees for every buy, matching
/// `LocalTrade.close_profit` rather than the fee-free `max_stake_amount`.
fn replay_spot_profit(trade: &OpenTrade, config: &PortfolioConfig) -> Option<ProfitReplay> {
    let mut current_amount = BigRational::zero();
    let mut current_stake = BigRational::zero();
    let mut average_price = BigRational::zero();
    let mut total_entry_value = 0.0;
    let mut profit_abs = 0.0;

    for order in &trade.orders {
        let amount = exact_rational(order.amount)?;
        let price = exact_rational(order.price)?;
        if amount <= BigRational::zero() || price <= BigRational::zero() {
            return None;
        }
        if order.is_entry {
            current_amount += &amount;
            current_stake += &amount * &price;
            average_price = ft_precise_division(&current_stake, &current_amount)?;
            total_entry_value +=
                precise_product(&[order.amount, order.price, 1.0 + fee_open(config)])?;
            continue;
        }

        if amount > current_amount {
            return None;
        }
        let open_value = precise_product(&[
            order.amount,
            average_price.to_f64()?,
            1.0 + fee_open(config),
        ])?;
        let close_value = precise_product(&[order.amount, order.price, 1.0 - fee_close(config)])?;
        let exit_profit = if trade.side == TradeSide::Long {
            close_value - open_value
        } else {
            open_value - close_value
        };
        profit_abs += round_eight(exit_profit);
        current_amount -= &amount;
        current_stake -= &average_price * &amount;
    }

    Some(ProfitReplay {
        profit_abs,
        total_entry_value,
    })
}

/// Replay Freqtrade's leveraged/futures realized-profit calculation.
///
/// Freqtrade stores the full running funding amount on the next filled order.
/// During order replay it accumulates those values until an exit, includes the
/// accumulated funding in that exit's profit, rounds the exit profit to eight
/// decimals, and then resets the funding accumulator. This differs materially
/// from prorating funding by the partial-exit amount.
fn replay_leveraged_profit(trade: &OpenTrade, config: &PortfolioConfig) -> Option<f64> {
    let mut current_amount = BigRational::zero();
    let mut current_stake = BigRational::zero();
    let mut average_price = BigRational::zero();
    let mut current_funding = 0.0;
    let mut profit_abs = 0.0;

    for order in &trade.orders {
        current_funding += order.funding_fee;
        let amount = exact_rational(order.amount)?;
        let price = exact_rational(order.price)?;
        if amount <= BigRational::zero() || price <= BigRational::zero() {
            return None;
        }
        if order.is_entry {
            current_amount += &amount;
            current_stake += &amount * &price;
            average_price = ft_precise_division(&current_stake, &current_amount)?;
            continue;
        }
        if amount > current_amount {
            return None;
        }

        let average = average_price.to_f64()?;
        let open_multiplier = if trade.side == TradeSide::Short {
            1.0 - fee_open(config)
        } else {
            1.0 + fee_open(config)
        };
        let close_multiplier = if trade.side == TradeSide::Short {
            1.0 + fee_close(config)
        } else {
            1.0 - fee_close(config)
        };
        let open_value = precise_product(&[order.amount, average, open_multiplier])?;
        let close_value = precise_product(&[order.amount, order.price, close_multiplier])?;
        let exit_profit = if trade.side == TradeSide::Short {
            open_value - close_value + current_funding
        } else {
            close_value - open_value + current_funding
        };
        profit_abs += round_eight(exit_profit);
        current_funding = 0.0;
        current_amount -= &amount;
        current_stake -= &average_price * &amount;
    }
    Some(profit_abs)
}

fn freqtrade_total_entry_value(trade: &OpenTrade, config: &PortfolioConfig) -> Option<f64> {
    let open_multiplier = if trade.side == TradeSide::Short {
        1.0 - fee_open(config)
    } else {
        1.0 + fee_open(config)
    };
    trade
        .orders
        .iter()
        .filter(|order| order.is_entry)
        .try_fold(0.0, |total, order| {
            precise_product(&[order.amount, order.price, open_multiplier])
                .map(|entry_value| total + entry_value)
        })
}

fn rule_adjustment(
    trade: &OpenTrade,
    candle: &Candle,
    config: &PortfolioConfig,
) -> Option<AdjustmentSignal> {
    let rule = config.adjustment_rule.as_ref()?;
    if trade.adjustment_count >= rule.max_adjustments {
        return None;
    }
    let current_profit = current_profit_ratio(trade, candle.open, fee_close(config));
    (current_profit < rule.profit_below).then(|| AdjustmentSignal {
        stake_amount: trade.first_entry_cost_with_fees * rule.stake_ratio,
        tag: rule.tag.clone(),
    })
}

struct ExitDecision {
    rate: f64,
    reason: String,
    requires_confirmation: bool,
}

fn exit_decision(
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    profit_targets: &mut BTreeMap<String, ProfitTarget>,
) -> Result<Option<ExitDecision>, SimError> {
    // This order mirrors Freqtrade 2026.5.1 `IStrategy.should_exit`.
    // Strategy exits precede liquidation and stop-loss candidates, so a
    // same-candle collision keeps the strategy reason and candle-open rate.
    let signal = match trade.side {
        TradeSide::Long => &candle.exit_long,
        TradeSide::Short => &candle.exit_short,
    };
    if let Some(signal) = signal {
        return Ok(Some(ExitDecision {
            rate: candle.open,
            reason: signal.reason.clone(),
            requires_confirmation: true,
        }));
    }
    if let Some(manager) = &config.nfi_x7_trade_manager {
        let feature_index =
            callback_feature_index(candle_index).ok_or(SimError::InvalidNfiTradeManager)?;
        let decision = evaluate_nfi_exit(
            manager,
            trade,
            pair,
            feature_index,
            candle,
            config,
            profit_targets,
        )
        .ok_or(SimError::InvalidNfiTradeManager)?;
        if let CustomExitDecision::Exit(reason) = decision {
            return Ok(Some(ExitDecision {
                rate: candle.open,
                reason,
                requires_confirmation: true,
            }));
        }
    }
    if let Some(bundle) = &config.custom_exit_program {
        let feature_index =
            callback_feature_index(candle_index).ok_or_else(|| SimError::InvalidCustomExit {
                pair: trade.pair.clone(),
                timestamp_ms: candle.timestamp_ms,
            })?;
        let decision =
            evaluate_custom_exit_bundle(bundle, trade, pair, feature_index, candle, config)
                .ok_or_else(|| SimError::InvalidCustomExit {
                    pair: trade.pair.clone(),
                    timestamp_ms: candle.timestamp_ms,
                })?;
        if let CustomExitDecision::Exit(reason) = decision {
            return Ok(Some(ExitDecision {
                rate: candle.open,
                reason,
                requires_confirmation: true,
            }));
        }
    }
    if config
        .custom_exit_after_ms
        .is_some_and(|duration| candle.timestamp_ms - trade.open_timestamp_ms >= duration)
    {
        return Ok(Some(ExitDecision {
            rate: candle.open,
            reason: "contract_timed_exit".to_owned(),
            requires_confirmation: true,
        }));
    }
    if let Some(liquidation_price) = trade.liquidation_price {
        let liquidated = match trade.side {
            TradeSide::Long => candle.low <= liquidation_price,
            TradeSide::Short => candle.high >= liquidation_price,
        };
        if liquidated {
            return Ok(Some(ExitDecision {
                rate: liquidation_price,
                reason: "liquidation".to_owned(),
                requires_confirmation: false,
            }));
        }
    }
    let stopped = match trade.side {
        TradeSide::Long => candle.low <= trade.stop_loss,
        TradeSide::Short => candle.high >= trade.stop_loss,
    };
    if stopped {
        return Ok(Some(ExitDecision {
            rate: trade.stop_loss,
            reason: "stop_loss".to_owned(),
            requires_confirmation: true,
        }));
    }
    Ok(None)
}

enum CustomExitDecision {
    NoExit,
    Exit(String),
}

/// Route NFI custom exits in the exact order used by the strategy.
///
/// A route that does not exit may still update the pair-level target cache.
/// Therefore this loop must continue through later matching routes instead of
/// selecting one route up front. That distinction is observable for mixed NFI
/// entry tags and is why ``route_order`` is part of the sealed input.
#[allow(clippy::too_many_arguments)]
fn evaluate_nfi_exit(
    manager: &NfiX7TradeManager,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    profit_targets: &mut BTreeMap<String, ProfitTarget>,
) -> Option<CustomExitDecision> {
    if trade.side == TradeSide::Short {
        return evaluate_nfi_short_exit(
            manager,
            trade,
            pair,
            candle_index,
            candle,
            config,
            profit_targets,
        );
    }
    let words = trade
        .entry_tag
        .as_deref()
        .unwrap_or("")
        .split_whitespace()
        .collect::<Vec<_>>();
    let mut matched = false;
    for key in &manager.route_order {
        if let Some(route) = manager
            .managed_long_routes
            .iter()
            .find(|route| &route.key == key)
        {
            if !nfi_managed_route_supports_tags(manager, route, &words) {
                continue;
            }
            matched = true;
            match evaluate_nfi_managed_long_exit(
                manager,
                route,
                nfi_profile_program_order(route.profile),
                trade,
                pair,
                candle_index,
                candle,
                config,
                profit_targets,
            )? {
                CustomExitDecision::Exit(reason) => {
                    return Some(CustomExitDecision::Exit(reason));
                }
                CustomExitDecision::NoExit => continue,
            }
        }

        let legacy = match key.as_str() {
            "long_grind" => manager.long_grind.as_ref(),
            "long_btc" => manager.long_btc.as_ref(),
            _ => None,
        };
        if let Some(route) = legacy.filter(|route| nfi_long_grind_supports_trade(route, trade)) {
            matched = true;
            let snapshot = nfi_profit_snapshot(
                trade,
                candle.open,
                fee_open(config),
                fee_close(config),
                config.is_futures,
            )?;
            if snapshot.initial_stake_ratio > route.exit_profit_threshold {
                let entry_tag = trade.entry_tag.as_deref().unwrap_or("empty");
                let reason = format!("exit_{}_g", route.mode_name);
                return Some(CustomExitDecision::Exit(nfi_exit_reason(
                    &reason, entry_tag,
                )));
            }
        }
    }
    matched.then_some(CustomExitDecision::NoExit)
}

/// Execute the bounded short-rebuy branch in source order.
#[allow(clippy::too_many_arguments)]
fn evaluate_nfi_short_exit(
    manager: &NfiX7TradeManager,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    profit_targets: &mut BTreeMap<String, ProfitTarget>,
) -> Option<CustomExitDecision> {
    const PROGRAM_ORDER: &[&str] = &[
        "short_exit_signals",
        "short_exit_main",
        "short_exit_williams_r",
        "short_exit_dec",
    ];
    let words = trade
        .entry_tag
        .as_deref()
        .unwrap_or("")
        .split_whitespace()
        .collect::<Vec<_>>();
    let mut matched = false;
    for key in &manager.short_route_order {
        let route = manager
            .managed_short_routes
            .iter()
            .find(|route| &route.key == key)?;
        if !nfi_short_route_supports_tags(route, &words) {
            continue;
        }
        matched = true;
        match evaluate_nfi_managed_long_exit(
            manager,
            route,
            PROGRAM_ORDER,
            trade,
            pair,
            candle_index,
            candle,
            config,
            profit_targets,
        )? {
            CustomExitDecision::Exit(reason) => {
                return Some(CustomExitDecision::Exit(reason));
            }
            CustomExitDecision::NoExit => {}
        }
    }
    matched.then_some(CustomExitDecision::NoExit)
}

/// Execute one source-bound NFI X7 managed custom-exit route.
///
/// Every profile follows the source callback's order: pure signal programs,
/// optional inline quick/rapid logic, profile stoploss, existing target,
/// target mutation, then the profile's ignored-signal filter. Target writes
/// happen even when `confirm_trade_exit` later rejects the candidate, exactly
/// as in Freqtrade.
#[allow(clippy::too_many_arguments)]
fn evaluate_nfi_managed_long_exit(
    manager: &NfiX7TradeManager,
    route: &NfiManagedLongRoute,
    program_order: &[&str],
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    profit_targets: &mut BTreeMap<String, ProfitTarget>,
) -> Option<CustomExitDecision> {
    let entry_tag = trade.entry_tag.as_deref().unwrap_or("empty");
    let enter_tags = entry_tag
        .split_whitespace()
        .map(ToString::to_string)
        .collect::<Vec<_>>();
    let snapshot = nfi_profit_snapshot(
        trade,
        candle.open,
        fee_open(config),
        fee_close(config),
        config.is_futures,
    )?;
    let (mut sell, mut signal_name) = nfi_managed_long_signals(
        manager,
        route,
        program_order,
        trade,
        pair,
        candle_index,
        candle,
        snapshot,
        &enter_tags,
    )?;

    // X7 places rapid's inline RSI/MFI checks before its custom stop, while
    // quick places the same-shaped checks after `long_exit_stoploss()`. The
    // distinction matters when both predicates are true because the returned
    // reason changes.
    if !sell && route.profile == NfiManagedLongProfile::Rapid {
        (sell, signal_name) = nfi_inline_profile_exit(route, pair, candle_index, snapshot)?;
    }
    if !sell {
        (sell, signal_name) = nfi_managed_long_stoploss(
            manager,
            route,
            trade,
            pair,
            candle_index,
            snapshot,
            config.is_futures,
        )?;
    }
    if !sell && route.profile == NfiManagedLongProfile::Quick {
        (sell, signal_name) = nfi_inline_profile_exit(route, pair, candle_index, snapshot)?;
    }

    let previous_target = profit_targets.get(&trade.pair).cloned();
    if let NfiExistingTargetOutcome::Exit(reason) = evaluate_existing_nfi_target(
        route,
        trade,
        pair,
        candle_index,
        candle,
        snapshot,
        previous_target.as_ref(),
        profit_targets,
    )? {
        return Some(CustomExitDecision::Exit(nfi_exit_reason(
            &reason, entry_tag,
        )));
    }
    update_nfi_target_candidate(
        route,
        trade,
        candle,
        snapshot,
        sell,
        signal_name.as_deref(),
        previous_target.as_ref(),
        profit_targets,
    );

    let Some(reason) = signal_name else {
        return Some(CustomExitDecision::NoExit);
    };
    if sell && !nfi_ignored_signal(route, &reason) {
        return Some(CustomExitDecision::Exit(nfi_exit_reason(
            &reason, entry_tag,
        )));
    }
    Some(CustomExitDecision::NoExit)
}

#[allow(clippy::too_many_arguments)]
fn nfi_managed_long_signals(
    manager: &NfiX7TradeManager,
    route: &NfiManagedLongRoute,
    program_order: &[&str],
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    snapshot: NfiProfitSnapshot,
    enter_tags: &[String],
) -> Option<(bool, Option<String>)> {
    if nfi_profile_requires_positive_profit(route.profile) && snapshot.initial_stake_ratio <= 0.0 {
        return Some((false, None));
    }
    let base_variables = BTreeMap::from([
        (
            "mode_name".to_owned(),
            Value::String(route.mode_name.clone()),
        ),
        (
            "current_profit".to_owned(),
            number_value(if route.profile == NfiManagedLongProfile::Rebuy {
                snapshot.current_stake_ratio
            } else {
                snapshot.initial_stake_ratio
            })?,
        ),
        ("max_profit".to_owned(), number_value(0.0)?),
        ("max_loss".to_owned(), number_value(0.0)?),
        ("trade".to_owned(), scalar_trade_value(trade)?),
        (
            "current_time".to_owned(),
            Value::Number(candle.timestamp_ms.into()),
        ),
        (
            "buy_tag".to_owned(),
            Value::Array(enter_tags.iter().cloned().map(Value::String).collect()),
        ),
    ]);
    let mut result = (false, None);
    for program_name in program_order {
        let mut variables = base_variables.clone();
        insert_projected_feature_window(
            &mut variables,
            pair,
            candle_index,
            manager.feature_projection(program_name)?,
        )?;
        let value = evaluate_scalar_program_bundle(&manager.programs, program_name, variables)?;
        let fields = value.as_array()?;
        if fields.len() != 2 {
            return None;
        }
        result.0 = fields.first()?.as_bool()?;
        result.1 = match fields.get(1)? {
            Value::Null => None,
            Value::String(reason) => Some(reason.clone()),
            _ => return None,
        };
        if result.0 {
            break;
        }
    }
    Some(result)
}

fn nfi_profile_program_order(profile: NfiManagedLongProfile) -> &'static [&'static str] {
    const ALL: &[&str] = &[
        "long_exit_signals",
        "long_exit_main",
        "long_exit_williams_r",
        "long_exit_dec",
    ];
    const WITHOUT_DESCENDING: &[&str] = &[
        "long_exit_signals",
        "long_exit_main",
        "long_exit_williams_r",
    ];
    match profile {
        NfiManagedLongProfile::HighProfit => WITHOUT_DESCENDING,
        _ => ALL,
    }
}

fn nfi_profile_requires_positive_profit(profile: NfiManagedLongProfile) -> bool {
    matches!(
        profile,
        NfiManagedLongProfile::Normal
            | NfiManagedLongProfile::Pump
            | NfiManagedLongProfile::Quick
            | NfiManagedLongProfile::Rapid
    )
}

fn nfi_inline_profile_exit(
    route: &NfiManagedLongRoute,
    pair: &PairSeries,
    candle_index: usize,
    snapshot: NfiProfitSnapshot,
) -> Option<(bool, Option<String>)> {
    let suffix_prefix = match route.profile {
        NfiManagedLongProfile::Quick
            if snapshot.initial_stake_ratio > 0.02 && snapshot.initial_stake_ratio <= 0.09 =>
        {
            "q"
        }
        NfiManagedLongProfile::Rapid
            if snapshot.initial_stake_ratio > 0.005 && snapshot.initial_stake_ratio <= 0.09 =>
        {
            "rpd"
        }
        _ => return Some((false, None)),
    };
    let rsi_14 = feature_number_at(pair, candle_index, "RSI_14")?;
    let mfi_14 = feature_number_at(pair, candle_index, "MFI_14")?;
    let willr_14 = feature_number_at(pair, candle_index, "WILLR_14")?;
    let rsi_3 = feature_number_at(pair, candle_index, "RSI_3")?;
    let rsi_3_15m = feature_number_at(pair, candle_index, "RSI_3_15m")?;
    let conditions = [
        rsi_14 > 78.0,
        mfi_14 > 84.0,
        willr_14 >= -0.1,
        rsi_14 >= 72.0 && rsi_3 > 90.0 && rsi_3_15m > 90.0,
        rsi_3_15m > 96.0,
        rsi_3 > 85.0 && rsi_3_15m > 85.0,
        rsi_3 > 90.0 && rsi_3_15m > 80.0,
        rsi_3 > 92.0 && rsi_3_15m > 75.0,
        rsi_3 > 94.0 && rsi_3_15m > 70.0,
        rsi_3 > 99.0,
    ];
    let reason = conditions
        .iter()
        .position(|condition| *condition)
        .map(|index| format!("exit_{}_{}_{}", route.mode_name, suffix_prefix, index + 1));
    Some((reason.is_some(), reason))
}

#[allow(clippy::too_many_arguments)]
fn evaluate_existing_nfi_target(
    route: &NfiManagedLongRoute,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    snapshot: NfiProfitSnapshot,
    previous: Option<&ProfitTarget>,
    profit_targets: &mut BTreeMap<String, ProfitTarget>,
) -> Option<NfiExistingTargetOutcome> {
    let Some(previous) = previous else {
        return Some(NfiExistingTargetOutcome::NoExit);
    };
    let decision =
        nfi_managed_long_profit_target_exit(route, trade, pair, candle_index, snapshot, previous)?;
    if decision.remove {
        profit_targets.remove(&trade.pair);
    }
    if let Some(reason) = decision.exit_reason {
        return Some(NfiExistingTargetOutcome::Exit(format!("{reason}_m")));
    }
    let stoploss_u_e = format!("exit_{}_stoploss_u_e", route.mode_name);
    let stoploss_doom = format!("exit_{}_stoploss_doom", route.mode_name);
    if previous.sell_reason == stoploss_u_e
        && snapshot.ratio > previous.profit + nfi_u_e_raise_delta(route.profile)
    {
        set_profit_target(
            profit_targets,
            trade,
            candle,
            previous.sell_reason.clone(),
            snapshot.ratio,
        );
    } else if snapshot.initial_stake_ratio > previous.profit + 0.001
        && previous.sell_reason != stoploss_doom
    {
        set_profit_target(
            profit_targets,
            trade,
            candle,
            previous.sell_reason.clone(),
            snapshot.initial_stake_ratio,
        );
    }
    Some(NfiExistingTargetOutcome::NoExit)
}

#[allow(clippy::too_many_arguments)]
fn update_nfi_target_candidate(
    route: &NfiManagedLongRoute,
    trade: &OpenTrade,
    candle: &Candle,
    snapshot: NfiProfitSnapshot,
    sell: bool,
    reason: Option<&str>,
    previous: Option<&ProfitTarget>,
    profit_targets: &mut BTreeMap<String, ProfitTarget>,
) {
    if let (true, Some(reason)) = (sell, reason) {
        let stoploss_doom = format!("exit_{}_stoploss_doom", route.mode_name);
        let stoploss_u_e = format!("exit_{}_stoploss_u_e", route.mode_name);
        let blocked_u_e = format!("exit_profit_{}_stoploss_u_e", route.mode_name);
        let protected = reason == stoploss_doom || reason == stoploss_u_e;
        let blocked_previous = previous.is_some_and(|previous| {
            previous.sell_reason == stoploss_doom || previous.sell_reason == blocked_u_e
        });
        let target_profit = if protected {
            snapshot.ratio
        } else {
            snapshot.initial_stake_ratio
        };
        let should_mark = (protected
            && (!nfi_protected_target_has_reentry_guard(route.profile) || !blocked_previous))
            || (!protected
                && previous.is_none_or(|previous| previous.profit < snapshot.initial_stake_ratio));
        if should_mark {
            set_profit_target(
                profit_targets,
                trade,
                candle,
                reason.to_owned(),
                target_profit,
            );
        }
    } else if snapshot.initial_stake_ratio >= nfi_max_target_floor(route.profile)
        && previous.is_none_or(|previous| previous.profit < snapshot.initial_stake_ratio)
    {
        set_profit_target(
            profit_targets,
            trade,
            candle,
            format!("exit_profit_{}_max", route.mode_name),
            snapshot.initial_stake_ratio,
        );
    }
}

fn nfi_ignored_signal(route: &NfiManagedLongRoute, reason: &str) -> bool {
    let maximum = format!("exit_profit_{}_max", route.mode_name);
    if reason == maximum {
        return true;
    }
    // X7 high-profit writes the stop target and still returns the stop in the
    // same callback. Every other managed-long mode suppresses that immediate
    // candidate and lets the target helper decide on a later candle.
    route.profile != NfiManagedLongProfile::HighProfit
        && [
            format!("exit_{}_stoploss_doom", route.mode_name),
            format!("exit_{}_stoploss_u_e", route.mode_name),
        ]
        .iter()
        .any(|ignored| ignored == reason)
}

fn nfi_u_e_raise_delta(profile: NfiManagedLongProfile) -> f64 {
    match profile {
        NfiManagedLongProfile::Normal
        | NfiManagedLongProfile::Pump
        | NfiManagedLongProfile::TopCoins
        | NfiManagedLongProfile::Scalp => 0.005,
        NfiManagedLongProfile::Quick
        | NfiManagedLongProfile::Rebuy
        | NfiManagedLongProfile::HighProfit
        | NfiManagedLongProfile::Rapid => 0.001,
    }
}

fn nfi_max_target_floor(profile: NfiManagedLongProfile) -> f64 {
    if profile == NfiManagedLongProfile::HighProfit {
        0.03
    } else {
        0.005
    }
}

fn nfi_protected_target_has_reentry_guard(profile: NfiManagedLongProfile) -> bool {
    matches!(
        profile,
        NfiManagedLongProfile::Normal
            | NfiManagedLongProfile::Quick
            | NfiManagedLongProfile::Rapid
            | NfiManagedLongProfile::TopCoins
    )
}

enum NfiExistingTargetOutcome {
    NoExit,
    Exit(String),
}

#[derive(Debug, Default)]
struct NfiTargetDecision {
    exit_reason: Option<String>,
    remove: bool,
}

/// Evaluate the branches reachable from the compiled managed-long profiles.
///
/// Short routes remain outside the adapter. The scalp branch is selected only
/// when every entry-tag word is a scalp tag, matching X7's ``all(...)`` test;
/// a supported scalp+grind combination uses the ordinary long trailing logic.
#[allow(clippy::too_many_arguments)]
fn nfi_managed_long_profit_target_exit(
    route: &NfiManagedLongRoute,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    snapshot: NfiProfitSnapshot,
    previous: &ProfitTarget,
) -> Option<NfiTargetDecision> {
    let mode = &route.mode_name;
    let doom = format!("exit_{mode}_stoploss_doom");
    let ordinary_stop = format!("exit_{mode}_stoploss");
    let u_e = format!("exit_{mode}_stoploss_u_e");
    if previous.sell_reason == doom || previous.sell_reason == ordinary_stop {
        // This adapter is structurally gated to `system_name_use ==
        // system_v3_2_name`; X7 returns the cached stop immediately for all
        // system-v3 variants.
        return Some(NfiTargetDecision {
            exit_reason: Some(previous.sell_reason.clone()),
            remove: false,
        });
    }
    if previous.sell_reason == u_e {
        if snapshot.initial_stake_ratio > 0.0 || nfi_trade_is_derisked(trade)? {
            return Some(NfiTargetDecision {
                exit_reason: None,
                remove: true,
            });
        }
        if snapshot.ratio < previous.profit - 0.04 / trade.leverage {
            return Some(NfiTargetDecision {
                exit_reason: Some(previous.sell_reason.clone()),
                remove: false,
            });
        }
        return Some(NfiTargetDecision::default());
    }
    if previous.sell_reason != format!("exit_profit_{mode}_max") {
        return Some(NfiTargetDecision::default());
    }
    if snapshot.initial_stake_ratio < -0.08 {
        return Some(NfiTargetDecision {
            exit_reason: None,
            remove: true,
        });
    }

    let previous_index = candle_index.checked_sub(1)?;
    let last_rsi = feature_number_at(pair, candle_index, "RSI_14")?;
    let previous_rsi = feature_number_at(pair, previous_index, "RSI_14")?;
    let cmf = feature_number_at(pair, candle_index, "CMF_20")?;
    let cmf_1h = feature_number_at(pair, candle_index, "CMF_20_1h")?;
    let cmf_4h = feature_number_at(pair, candle_index, "CMF_20_4h")?;
    let roc_4h = feature_number_at(pair, candle_index, "ROC_9_4h")?;
    let Some(bucket) = nfi_profit_bucket(snapshot.initial_stake_ratio) else {
        return Some(NfiTargetDecision::default());
    };
    let pure_scalp_tags = route.profile == NfiManagedLongProfile::Scalp
        && trade.entry_tag.as_deref().is_some_and(|entry_tag| {
            let words = entry_tag.split_whitespace().collect::<Vec<_>>();
            !words.is_empty()
                && words
                    .iter()
                    .all(|word| route.entry_tags.iter().any(|tag| tag == word))
        });
    if pure_scalp_tags {
        let trailing_delta = match bucket {
            0 => 0.008,
            1 | 2 => 0.01,
            3..=6 => 0.015,
            7..=9 => 0.02,
            10..=12 => 0.025,
            _ => return None,
        };
        return Some(NfiTargetDecision {
            exit_reason: (snapshot.initial_stake_ratio < previous.profit - trailing_delta)
                .then(|| format!("exit_profit_{mode}_t_{bucket}_1")),
            remove: false,
        });
    }
    let trail_1 = snapshot.initial_stake_ratio < previous.profit - 0.03
        && last_rsi < 50.0
        && last_rsi < previous_rsi
        && cmf < -0.0;
    let trail_2 = snapshot.initial_stake_ratio < previous.profit - 0.03
        && cmf < -0.0
        && cmf_1h < -0.0
        && cmf_4h < -0.0;
    let trail_3 = snapshot.initial_stake_ratio < previous.profit - 0.05 && roc_4h > 40.0;
    let suffix = if trail_1 {
        Some(1)
    } else if trail_2 {
        Some(2)
    } else if trail_3 {
        Some(3)
    } else {
        None
    };
    Some(NfiTargetDecision {
        exit_reason: suffix.map(|suffix| format!("exit_profit_{mode}_t_{bucket}_{suffix}")),
        remove: false,
    })
}

fn nfi_managed_long_stoploss(
    manager: &NfiX7TradeManager,
    route: &NfiManagedLongRoute,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    snapshot: NfiProfitSnapshot,
    is_futures: bool,
) -> Option<(bool, Option<String>)> {
    let constants = &manager.constants;
    let first_entry = trade.orders.iter().find(|order| order.is_entry)?;
    let entry_cost = first_entry.amount * first_entry.price;
    let system_version = trade.custom_data.get("system_version")?.as_str()?;
    if system_version != constants.system_name_use {
        return None;
    }

    if matches!(
        route.profile,
        NfiManagedLongProfile::Rebuy | NfiManagedLongProfile::Rapid | NfiManagedLongProfile::Scalp
    ) {
        if !constants.system_v3_2_stops_enable {
            return Some((false, None));
        }
        let threshold = if is_futures {
            route.stop_threshold_futures?
        } else {
            route.stop_threshold_spot?
        };
        let stopped = snapshot.stake < -(entry_cost * threshold / trade.leverage);
        return Some((
            stopped,
            stopped.then(|| format!("exit_{}_stoploss_doom", route.mode_name)),
        ));
    }

    if !constants.stops_enable {
        return Some((false, None));
    }
    if constants.system_v3_2_stops_enable {
        let threshold = if is_futures {
            constants.system_v3_2_stop_threshold_doom_futures
        } else {
            constants.system_v3_2_stop_threshold_doom_spot
        };
        if snapshot.stake < -(entry_cost * threshold / trade.leverage) {
            return Some((
                true,
                Some(format!("exit_{}_stoploss_doom", route.mode_name)),
            ));
        }
    }
    if !constants.u_e_stops_enable {
        return Some((false, None));
    }
    let previous_index = candle_index.checked_sub(1)?;
    let close = feature_number_at(pair, candle_index, "close")?;
    let ema_200 = feature_number_at(pair, candle_index, "EMA_200")?;
    let rsi = feature_number_at(pair, candle_index, "RSI_14")?;
    let cmf = feature_number_at(pair, candle_index, "CMF_20")?;
    let rsi_1h = feature_number_at(pair, candle_index, "RSI_14_1h")?;
    let previous_rsi = feature_number_at(pair, previous_index, "RSI_14")?;
    let threshold = if is_futures {
        constants.stop_threshold_futures
    } else {
        constants.stop_threshold_spot
    };
    let should_stop = snapshot.stake < -(entry_cost * threshold)
        && close < ema_200
        && cmf < -0.0
        && (ema_200 - close) / close < 0.010
        && rsi > previous_rsi
        && rsi > rsi_1h + 24.0;
    Some((
        should_stop,
        should_stop.then(|| format!("exit_{}_stoploss_u_e", route.mode_name)),
    ))
}

fn nfi_trade_is_derisked(trade: &OpenTrade) -> Option<bool> {
    let first_entry = trade.orders.iter().find(|order| order.is_entry)?;
    let tagged_exit = trade
        .orders
        .iter()
        .filter(|order| !order.is_entry)
        .any(|order| {
            order
                .tag
                .as_deref()
                .and_then(|tag| tag.split_whitespace().next())
                .is_some_and(|tag| {
                    matches!(
                        tag,
                        "d" | "d1" | "derisk_level_1" | "derisk_level_2" | "derisk_level_3"
                    )
                })
        });
    Some(tagged_exit || trade.amount < first_entry.amount * 0.95)
}

fn set_profit_target(
    profit_targets: &mut BTreeMap<String, ProfitTarget>,
    trade: &OpenTrade,
    candle: &Candle,
    sell_reason: String,
    profit: f64,
) {
    profit_targets.insert(
        trade.pair.clone(),
        ProfitTarget {
            rate: candle.open,
            profit,
            sell_reason,
            time_profit_reached_ms: candle.timestamp_ms,
        },
    );
}

fn nfi_profit_bucket(profit: f64) -> Option<u8> {
    if profit < 0.001 {
        return None;
    }
    if profit >= 0.12 {
        return Some(12);
    }
    let mut bucket = 0_u8;
    for candidate in 1_u8..=11 {
        if profit >= f64::from(candidate) / 100.0 {
            bucket = candidate;
        }
    }
    Some(bucket)
}

fn nfi_exit_reason(reason: &str, entry_tag: &str) -> String {
    format!("{reason} ( {entry_tag})")
}

fn feature_number_at(pair: &PairSeries, index: usize, name: &str) -> Option<f64> {
    let candle = pair.candles.get(index)?;
    match name {
        "open" => Some(candle.open),
        "high" => Some(candle.high),
        "low" => Some(candle.low),
        "close" => Some(candle.close),
        "volume" => Some(candle.volume),
        _ => pair.feature_columns.get(name)?.number(index),
    }
}

fn feature_bool_at(pair: &PairSeries, index: usize, name: &str) -> Option<bool> {
    pair.feature_columns.get(name)?.boolean(index)
}

fn evaluate_custom_exit_bundle(
    bundle: &ScalarProgramBundle,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
) -> Option<CustomExitDecision> {
    let trade_value = scalar_trade_value(trade)?;
    let mut variables = BTreeMap::from([
        ("pair".to_owned(), Value::String(trade.pair.clone())),
        ("trade".to_owned(), trade_value),
        (
            "current_time".to_owned(),
            Value::Number(candle.timestamp_ms.into()),
        ),
        ("current_rate".to_owned(), number_value(candle.open)?),
        (
            "current_profit".to_owned(),
            number_value(current_profit_ratio(trade, candle.open, fee_close(config)))?,
        ),
        ("kwargs".to_owned(), Value::Object(serde_json::Map::new())),
    ]);
    insert_feature_window(&mut variables, pair, candle_index)?;
    let value = evaluate_scalar_program_bundle(&bundle.programs, &bundle.entry, variables)?;
    if !scalar_truthy(&value) {
        return Some(CustomExitDecision::NoExit);
    }
    // Freqtrade preserves a truthy string as the custom reason. Any other
    // truthy Python value exits with ExitType.CUSTOM_EXIT's default reason.
    let reason = value.as_str().map_or_else(
        || "custom_exit".to_owned(),
        |value| value.chars().take(255).collect(),
    );
    Some(CustomExitDecision::Exit(reason))
}

fn scalar_trade_value(trade: &OpenTrade) -> Option<Value> {
    let entry_count = trade.orders.iter().filter(|order| order.is_entry).count();
    let exit_count = trade.orders.iter().filter(|order| !order.is_entry).count();
    Some(Value::Object(serde_json::Map::from_iter([
        ("id".to_owned(), Value::Number(trade.id.into())),
        ("pair".to_owned(), Value::String(trade.pair.clone())),
        (
            "is_short".to_owned(),
            Value::Bool(trade.side == TradeSide::Short),
        ),
        ("amount".to_owned(), number_value(trade.amount)?),
        ("stake_amount".to_owned(), number_value(trade.stake_amount)?),
        ("open_rate".to_owned(), number_value(trade.open_rate)?),
        ("leverage".to_owned(), number_value(trade.leverage)?),
        (
            "open_date_utc".to_owned(),
            Value::Number(trade.open_timestamp_ms.into()),
        ),
        (
            "enter_tag".to_owned(),
            trade
                .entry_tag
                .as_ref()
                .map_or(Value::Null, |tag| Value::String(tag.clone())),
        ),
        (
            "nr_of_successful_entries".to_owned(),
            Value::Number(u64::try_from(entry_count).ok()?.into()),
        ),
        (
            "nr_of_successful_exits".to_owned(),
            Value::Number(u64::try_from(exit_count).ok()?.into()),
        ),
        (
            "custom_data".to_owned(),
            Value::Object(trade.custom_data.clone().into_iter().collect()),
        ),
    ])))
}

fn evaluate_adjustment_bundle(
    bundle: &ScalarProgramBundle,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    available_balance: f64,
) -> Result<Option<AdjustmentSignal>, ()> {
    let has_minimum = pair.minimum_stake.is_some()
        || pair.minimum_amount.is_some()
        || pair.minimum_cost.is_some();
    // Adjustment callbacks use Freqtrade's unleveraged minimum-stake
    // boundary, not the leverage-aware entry-order boundary.
    let minimum_stake = if has_minimum {
        number_value(adjustment_minimum_pair_stake(
            pair,
            candle.open,
            config.amount_reserve_percent,
        ))
        .ok_or(())?
    } else {
        Value::Null
    };
    let current_profit =
        number_value(current_profit_ratio(trade, candle.open, fee_close(config))).ok_or(())?;
    let mut variables = BTreeMap::from([
        ("trade".to_owned(), scalar_trade_value(trade).ok_or(())?),
        (
            "current_time".to_owned(),
            Value::Number(candle.timestamp_ms.into()),
        ),
        (
            "current_rate".to_owned(),
            number_value(candle.open).ok_or(())?,
        ),
        ("current_profit".to_owned(), current_profit.clone()),
        ("min_stake".to_owned(), minimum_stake),
        (
            "max_stake".to_owned(),
            number_value(available_balance).ok_or(())?,
        ),
        (
            "current_entry_rate".to_owned(),
            number_value(candle.open).ok_or(())?,
        ),
        (
            "current_exit_rate".to_owned(),
            number_value(candle.open).ok_or(())?,
        ),
        ("current_entry_profit".to_owned(), current_profit.clone()),
        ("current_exit_profit".to_owned(), current_profit),
        ("kwargs".to_owned(), Value::Object(serde_json::Map::new())),
    ]);
    insert_feature_window(&mut variables, pair, candle_index).ok_or(())?;
    let value =
        evaluate_scalar_program_bundle(&bundle.programs, &bundle.entry, variables).ok_or(())?;
    let (stake_amount, tag) = match value {
        Value::Null => return Ok(None),
        Value::Array(values) => {
            let stake = scalar_adjustment_number(values.first().ok_or(())?).ok_or(())?;
            let tag = match values.get(1) {
                None | Some(Value::Null | Value::Bool(false)) => String::new(),
                Some(Value::String(tag)) => tag.clone(),
                _ => return Err(()),
            };
            (stake, tag)
        }
        value => (scalar_adjustment_number(&value).ok_or(())?, String::new()),
    };
    if !stake_amount.is_finite() || stake_amount == 0.0 {
        return Ok(None);
    }
    if stake_amount > 0.0 && config.max_entry_position_adjustment >= 0 {
        let entry_count = trade.orders.iter().filter(|order| order.is_entry).count();
        if i64::try_from(entry_count).map_err(|_| ())? > config.max_entry_position_adjustment {
            return Ok(None);
        }
    }
    Ok(Some(AdjustmentSignal { stake_amount, tag }))
}

/// Map an execution candle to the last analyzed candle visible to callbacks.
///
/// Freqtrade shifts entry/exit signals onto the next candle before simulation,
/// but its data provider still ends at the last fully analyzed candle. At an
/// execution time of 15:30, callbacks therefore see the 15:25 row. Keeping
/// this translation at the callback boundary prevents order prices/timestamps
/// from being shifted along with indicator data.
fn callback_feature_index(execution_index: usize) -> Option<usize> {
    execution_index.checked_sub(1)
}

/// Materialize one strategy-visible dataframe row from the pair-level columns.
///
/// Freqtrade callbacks see the current analyzed row plus recent predecessors,
/// while the transport keeps those values columnar to avoid repeating 100+
/// field names for every NFI candle. Validation has already guaranteed equal
/// column lengths, but this helper still returns `None` so any internal/schema
/// mismatch fails closed instead of silently substituting a value.
fn feature_row(pair: &PairSeries, index: usize) -> Option<Value> {
    let candle = pair.candles.get(index)?;
    let mut row = serde_json::Map::from_iter([
        ("open".to_owned(), number_value(candle.open)?),
        ("high".to_owned(), number_value(candle.high)?),
        ("low".to_owned(), number_value(candle.low)?),
        ("close".to_owned(), number_value(candle.close)?),
        ("volume".to_owned(), number_value(candle.volume)?),
    ]);
    for (name, values) in &pair.feature_columns {
        row.insert(name.clone(), values.value(index)?);
    }
    Some(Value::Object(row))
}

type FeatureProjection = BTreeMap<String, BTreeSet<String>>;

impl NfiX7TradeManager {
    fn feature_projection(&self, program_name: &str) -> Option<&FeatureProjection> {
        self.feature_projections
            .get_or_init(|| {
                self.programs
                    .iter()
                    .map(|(name, program)| {
                        (name.clone(), scalar_program_feature_projection(program))
                    })
                    .collect()
            })
            .get(program_name)
    }
}

/// Derive dataframe field access directly from the immutable scalar arena.
///
/// The compiler represents `last_candle["RSI_14"]` as an `index` expression
/// whose operands point at a `variable` and a literal string expression. We do
/// not accept a serialized projection list: deriving it here prevents an input
/// from omitting a field that executable bytecode can read.
fn scalar_program_feature_projection(program: &ScalarDecisionProgram) -> FeatureProjection {
    let mut projection = FeatureProjection::new();
    for expression in &program.expressions {
        let Some(fields) = expression.as_array() else {
            continue;
        };
        if fields.first().and_then(Value::as_str) != Some("index") {
            continue;
        }
        let Some(base_index) = fields.get(1).and_then(value_index) else {
            continue;
        };
        let Some(key_index) = fields.get(2).and_then(value_index) else {
            continue;
        };
        let Some(base) = program
            .expressions
            .get(base_index)
            .and_then(Value::as_array)
        else {
            continue;
        };
        let Some(key) = program.expressions.get(key_index).and_then(Value::as_array) else {
            continue;
        };
        if base.first().and_then(Value::as_str) != Some("variable")
            || key.first().and_then(Value::as_str) != Some("literal")
        {
            continue;
        }
        let Some(variable) = base.get(1).and_then(Value::as_str) else {
            continue;
        };
        if !is_feature_row_variable(variable) {
            continue;
        }
        if let Some(column) = key.get(1).and_then(Value::as_str) {
            projection
                .entry(variable.to_owned())
                .or_default()
                .insert(column.to_owned());
        }
    }
    projection
}

fn is_feature_row_variable(name: &str) -> bool {
    name == "last_candle"
        || name == "previous_candle"
        || (1..=5).any(|offset| name == format!("previous_candle_{offset}"))
}

fn projected_feature_row(
    pair: &PairSeries,
    index: usize,
    columns: Option<&BTreeSet<String>>,
) -> Option<Value> {
    let candle = pair.candles.get(index)?;
    // OHLCV is always present in Freqtrade's analyzed row. Keeping these five
    // fields also preserves row truthiness if a future compiled branch checks
    // the row object itself without indexing a feature.
    let mut row = serde_json::Map::from_iter([
        ("open".to_owned(), number_value(candle.open)?),
        ("high".to_owned(), number_value(candle.high)?),
        ("low".to_owned(), number_value(candle.low)?),
        ("close".to_owned(), number_value(candle.close)?),
        ("volume".to_owned(), number_value(candle.volume)?),
    ]);
    for name in columns.into_iter().flatten() {
        if row.contains_key(name) {
            continue;
        }
        row.insert(name.clone(), pair.feature_columns.get(name)?.value(index)?);
    }
    Some(Value::Object(row))
}

fn insert_projected_feature_window(
    variables: &mut BTreeMap<String, Value>,
    pair: &PairSeries,
    candle_index: usize,
    projection: &FeatureProjection,
) -> Option<()> {
    variables.insert(
        "last_candle".to_owned(),
        projected_feature_row(pair, candle_index, projection.get("last_candle"))?,
    );
    for offset in 1..=5 {
        let name = format!("previous_candle_{offset}");
        let value = candle_index
            .checked_sub(offset)
            .and_then(|index| projected_feature_row(pair, index, projection.get(&name)))
            .unwrap_or(Value::Null);
        variables.insert(name, value);
    }
    let previous = candle_index
        .checked_sub(1)
        .and_then(|index| projected_feature_row(pair, index, projection.get("previous_candle")))
        .unwrap_or(Value::Null);
    variables.insert("previous_candle".to_owned(), previous);
    Some(())
}

/// Add the six analyzed dataframe rows used by NFI scalar decisions.
///
/// `candle_index` is already the callback-visible feature index, not the
/// execution-candle index. The names intentionally match the strategy method
/// parameters. A missing predecessor is represented as `None`; accessing a
/// field on it makes the scalar VM reject the callback. Real NFI signals only
/// become executable after `startup_candle_count`, so valid reference runs
/// always have the full lookback instead of receiving fabricated warm-up data.
fn insert_feature_window(
    variables: &mut BTreeMap<String, Value>,
    pair: &PairSeries,
    candle_index: usize,
) -> Option<()> {
    variables.insert("last_candle".to_owned(), feature_row(pair, candle_index)?);
    for offset in 1..=5 {
        let value = candle_index
            .checked_sub(offset)
            .and_then(|index| feature_row(pair, index))
            .unwrap_or(Value::Null);
        variables.insert(format!("previous_candle_{offset}"), value.clone());
        if offset == 1 {
            // Grind entry helpers use the shorter historical parameter name.
            variables.insert("previous_candle".to_owned(), value);
        }
    }
    Some(())
}

fn scalar_adjustment_number(value: &Value) -> Option<f64> {
    match value {
        Value::Bool(value) => Some(f64::from(u8::from(*value))),
        value => scalar_number(value),
    }
}

fn apply_funding(trade: &mut OpenTrade, candle: &Candle) {
    let (Some(rate), Some(mark_price)) = (candle.funding_rate, candle.funding_mark_price) else {
        return;
    };
    // Pandas evaluates Freqtrade's expression left-to-right as
    // `(open_fund * open_mark) * amount`. Multiplying amount first is
    // mathematically equivalent but changes exported float tokens.
    let fee = rate * mark_price * trade.amount;
    // Freqtrade's persisted convention is positive when the trade receives
    // funding and negative when it pays. A positive market funding rate is
    // therefore income for shorts and a cost for longs.
    let signed = match trade.side {
        TradeSide::Long => -fee,
        TradeSide::Short => fee,
    };
    // `Exchange.calculate_funding_fees()` uses Python `sum()` over all
    // funding rows since the most recent filled order. CPython 3.14 uses a
    // Neumaier correction for float iterables, so a plain `+=` can differ by
    // an exported ulp on long-running adjustment trades.
    let next = trade.funding_sum_high + signed;
    if trade.funding_sum_high.abs() >= signed.abs() {
        trade.funding_sum_low += (trade.funding_sum_high - next) + signed;
    } else {
        trade.funding_sum_low += (signed - next) + trade.funding_sum_high;
    }
    trade.funding_sum_high = next;
    trade.funding_fees = compensated_sum_result(trade.funding_sum_high, trade.funding_sum_low);

    // `Trade.set_funding_fees()` separately performs Python `sum()` over the
    // already-filled orders, then adds the current running segment.
    let prior_funding = python_float_sum(trade.orders.iter().map(|order| order.funding_fee));
    trade.funding_fees_total = prior_funding + trade.funding_fees;
}

fn compensated_sum_result(high: f64, low: f64) -> f64 {
    if low != 0.0 && low.is_finite() {
        high + low
    } else {
        high
    }
}

/// Move the current funding segment to a newly filled order.
///
/// Freqtrade resets `funding_fee_running` after every non-stoploss fill. The
/// compensated state must be reset at the same boundary or later segments
/// would retain an invisible correction from an earlier order.
fn take_running_funding(trade: &mut OpenTrade) -> f64 {
    trade.funding_sum_high = 0.0;
    trade.funding_sum_low = 0.0;
    std::mem::take(&mut trade.funding_fees)
}

/// Mirror the ordinary left-to-right accumulation in
/// `LocalTrade.recalc_trade_from_orders()`.
///
/// This intentionally does not use `python_float_sum`: Freqtrade's order
/// replay is an explicit `+=` loop, which has different rounding behavior.
fn recalculate_order_funding_total(trade: &mut OpenTrade) {
    trade.funding_fees_total = trade
        .orders
        .iter()
        .fold(0.0, |total, order| total + order.funding_fee);
}

fn nfi_profit_snapshot(
    trade: &OpenTrade,
    exit_rate: f64,
    open_fee_rate: f64,
    close_fee_rate: f64,
    is_futures: bool,
) -> Option<NfiProfitSnapshot> {
    if !exit_rate.is_finite()
        || !open_fee_rate.is_finite()
        || !close_fee_rate.is_finite()
        || trade.orders.is_empty()
    {
        return None;
    }
    let mut total_amount = 0.0;
    let mut total_stake = 0.0;
    let mut total_profit = 0.0;
    let (open_multiplier, close_multiplier) = if trade.side == TradeSide::Short {
        (1.0 - open_fee_rate, 1.0 + close_fee_rate)
    } else {
        (1.0 + open_fee_rate, 1.0 - close_fee_rate)
    };
    let mut first_entry_cost = None;
    for order in &trade.orders {
        let stake = order.amount * order.price;
        if order.is_entry {
            first_entry_cost.get_or_insert(stake);
            let entry_stake = stake * open_multiplier;
            total_amount += order.amount;
            total_stake += entry_stake;
            if trade.side == TradeSide::Short {
                total_profit += entry_stake;
            } else {
                total_profit -= entry_stake;
            }
        } else {
            let exit_stake = stake * close_multiplier;
            total_amount -= order.amount;
            if trade.side == TradeSide::Short {
                total_profit -= exit_stake;
            } else {
                total_profit += exit_stake;
            }
        }
    }
    let current_stake = total_amount * exit_rate * close_multiplier;
    if trade.side == TradeSide::Short {
        total_profit -= current_stake;
    } else {
        total_profit += current_stake;
    }
    if is_futures {
        // NFI reads `trade.funding_fees`, which Freqtrade keeps as the
        // cumulative fee across filled orders plus the current running
        // interval. A partial exit realizes part of the position but does not
        // reduce this callback-visible cumulative value.
        total_profit += trade.funding_fees_total;
    }
    let first_entry_cost = first_entry_cost?;
    if total_stake == 0.0 || current_stake == 0.0 || first_entry_cost == 0.0 {
        return None;
    }
    Some(NfiProfitSnapshot {
        stake: total_profit,
        ratio: total_profit / total_stake,
        current_stake_ratio: total_profit / current_stake,
        initial_stake_ratio: total_profit / first_entry_cost,
    })
}

fn current_profit_ratio(trade: &OpenTrade, rate: f64, close_fee_rate: f64) -> f64 {
    if trade.side == TradeSide::Long && (trade.leverage - 1.0).abs() < f64::EPSILON {
        let hypothetical_proceeds = trade.amount * rate * (1.0 - close_fee_rate);
        return (hypothetical_proceeds - trade.entry_cost_with_fees + trade.funding_fees_total)
            / trade.entry_cost_with_fees;
    }
    let direction = if trade.side == TradeSide::Long {
        1.0
    } else {
        -1.0
    };
    let gross_profit = trade.amount * (rate - trade.open_rate) * direction;
    let entry_fees = trade.entry_cost_with_fees - trade.stake_amount;
    let close_fees = trade.amount * rate * close_fee_rate;
    let profit = gross_profit - entry_fees - close_fees + trade.funding_fees_total;
    let open_fee_multiplier = if trade.side == TradeSide::Short {
        1.0 - (entry_fees / (trade.amount * trade.open_rate))
    } else {
        1.0 + (entry_fees / (trade.amount * trade.open_rate))
    };
    profit / (trade.stake_amount * open_fee_multiplier)
}

fn close_trade(
    mut trade: OpenTrade,
    timestamp_ms: i64,
    rate: f64,
    reason: String,
    config: &PortfolioConfig,
    sequence: usize,
    order_id: u64,
) -> (ClosedTrade, f64) {
    let gross_proceeds = trade.amount * rate;
    let open_fee_rate = fee_open(config);
    let close_fee_rate = fee_close(config);
    let (fallback_remaining_profit, fallback_remaining_profit_ratio) =
        fallback_close_profit(&trade, rate, open_fee_rate, close_fee_rate, gross_proceeds);
    let funding_fee = take_running_funding(&mut trade);
    trade.orders.push(FilledOrder {
        id: order_id,
        funding_fee,
        sequence: trade.orders.len(),
        side: exit_order_side(trade.side),
        is_entry: false,
        filled_timestamp_ms: timestamp_ms,
        amount: trade.amount,
        price: rate,
        cost: gross_proceeds * (1.0 + close_fee_rate),
        tag: Some(reason.clone()),
    });
    recalculate_order_funding_total(&mut trade);
    let (profit_abs, fallback_profit_ratio) = replay_closed_profit(
        &trade,
        config,
        open_fee_rate,
        fallback_remaining_profit,
        fallback_remaining_profit_ratio,
    );
    let profit_ratio =
        freqtrade_total_entry_value(&trade, config).map_or(fallback_profit_ratio, |total_stake| {
            if total_stake == 0.0 {
                0.0
            } else {
                (profit_abs / total_stake) * trade.leverage
            }
        });
    let wallet_proceeds = trade.stake_amount + profit_abs;
    (
        ClosedTrade {
            sequence,
            id: trade.id,
            pair: trade.pair,
            is_short: trade.side == TradeSide::Short,
            leverage: trade.leverage,
            open_timestamp_ms: trade.open_timestamp_ms,
            close_timestamp_ms: timestamp_ms,
            open_rate: trade.open_rate,
            close_rate: rate,
            amount: trade.amount,
            stake_amount: trade.stake_amount,
            max_stake_amount: trade.max_stake_amount,
            entry_tag: trade.entry_tag,
            exit_reason: reason,
            fee_open: open_fee_rate,
            fee_close: close_fee_rate,
            funding_fees: trade.funding_fees_total,
            liquidation_price: trade.liquidation_price,
            profit_abs,
            profit_ratio,
            initial_stop_loss: trade.initial_stop_loss,
            stop_loss: trade.stop_loss,
            minimum_rate: trade.minimum_rate,
            maximum_rate: trade.maximum_rate,
            orders: trade.orders,
        },
        wallet_proceeds,
    )
}

fn fallback_close_profit(
    trade: &OpenTrade,
    rate: f64,
    open_fee_rate: f64,
    close_fee_rate: f64,
    gross_proceeds: f64,
) -> (f64, f64) {
    if trade.side == TradeSide::Long && (trade.leverage - 1.0).abs() < f64::EPSILON {
        let proceeds =
            precise_product(&[trade.amount, rate, 1.0 - close_fee_rate]).unwrap_or(gross_proceeds);
        let profit_abs = round_eight(proceeds - trade.entry_cost_with_fees + trade.funding_fees);
        return (profit_abs, profit_abs / trade.entry_cost_with_fees);
    }
    let direction = if trade.side == TradeSide::Long {
        1.0
    } else {
        -1.0
    };
    let gross_profit = trade.amount * (rate - trade.open_rate) * direction;
    let entry_fees = trade.entry_cost_with_fees - trade.stake_amount;
    let close_fees = trade.amount * rate * close_fee_rate;
    let profit_abs = round_eight(gross_profit - entry_fees - close_fees + trade.funding_fees);
    let open_fee_multiplier = if trade.side == TradeSide::Short {
        1.0 - open_fee_rate
    } else {
        1.0 + open_fee_rate
    };
    (
        profit_abs,
        profit_abs / (trade.stake_amount * open_fee_multiplier),
    )
}

fn replay_closed_profit(
    trade: &OpenTrade,
    config: &PortfolioConfig,
    open_fee_rate: f64,
    fallback_remaining_profit: f64,
    fallback_remaining_profit_ratio: f64,
) -> (f64, f64) {
    if is_unleveraged_spot(trade, config) {
        return replay_spot_profit(trade, config).map_or_else(
            || {
                let profit_abs =
                    round_eight(trade.realized_partial_profit + fallback_remaining_profit);
                (profit_abs, fallback_remaining_profit_ratio)
            },
            |replay| {
                let ratio = if replay.total_entry_value == 0.0 {
                    0.0
                } else {
                    replay.profit_abs / replay.total_entry_value
                };
                (replay.profit_abs, ratio)
            },
        );
    }
    if trade.adjustment_count > 0 {
        let profit_abs = replay_leveraged_profit(trade, config).unwrap_or_else(|| {
            round_eight(trade.realized_partial_profit + fallback_remaining_profit)
        });
        let open_fee_multiplier = if trade.side == TradeSide::Short {
            1.0 - open_fee_rate
        } else {
            1.0 + open_fee_rate
        };
        return (
            profit_abs,
            profit_abs / (trade.max_stake_amount * open_fee_multiplier),
        );
    }
    let profit_abs = round_eight(trade.realized_partial_profit + fallback_remaining_profit);
    let profit_ratio = if trade.realized_partial_profit == 0.0 {
        fallback_remaining_profit_ratio
    } else {
        let open_fee_multiplier = if trade.side == TradeSide::Short {
            1.0 - open_fee_rate
        } else {
            1.0 + open_fee_rate
        };
        profit_abs / (trade.max_stake_amount * open_fee_multiplier)
    };
    (profit_abs, profit_ratio)
}

fn round_eight(value: f64) -> f64 {
    (value * 100_000_000.0).round() / 100_000_000.0
}

fn pairwise_sum(values: &[f64]) -> f64 {
    const NUMPY_BLOCK_SIZE: usize = 128;
    if values.len() < 8 {
        return values.iter().fold(-0.0, |sum, value| sum + value);
    }
    if values.len() <= NUMPY_BLOCK_SIZE {
        // NumPy seeds four accumulators from the first eight values, combines
        // them as a balanced tree, and then folds the block tail in order.
        // The grouping is observable in Pandas' exported profit_total_abs.
        let mut accumulators = [
            values[0] + values[1],
            values[2] + values[3],
            values[4] + values[5],
            values[6] + values[7],
        ];
        let mut index = 8;
        while index + 7 < values.len() {
            for lane in 0..4 {
                accumulators[lane] += values[index + (lane * 2)];
                accumulators[lane] += values[index + (lane * 2) + 1];
            }
            index += 8;
        }
        let mut result = (accumulators[0] + accumulators[1]) + (accumulators[2] + accumulators[3]);
        while index < values.len() {
            result += values[index];
            index += 1;
        }
        return result;
    }
    let mut middle = values.len() / 2;
    middle -= middle % 8;
    pairwise_sum(&values[..middle]) + pairwise_sum(&values[middle..])
}

fn python_float_sum(values: impl IntoIterator<Item = f64>) -> f64 {
    // This is CPython 3.14's Neumaier compensated fast path for built-in
    // sum(float_iterable). Freqtrade 2026.5.1 runs on that interpreter, and
    // total_volume is exported without decimal rounding. Keeping this small
    // implementation local makes the parity rule explicit and testable.
    let mut high = 0.0;
    let mut low = 0.0;
    for value in values {
        let next = high + value;
        if high.abs() >= value.abs() {
            low += (high - next) + value;
        } else {
            low += (value - next) + high;
        }
        high = next;
    }
    if low != 0.0 && low.is_finite() {
        high + low
    } else {
        high
    }
}

#[cfg(test)]
#[allow(clippy::float_cmp)] // These tests assert exact Freqtrade float tokens.
mod tests {
    use super::*;

    fn candle(timestamp_ms: i64, open: f64, low: f64) -> Candle {
        Candle {
            timestamp_ms,
            open,
            high: open + 10.0,
            low,
            close: open,
            volume: 1.0,
            previous_close: None,
            enter_long: None,
            enter_short: None,
            exit_long: None,
            exit_short: None,
            funding_rate: None,
            funding_mark_price: None,
            adjustment: None,
        }
    }

    fn config(max_open_trades: usize) -> PortfolioConfig {
        PortfolioConfig {
            starting_balance: 1_000.0,
            max_open_trades,
            stake_amount: 100.0,
            fee_rate: 0.001,
            fee_open_rate: None,
            fee_close_rate: None,
            leverage: None,
            stoploss_ratio: -0.01,
            amount_step: 0.00001,
            price_step: 0.01,
            custom_exit_after_ms: None,
            adjustment_rule: None,
            callback_program: None,
            stake_program: None,
            amount_reserve_percent: 0.05,
            unlimited_stake: false,
            tradable_balance_ratio: 1.0,
            entry_confirmation_program: None,
            exit_confirmation_program: None,
            custom_exit_program: None,
            adjust_trade_position_program: None,
            nfi_x7_trade_manager: None,
            max_entry_position_adjustment: -1,
            is_futures: false,
        }
    }

    fn nfi_false_program() -> ScalarDecisionProgram {
        serde_json::from_value(serde_json::json!({
            "schema_version": "1.1.0",
            "opcode": "scalar-decision-program-v1",
            "parameters": [],
            "expressions": [
                ["literal", false],
                ["literal", null],
                ["tuple", [0, 1]]
            ],
            "statements": [["return", 2]]
        }))
        .expect("valid false decision")
    }

    fn nfi_boolean_false_program() -> ScalarDecisionProgram {
        serde_json::from_value(serde_json::json!({
            "schema_version": "1.1.0",
            "opcode": "scalar-decision-program-v1",
            "parameters": [],
            "expressions": [["literal", false]],
            "statements": [["return", 0]]
        }))
        .expect("valid false predicate")
    }

    fn nfi_boolean_true_program() -> ScalarDecisionProgram {
        serde_json::from_value(serde_json::json!({
            "schema_version": "1.1.0",
            "opcode": "scalar-decision-program-v1",
            "parameters": [],
            "expressions": [["literal", true]],
            "statements": [["return", 0]]
        }))
        .expect("valid true predicate")
    }

    fn nfi_profit_program(threshold: f64, reason: &str) -> ScalarDecisionProgram {
        serde_json::from_value(serde_json::json!({
            "schema_version": "1.1.0",
            "opcode": "scalar-decision-program-v1",
            "parameters": ["current_profit"],
            "expressions": [
                ["variable", "current_profit"],
                ["literal", threshold],
                ["compare", 0, [["greater", 1]]],
                ["literal", true],
                ["literal", reason],
                ["tuple", [3, 4]],
                ["literal", false],
                ["literal", null],
                ["tuple", [6, 7]]
            ],
            "statements": [
                ["if", 2, [["return", 5]], []],
                ["return", 8]
            ]
        }))
        .expect("valid profit decision")
    }

    fn nfi_managed_route(
        key: &str,
        profile: NfiManagedLongProfile,
        mode_name: &str,
        entry_tags: &[&str],
    ) -> NfiManagedLongRoute {
        let has_dedicated_stop = matches!(
            profile,
            NfiManagedLongProfile::Rebuy
                | NfiManagedLongProfile::Rapid
                | NfiManagedLongProfile::Scalp
        );
        NfiManagedLongRoute {
            key: key.to_owned(),
            profile,
            mode_name: mode_name.to_owned(),
            entry_tags: entry_tags.iter().map(ToString::to_string).collect(),
            stop_threshold_futures: has_dedicated_stop.then_some(0.35),
            stop_threshold_spot: has_dedicated_stop.then_some(0.12),
        }
    }

    fn nfi_legacy_grind_constants() -> NfiLegacyGrindConstants {
        let tags = [
            ("gd1", "dd1"),
            ("gd2", "dd2"),
            ("gd3", "dd3"),
            ("gd4", "dd4"),
            ("gd5", "dd5"),
            ("gd6", "dd6"),
            ("dl1", "ddl1"),
            ("dl2", "ddl2"),
        ];
        NfiLegacyGrindConstants {
            max_stake_multiplier: 1.0,
            stake_multipliers_futures: vec![0.2, 0.3, 0.4, 0.5],
            stake_multipliers_spot: vec![0.2, 0.3, 0.4, 0.5, 0.6, 0.7],
            derisk_1_reentry_futures: -0.08,
            derisk_1_reentry_spot: -0.08,
            clusters: tags
                .into_iter()
                .map(|(entry_tag, stop_tag)| NfiLegacyGrindCluster {
                    entry_tag: entry_tag.to_owned(),
                    stop_tag: stop_tag.to_owned(),
                    stakes_futures: vec![0.2, 0.24, 0.28],
                    stakes_spot: vec![0.2, 0.24, 0.28],
                    thresholds_futures: vec![-0.12, -0.16, -0.20],
                    thresholds_spot: vec![-0.12, -0.16, -0.20],
                    stop_threshold_futures: -0.06,
                    stop_threshold_spot: -0.06,
                    profit_threshold_futures: 0.018,
                    profit_threshold_spot: 0.018,
                })
                .collect(),
        }
    }

    #[allow(clippy::too_many_lines)] // Full valid manager fixture is intentionally explicit.
    fn nfi_top_coins_manager(first: ScalarDecisionProgram) -> NfiX7TradeManager {
        let false_program = nfi_false_program();
        let managed_long_routes = vec![
            nfi_managed_route(
                "long_normal",
                NfiManagedLongProfile::Normal,
                "long_normal",
                &["1"],
            ),
            nfi_managed_route(
                "long_pump",
                NfiManagedLongProfile::Pump,
                "long_pump",
                &["21"],
            ),
            nfi_managed_route(
                "long_quick",
                NfiManagedLongProfile::Quick,
                "long_quick",
                &["41"],
            ),
            nfi_managed_route(
                "long_rebuy",
                NfiManagedLongProfile::Rebuy,
                "long_rebuy",
                &["61", "62", "63", "64", "65"],
            ),
            nfi_managed_route(
                "long_high_profit",
                NfiManagedLongProfile::HighProfit,
                "long_hp",
                &["81"],
            ),
            nfi_managed_route(
                "long_rapid",
                NfiManagedLongProfile::Rapid,
                "long_rapid",
                &["101"],
            ),
            nfi_managed_route(
                "long_top_coins",
                NfiManagedLongProfile::TopCoins,
                "long_tc",
                &["141", "142", "143", "144", "145"],
            ),
            nfi_managed_route(
                "long_scalp",
                NfiManagedLongProfile::Scalp,
                "long_scalp",
                &["161"],
            ),
        ];
        let adjustment_tags = managed_long_routes
            .iter()
            .flat_map(|route| route.entry_tags.clone())
            .collect();
        let mut short_rebuy_route = nfi_managed_route(
            "short_rebuy",
            NfiManagedLongProfile::Rebuy,
            "short_rebuy",
            &["561", "562", "563"],
        );
        short_rebuy_route.stop_threshold_futures = Some(1.4);
        short_rebuy_route.stop_threshold_spot = Some(0.48);
        let rebuy_constants = NfiX7RebuyConstants {
            derisk_enable: true,
            stakes_futures: vec![1.0, 1.0, 1.0, 1.0],
            stakes_spot: vec![1.0, 1.0, 1.0, 1.0],
            thresholds_futures: vec![-0.08, -0.12, -0.16, -0.20],
            thresholds_spot: vec![-0.08, -0.12, -0.16, -0.20],
            derisk_futures: -1.40,
            derisk_spot: -0.48,
        };
        NfiX7TradeManager {
            schema_version: "0.8.0".to_owned(),
            source_sha256: "a".repeat(64),
            route_order: [
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
            .map(ToOwned::to_owned)
            .collect(),
            managed_long_routes,
            short_route_order: vec!["short_rebuy".to_owned()],
            managed_short_routes: vec![short_rebuy_route],
            long_grind: None,
            long_btc: None,
            rebuy_adjustment: NfiX7RebuyAdjustment {
                enabled: true,
                entry_tags: ["61", "62", "63", "64", "65"]
                    .into_iter()
                    .map(ToOwned::to_owned)
                    .collect(),
                system_version: "system_v3_2".to_owned(),
                stateful_input_contract: serde_json::json!({"indexed_fields": {}}),
                constants: rebuy_constants.clone(),
            },
            short_rebuy_adjustment: NfiX7ShortRebuyAdjustment {
                enabled: true,
                entry_tags: ["561", "562", "563"]
                    .into_iter()
                    .map(ToOwned::to_owned)
                    .collect(),
                system_version: "system_v3_2".to_owned(),
                execution_scope: "pre-derisk-only-v1".to_owned(),
                post_derisk_action: "fail-simulation".to_owned(),
                stateful_input_contract: serde_json::json!({"indexed_fields": {}}),
                constants: rebuy_constants,
            },
            position_adjustment: Some(NfiX7PositionAdjustment {
                enabled: false,
                entry_tags: adjustment_tags,
                system_version: "system_v3_2".to_owned(),
                decision_program: "long_grind_entry_v3".to_owned(),
                program_order: [
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
                ]
                .into_iter()
                .map(ToOwned::to_owned)
                .collect(),
                stateful_input_contract: serde_json::json!({"indexed_fields": {}}),
                constants: NfiX7AdjustmentConstants {
                    derisk_enable: false,
                    max_stake_multiplier: 1.0,
                    derisk_levels: (1..=3)
                        .map(|level| NfiX7DeriskLevel {
                            level,
                            enabled: false,
                            threshold_futures: -0.1,
                            threshold_spot: -0.1,
                            stake_futures: 0.1,
                            stake_spot: 0.1,
                        })
                        .collect(),
                    grinds: (1..=5)
                        .map(|level| NfiX7GrindLevel {
                            level,
                            enabled: false,
                            use_derisk: false,
                            derisk_futures: -0.2,
                            derisk_spot: -0.2,
                            profit_threshold_futures: 0.02,
                            profit_threshold_spot: 0.02,
                            stakes_futures: vec![0.1],
                            stakes_spot: vec![0.1],
                            thresholds_futures: vec![-0.1],
                            thresholds_spot: vec![-0.1],
                        })
                        .collect(),
                },
            }),
            constants: NfiManagedLongConstants {
                stops_enable: true,
                stop_threshold_futures: 0.1,
                stop_threshold_spot: 0.1,
                system_name_use: "system_v3_2".to_owned(),
                system_v3_2_name: "system_v3_2".to_owned(),
                system_v3_2_stop_threshold_doom_futures: 0.35,
                system_v3_2_stop_threshold_doom_spot: 0.12,
                system_v3_2_stops_enable: false,
                u_e_stops_enable: false,
            },
            programs: BTreeMap::from([
                ("long_exit_signals".to_owned(), first),
                ("long_exit_main".to_owned(), false_program.clone()),
                ("long_exit_williams_r".to_owned(), false_program.clone()),
                ("long_exit_dec".to_owned(), false_program.clone()),
                ("short_exit_signals".to_owned(), false_program.clone()),
                ("short_exit_main".to_owned(), false_program.clone()),
                ("short_exit_williams_r".to_owned(), false_program.clone()),
                ("short_exit_dec".to_owned(), false_program),
                (
                    "long_grind_entry_v3".to_owned(),
                    nfi_boolean_false_program(),
                ),
            ]),
            feature_projections: OnceLock::new(),
        }
    }

    fn enable_nfi_manager(config: &mut PortfolioConfig, manager: NfiX7TradeManager) {
        config.stoploss_ratio = -0.99;
        config.callback_program = Some(CallbackProgram {
            order_filled: Some(OrderFilledProgram {
                initial_successful_entry_writes: vec![CustomDataWrite {
                    key: "system_version".to_owned(),
                    value: Value::String("system_v3_2".to_owned()),
                }],
                order_tag_actions: BTreeMap::new(),
            }),
        });
        config.nfi_x7_trade_manager = Some(manager);
    }

    fn nfi_pair(candles: Vec<Candle>, feature_columns: BTreeMap<String, Vec<Value>>) -> PairSeries {
        PairSeries {
            pair: "AAA/USDT".to_owned(),
            execution_start_index: 0,
            amount_step: None,
            price_step: None,
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: feature_columns
                .into_iter()
                .map(|(name, values)| {
                    let encoded = serde_json::to_value(values).expect("test feature values encode");
                    let column = serde_json::from_value(encoded)
                        .expect("test feature values form one homogeneous column");
                    (name, column)
                })
                .collect(),
            candles: candles.into(),
        }
    }

    #[test]
    fn adjustment_minimum_stake_uses_unleveraged_freqtrade_boundary() {
        let mut pair = nfi_pair(Vec::new(), BTreeMap::new());
        pair.minimum_amount = Some(1.0);
        pair.minimum_cost = Some(5.0);

        let adjustment_minimum = adjustment_minimum_pair_stake(&pair, 17.213, 0.05);
        let leverage_aware_minimum = minimum_pair_stake(&pair, 17.213, -0.1, 3.0, 0.05);

        // The APE futures market is amount-limited at one contract. Freqtrade
        // exposes 17.213 * 1.05 to adjust_trade_position even on a 3x trade.
        assert!((adjustment_minimum - 18.07365).abs() < 1e-12);
        assert!((leverage_aware_minimum * 3.0 - adjustment_minimum).abs() < 1e-12);
    }

    #[test]
    fn ft_precise_partial_exit_division_preserves_integer_contract() {
        let raw_amount =
            precise_product_quotient(2_913.868_487_754_348_3, 2_616.0, 2_927.296_453_135_704_3)
                .expect("valid Freqtrade partial-exit conversion");

        // These are the pinned X7 trade values immediately before order 145.
        // Unlimited rational division lands just below 2604 and loses one
        // integer contract; CCXT Precise's 18-place division lands above it.
        assert_eq!(floor_step(raw_amount, 1.0), 2_604.0);
    }

    #[test]
    fn nfi_grind_wallet_rejection_stops_source_order_evaluation() {
        const HOUR: i64 = 60 * 60 * 1_000;
        let mut entry = candle(0, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("141 ".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let adjustment_candle = candle(7 * HOUR, 90.0, 90.0);
        let mut force_exit = candle(8 * HOUR, 90.0, 90.0);
        force_exit.exit_long = Some(ExitSignal {
            reason: "force_exit".to_owned(),
        });

        let mut manager = nfi_top_coins_manager(nfi_false_program());
        manager
            .programs
            .insert("long_grind_entry_v3".to_owned(), nfi_boolean_true_program());
        let adjustment = manager
            .position_adjustment
            .as_mut()
            .expect("test manager has position adjustment");
        adjustment.enabled = true;
        // With 900 USDT already tied up, the first source-ordered grind asks
        // for 180 USDT while the wallet has less than 100 USDT available.
        // Grind 4 would fit at 45 USDT, but NFI returns None at grind 1 and
        // never evaluates that later branch.
        adjustment.constants.grinds[0].enabled = true;
        adjustment.constants.grinds[0].stakes_spot = vec![0.2];
        adjustment.constants.grinds[3].enabled = true;
        adjustment.constants.grinds[3].stakes_spot = vec![0.05];

        let mut portfolio = config(1);
        portfolio.starting_balance = 1_000.0;
        portfolio.stake_amount = 900.0;
        enable_nfi_manager(&mut portfolio, manager);
        let values = |value| vec![Value::from(value), Value::from(value), Value::from(value)];
        let mut pair = nfi_pair(
            vec![entry, adjustment_candle, force_exit],
            BTreeMap::from([
                ("RSI_3".to_owned(), values(50.0)),
                ("RSI_3_15m".to_owned(), values(50.0)),
                ("RSI_14".to_owned(), values(50.0)),
                ("close".to_owned(), values(90.0)),
                ("EMA_20".to_owned(), values(90.0)),
            ]),
        );
        pair.minimum_cost = Some(5.0);

        let result = simulate(&SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: portfolio,
            pairs: vec![pair],
        })
        .expect("wallet rejection is a normal NFI callback result");

        assert_eq!(result.trades[0].orders.len(), 2);
        assert_eq!(result.trades[0].orders[0].tag.as_deref(), Some("141 "));
        assert_eq!(
            result.trades[0].orders[1].tag.as_deref(),
            Some("force_exit")
        );
    }

    #[test]
    fn global_slot_competition_uses_pair_order() {
        let mut first = candle(1, 100.0, 100.0);
        first.enter_long = Some(EntrySignal {
            tag: Some("first".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut second = first.clone();
        second.enter_long = Some(EntrySignal {
            tag: Some("second".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(1),
            pairs: vec![
                PairSeries {
                    pair: "AAA/USDT".to_owned(),
                    execution_start_index: 0,
                    amount_step: None,
                    price_step: None,
                    price_steps: Vec::new(),
                    minimum_stake: None,
                    minimum_amount: None,
                    minimum_cost: None,
                    feature_columns: BTreeMap::new(),
                    candles: vec![first, candle(2, 100.0, 100.0)].into(),
                },
                PairSeries {
                    pair: "BBB/USDT".to_owned(),
                    execution_start_index: 0,
                    amount_step: None,
                    price_step: None,
                    price_steps: Vec::new(),
                    minimum_stake: None,
                    minimum_amount: None,
                    minimum_cost: None,
                    feature_columns: BTreeMap::new(),
                    candles: vec![second, candle(2, 100.0, 100.0)].into(),
                },
            ],
        };

        let result = simulate(&input).expect("valid simulation");

        assert_eq!(result.trades.len(), 1);
        assert_eq!(result.trades[0].pair, "AAA/USDT");
        assert_eq!(result.rejected_signals, 1);
    }

    #[test]
    fn profiled_simulation_preserves_result_and_counts_only_visible_rows() {
        let mut first = candle(1, 100.0, 100.0);
        first.enter_long = Some(EntrySignal {
            tag: Some("entry".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(1),
            pairs: vec![PairSeries {
                pair: "AAA/USDT".to_owned(),
                execution_start_index: 1,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![candle(0, 100.0, 100.0), first, candle(2, 101.0, 101.0)].into(),
            }],
        };

        let ordinary = simulate(&input).expect("valid ordinary simulation");
        let (profiled, profile) = simulate_profiled(&input).expect("valid profiled simulation");

        assert_eq!(profiled, ordinary);
        assert_eq!(profile.timestamp_batches, 2);
        assert_eq!(profile.pair_events, 2);
    }

    #[test]
    fn timerange_stop_boundary_does_not_open_a_new_trade() {
        let mut boundary = candle(2, 101.0, 100.0);
        boundary.enter_long = Some(EntrySignal {
            tag: Some("boundary".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(1),
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
                candles: vec![boundary].into(),
            }],
        };

        let result = simulate(&input).expect("valid stop-boundary candle");

        assert!(result.trades.is_empty());
        assert_eq!(result.rejected_signals, 0);
    }

    #[test]
    fn callback_context_rows_are_visible_but_never_executed() {
        let mut context = candle(1, 90.0, 90.0);
        context.enter_long = Some(EntrySignal {
            tag: Some("context-only".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut executable = candle(2, 100.0, 100.0);
        executable.enter_long = Some(EntrySignal {
            tag: Some("executable".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(1),
            pairs: vec![PairSeries {
                pair: "AAA/USDT".to_owned(),
                execution_start_index: 1,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![context, executable, candle(3, 101.0, 101.0)].into(),
            }],
        };

        let result = simulate(&input).expect("context boundary is valid");

        assert_eq!(result.trades.len(), 1);
        assert_eq!(result.trades[0].open_timestamp_ms, 2);
        assert_eq!(result.trades[0].entry_tag.as_deref(), Some("executable"));
        assert_eq!(result.rejected_signals, 0);
    }

    #[test]
    fn execution_start_index_must_point_to_a_candle() {
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(1),
            pairs: vec![PairSeries {
                pair: "AAA/USDT".to_owned(),
                execution_start_index: 1,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![candle(1, 100.0, 100.0)].into(),
            }],
        };

        assert_eq!(
            simulate(&input),
            Err(SimError::InvalidExecutionStart {
                pair: "AAA/USDT".to_owned(),
                index: 1,
                rows: 1,
            })
        );
    }

    #[test]
    fn pairwise_profit_sum_matches_numpy_reduction_order() {
        let profits = [
            13.433_598_31,
            5.716_389_78,
            8.516_438_52,
            1.152_679_260_000_020_2,
            2.817_485_03,
            2.228_106_82,
            0.982_624_96,
            0.735_159,
            2.030_196_569_999_998,
            2.782_651_25,
            2.093_312_4,
            0.941_256_3,
        ];

        assert_eq!(pairwise_sum(&profits), 43.429_898_200_000_025);
    }

    #[test]
    fn pairwise_profit_sum_matches_x7_annual_pandas_token() {
        let profits = [
            145.507_105_8,
            1_169.701_240_65,
            753.539_616,
            382.422_002_739_998_7,
            627.860_778,
            284.871_360_94,
            576.035_552,
            417.658_364_52,
            248.585_082_58,
            541.245_411_6,
            -4_831.775_913_230_002_5,
        ];

        assert_eq!(pairwise_sum(&profits), 315.650_601_599_995_75);
    }

    #[test]
    fn total_volume_uses_cpython_compensated_sum() {
        // Costs are the exact serialized values from the latest X7 tag-120
        // ZEC fixture. A naive Rust fold ends in ...00004; CPython/Freqtrade
        // exports ...9999.
        let costs = [
            32.994_561_599_999_99,
            24.689_464_799_999_996,
            39.540_500_999_999_99,
            40.349_809_499_999_99,
            39.569_630_1,
            32.969_036_1,
            33.636_302_699_999_995,
            40.446_706_299_999_995,
            39.507_467_999_999_99,
            40.566_726_199_999_99,
            39.462_122_7,
            40.322_482_199_999_996,
            12.908_996_1,
        ];

        assert_eq!(python_float_sum(costs), 456.963_807_299_999_9);
        assert_ne!(costs.into_iter().sum::<f64>(), 456.963_807_299_999_9);
    }

    #[test]
    fn nfi_top_coins_pure_decision_exits_with_the_original_entry_tag() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("141 142".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut manager_config = config(1);
        enable_nfi_manager(
            &mut manager_config,
            nfi_top_coins_manager(nfi_profit_program(0.01, "exit_long_tc_test")),
        );
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![nfi_pair(
                vec![entry, candle(2, 103.0, 102.0)],
                BTreeMap::new(),
            )],
        };

        let result = simulate(&input).expect("supported top-coins route");

        assert_eq!(result.trades.len(), 1);
        assert_eq!(result.trades[0].exit_reason, "exit_long_tc_test ( 141 142)");
        assert_eq!(result.trades[0].close_timestamp_ms, 2);
    }

    #[test]
    fn nfi_short_rebuy_runs_the_short_program_order_with_leverage() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_short = Some(EntrySignal {
            tag: Some("562 ".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut manager = nfi_top_coins_manager(nfi_false_program());
        manager.programs.insert(
            "short_exit_dec".to_owned(),
            nfi_profit_program(0.01, "exit_short_rebuy_d_3_100"),
        );
        let mut manager_config = config(1);
        manager_config.is_futures = true;
        manager_config.leverage = Some(3.0);
        enable_nfi_manager(&mut manager_config, manager);
        let mut pair = nfi_pair(vec![entry, candle(2, 90.0, 90.0)], BTreeMap::new());
        pair.minimum_cost = Some(5.0);
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![pair],
        };

        let result = simulate(&input).expect("bounded short-rebuy route");

        assert_eq!(result.trades.len(), 1);
        assert!(result.trades[0].is_short);
        assert_eq!(result.trades[0].leverage, 3.0);
        assert_eq!(
            result.trades[0].exit_reason,
            "exit_short_rebuy_d_3_100 ( 562 )"
        );
        assert_eq!(result.trades[0].close_timestamp_ms, 2);
    }

    #[test]
    fn nfi_normal_skips_profit_programs_while_initial_stake_is_negative() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("1".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut force_exit = candle(3, 99.0, 99.0);
        force_exit.exit_long = Some(ExitSignal {
            reason: "force_exit".to_owned(),
        });
        let mut manager_config = config(1);
        enable_nfi_manager(
            &mut manager_config,
            // The predicate would return true at -1%, so a custom exit here
            // would prove that the source's positive-profit guard was lost.
            nfi_top_coins_manager(nfi_profit_program(-1.0, "should_not_run")),
        );
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![nfi_pair(
                vec![entry, candle(2, 99.0, 99.0), force_exit],
                BTreeMap::new(),
            )],
        };

        let result = simulate(&input).expect("normal positive-profit guard");

        assert_eq!(result.trades[0].exit_reason, "force_exit");
    }

    #[test]
    fn nfi_quick_runs_inline_exit_after_the_common_stop_check() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("41".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let features = BTreeMap::from([
            (
                "RSI_14".to_owned(),
                vec![serde_json::json!(79.0), serde_json::json!(50.0)],
            ),
            (
                "MFI_14".to_owned(),
                vec![serde_json::json!(50.0), serde_json::json!(50.0)],
            ),
            (
                "WILLR_14".to_owned(),
                vec![serde_json::json!(-50.0), serde_json::json!(-50.0)],
            ),
            (
                "RSI_3".to_owned(),
                vec![serde_json::json!(50.0), serde_json::json!(50.0)],
            ),
            (
                "RSI_3_15m".to_owned(),
                vec![serde_json::json!(50.0), serde_json::json!(50.0)],
            ),
        ]);
        let mut manager_config = config(1);
        enable_nfi_manager(
            &mut manager_config,
            nfi_top_coins_manager(nfi_false_program()),
        );
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![nfi_pair(vec![entry, candle(2, 103.0, 103.0)], features)],
        };

        let result = simulate(&input).expect("quick inline profile exit");

        assert_eq!(result.trades[0].exit_reason, "exit_long_quick_q_1 ( 41)");
    }

    #[test]
    fn nfi_high_profit_returns_a_doom_stop_without_waiting_for_target_replay() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("81".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut manager = nfi_top_coins_manager(nfi_false_program());
        manager.constants.system_v3_2_stops_enable = true;
        manager.constants.system_v3_2_stop_threshold_doom_spot = 0.05;
        let mut manager_config = config(1);
        enable_nfi_manager(&mut manager_config, manager);
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![nfi_pair(
                vec![entry, candle(2, 94.0, 94.0)],
                BTreeMap::new(),
            )],
        };

        let result = simulate(&input).expect("high-profit immediate stop policy");

        assert_eq!(
            result.trades[0].exit_reason,
            "exit_long_hp_stoploss_doom ( 81)"
        );
        assert_eq!(result.trades[0].close_timestamp_ms, 2);
    }

    #[test]
    fn nfi_top_coins_profit_target_trails_on_the_next_candle() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("141".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let features = BTreeMap::from([
            (
                "RSI_14".to_owned(),
                vec![
                    serde_json::json!(55.0),
                    serde_json::json!(60.0),
                    serde_json::json!(40.0),
                    serde_json::json!(40.0),
                ],
            ),
            (
                "CMF_20".to_owned(),
                vec![
                    serde_json::json!(0.1),
                    serde_json::json!(0.1),
                    serde_json::json!(-0.1),
                    serde_json::json!(-0.1),
                ],
            ),
            (
                "CMF_20_1h".to_owned(),
                vec![
                    serde_json::json!(0.1),
                    serde_json::json!(0.1),
                    serde_json::json!(-0.1),
                    serde_json::json!(-0.1),
                ],
            ),
            (
                "CMF_20_4h".to_owned(),
                vec![
                    serde_json::json!(0.1),
                    serde_json::json!(0.1),
                    serde_json::json!(-0.1),
                    serde_json::json!(-0.1),
                ],
            ),
            (
                "ROC_9_4h".to_owned(),
                vec![
                    serde_json::json!(0.0),
                    serde_json::json!(0.0),
                    serde_json::json!(0.0),
                    serde_json::json!(0.0),
                ],
            ),
        ]);
        let mut manager_config = config(1);
        enable_nfi_manager(
            &mut manager_config,
            nfi_top_coins_manager(nfi_false_program()),
        );
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![nfi_pair(
                vec![
                    entry,
                    candle(2, 110.0, 109.0),
                    candle(3, 106.0, 105.0),
                    candle(4, 106.0, 105.0),
                ],
                features,
            )],
        };

        let result = simulate(&input).expect("exact top-coins trailing target");

        assert_eq!(result.trades.len(), 1);
        assert_eq!(
            result.trades[0].exit_reason,
            "exit_profit_long_tc_t_5_1_m ( 141)"
        );
        // Candle 3's indicator values are not visible until candle 4 opens.
        assert_eq!(result.trades[0].close_timestamp_ms, 4);
    }

    #[test]
    fn nfi_top_coins_doom_stop_is_reserved_then_exits_with_m_suffix() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("145".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut manager = nfi_top_coins_manager(nfi_false_program());
        manager.constants.system_v3_2_stops_enable = true;
        manager.constants.system_v3_2_stop_threshold_doom_spot = 0.05;
        let mut manager_config = config(1);
        enable_nfi_manager(&mut manager_config, manager);
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![nfi_pair(
                vec![entry, candle(2, 94.0, 93.0), candle(3, 94.0, 93.0)],
                BTreeMap::new(),
            )],
        };

        let result = simulate(&input).expect("two-phase NFI doom stop");

        assert_eq!(result.trades.len(), 1);
        assert_eq!(
            result.trades[0].exit_reason,
            "exit_long_tc_stoploss_doom_m ( 145)"
        );
        assert_eq!(result.trades[0].close_timestamp_ms, 3);
    }

    #[test]
    fn nfi_trade_manager_rejects_unsupported_entry_tags_before_simulation() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("120".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut manager_config = config(1);
        enable_nfi_manager(
            &mut manager_config,
            nfi_top_coins_manager(nfi_false_program()),
        );
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![nfi_pair(
                vec![entry, candle(2, 100.0, 100.0)],
                BTreeMap::new(),
            )],
        };

        assert!(matches!(
            simulate(&input),
            Err(SimError::UnsupportedNfiEntryTag { entry_tag, .. })
                if entry_tag == "120"
        ));
    }

    #[test]
    fn nfi_trade_manager_rejects_a_mixed_unknown_tag() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            // Rebuy is compiled, but one unknown word can still select an
            // unreviewed source branch after future strategy changes.
            tag: Some("61 999".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut manager_config = config(1);
        enable_nfi_manager(
            &mut manager_config,
            nfi_top_coins_manager(nfi_false_program()),
        );
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![nfi_pair(
                vec![entry, candle(2, 100.0, 100.0)],
                BTreeMap::new(),
            )],
        };

        assert!(matches!(
            simulate(&input),
            Err(SimError::UnsupportedNfiEntryTag { entry_tag, .. })
                if entry_tag == "61 999"
        ));
    }

    #[test]
    fn nfi_rebuy_adds_the_first_source_ladder_entry() {
        let mut entry = candle(1, 100.0, 100.0);
        // OHLC columns are read from the candle, not duplicated feature
        // storage. This is the analyzed close visible to candle 2.
        entry.close = 90.0;
        entry.enter_long = Some(EntrySignal {
            tag: Some("61".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut force_exit = candle(3, 100.0, 100.0);
        force_exit.exit_long = Some(ExitSignal {
            reason: "force_exit".to_owned(),
        });
        // Callback features are shifted by one row: the candle-2 callback
        // reads index 0, exactly as Freqtrade reads its last analyzed candle.
        let features = BTreeMap::from([
            (
                "protections_long_global".to_owned(),
                vec![
                    serde_json::json!(true),
                    serde_json::json!(true),
                    serde_json::json!(true),
                ],
            ),
            (
                "RSI_3".to_owned(),
                vec![
                    serde_json::json!(20.0),
                    serde_json::json!(20.0),
                    serde_json::json!(20.0),
                ],
            ),
            (
                "RSI_3_15m".to_owned(),
                vec![
                    serde_json::json!(20.0),
                    serde_json::json!(20.0),
                    serde_json::json!(20.0),
                ],
            ),
            (
                "AROONU_14".to_owned(),
                vec![
                    serde_json::json!(10.0),
                    serde_json::json!(10.0),
                    serde_json::json!(10.0),
                ],
            ),
            (
                "AROONU_14_15m".to_owned(),
                vec![
                    serde_json::json!(10.0),
                    serde_json::json!(10.0),
                    serde_json::json!(10.0),
                ],
            ),
            (
                "close".to_owned(),
                vec![
                    serde_json::json!(90.0),
                    serde_json::json!(100.0),
                    serde_json::json!(100.0),
                ],
            ),
            (
                "EMA_26".to_owned(),
                vec![
                    serde_json::json!(100.0),
                    serde_json::json!(100.0),
                    serde_json::json!(100.0),
                ],
            ),
        ]);
        let mut manager_config = config(1);
        enable_nfi_manager(
            &mut manager_config,
            nfi_top_coins_manager(nfi_false_program()),
        );
        let mut pair = nfi_pair(vec![entry, candle(2, 90.0, 90.0), force_exit], features);
        pair.minimum_cost = Some(5.0);
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![pair],
        };

        let result = simulate(&input).expect("reviewed rebuy ladder entry");
        let trade = &result.trades[0];

        assert_eq!(trade.orders.len(), 3);
        assert_eq!(trade.orders[1].tag.as_deref(), Some("r"));
        assert!(trade.orders[1].is_entry);
        assert_eq!(trade.orders[1].price, 90.0);
        assert_eq!(trade.exit_reason, "force_exit");
    }

    #[test]
    fn nfi_rebuy_derisk_leaves_the_exchange_minimum_reserve() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("65".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut force_exit = candle(3, 40.0, 40.0);
        force_exit.exit_long = Some(ExitSignal {
            reason: "force_exit".to_owned(),
        });
        // A false protection gate disables the entry branch. The de-risk
        // branch is intentionally independent of the indicator predicate.
        let features = BTreeMap::from([
            (
                "protections_long_global".to_owned(),
                vec![
                    serde_json::json!(false),
                    serde_json::json!(false),
                    serde_json::json!(false),
                ],
            ),
            (
                "RSI_3".to_owned(),
                vec![
                    serde_json::json!(20.0),
                    serde_json::json!(20.0),
                    serde_json::json!(20.0),
                ],
            ),
            (
                "RSI_3_15m".to_owned(),
                vec![
                    serde_json::json!(20.0),
                    serde_json::json!(20.0),
                    serde_json::json!(20.0),
                ],
            ),
            (
                "AROONU_14".to_owned(),
                vec![
                    serde_json::json!(10.0),
                    serde_json::json!(10.0),
                    serde_json::json!(10.0),
                ],
            ),
            (
                "AROONU_14_15m".to_owned(),
                vec![
                    serde_json::json!(10.0),
                    serde_json::json!(10.0),
                    serde_json::json!(10.0),
                ],
            ),
            (
                "close".to_owned(),
                vec![
                    serde_json::json!(40.0),
                    serde_json::json!(40.0),
                    serde_json::json!(40.0),
                ],
            ),
            (
                "EMA_26".to_owned(),
                vec![
                    serde_json::json!(100.0),
                    serde_json::json!(100.0),
                    serde_json::json!(100.0),
                ],
            ),
        ]);
        let mut manager_config = config(1);
        enable_nfi_manager(
            &mut manager_config,
            nfi_top_coins_manager(nfi_false_program()),
        );
        let mut pair = nfi_pair(vec![entry, candle(2, 40.0, 40.0), force_exit], features);
        pair.minimum_cost = Some(5.0);
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![pair],
        };

        let result = simulate(&input).expect("reviewed rebuy de-risk");
        let trade = &result.trades[0];

        assert_eq!(trade.orders.len(), 3);
        assert_eq!(trade.orders[1].tag.as_deref(), Some("derisk_level_3"));
        assert!(!trade.orders[1].is_entry);
        assert!(trade.stake_amount < trade.max_stake_amount);
        assert_eq!(trade.exit_reason, "force_exit");
    }

    #[test]
    fn nfi_long_grind_recovers_the_first_entry_once_with_gm0() {
        let mut entry = candle(1, 3.957, 3.957);
        entry.enter_long = Some(EntrySignal {
            tag: Some("120 ".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let recovery = candle(2, 4.037, 4.037);
        let mut force_exit = candle(3, 4.178, 4.178);
        force_exit.exit_long = Some(ExitSignal {
            reason: "force_exit".to_owned(),
        });

        let mut manager = nfi_top_coins_manager(nfi_false_program());
        manager.long_grind = Some(NfiLongGrindRoute {
            mode_name: "long_grind".to_owned(),
            entry_tags: vec!["120".to_owned()],
            exit_profit_threshold: 0.25,
            adjustment_scope: "spot-grind-backtest-v1".to_owned(),
            grind_mode: true,
            decision_program: "long_grind_entry_v3".to_owned(),
            first_entry_profit_threshold_spot: 0.018,
            first_entry_stop_threshold_spot: -0.2,
            derisk_use_grind_stops: true,
            stateful_input_contract: serde_json::json!({"indexed_fields": {}}),
            constants: nfi_legacy_grind_constants(),
        });
        manager.route_order.insert(6, "long_grind".to_owned());
        let mut manager_config = config(1);
        manager_config.price_step = 0.001;
        manager_config.amount_step = 0.01;
        enable_nfi_manager(&mut manager_config, manager);
        let mut pair = nfi_pair(vec![entry, recovery, force_exit], BTreeMap::new());
        pair.price_step = Some(0.001);
        pair.amount_step = Some(0.01);
        pair.minimum_cost = Some(5.0);
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![pair],
        };

        let result = simulate(&input).expect("reviewed long-grind recovery route");
        let trade = &result.trades[0];

        assert_eq!(trade.orders.len(), 3);
        assert_eq!(trade.orders[1].tag.as_deref(), Some("gm0"));
        assert_eq!(trade.orders[1].price, 4.037);
        assert!(!trade.orders[1].is_entry);
        assert_eq!(trade.exit_reason, "force_exit");
    }

    #[test]
    fn nfi_long_grind_opens_and_closes_a_gd1_cluster_in_source_order() {
        const HOUR: i64 = 60 * 60 * 1_000;
        let mut entry = candle(0, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("120 ".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let grind_entry = candle(25 * HOUR, 90.0, 90.0);
        let grind_exit = candle(26 * HOUR, 93.0, 93.0);
        let mut force_exit = candle(27 * HOUR, 93.0, 93.0);
        force_exit.exit_long = Some(ExitSignal {
            reason: "force_exit".to_owned(),
        });

        let mut manager = nfi_top_coins_manager(nfi_false_program());
        manager
            .programs
            .insert("long_grind_entry_v3".to_owned(), nfi_boolean_true_program());
        manager.long_grind = Some(NfiLongGrindRoute {
            mode_name: "long_grind".to_owned(),
            entry_tags: vec!["120".to_owned()],
            exit_profit_threshold: 0.25,
            adjustment_scope: "spot-grind-backtest-v1".to_owned(),
            grind_mode: true,
            decision_program: "long_grind_entry_v3".to_owned(),
            first_entry_profit_threshold_spot: 0.018,
            first_entry_stop_threshold_spot: -0.2,
            derisk_use_grind_stops: true,
            stateful_input_contract: serde_json::json!({"indexed_fields": {}}),
            constants: nfi_legacy_grind_constants(),
        });
        manager.route_order.insert(6, "long_grind".to_owned());
        let mut manager_config = config(1);
        enable_nfi_manager(&mut manager_config, manager);
        let mut pair = nfi_pair(
            vec![entry, grind_entry, grind_exit, force_exit],
            BTreeMap::new(),
        );
        pair.minimum_cost = Some(5.0);
        let result = simulate(&SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![pair],
        })
        .expect("reviewed legacy grind cluster");
        let trade = &result.trades[0];

        assert_eq!(trade.orders.len(), 4);
        assert_eq!(trade.orders[1].tag.as_deref(), Some("gd1"));
        assert!(trade.orders[1].is_entry);
        assert_eq!(
            trade.orders[2].tag.as_deref(),
            Some(format!("gd1 {}", trade.orders[1].id).as_str())
        );
        assert!(!trade.orders[2].is_entry);
        assert_eq!(trade.exit_reason, "force_exit");
    }

    #[test]
    fn nfi_long_btc_uses_its_source_ordered_profit_exit() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("121".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut manager = nfi_top_coins_manager(nfi_false_program());
        manager.long_btc = Some(NfiLongGrindRoute {
            mode_name: "long_btc".to_owned(),
            entry_tags: vec!["121".to_owned()],
            exit_profit_threshold: 0.25,
            adjustment_scope: "exit-only-v1".to_owned(),
            grind_mode: false,
            decision_program: "long_grind_entry_v3".to_owned(),
            // Keep the independent adjustment branch dormant so this test
            // isolates the source-ordered BTC custom-exit dispatcher.
            first_entry_profit_threshold_spot: 10.0,
            first_entry_stop_threshold_spot: -0.2,
            derisk_use_grind_stops: true,
            stateful_input_contract: serde_json::json!({"indexed_fields": {}}),
            constants: nfi_legacy_grind_constants(),
        });
        manager.route_order.insert(6, "long_btc".to_owned());
        let mut manager_config = config(1);
        enable_nfi_manager(&mut manager_config, manager);
        let mut pair = nfi_pair(
            vec![entry, candle(2, 126.0, 126.0), candle(3, 126.0, 126.0)],
            BTreeMap::new(),
        );
        pair.minimum_cost = Some(5.0);
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![pair],
        };

        let result = simulate(&input).expect("reviewed long-btc route");

        assert_eq!(result.trades.len(), 1);
        assert_eq!(result.trades[0].close_timestamp_ms, 2);
        assert_eq!(result.trades[0].exit_reason, "exit_long_btc_g ( 121)");
    }

    #[test]
    fn entry_adjustment_stop_and_fees_are_accounted_in_order() {
        let mut entry = candle(1, 100.0, 99.5);
        entry.enter_long = Some(EntrySignal {
            tag: Some("entry".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut adjustment = candle(2, 99.5, 99.2);
        adjustment.adjustment = Some(AdjustmentSignal {
            stake_amount: 50.0,
            tag: "rebuy".to_owned(),
        });
        let stop = candle(3, 99.0, 98.0);
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(1),
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
                candles: vec![entry, adjustment, stop].into(),
            }],
        };

        let result = simulate(&input).expect("valid simulation");
        let trade = &result.trades[0];

        assert_eq!(trade.exit_reason, "stop_loss");
        assert_eq!(trade.orders.len(), 3);
        assert_eq!(trade.orders[1].tag.as_deref(), Some("rebuy"));
        assert!(trade.profit_abs < 0.0);
        assert!((trade.close_rate - 99.0).abs() < f64::EPSILON);
    }

    #[test]
    fn negative_adjustment_realizes_a_partial_exit() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("entry".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut derisk = candle(2, 110.0, 109.0);
        derisk.adjustment = Some(AdjustmentSignal {
            stake_amount: -40.0,
            tag: "derisk".to_owned(),
        });
        let mut exit = candle(3, 120.0, 119.0);
        exit.exit_long = Some(ExitSignal {
            reason: "signal_exit".to_owned(),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(1),
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
                candles: vec![entry, derisk, exit].into(),
            }],
        };

        let result = simulate(&input).expect("valid partial exit simulation");
        let trade = &result.trades[0];

        assert_eq!(trade.orders.len(), 3);
        assert!(!trade.orders[1].is_entry);
        assert_eq!(trade.orders[1].tag.as_deref(), Some("derisk"));
        assert!(trade.stake_amount < trade.max_stake_amount);
        assert!(trade.profit_abs > 0.0);
    }

    #[test]
    fn explicit_exit_is_filled_at_candle_open() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: None,
            leverage: None,
            liquidation_price: None,
        });
        let mut exit = candle(2, 105.0, 104.0);
        exit.exit_long = Some(ExitSignal {
            reason: "custom_exit".to_owned(),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(1),
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
                candles: vec![entry, exit].into(),
            }],
        };

        let result = simulate(&input).expect("valid simulation");

        assert!((result.trades[0].close_rate - 105.0).abs() < f64::EPSILON);
        assert_eq!(result.trades[0].exit_reason, "custom_exit");
        assert!(result.final_balance > result.starting_balance);
    }

    #[test]
    fn strategy_exit_precedes_stoploss_on_the_same_freqtrade_candle() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: None,
            leverage: None,
            liquidation_price: None,
        });
        let mut exit = candle(2, 105.0, 90.0);
        exit.exit_long = Some(ExitSignal {
            reason: "custom_exit".to_owned(),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(1),
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
                candles: vec![entry, exit].into(),
            }],
        };

        let result = simulate(&input).expect("valid collision simulation");

        assert_eq!(result.trades[0].exit_reason, "custom_exit");
        assert!((result.trades[0].close_rate - 105.0).abs() < f64::EPSILON);
    }

    #[test]
    fn compiled_custom_exit_bundle_runs_inside_the_native_trade_loop() {
        let mut config = config(1);
        config.custom_exit_program = Some(
            serde_json::from_value(serde_json::json!({
                "schema_version": "1.0.0",
                "entry": "custom_exit",
                "programs": {
                    "custom_exit": {
                        "schema_version": "1.1.0",
                        "opcode": "scalar-decision-program-v1",
                        "parameters": [
                            "pair",
                            "trade",
                            "current_time",
                            "current_rate",
                            "current_profit"
                        ],
                        "expressions": [
                            ["variable", "current_profit"],
                            ["literal", 0.01],
                            ["compare", 0, [["greater", 1]]],
                            ["literal", "native_custom_exit"],
                            ["literal", null]
                        ],
                        "statements": [
                            ["if", 2, [["return", 3]], []],
                            ["return", 4]
                        ]
                    }
                }
            }))
            .expect("valid custom exit bundle"),
        );
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("test".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let exit = candle(2, 105.0, 104.0);
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config,
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
                candles: vec![entry, exit].into(),
            }],
        };

        let result = simulate(&input).expect("valid compiled custom exit");

        assert_eq!(result.trades[0].exit_reason, "native_custom_exit");
        assert!((result.trades[0].close_rate - 105.0).abs() < f64::EPSILON);
    }

    #[test]
    fn compiled_position_adjustment_bundle_adds_a_tagged_entry() {
        let mut portfolio = config(1);
        portfolio.stoploss_ratio = -0.99;
        portfolio.adjust_trade_position_program = Some(
            serde_json::from_value(serde_json::json!({
                "schema_version": "1.0.0",
                "entry": "adjust_trade_position",
                "programs": {
                    "adjust_trade_position": {
                        "schema_version": "1.1.0",
                        "opcode": "scalar-decision-program-v1",
                        "parameters": [
                            "trade",
                            "current_time",
                            "current_rate",
                            "current_profit",
                            "min_stake",
                            "max_stake",
                            "current_entry_rate",
                            "current_exit_rate",
                            "current_entry_profit",
                            "current_exit_profit"
                        ],
                        "expressions": [
                            ["variable", "current_profit"],
                            ["literal", -0.01],
                            ["compare", 0, [["less", 1]]],
                            ["literal", 50.0],
                            ["literal", "compiled_rebuy"],
                            ["tuple", [3, 4]],
                            ["literal", null]
                        ],
                        "statements": [
                            ["if", 2, [["return", 5]], []],
                            ["return", 6]
                        ]
                    }
                }
            }))
            .expect("valid adjustment bundle"),
        );
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("test".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let adjustment = candle(2, 90.0, 90.0);
        let mut exit = candle(3, 100.0, 100.0);
        exit.exit_long = Some(ExitSignal {
            reason: "done".to_owned(),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: portfolio,
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
                candles: vec![entry, adjustment, exit].into(),
            }],
        };

        let result = simulate(&input).expect("valid compiled adjustment");
        let trade = &result.trades[0];

        assert_eq!(trade.orders.len(), 3);
        assert_eq!(trade.orders[1].tag.as_deref(), Some("compiled_rebuy"));
        assert!(trade.orders[1].is_entry);
    }

    #[test]
    fn position_adjustment_receives_tradable_balance_limited_max_stake() {
        let mut portfolio = config(1);
        portfolio.starting_balance = 1_000.0;
        portfolio.stake_amount = 100.0;
        portfolio.tradable_balance_ratio = 0.99;
        portfolio.stoploss_ratio = -0.99;
        portfolio.adjust_trade_position_program = Some(
            serde_json::from_value(serde_json::json!({
                "schema_version": "1.0.0",
                "entry": "adjust_trade_position",
                "programs": {
                    "adjust_trade_position": {
                        "schema_version": "1.1.0",
                        "opcode": "scalar-decision-program-v1",
                        "parameters": [
                            "trade",
                            "current_time",
                            "current_rate",
                            "current_profit",
                            "min_stake",
                            "max_stake",
                            "current_entry_rate",
                            "current_exit_rate",
                            "current_entry_profit",
                            "current_exit_profit"
                        ],
                        "expressions": [
                            ["variable", "max_stake"],
                            ["literal", 895.0],
                            ["compare", 0, [["greater", 1]]],
                            ["literal", 50.0],
                            ["literal", "must_not_run"],
                            ["tuple", [3, 4]],
                            ["literal", null]
                        ],
                        "statements": [
                            ["if", 2, [["return", 5]], []],
                            ["return", 6]
                        ]
                    }
                }
            }))
            .expect("valid adjustment bundle"),
        );
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("test".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let adjustment = candle(2, 99.0, 99.0);
        let mut exit = candle(3, 100.0, 100.0);
        exit.exit_long = Some(ExitSignal {
            reason: "done".to_owned(),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: portfolio,
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
                candles: vec![entry, adjustment, exit].into(),
            }],
        };

        let result = simulate(&input).expect("valid tradable-balance adjustment");

        assert_eq!(result.trades[0].orders.len(), 2);
        assert!(result.trades[0]
            .orders
            .iter()
            .all(|order| order.tag.as_deref() != Some("must_not_run")));
    }

    #[test]
    fn leveraged_short_uses_side_specific_orders_and_funding() {
        let mut entry = candle(1, 100.0, 99.0);
        entry.enter_short = Some(EntrySignal {
            tag: Some("short".to_owned()),
            leverage: Some(3.0),
            liquidation_price: Some(130.0),
        });
        let mut exit = candle(2, 90.0, 89.0);
        exit.high = 91.0;
        exit.funding_rate = Some(0.001);
        exit.funding_mark_price = Some(90.0);
        exit.exit_short = Some(ExitSignal {
            reason: "signal_exit".to_owned(),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(1),
            pairs: vec![PairSeries {
                pair: "AAA/USDT:USDT".to_owned(),
                execution_start_index: 0,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![entry, exit].into(),
            }],
        };

        let result = simulate(&input).expect("valid short simulation");
        let trade = &result.trades[0];

        assert!(trade.is_short);
        assert!((trade.leverage - 3.0).abs() < f64::EPSILON);
        assert_eq!(trade.orders[0].side, OrderSide::Sell);
        assert_eq!(trade.orders[1].side, OrderSide::Buy);
        assert!(trade.funding_fees > 0.0);
        assert!(trade.profit_abs > 0.0);
    }

    #[test]
    fn ape_short_funding_and_profit_match_freqtrade_2026_5_1() {
        let mut portfolio = config(1);
        portfolio.starting_balance = 10_000.0;
        portfolio.stake_amount = 3_236.574;
        portfolio.fee_rate = 0.0005;
        portfolio.fee_open_rate = Some(0.0005);
        portfolio.fee_close_rate = Some(0.0005);
        portfolio.leverage = Some(3.0);
        portfolio.stoploss_ratio = -0.99;
        portfolio.amount_step = 1.0;
        portfolio.price_step = 0.001;
        portfolio.is_futures = true;

        let mut entry = candle(1_654_801_500_000, 5.742, 5.74);
        entry.high = 5.758;
        entry.enter_short = Some(EntrySignal {
            tag: Some("562 ".to_owned()),
            leverage: Some(3.0),
            liquidation_price: None,
        });
        let mut funding = candle(1_654_819_200_000, 5.721, 5.525_74);
        funding.high = 5.738_839;
        funding.funding_rate = Some(0.000_020_67);
        funding.funding_mark_price = Some(5.721);
        let mut exit = candle(1_654_820_400_000, 5.568, 5.5);
        exit.high = 5.58;
        exit.exit_short = Some(ExitSignal {
            reason: "exit_short_rebuy_d_3_100 ( 562 )".to_owned(),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: portfolio,
            pairs: vec![PairSeries {
                pair: "APE/USDT:USDT".to_owned(),
                execution_start_index: 0,
                amount_step: Some(1.0),
                price_step: Some(0.001),
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: Some(5.0),
                feature_columns: BTreeMap::new(),
                candles: vec![entry, funding, exit].into(),
            }],
        };

        let result = simulate(&input).expect("valid APE short simulation");
        let trade = &result.trades[0];

        assert_eq!(trade.amount, 1_691.0);
        assert_eq!(trade.funding_fees, 0.199_965_941_37);
        assert!((trade.profit_abs - 284.871_360_94).abs() < 1e-10);
        assert!((trade.profit_ratio - 0.088_060_358_846_711_66).abs() < 1e-14);
    }

    #[test]
    fn explicit_short_liquidation_price_has_priority_over_stop() {
        let mut portfolio = config(1);
        portfolio.exit_confirmation_program = Some(
            serde_json::from_value(serde_json::json!({
                "statements": [{
                    "op": "return",
                    "value": {"op": "literal", "value": false}
                }],
                "functions": {}
            }))
            .expect("valid rejecting confirmation program"),
        );
        let mut entry = candle(1, 100.0, 99.0);
        entry.enter_short = Some(EntrySignal {
            tag: None,
            leverage: Some(5.0),
            liquidation_price: Some(105.0),
        });
        let mut liquidated = candle(2, 104.0, 103.0);
        liquidated.high = 110.0;
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: portfolio,
            pairs: vec![PairSeries {
                pair: "AAA/USDT:USDT".to_owned(),
                execution_start_index: 0,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![entry, liquidated].into(),
            }],
        };

        let result = simulate(&input).expect("valid liquidation simulation");

        assert_eq!(result.trades[0].exit_reason, "liquidation");
        assert!((result.trades[0].close_rate - 105.0).abs() < f64::EPSILON);
    }

    #[test]
    fn order_filled_program_updates_compiled_trade_custom_state() {
        let mut config = config(1);
        config.callback_program = Some(CallbackProgram {
            order_filled: Some(OrderFilledProgram {
                initial_successful_entry_writes: vec![CustomDataWrite {
                    key: "system_version".to_owned(),
                    value: Value::String("system_v3_2".to_owned()),
                }],
                order_tag_actions: BTreeMap::from([(
                    "grind_1_exit".to_owned(),
                    vec![
                        CustomDataWrite {
                            key: "grind_1_cluster_max_profit_stake".to_owned(),
                            value: serde_json::json!(0.0),
                        },
                        CustomDataWrite {
                            key: "grind_1_cluster_max_profit_rate".to_owned(),
                            value: serde_json::json!(0.0),
                        },
                    ],
                )]),
            }),
        });
        let pair = PairSeries {
            pair: "AAA/USDT".to_owned(),
            execution_start_index: 0,
            amount_step: None,
            price_step: None,
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: BTreeMap::new(),
            candles: vec![candle(1, 100.0, 99.0)].into(),
        };
        let signal = EntrySignal {
            tag: Some("grind_1_exit detail".to_owned()),
            leverage: None,
            liquidation_price: None,
        };
        let entry_candle = pair.candles.get(0).expect("fixture candle");

        let trade = enter_trade(
            EntryRequest {
                pair_index: 0,
                pair: &pair,
                candle: &entry_candle,
                side: TradeSide::Long,
                signal: &signal,
                stake: EntryStake {
                    proposed: 100.0,
                    maximum: 1_000.0,
                },
                open_trades: &[],
                id: 1,
                order_id: 1,
            },
            &config,
        )
        .expect("valid entry")
        .expect("sized entry");

        assert_eq!(
            trade.custom_data.get("system_version"),
            Some(&Value::String("system_v3_2".to_owned()))
        );
        assert_eq!(
            trade.custom_data.get("grind_1_cluster_max_profit_stake"),
            Some(&serde_json::json!(0.0))
        );
    }

    #[test]
    fn bounded_stake_vm_applies_tag_rule_and_exchange_minimum() {
        let program: StakeProgram = serde_json::from_value(serde_json::json!({
            "statements": [
                {
                    "op": "let",
                    "name": "enter_tags",
                    "value": {
                        "op": "split_words",
                        "value": {"op": "variable", "name": "entry_tag"}
                    }
                },
                {
                    "op": "if",
                    "condition": {
                        "op": "all_in",
                        "items": {"op": "variable", "name": "enter_tags"},
                        "container": {"op": "literal", "value": ["61", "62"]}
                    },
                    "then": [{
                        "op": "return",
                        "value": {
                            "op": "stake_clamp_min",
                            "multiplier": {"op": "literal", "value": 0.25}
                        }
                    }],
                    "otherwise": []
                },
                {
                    "op": "return",
                    "value": {"op": "variable", "name": "proposed_stake"}
                }
            ]
        }))
        .expect("valid stake program");

        let stake = evaluate_stake_program(
            &program,
            &StakeInputs {
                proposed_stake: 100.0,
                minimum_stake: 30.0,
                maximum_stake: 1_000.0,
                current_rate: 100.0,
                leverage: 1.0,
                entry_tag: Some("61"),
                side: TradeSide::Long,
            },
        )
        .expect("stake result");

        assert!((stake - 30.0).abs() < f64::EPSILON);
    }

    #[test]
    fn entry_confirmation_vm_evaluates_tag_and_slippage_gates() {
        let program: ConfirmProgram = serde_json::from_value(serde_json::json!({
            "statements": [
                {
                    "op": "let",
                    "name": "entry_tags",
                    "value": {
                        "op": "split_words",
                        "value": {"op": "variable", "name": "entry_tag"}
                    }
                },
                {
                    "op": "if",
                    "condition": {
                        "op": "all_in",
                        "items": {"op": "variable", "name": "entry_tags"},
                        "container": {"op": "literal", "value": ["120"]}
                    },
                    "then": [{
                        "op": "return",
                        "value": {"op": "literal", "value": false}
                    }],
                    "otherwise": []
                },
                {
                    "op": "if",
                    "condition": {
                        "op": "greater",
                        "left": {"op": "variable", "name": "rate"},
                        "right": {"op": "literal", "value": 102.0}
                    },
                    "then": [{
                        "op": "return",
                        "value": {"op": "literal", "value": false}
                    }],
                    "otherwise": []
                },
                {
                    "op": "return",
                    "value": {"op": "literal", "value": true}
                }
            ],
            "functions": {}
        }))
        .expect("valid confirmation program");
        let open_trades = Vec::new();
        let base = ConfirmInputs {
            pair: "BTC/USDT",
            timestamp_ms: 1,
            amount: 0.99,
            rate: 101.0,
            entry_tag: Some("61"),
            side: TradeSide::Long,
            previous_close: Some(100.0),
            open_trades: &open_trades,
            max_open_trades: 6,
        };

        assert_eq!(evaluate_confirm_program(&program, base), Some(true));
        assert_eq!(
            evaluate_confirm_program(
                &program,
                ConfirmInputs {
                    entry_tag: Some("120"),
                    ..base
                },
            ),
            Some(false)
        );
        assert_eq!(
            evaluate_confirm_program(
                &program,
                ConfirmInputs {
                    rate: 103.0,
                    ..base
                },
            ),
            Some(false)
        );
    }

    #[test]
    fn entry_confirmation_vm_accepts_a_computed_negative_dataframe_index() {
        let program: ConfirmProgram = serde_json::from_value(serde_json::json!({
            "statements": [
                {
                    "op": "let",
                    "name": "df",
                    "value": {"op": "analyzed_frame"}
                },
                {
                    "op": "let",
                    "name": "last_candle",
                    "value": {
                        "op": "index",
                        "value": {"op": "variable", "name": "df"},
                        "index": {
                            "op": "negative",
                            "value": {"op": "literal", "value": 1}
                        }
                    }
                },
                {
                    "op": "return",
                    "value": {
                        "op": "less",
                        "left": {
                            "op": "field",
                            "value": {"op": "variable", "name": "last_candle"},
                            "name": "close"
                        },
                        "right": {"op": "variable", "name": "rate"}
                    }
                }
            ],
            "functions": {}
        }))
        .expect("valid analyzed-frame confirmation program");
        let open_trades = Vec::new();
        let inputs = ConfirmInputs {
            pair: "APE/USDT",
            timestamp_ms: 1,
            amount: 1.0,
            rate: 101.0,
            entry_tag: Some("62"),
            side: TradeSide::Long,
            previous_close: Some(100.0),
            open_trades: &open_trades,
            max_open_trades: 6,
        };

        assert_eq!(evaluate_confirm_program(&program, inputs), Some(true));
    }

    #[test]
    fn exit_confirmation_vm_rejects_spot_stop_and_emits_clear_effect() {
        let program: ConfirmProgram = serde_json::from_value(serde_json::json!({
            "statements": [
                {
                    "op": "if",
                    "condition": {
                        "op": "contains",
                        "container": {
                            "op": "literal",
                            "value": ["stop_loss", "trailing_stop_loss"]
                        },
                        "value": {"op": "variable", "name": "exit_reason"}
                    },
                    "then": [{
                        "op": "return",
                        "value": {"op": "literal", "value": false}
                    }],
                    "otherwise": []
                },
                {
                    "op": "clear_profit_target",
                    "pair": {"op": "variable", "name": "pair"}
                },
                {
                    "op": "return",
                    "value": {"op": "literal", "value": true}
                }
            ],
            "functions": {}
        }))
        .expect("valid exit confirmation program");
        let config = config(1);
        let pair = PairSeries {
            pair: "AAA/USDT".to_owned(),
            execution_start_index: 0,
            amount_step: None,
            price_step: None,
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: BTreeMap::new(),
            candles: vec![candle(1, 100.0, 99.0)].into(),
        };
        let signal = EntrySignal {
            tag: Some("61".to_owned()),
            leverage: None,
            liquidation_price: None,
        };
        let entry_candle = pair.candles.get(0).expect("fixture candle");
        let trade = enter_trade(
            EntryRequest {
                pair_index: 0,
                pair: &pair,
                candle: &entry_candle,
                side: TradeSide::Long,
                signal: &signal,
                stake: EntryStake {
                    proposed: 100.0,
                    maximum: 1_000.0,
                },
                open_trades: &[],
                id: 1,
                order_id: 1,
            },
            &config,
        )
        .expect("valid entry")
        .expect("sized entry");

        assert_eq!(
            evaluate_exit_confirm_program(&program, &trade, 1, 99.0, "stop_loss", &config),
            Some((false, false))
        );
        assert_eq!(
            evaluate_exit_confirm_program(&program, &trade, 2, 101.0, "custom_exit", &config),
            Some((true, true))
        );
    }

    #[test]
    fn scalar_decision_vm_evaluates_chained_comparison_and_formatted_reason() {
        let program: ScalarDecisionProgram = serde_json::from_value(serde_json::json!({
            "schema_version": "1.0.0",
            "opcode": "scalar-decision-program-v1",
            "parameters": ["mode", "current_profit", "last_candle"],
            "expressions": [
                ["literal", "RSI_14"],
                ["variable", "last_candle"],
                ["index", 1, 0],
                ["variable", "current_profit"],
                ["literal", 0.01],
                ["literal", 0.001],
                ["compare", 4, [["greater", 3], ["greater-equal", 5]]],
                ["is-instance", 2, "np.float64"],
                ["literal", 80.0],
                ["compare", 2, [["greater", 8]]],
                ["and", [6, 7, 9]],
                ["literal", true],
                ["variable", "mode"],
                ["format", [["text", "exit_"], ["value", 12], ["text", "_0_1"]]],
                ["tuple", [11, 13]],
                ["literal", false],
                ["literal", null],
                ["tuple", [15, 16]]
            ],
            "statements": [
                ["set", "last_rsi", 2],
                ["if", 10, [["return", 14]], []],
                ["return", 17]
            ]
        }))
        .expect("valid scalar decision program");
        let inputs = BTreeMap::from([
            ("mode".to_owned(), Value::String("normal".to_owned())),
            ("current_profit".to_owned(), serde_json::json!(0.005)),
            (
                "last_candle".to_owned(),
                serde_json::json!({"RSI_14": 85.0}),
            ),
        ]);

        assert_eq!(
            evaluate_scalar_decision_program(&program, inputs),
            Some(serde_json::json!([true, "exit_normal_0_1"]))
        );
        let nan_inputs = BTreeMap::from([
            ("mode".to_owned(), Value::String("normal".to_owned())),
            ("current_profit".to_owned(), serde_json::json!(0.005)),
            (
                "last_candle".to_owned(),
                serde_json::json!({"RSI_14": {"$float": "nan"}}),
            ),
        ]);
        assert_eq!(
            evaluate_scalar_decision_program(&program, nan_inputs),
            Some(serde_json::json!([false, null]))
        );
    }

    #[test]
    fn scalar_decision_vm_resolves_transitive_program_calls_fail_closed() {
        let entry_program: ScalarDecisionProgram = serde_json::from_value(serde_json::json!({
            "schema_version": "1.1.0",
            "opcode": "scalar-decision-program-v1",
            "parameters": ["mode", "current_profit"],
            "expressions": [
                ["variable", "mode"],
                ["variable", "current_profit"],
                ["call-program", "decide", [0, 1]]
            ],
            "statements": [["return", 2]]
        }))
        .expect("valid caller");
        let decision_program: ScalarDecisionProgram = serde_json::from_value(serde_json::json!({
        "schema_version": "1.1.0",
        "opcode": "scalar-decision-program-v1",
        "parameters": ["mode", "current_profit"],
        "expressions": [
            ["variable", "current_profit"],
            ["literal", 0.1],
            ["compare", 0, [["greater", 1]]],
            ["literal", true],
            ["variable", "mode"],
            ["format", [["text", "exit_"], ["value", 4]]],
            ["tuple", [3, 5]],
            ["literal", false],
            ["literal", null],
            ["tuple", [7, 8]]
        ],
        "statements": [
            ["if", 2, [["return", 6]], []],
            ["return", 9]
        ]
        }))
        .expect("valid decision program");
        let programs = BTreeMap::from([
            ("custom_exit".to_owned(), entry_program.clone()),
            ("decide".to_owned(), decision_program),
        ]);
        let inputs = BTreeMap::from([
            ("mode".to_owned(), Value::String("normal".to_owned())),
            ("current_profit".to_owned(), serde_json::json!(0.2)),
        ]);

        assert_eq!(
            evaluate_scalar_program_bundle(&programs, "custom_exit", inputs.clone()),
            Some(serde_json::json!([true, "exit_normal"]))
        );
        assert_eq!(
            evaluate_scalar_decision_program(&entry_program, inputs),
            None
        );
        assert_eq!(
            evaluate_scalar_program_bundle(&programs, "missing", BTreeMap::new()),
            None
        );
    }

    #[test]
    fn scalar_decision_vm_preserves_first_match_for_flat_elif_chains() {
        let program: ScalarDecisionProgram = serde_json::from_value(serde_json::json!({
            "schema_version": "1.2.0",
            "opcode": "scalar-decision-program-v1",
            "parameters": ["score"],
            "expressions": [
                ["variable", "score"],
                ["literal", 1.0],
                ["compare", 0, [["less", 1]]],
                ["literal", "first"],
                ["literal", 3.0],
                ["compare", 0, [["less", 4]]],
                ["literal", "second"],
                ["literal", "fallback"]
            ],
            "statements": [
                ["if-chain", [
                    [2, [["return", 3]]],
                    [5, [["return", 6]]]
                ], [["return", 7]]]
            ]
        }))
        .expect("valid flat elif program");

        assert_eq!(
            evaluate_scalar_decision_program(
                &program,
                BTreeMap::from([("score".to_owned(), serde_json::json!(2.0))]),
            ),
            Some(Value::String("second".to_owned()))
        );
    }

    #[test]
    fn callback_feature_index_selects_the_last_closed_analyzed_row() {
        assert_eq!(callback_feature_index(0), None);
        assert_eq!(callback_feature_index(1), Some(0));
        assert_eq!(callback_feature_index(42), Some(41));
    }

    #[test]
    fn exchange_step_quantization_uses_decimal_ticks() {
        assert_eq!(floor_step(8.45, 0.01), 8.45);
        assert_eq!(floor_step(0.459_999_999_999_999_1, 0.01), 0.45);
        assert_eq!(ceil_step(0.044_361, 0.0001), 0.0444);
        assert_eq!(round_step(20.562_49, 0.0001), 20.5625);
    }

    #[test]
    fn pair_price_step_selects_the_latest_historical_change() {
        let mut pair = nfi_pair(vec![candle(10, 5.0, 4.0)], BTreeMap::new());
        pair.price_step = Some(0.0001);
        pair.price_steps = vec![
            PriceStepChange {
                timestamp_ms: 1,
                step: 0.0001,
            },
            PriceStepChange {
                timestamp_ms: 9,
                step: 0.001,
            },
        ];

        assert_eq!(
            pair_price_step(&pair, &pair.candles.get(0).expect("fixture candle"), 0.01),
            0.001
        );
        assert_eq!(pair_price_step(&pair, &candle(5, 5.0, 4.0), 0.01), 0.0001);
    }

    #[test]
    fn columnar_features_reconstruct_the_exact_selected_and_previous_rows() {
        let pair = PairSeries {
            pair: "AAA/USDT".to_owned(),
            execution_start_index: 0,
            amount_step: None,
            price_step: None,
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: BTreeMap::from([
                (
                    "RSI_14".to_owned(),
                    FeatureColumn::numbers(vec![41.0, f64::NAN]),
                ),
                (
                    "protections_long_global".to_owned(),
                    FeatureColumn::booleans(vec![false, true]),
                ),
            ]),
            candles: vec![candle(1, 100.0, 100.0), candle(2, 101.0, 101.0)].into(),
        };
        let mut variables = BTreeMap::new();

        insert_feature_window(&mut variables, &pair, 1).expect("aligned feature window");

        assert_eq!(
            variables["last_candle"],
            serde_json::json!({
                "open": 101.0,
                "high": 111.0,
                "low": 101.0,
                "close": 101.0,
                "volume": 1.0,
                "RSI_14": {"$float": "nan"},
                "protections_long_global": true
            })
        );
        assert_eq!(variables["previous_candle"]["RSI_14"], 41.0);
        assert_eq!(variables["previous_candle_1"], variables["previous_candle"]);
        assert_eq!(variables["previous_candle_2"], Value::Null);
    }

    #[test]
    fn nfi_profit_snapshot_uses_filled_order_cashflows_and_first_entry_basis() {
        let config = config(1);
        let pair = PairSeries {
            pair: "AAA/USDT".to_owned(),
            execution_start_index: 0,
            amount_step: None,
            price_step: None,
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: BTreeMap::new(),
            candles: vec![candle(1, 100.0, 100.0)].into(),
        };
        let signal = EntrySignal {
            tag: Some("141".to_owned()),
            leverage: None,
            liquidation_price: None,
        };
        let entry_candle = pair.candles.get(0).expect("fixture candle");
        let mut trade = enter_trade(
            EntryRequest {
                pair_index: 0,
                pair: &pair,
                candle: &entry_candle,
                side: TradeSide::Long,
                signal: &signal,
                stake: EntryStake {
                    proposed: 100.0,
                    maximum: 1_000.0,
                },
                open_trades: &[],
                id: 1,
                order_id: 1,
            },
            &config,
        )
        .expect("valid entry")
        .expect("sized entry");
        let first = trade.orders[0].clone();
        let exit_amount = first.amount * 0.25;
        trade.orders.push(FilledOrder {
            id: 2,
            funding_fee: 0.0,
            sequence: 1,
            side: OrderSide::Sell,
            is_entry: false,
            filled_timestamp_ms: 2,
            amount: exit_amount,
            price: 110.0,
            cost: exit_amount * 110.0,
            tag: Some("d1".to_owned()),
        });

        let snapshot =
            nfi_profit_snapshot(&trade, 105.0, fee_open(&config), fee_close(&config), false)
                .expect("open amount remains");
        let entry_stake = first.amount * first.price * (1.0 + fee_open(&config));
        let exit_stake = exit_amount * 110.0 * (1.0 - fee_close(&config));
        let current_stake = (first.amount - exit_amount) * 105.0 * (1.0 - fee_close(&config));
        let expected = -entry_stake + exit_stake + current_stake;

        assert!((snapshot.stake - expected).abs() < 1e-12);
        assert!((snapshot.ratio - expected / entry_stake).abs() < 1e-12);
        assert!((snapshot.current_stake_ratio - expected / current_stake).abs() < 1e-12);
        assert!(
            (snapshot.initial_stake_ratio - expected / (first.amount * first.price)).abs() < 1e-12
        );
    }

    #[test]
    fn entry_confirmation_receives_post_precision_amount() {
        let mut config = config(1);
        config.entry_confirmation_program = Some(
            serde_json::from_value(serde_json::json!({
                "statements": [{
                    "op": "return",
                    "value": {
                        "op": "greater",
                        "left": {"op": "variable", "name": "amount"},
                        "right": {"op": "literal", "value": 0.9}
                    }
                }],
                "functions": {}
            }))
            .expect("valid confirmation program"),
        );
        let mut first = candle(1, 100.0, 100.0);
        first.enter_long = Some(EntrySignal {
            tag: Some("141".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut second = candle(2, 101.0, 101.0);
        second.exit_long = Some(ExitSignal {
            reason: "signal_exit".to_owned(),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config,
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
                candles: vec![first, second].into(),
            }],
        };

        let result = simulate(&input).expect("simulation succeeds");

        assert_eq!(result.trades.len(), 1);
    }
}
