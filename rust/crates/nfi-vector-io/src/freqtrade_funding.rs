//! Exact sparse Futures funding preparation for the native vector pipeline.
//!
//! Freqtrade keeps funding-rate and mark-price archives as separate roles.
//! Each role is stable-sorted and deduplicated by timestamp with the last row
//! winning, then the two roles are inner-joined. Only those paired events are
//! mapped to equal base-candle timestamps; funding is never forward-filled.

use std::collections::BTreeMap;

use nfi_vector_core::alignment::{NumericFrame, Timeframe};
use nfi_vector_core::column::OwnedColumn;

use crate::{FuturesFrameSet, NativeContractError, TradingMode};

pub const RATE_COLUMN: &str = "nfi_exec_funding_rate";
pub const MARK_COLUMN: &str = "nfi_exec_funding_mark_price";

/// Pair-aligned sparse funding columns ready for [`crate::InMemoryVectorPair`].
///
/// Missing rows contain a present canonical NaN, matching the Python worker's
/// float columns. The in-memory adapter deliberately maps both NaN and Arrow
/// null to `None` before constructing simulator candles.
#[derive(Clone, Debug, PartialEq)]
pub struct PreparedEvents {
    pub pair: String,
    pub funding_interval: Option<Timeframe>,
    pub funding_rates: Vec<Option<f64>>,
    pub mark_prices: Vec<Option<f64>>,
}

impl PreparedEvents {
    /// Consume the result as the two conventional `nfi_exec_*` columns.
    #[must_use]
    pub fn into_owned_columns(self) -> BTreeMap<String, OwnedColumn> {
        BTreeMap::from([
            (RATE_COLUMN.to_owned(), OwnedColumn::f64(self.funding_rates)),
            (MARK_COLUMN.to_owned(), OwnedColumn::f64(self.mark_prices)),
        ])
    }
}

/// Prepare exact sparse funding events for one base pair.
///
/// `futures_frames` is the manifest-decoded collection for the complete run.
/// Futures mode requires exactly one matching descriptor. Spot mode requires
/// the collection to be empty and returns all-missing compatibility columns.
/// The returned interval comes from the descriptor's frame identity; no
/// funding cadence is assumed by this stage.
///
/// # Errors
///
/// Returns a fail-closed contract error for an empty pair, invalid mode/data
/// combination, duplicate or missing descriptors, identity/interval drift,
/// missing `open` columns, or a paired event whose rate is non-finite or whose
/// mark price is non-finite/non-positive.
pub fn prepare_events(
    trading_mode: TradingMode,
    pair: &str,
    base_timestamps_ms: &[i64],
    futures_frames: &[FuturesFrameSet],
) -> Result<PreparedEvents, NativeContractError> {
    if pair.is_empty() {
        return Err(invalid("funding preparation pair is empty"));
    }
    if trading_mode == TradingMode::Spot {
        if !futures_frames.is_empty() {
            return Err(invalid(
                "Spot execution cannot contain Futures funding frames",
            ));
        }
        return Ok(missing_events(pair, base_timestamps_ms.len(), None));
    }

    let matching_descriptors = futures_frames
        .iter()
        .filter(|frame_set| frame_set.pair == pair)
        .collect::<Vec<_>>();
    let [frame_set] = matching_descriptors.as_slice() else {
        let message = if matching_descriptors.is_empty() {
            format!("Futures execution has no funding descriptor for {pair}")
        } else {
            format!("Futures execution has duplicate funding descriptors for {pair}")
        };
        return Err(invalid(message));
    };
    validate_frame_set(frame_set, pair)?;
    let interval = frame_set.funding_rate.identity.timeframe.clone();
    let funding = stable_deduplicate_open(&frame_set.funding_rate, "funding-rate")?;
    let mark_by_time = stable_deduplicate_open(&frame_set.mark, "mark-price")?;

    let mut paired = BTreeMap::new();
    for (timestamp_ms, rate) in funding {
        let Some(mark) = mark_by_time.get(&timestamp_ms).copied() else {
            continue;
        };
        if !rate.is_finite() {
            return Err(invalid(format!(
                "paired funding rate for {pair} at {timestamp_ms} is not finite"
            )));
        }
        if !mark.is_finite() || mark <= 0.0 {
            return Err(invalid(format!(
                "paired funding mark price for {pair} at {timestamp_ms} must be positive and finite"
            )));
        }
        paired.insert(timestamp_ms, (rate, mark));
    }

    let mut result = missing_events(pair, base_timestamps_ms.len(), Some(interval));
    for (row, timestamp_ms) in base_timestamps_ms.iter().enumerate() {
        if let Some((rate, mark)) = paired.get(timestamp_ms) {
            result.funding_rates[row] = Some(*rate);
            result.mark_prices[row] = Some(*mark);
        }
    }
    Ok(result)
}

fn validate_frame_set(frame_set: &FuturesFrameSet, pair: &str) -> Result<(), NativeContractError> {
    for (role, frame) in [
        ("funding-rate", &frame_set.funding_rate),
        ("mark-price", &frame_set.mark),
    ] {
        frame
            .validate()
            .map_err(|error| invalid(format!("invalid {role} frame for {pair}: {error}")))?;
        if frame.identity.pair != pair {
            return Err(invalid(format!(
                "{role} frame identity {} differs from descriptor pair {pair}",
                frame.identity.pair
            )));
        }
    }
    Ok(())
}

fn stable_deduplicate_open(
    frame: &NumericFrame,
    role: &str,
) -> Result<BTreeMap<i64, f64>, NativeContractError> {
    let open = frame.columns.get("open").ok_or_else(|| {
        invalid(format!(
            "{role} frame for {} {} is missing column \"open\"",
            frame.identity.pair,
            frame.identity.timeframe.as_str()
        ))
    })?;
    let mut positions = (0..frame.timestamps_ms.len()).collect::<Vec<_>>();
    positions.sort_by_key(|row| frame.timestamps_ms[*row]);
    let mut result = BTreeMap::new();
    for row in positions {
        // Arrow null and present NaN are both observable missing numeric
        // values. Retaining NaN here lets the paired-event validation report
        // one deterministic error after the exact inner join.
        let value = open[row].unwrap_or(f64::NAN);
        result.insert(frame.timestamps_ms[row], value);
    }
    Ok(result)
}

fn missing_events(pair: &str, rows: usize, funding_interval: Option<Timeframe>) -> PreparedEvents {
    PreparedEvents {
        pair: pair.to_owned(),
        funding_interval,
        funding_rates: vec![Some(f64::NAN); rows],
        mark_prices: vec![Some(f64::NAN); rows],
    }
}

fn invalid(message: impl Into<String>) -> NativeContractError {
    NativeContractError::Invalid(format!(
        "Freqtrade funding preparation failed: {}",
        message.into()
    ))
}

#[cfg(test)]
mod tests {
    use nfi_vector_core::alignment::FrameIdentity;

    use super::*;

    fn identity(pair: &str, timeframe: &str) -> FrameIdentity {
        FrameIdentity::new(pair, Timeframe::parse(timeframe).expect("timeframe")).expect("identity")
    }

    fn sparse_frame(
        pair: &str,
        timeframe: &str,
        timestamps_ms: Vec<i64>,
        opens: Vec<Option<f64>>,
    ) -> NumericFrame {
        NumericFrame {
            identity: identity(pair, timeframe),
            timestamps_ms,
            columns: BTreeMap::from([("open".to_owned(), opens)]),
        }
    }

    fn frame_set(
        pair: &str,
        timeframe: &str,
        funding_timestamps: Vec<i64>,
        rates: Vec<Option<f64>>,
        mark_timestamps: Vec<i64>,
        marks: Vec<Option<f64>>,
    ) -> FuturesFrameSet {
        FuturesFrameSet {
            pair: pair.to_owned(),
            funding_rate: sparse_frame(pair, timeframe, funding_timestamps, rates),
            mark: sparse_frame(pair, timeframe, mark_timestamps, marks),
        }
    }

    fn assert_nan(value: Option<f64>) {
        assert!(value
            .expect("present Python-compatible missing value")
            .is_nan());
    }

    #[test]
    fn matches_python_attach_funding_events_duplicate_and_sparse_oracle() {
        // Generated on 2026-08-12 with vector_worker._attach_funding_events.
        // Each role is keep-last at 1h; the unpaired 3h/4h rows disappear.
        let frames = [frame_set(
            "ORACLE/USDT",
            "1h",
            vec![7_200_000, 3_600_000, 3_600_000, 14_400_000],
            vec![Some(0.003), Some(0.001), Some(0.002), Some(0.004)],
            vec![10_800_000, 3_600_000, 7_200_000, 3_600_000],
            vec![Some(300.0), Some(100.0), Some(200.0), Some(101.0)],
        )];

        let result = prepare_events(
            TradingMode::Futures,
            "ORACLE/USDT",
            &[0, 3_600_000, 7_200_000, 10_800_000],
            &frames,
        )
        .expect("events");

        assert_eq!(
            result
                .funding_interval
                .as_ref()
                .expect("manifest interval")
                .as_str(),
            "1h"
        );
        assert_nan(result.funding_rates[0]);
        assert_eq!(result.funding_rates[1..3], [Some(0.002), Some(0.003)]);
        assert_nan(result.funding_rates[3]);
        assert_nan(result.mark_prices[0]);
        assert_eq!(result.mark_prices[1..3], [Some(101.0), Some(200.0)]);
        assert_nan(result.mark_prices[3]);
    }

    #[test]
    fn exact_timestamp_mapping_never_forward_fills() {
        let frames = [frame_set(
            "ORACLE/USDT",
            "15m",
            vec![0],
            vec![Some(-0.000_1)],
            vec![0],
            vec![Some(250.0)],
        )];

        let result = prepare_events(
            TradingMode::Futures,
            "ORACLE/USDT",
            &[0, 300_000, 900_000],
            &frames,
        )
        .expect("events");

        assert_eq!(result.funding_rates[0], Some(-0.000_1));
        assert_eq!(result.mark_prices[0], Some(250.0));
        for row in 1..3 {
            assert_nan(result.funding_rates[row]);
            assert_nan(result.mark_prices[row]);
        }
    }

    #[test]
    fn interval_is_manifest_derived_instead_of_assuming_one_hour() {
        let frames = [frame_set(
            "ORACLE/USDT",
            "8h",
            vec![0],
            vec![Some(0.001)],
            vec![0],
            vec![Some(100.0)],
        )];

        let result =
            prepare_events(TradingMode::Futures, "ORACLE/USDT", &[0], &frames).expect("events");

        assert_eq!(result.funding_interval.expect("interval").as_str(), "8h");
    }

    #[test]
    fn spot_has_missing_compatibility_columns_and_rejects_futures_sources() {
        let spot = prepare_events(TradingMode::Spot, "ORACLE/USDT", &[0, 300_000], &[])
            .expect("Spot compatibility columns");
        assert_eq!(spot.pair, "ORACLE/USDT");
        assert!(spot.funding_interval.is_none());
        assert_nan(spot.funding_rates[0]);
        assert_nan(spot.funding_rates[1]);
        assert_nan(spot.mark_prices[0]);
        assert_nan(spot.mark_prices[1]);

        let frames = [frame_set(
            "ORACLE/USDT",
            "1h",
            vec![0],
            vec![Some(0.001)],
            vec![0],
            vec![Some(100.0)],
        )];
        assert!(
            prepare_events(TradingMode::Spot, "ORACLE/USDT", &[0], &frames)
                .expect_err("Spot/Futures conflict")
                .to_string()
                .contains("Spot execution cannot contain Futures funding frames")
        );
    }

    #[test]
    fn futures_requires_exactly_one_pair_descriptor() {
        let first = frame_set(
            "ORACLE/USDT",
            "1h",
            vec![0],
            vec![Some(0.001)],
            vec![0],
            vec![Some(100.0)],
        );
        assert!(
            prepare_events(TradingMode::Futures, "MISSING/USDT", &[0], &[])
                .expect_err("missing descriptor")
                .to_string()
                .contains("no funding descriptor")
        );
        assert!(prepare_events(
            TradingMode::Futures,
            "ORACLE/USDT",
            &[0],
            &[first.clone(), first],
        )
        .expect_err("duplicate descriptors")
        .to_string()
        .contains("duplicate funding descriptors"));
    }

    #[test]
    fn rejects_identity_drift_and_allows_distinct_role_timeframes() {
        let mut pair_drift = frame_set(
            "ORACLE/USDT",
            "1h",
            vec![0],
            vec![Some(0.001)],
            vec![0],
            vec![Some(100.0)],
        );
        pair_drift.mark.identity = identity("OTHER/USDT", "1h");
        assert!(
            prepare_events(TradingMode::Futures, "ORACLE/USDT", &[0], &[pair_drift],)
                .expect_err("identity drift")
                .to_string()
                .contains("differs from descriptor pair")
        );

        let mut distinct_timeframes = frame_set(
            "ORACLE/USDT",
            "1h",
            vec![0],
            vec![Some(0.001)],
            vec![0],
            vec![Some(100.0)],
        );
        distinct_timeframes.mark.identity = identity("ORACLE/USDT", "4h");
        let result = prepare_events(
            TradingMode::Futures,
            "ORACLE/USDT",
            &[0],
            &[distinct_timeframes],
        )
        .expect("funding and mark roles retain independent source timeframes");
        assert_eq!(
            result.funding_interval.expect("funding interval").as_str(),
            "1h"
        );
        assert_eq!(result.funding_rates, [Some(0.001)]);
        assert_eq!(result.mark_prices, [Some(100.0)]);
    }

    #[test]
    fn validates_only_paired_events_and_requires_finite_rate_positive_mark() {
        for (rate, mark, expected) in [
            (f64::NAN, 100.0, "rate"),
            (f64::INFINITY, 100.0, "rate"),
            (0.001, f64::NAN, "mark price"),
            (0.001, f64::INFINITY, "mark price"),
            (0.001, 0.0, "mark price"),
            (0.001, -1.0, "mark price"),
        ] {
            let frames = [frame_set(
                "ORACLE/USDT",
                "1h",
                vec![0],
                vec![Some(rate)],
                vec![0],
                vec![Some(mark)],
            )];
            assert!(
                prepare_events(TradingMode::Futures, "ORACLE/USDT", &[0], &frames,)
                    .expect_err("invalid paired event")
                    .to_string()
                    .contains(expected)
            );
        }

        // Invalid values outside the inner join are unobservable, as in the
        // Python merge, and therefore do not create a base funding event.
        let unpaired = [frame_set(
            "ORACLE/USDT",
            "1h",
            vec![0],
            vec![Some(f64::NAN)],
            vec![3_600_000],
            vec![Some(-1.0)],
        )];
        let result = prepare_events(
            TradingMode::Futures,
            "ORACLE/USDT",
            &[0, 3_600_000],
            &unpaired,
        )
        .expect("unpaired invalid values are absent events");
        assert_nan(result.funding_rates[0]);
        assert_nan(result.funding_rates[1]);
    }

    #[test]
    fn owned_columns_preserve_in_memory_optional_number_contract() {
        let prepared = missing_events(
            "ORACLE/USDT",
            2,
            Some(Timeframe::parse("4h").expect("timeframe")),
        );

        let columns = prepared.into_owned_columns();

        for name in [RATE_COLUMN, MARK_COLUMN] {
            let view = columns[name].as_view();
            assert!(view.f64_at(0).expect("present NaN").is_nan());
            assert!(view.f64_at(1).expect("present NaN").is_nan());
        }
    }
}
