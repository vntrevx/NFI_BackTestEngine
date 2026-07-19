//! Exact NFI X7 system-v3.2 position adjustment for managed long routes.
//!
//! The strategy intentionally reconstructs each open grind cluster from filled
//! orders on every candle. We do the same here. A compact cached cluster would
//! be faster, but it would add state that Freqtrade and NFI do not own and make
//! partial-exit/order-tag edge cases harder to prove.

use super::{
    adjustment_minimum_pair_stake, evaluate_scalar_program_bundle, feature_number_at, fee_close,
    fee_open, insert_projected_feature_window, nfi_profit_snapshot, number_value,
    scalar_trade_value, scalar_truthy, AdjustmentSignal, BTreeMap, Candle, NfiProfitSnapshot,
    NfiX7GrindLevel, NfiX7PositionAdjustment, NfiX7TradeManager, OpenTrade, PairSeries,
    PortfolioConfig, TradeSide, Value,
};

const FIVE_MINUTES_MS: i64 = 5 * 60 * 1_000;
const SIX_HOURS_MS: i64 = 6 * 60 * 60 * 1_000;

#[derive(Debug, Default)]
struct GrindCluster {
    count: usize,
    total_amount: f64,
    total_cost: f64,
    entry_ids: Vec<u64>,
    latest_entry_price: Option<f64>,
    exit_price: Option<f64>,
    open_rate: f64,
    current_stake: f64,
    profit_stake: f64,
    profit_rate: f64,
}

impl GrindCluster {
    fn finish(&mut self, rate: f64, close_fee: f64) {
        if self.count == 0 {
            return;
        }
        self.open_rate = self.total_cost / self.total_amount;
        self.current_stake = self.total_amount * rate * (1.0 - close_fee);
        self.profit_stake = self.current_stake - self.total_cost;
        self.profit_rate = (rate - self.open_rate) / self.open_rate;
    }

    fn distance(&self, rate: f64) -> f64 {
        self.latest_entry_price
            .map_or(0.0, |price| (rate - price) / price)
    }
}

#[derive(Debug)]
struct AdjustmentState {
    clusters: [GrindCluster; 5],
    derisk_found: [bool; 3],
    first_entry_amount: f64,
    first_entry_cost: f64,
    latest_entry_price: f64,
    latest_entry_timestamp_ms: i64,
    latest_exit_price: Option<f64>,
    latest_order_price: f64,
    latest_order_timestamp_ms: i64,
}

struct AdjustmentContext<'a> {
    adjustment: &'a NfiX7PositionAdjustment,
    pair: &'a PairSeries,
    candle_index: usize,
    candle: &'a Candle,
    config: &'a PortfolioConfig,
    available_balance: f64,
    minimum_stake: f64,
    snapshot: NfiProfitSnapshot,
    slice_amount: f64,
    slice_profit_entry: f64,
    current_stake_amount: f64,
    is_long_grind_entry: bool,
    extra_entry_checks: bool,
}

/// Source-order result for one grind level.
///
/// NFI has two different no-order paths which must not be collapsed:
/// `Continue` means this grind level did not match and evaluation may proceed
/// to the next level. `ReturnNone` models an explicit strategy `return None`
/// (notably when a matched entry is larger than Freqtrade's `max_stake`) and
/// must stop the callback immediately.
enum GrindLevelOutcome {
    Continue,
    ReturnNone,
    Signal(AdjustmentSignal),
}

/// Evaluate the source-bound adjustment callback.
///
/// The outer `Option` is the evaluator validity boundary. The inner `Option`
/// is the callback's ordinary `None` result.
#[allow(clippy::option_option)] // Outer None is invalid IR; inner None is callback no-op.
pub(super) fn evaluate_nfi_position_adjustment(
    manager: &NfiX7TradeManager,
    trade: &mut OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    available_balance: f64,
) -> Option<Option<AdjustmentSignal>> {
    let adjustment = manager.position_adjustment.as_ref();
    if adjustment.is_none_or(|adjustment| !adjustment.enabled) {
        return Some(None);
    }
    let adjustment = adjustment?;
    if trade.side != TradeSide::Long || !nfi_adjustment_supports_trade(adjustment, trade) {
        return None;
    }
    if trade.custom_data.get("system_version")?.as_str()? != adjustment.system_version {
        return None;
    }

    let minimum_stake = adjustment_minimum_stake(pair, candle, trade, config)?;
    let snapshot = nfi_profit_snapshot(
        trade,
        candle.open,
        fee_open(config),
        fee_close(config),
        config.is_futures,
    )?;
    let state = rebuild_adjustment_state(trade, candle.open, fee_close(config))?;
    let slice_amount = state.first_entry_cost;
    let slice_profit = price_distance(candle.open, state.latest_order_price)?;
    let slice_profit_entry = price_distance(candle.open, state.latest_entry_price)?;
    let slice_profit_exit = state
        .latest_exit_price
        .and_then(|price| price_distance(candle.open, price))
        .unwrap_or(0.0);
    let is_long_grind_entry = evaluate_grind_entry_program(
        manager,
        &adjustment.decision_program,
        trade,
        pair,
        candle_index,
        candle,
        state
            .clusters
            .iter()
            .map(|cluster| cluster.count)
            .sum::<usize>(),
        slice_profit,
        slice_profit_entry,
        slice_profit_exit,
    )?;
    let extra_entry_checks = candle.timestamp_ms - FIVE_MINUTES_MS
        > state.latest_entry_timestamp_ms
        && (candle.timestamp_ms - SIX_HOURS_MS > state.latest_order_timestamp_ms
            || slice_profit < -0.06
            || state.derisk_found[2]);

    // X7 reads the previous maxima for this invocation, then persists any new
    // maxima before evaluating exits. `long_grind_exit_v3` currently has its
    // trailing branch disabled, but preserving the write order protects the
    // order_filled reset contract and future proof fixtures.
    let previous_maxima = read_and_update_cluster_maxima(trade, &state.clusters);
    let context = AdjustmentContext {
        adjustment,
        pair,
        candle_index,
        candle,
        config,
        available_balance,
        minimum_stake,
        snapshot,
        slice_amount,
        slice_profit_entry,
        current_stake_amount: trade.amount * candle.open,
        is_long_grind_entry,
        extra_entry_checks,
    };

    if let Some(adjustment) = evaluate_derisk_levels(&context, trade, &state)? {
        return Some(Some(adjustment));
    }
    for index in 0..state.clusters.len() {
        match evaluate_grind_level(&context, trade, &state, &previous_maxima, index)? {
            GrindLevelOutcome::Continue => {}
            GrindLevelOutcome::ReturnNone => return Some(None),
            GrindLevelOutcome::Signal(adjustment) => return Some(Some(adjustment)),
        }
    }
    Some(None)
}

fn nfi_adjustment_supports_trade(adjustment: &NfiX7PositionAdjustment, trade: &OpenTrade) -> bool {
    let entry_tag = trade.entry_tag.as_deref().unwrap_or("");
    entry_tag.split_whitespace().any(|word| {
        adjustment
            .entry_tags
            .iter()
            .any(|supported| supported == word)
    })
}

fn adjustment_minimum_stake(
    pair: &PairSeries,
    candle: &Candle,
    _trade: &OpenTrade,
    config: &PortfolioConfig,
) -> Option<f64> {
    let has_limit = pair.minimum_stake.is_some()
        || pair.minimum_amount.is_some()
        || pair.minimum_cost.is_some();
    has_limit
        .then(|| adjustment_minimum_pair_stake(pair, candle.open, config.amount_reserve_percent))
}

fn rebuild_adjustment_state(
    trade: &OpenTrade,
    rate: f64,
    close_fee: f64,
) -> Option<AdjustmentState> {
    let first = trade.orders.first()?;
    let latest = trade.orders.last()?;
    let latest_entry = trade.orders.iter().rev().find(|order| order.is_entry)?;
    let latest_exit = trade.orders.iter().rev().find(|order| !order.is_entry);
    let mut clusters: [GrindCluster; 5] = std::array::from_fn(|_| GrindCluster::default());
    let mut cluster_closed = [false; 5];
    let mut derisk_found = [false; 3];

    // Reversed traversal is observable: exit tags list still-open entry IDs
    // newest first, matching NFI's `reversed(filled_orders)` loop.
    for order in trade.orders.iter().rev() {
        let tag = order.tag.as_deref().unwrap_or("");
        if order.is_entry && order.sequence != 0 {
            if let Some(index) = grind_entry_index(tag) {
                if !cluster_closed[index] {
                    let cluster = &mut clusters[index];
                    cluster.count += 1;
                    cluster.total_amount += order.amount;
                    cluster.total_cost += order.amount * order.price;
                    cluster.entry_ids.push(order.id);
                    cluster.latest_entry_price.get_or_insert(order.price);
                }
            }
            continue;
        }
        if order.is_entry {
            continue;
        }
        let head = tag.split_whitespace().next().unwrap_or("");
        if let Some(index) = derisk_level_index(head) {
            derisk_found[index] = true;
        } else if let Some(index) = grind_exit_index(head) {
            if !cluster_closed[index] {
                cluster_closed[index] = true;
                clusters[index].exit_price = Some(order.price);
            }
        } else if head == "derisk_global" {
            for (closed, cluster) in cluster_closed.iter_mut().zip(&mut clusters) {
                if !*closed {
                    *closed = true;
                    cluster.exit_price = Some(order.price);
                }
            }
        }
    }
    for cluster in &mut clusters {
        cluster.finish(rate, close_fee);
    }
    Some(AdjustmentState {
        clusters,
        derisk_found,
        first_entry_amount: first.amount,
        first_entry_cost: first.amount * first.price,
        latest_entry_price: latest_entry.price,
        latest_entry_timestamp_ms: latest_entry.filled_timestamp_ms,
        latest_exit_price: latest_exit.map(|order| order.price),
        latest_order_price: latest.price,
        latest_order_timestamp_ms: latest.filled_timestamp_ms,
    })
}

fn grind_entry_index(tag: &str) -> Option<usize> {
    (0..5).find(|index| tag == format!("grind_{}_entry", index + 1))
}

fn grind_exit_index(tag: &str) -> Option<usize> {
    (0..5).find(|index| {
        let level = index + 1;
        tag == format!("grind_{level}_exit") || tag == format!("grind_{level}_derisk")
    })
}

fn derisk_level_index(tag: &str) -> Option<usize> {
    (0..3).find(|index| tag == format!("derisk_level_{}", index + 1))
}

fn price_distance(rate: f64, reference: f64) -> Option<f64> {
    (reference > 0.0).then_some((rate - reference) / reference)
}

/// Execute X7's source-compiled `long_grind_entry_v3` predicate.
///
/// Both the system-v3.2 and legacy tag-120 callbacks call this same Python
/// method. Sharing the scalar-program boundary here ensures they receive the
/// same dataframe projection and variable encoding.
#[allow(clippy::too_many_arguments)]
pub(super) fn evaluate_grind_entry_program(
    manager: &NfiX7TradeManager,
    program_name: &str,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    num_open_grinds: usize,
    slice_profit: f64,
    slice_profit_entry: f64,
    slice_profit_exit: f64,
) -> Option<bool> {
    let mut variables = BTreeMap::from([
        (
            "num_open_grinds_and_buybacks".to_owned(),
            Value::Number(u64::try_from(num_open_grinds).ok()?.into()),
        ),
        ("slice_profit".to_owned(), number_value(slice_profit)?),
        (
            "slice_profit_entry".to_owned(),
            number_value(slice_profit_entry)?,
        ),
        (
            "slice_profit_exit".to_owned(),
            number_value(slice_profit_exit)?,
        ),
        // The current X7 source names this direction flag `is_derisk` even
        // though callers pass `True` for the long route.
        ("is_derisk".to_owned(), Value::Bool(true)),
        ("trade".to_owned(), scalar_trade_value(trade)?),
        (
            "current_time".to_owned(),
            Value::Number(candle.timestamp_ms.into()),
        ),
    ]);
    insert_projected_feature_window(
        &mut variables,
        pair,
        candle_index,
        manager.feature_projection(program_name)?,
    )?;
    let value = evaluate_scalar_program_bundle(&manager.programs, program_name, variables)?;
    Some(scalar_truthy(&value))
}

fn read_and_update_cluster_maxima(
    trade: &mut OpenTrade,
    clusters: &[GrindCluster; 5],
) -> [(f64, f64); 5] {
    std::array::from_fn(|index| {
        let level = index + 1;
        let stake_key = format!("grind_{level}_cluster_max_profit_stake");
        let rate_key = format!("grind_{level}_cluster_max_profit_rate");
        let previous_stake = custom_number(trade, &stake_key);
        let previous_rate = custom_number(trade, &rate_key);
        if clusters[index].profit_stake > previous_stake {
            trade.custom_data.insert(
                stake_key,
                number_value(clusters[index].profit_stake).unwrap_or(Value::Null),
            );
        }
        if clusters[index].profit_rate > previous_rate {
            trade.custom_data.insert(
                rate_key,
                number_value(clusters[index].profit_rate).unwrap_or(Value::Null),
            );
        }
        (previous_stake, previous_rate)
    })
}

fn custom_number(trade: &OpenTrade, key: &str) -> f64 {
    trade
        .custom_data
        .get(key)
        .and_then(Value::as_f64)
        .unwrap_or(0.0)
}

#[allow(clippy::option_option)] // Preserve the evaluator-validity boundary.
fn evaluate_derisk_levels(
    context: &AdjustmentContext<'_>,
    trade: &OpenTrade,
    state: &AdjustmentState,
) -> Option<Option<AdjustmentSignal>> {
    let constants = &context.adjustment.constants;
    if !constants.derisk_enable {
        return Some(None);
    }
    for level in &constants.derisk_levels {
        let index = level.level.checked_sub(1)?;
        let threshold = if context.config.is_futures {
            level.threshold_futures
        } else {
            level.threshold_spot
        };
        if !level.enabled
            || state.derisk_found.get(index).copied()?
            || context.snapshot.stake >= context.slice_amount * threshold / trade.leverage
        {
            continue;
        }
        let stake_fraction = if context.config.is_futures {
            level.stake_futures
        } else {
            level.stake_spot
        };
        let sell_amount =
            state.first_entry_amount * stake_fraction * context.candle.open / trade.leverage;
        if let Some(stake_amount) = partial_exit_stake(context, trade, sell_amount) {
            return Some(Some(AdjustmentSignal {
                stake_amount: -stake_amount,
                tag: format!("derisk_level_{}", level.level),
            }));
        }
    }
    Some(None)
}

fn evaluate_grind_level(
    context: &AdjustmentContext<'_>,
    trade: &OpenTrade,
    state: &AdjustmentState,
    previous_maxima: &[(f64, f64); 5],
    index: usize,
) -> Option<GrindLevelOutcome> {
    let constants = context.adjustment.constants.grinds.get(index)?;
    let cluster = state.clusters.get(index)?;
    let stakes = if context.config.is_futures {
        &constants.stakes_futures
    } else {
        &constants.stakes_spot
    };
    let thresholds = if context.config.is_futures {
        &constants.thresholds_futures
    } else {
        &constants.thresholds_spot
    };
    let scaled_stakes = scale_stakes_for_minimum(
        stakes,
        context.slice_amount,
        context.minimum_stake,
        if index == 0 || context.config.is_futures {
            trade.leverage
        } else {
            1.0
        },
        trade.leverage,
    )?;
    let entry_signal = grind_entry_signal(context, state, index)?;
    let below_maximum = context.current_stake_amount
        < context.slice_amount * context.adjustment.constants.max_stake_multiplier;
    let distance_allows_entry = if cluster.count == 0 {
        true
    } else if cluster.count < scaled_stakes.len() {
        cluster.distance(context.candle.open) < *thresholds.get(cluster.count)?
    } else {
        false
    };
    if constants.enabled
        && entry_signal
        && context.extra_entry_checks
        && cluster.count < scaled_stakes.len()
        && distance_allows_entry
        && below_maximum
    {
        let requested = context.slice_amount * scaled_stakes[cluster.count] / trade.leverage;
        let requested = requested.max(context.minimum_stake * 1.5);
        // NFI returns None when the requested order exceeds the current wallet
        // maximum; Freqtrade does not clamp this callback result.
        if requested > context.available_balance {
            return Some(GrindLevelOutcome::ReturnNone);
        }
        return Some(GrindLevelOutcome::Signal(AdjustmentSignal {
            stake_amount: requested,
            tag: format!("grind_{}_entry", index + 1),
        }));
    }

    if cluster.count > 0 && grind_exit_signal(context, cluster, constants, previous_maxima[index])?
    {
        let raw_exit = cluster.total_amount * context.candle.open / trade.leverage;
        if let Some(stake_amount) = partial_exit_stake(context, trade, raw_exit) {
            return Some(GrindLevelOutcome::Signal(AdjustmentSignal {
                stake_amount: -stake_amount,
                tag: order_id_tag(&format!("grind_{}_exit", index + 1), &cluster.entry_ids),
            }));
        }
    }

    let derisk_threshold = if context.config.is_futures {
        constants.derisk_futures
    } else {
        constants.derisk_spot
    };
    if constants.use_derisk && cluster.count > 0 && cluster.profit_rate < derisk_threshold {
        let raw_exit = cluster.total_amount * context.candle.open / trade.leverage;
        if let Some(stake_amount) = partial_exit_stake(context, trade, raw_exit) {
            return Some(GrindLevelOutcome::Signal(AdjustmentSignal {
                stake_amount: -stake_amount,
                tag: order_id_tag(&format!("grind_{}_derisk", index + 1), &cluster.entry_ids),
            }));
        }
    }
    Some(GrindLevelOutcome::Continue)
}

fn scale_stakes_for_minimum(
    stakes: &[f64],
    slice_amount: f64,
    minimum_stake: f64,
    stake_leverage: f64,
    trade_leverage: f64,
) -> Option<Vec<f64>> {
    let first = *stakes.first()?;
    if slice_amount * first / stake_leverage >= minimum_stake {
        return Some(stakes.to_vec());
    }
    let multiplier = minimum_stake / slice_amount / first * trade_leverage;
    Some(stakes.iter().map(|stake| stake * multiplier).collect())
}

fn grind_entry_signal(
    context: &AdjustmentContext<'_>,
    state: &AdjustmentState,
    index: usize,
) -> Option<bool> {
    if index < 3 {
        return Some(context.is_long_grind_entry);
    }
    if index == 3 {
        let secondary = context.slice_profit_entry < -0.04
            && feature_number_at(context.pair, context.candle_index, "RSI_3")? > 5.0
            && feature_number_at(context.pair, context.candle_index, "RSI_3_15m")? > 10.0
            && feature_number_at(context.pair, context.candle_index, "RSI_14")? < 35.0
            && feature_number_at(context.pair, context.candle_index, "close")?
                < feature_number_at(context.pair, context.candle_index, "EMA_20")? * 0.985;
        let empty_cluster_fallback = context.slice_profit_entry < -0.06
            && state.clusters.iter().all(|cluster| cluster.count == 0)
            && feature_number_at(context.pair, context.candle_index, "RSI_14")? < 30.0
            && feature_number_at(context.pair, context.candle_index, "close")?
                < feature_number_at(context.pair, context.candle_index, "EMA_20")? * 0.980;
        return Some(context.is_long_grind_entry || secondary || empty_cluster_fallback);
    }
    let derisked = state.derisk_found.iter().any(|found| *found);
    let secondary = derisked
        && context.slice_profit_entry < -0.06
        && feature_number_at(context.pair, context.candle_index, "RSI_3")? > 10.0
        && feature_number_at(context.pair, context.candle_index, "RSI_3_15m")? > 20.0
        && feature_number_at(context.pair, context.candle_index, "AROONU_14")? < 50.0;
    Some(context.is_long_grind_entry || secondary)
}

fn grind_exit_signal(
    context: &AdjustmentContext<'_>,
    cluster: &GrindCluster,
    constants: &NfiX7GrindLevel,
    _previous_maximum: (f64, f64),
) -> Option<bool> {
    let profit_threshold = if context.config.is_futures {
        constants.profit_threshold_futures
    } else {
        constants.profit_threshold_spot
    };
    if cluster.profit_rate < profit_threshold + fee_open(context.config) + fee_close(context.config)
    {
        return Some(false);
    }
    let field = |name| feature_number_at(context.pair, context.candle_index, name);
    let normal_exit = field("RSI_3")? > 99.0
        || field("RSI_14")? > 70.0
        || field("WILLR_14")? > -0.1
        || field("STOCHRSIk_14_14_3_3")? > 95.0
        || field("close")? > field("BBU_20_2.0")? * 1.01
        || (field("RSI_3")? > 90.0 && field("RSI_14")? < 50.0)
        || (field("RSI_3")? > 80.0
            && field("RSI_3_1h")? < 20.0
            && field("RSI_3_4h")? < 20.0
            && field("ROC_9_1d")? > -10.0
            && field("BTC_RSI_14_4h")? < 35.0);
    Some(normal_exit)
}

fn partial_exit_stake(
    context: &AdjustmentContext<'_>,
    trade: &OpenTrade,
    requested_exit: f64,
) -> Option<f64> {
    let remaining = context.current_stake_amount / trade.leverage - requested_exit;
    let exit_amount = if remaining < context.minimum_stake * 1.55 {
        trade.amount * context.candle.open / trade.leverage - context.minimum_stake * 1.55
    } else {
        requested_exit
    };
    let ft_stake =
        exit_amount * trade.leverage * (trade.stake_amount / trade.amount) / context.candle.open;
    (exit_amount > context.minimum_stake && ft_stake > context.minimum_stake).then_some(ft_stake)
}

fn order_id_tag(prefix: &str, ids: &[u64]) -> String {
    ids.iter().fold(prefix.to_owned(), |mut tag, id| {
        tag.push(' ');
        tag.push_str(&id.to_string());
        tag
    })
}
