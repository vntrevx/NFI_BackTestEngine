//! Strategy-visible dataframe projection derived from scalar bytecode.

use std::collections::{BTreeMap, BTreeSet};

use serde_json::Value;

use crate::domain::{FeatureProjection, NfiX7TradeManager, PairSeries, ScalarDecisionProgram};
use crate::io::CALLBACK_FEATURE_LOOKBACK_ROWS;
use crate::nfi::{
    NFI_LONG_EXIT_PROGRAMS, NFI_LONG_EXIT_PROGRAMS_WITHOUT_DESCENDING, NFI_SHORT_EXIT_PROGRAMS,
};
use crate::scalar_vm::{number_value, value_index};

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

impl NfiX7TradeManager {
    pub(crate) fn feature_projection(&self, program_name: &str) -> Option<&FeatureProjection> {
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

    pub(crate) fn feature_projection_union(
        &self,
        program_order: &[&str],
    ) -> Option<&FeatureProjection> {
        let key = if program_order == NFI_LONG_EXIT_PROGRAMS {
            "long-all"
        } else if program_order == NFI_LONG_EXIT_PROGRAMS_WITHOUT_DESCENDING {
            "long-without-descending"
        } else if program_order == NFI_SHORT_EXIT_PROGRAMS {
            "short-all"
        } else {
            return None;
        };
        self.feature_projection_unions
            .get_or_init(|| {
                [
                    ("long-all", NFI_LONG_EXIT_PROGRAMS),
                    (
                        "long-without-descending",
                        NFI_LONG_EXIT_PROGRAMS_WITHOUT_DESCENDING,
                    ),
                    ("short-all", NFI_SHORT_EXIT_PROGRAMS),
                ]
                .into_iter()
                .filter_map(|(name, programs)| {
                    let mut union = FeatureProjection::new();
                    for program in programs {
                        let projection = self.feature_projection(program)?;
                        for (variable, columns) in projection {
                            union
                                .entry(variable.clone())
                                .or_default()
                                .extend(columns.iter().cloned());
                        }
                    }
                    Some((name.to_owned(), union))
                })
                .collect()
            })
            .get(key)
    }

    /// Derive a union for a source-provided program sequence.
    ///
    /// The staged managed-exit shadow may change order or add a scalar-pure
    /// helper before the legacy profile tables are retired. Its projection
    /// must therefore be computed from bytecode names, not a fixed route key.
    pub(crate) fn dynamic_feature_projection_union(
        &self,
        program_order: &[String],
    ) -> Option<FeatureProjection> {
        let mut union = FeatureProjection::new();
        for program in program_order {
            let projection = self.feature_projection(program)?;
            for (variable, columns) in projection {
                union
                    .entry(variable.clone())
                    .or_default()
                    .extend(columns.iter().cloned());
            }
        }
        Some(union)
    }
}

/// Derive dataframe field access directly from the immutable scalar arena.
///
/// The compiler represents `last_candle["RSI_14"]` as an `index` expression
/// whose operands point at a `variable` and a literal string expression. We do
/// not accept a serialized projection list: deriving it here prevents an input
/// from omitting a field that executable bytecode can read.
pub(crate) fn scalar_program_feature_projection(
    program: &ScalarDecisionProgram,
) -> FeatureProjection {
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
        || (1..=CALLBACK_FEATURE_LOOKBACK_ROWS)
            .any(|offset| name == format!("previous_candle_{offset}"))
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

pub(crate) fn insert_projected_feature_window(
    variables: &mut BTreeMap<String, Value>,
    pair: &PairSeries,
    candle_index: usize,
    projection: &FeatureProjection,
) -> Option<()> {
    variables.insert(
        "last_candle".to_owned(),
        projected_feature_row(pair, candle_index, projection.get("last_candle"))?,
    );
    for offset in 1..=CALLBACK_FEATURE_LOOKBACK_ROWS {
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
pub(crate) fn insert_feature_window(
    variables: &mut BTreeMap<String, Value>,
    pair: &PairSeries,
    candle_index: usize,
) -> Option<()> {
    variables.insert("last_candle".to_owned(), feature_row(pair, candle_index)?);
    for offset in 1..=CALLBACK_FEATURE_LOOKBACK_ROWS {
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
