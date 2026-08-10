//! Exact fixed-window reducers matching pandas 3.0.3 causal rolling semantics.

use std::collections::VecDeque;

use serde_json::{Map, Value};

use crate::VectorCoreError;

const MAX_WINDOW: usize = 100_000;

/// Bounded state for one causal pandas rolling reducer across record batches.
#[derive(Debug)]
pub(crate) struct RollingStream {
    inner: RollingStreamInner,
}

#[derive(Debug)]
enum RollingStreamInner {
    Sum(SumMeanState),
    Mean(SumMeanState),
    Max(ExtremeState),
    Min(ExtremeState),
}

impl RollingStream {
    pub(crate) fn execute(&mut self, values: &[f64]) -> Vec<f64> {
        match &mut self.inner {
            RollingStreamInner::Sum(state) => state.execute(values, false),
            RollingStreamInner::Mean(state) => state.execute(values, true),
            RollingStreamInner::Max(state) | RollingStreamInner::Min(state) => {
                state.execute(values)
            }
        }
    }

    pub(crate) fn retained(&self) -> usize {
        match &self.inner {
            RollingStreamInner::Sum(state) | RollingStreamInner::Mean(state) => state.values.len(),
            RollingStreamInner::Max(state) | RollingStreamInner::Min(state) => {
                state.finite.len().saturating_add(state.candidates.len())
            }
        }
    }
}

/// Execute a fixed, right-aligned pandas rolling reducer.
///
/// # Errors
///
/// Returns an error for lookahead-capable centering, an invalid window or
/// `min_periods`, or a reducer outside the latest-NFI exact registry.
pub fn execute_rolling(
    reducer: &str,
    values: &[f64],
    arguments: &Map<String, Value>,
) -> Result<Vec<f64>, VectorCoreError> {
    stream(reducer, arguments).map(|mut state| state.execute(values))
}

pub(crate) fn stream(
    reducer: &str,
    arguments: &Map<String, Value>,
) -> Result<RollingStream, VectorCoreError> {
    let window = integer(arguments, "window")?;
    let min_periods = arguments
        .get("min_periods")
        .filter(|value| !value.is_null())
        .map_or(Ok(window), |value| bounded_integer(value, "min_periods"))?;
    if window == 0 || window > MAX_WINDOW || min_periods > window {
        return Err(error(
            "window and min_periods must satisfy 0 < min_periods <= window",
        ));
    }
    if arguments
        .get("center")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        return Err(error("centered rolling would look ahead"));
    }
    match reducer {
        "mean" => Ok(RollingStream {
            inner: RollingStreamInner::Mean(SumMeanState::new(window, min_periods)),
        }),
        "sum" => Ok(RollingStream {
            inner: RollingStreamInner::Sum(SumMeanState::new(window, min_periods)),
        }),
        "max" => Ok(RollingStream {
            inner: RollingStreamInner::Max(ExtremeState::new(window, min_periods, true)),
        }),
        "min" => Ok(RollingStream {
            inner: RollingStreamInner::Min(ExtremeState::new(window, min_periods, false)),
        }),
        _ => Err(error("reducer is not in the exact rolling registry")),
    }
}

#[derive(Debug)]
pub(super) struct SumMeanState {
    window: usize,
    min_periods: usize,
    values: VecDeque<f64>,
    sum: f64,
    compensation_add: f64,
    compensation_remove: f64,
    observations: usize,
    negative: usize,
    previous: f64,
    consecutive_same: usize,
}

impl SumMeanState {
    fn new(window: usize, min_periods: usize) -> Self {
        Self {
            window,
            min_periods,
            values: VecDeque::with_capacity(window),
            sum: 0.0,
            compensation_add: 0.0,
            compensation_remove: 0.0,
            observations: 0,
            negative: 0,
            previous: f64::NAN,
            consecutive_same: 0,
        }
    }

    fn execute(&mut self, values: &[f64], mean: bool) -> Vec<f64> {
        let mut output = Vec::with_capacity(values.len());
        for value in values.iter().copied() {
            if self.values.len() == self.window {
                let removed = self.values.pop_front().expect("full rolling state");
                remove_value(
                    removed,
                    &mut self.observations,
                    &mut self.negative,
                    &mut self.sum,
                    &mut self.compensation_remove,
                );
            }
            self.values.push_back(value);
            add_value(
                value,
                &mut self.observations,
                &mut self.negative,
                &mut self.sum,
                &mut self.compensation_add,
                &mut self.consecutive_same,
                &mut self.previous,
            );
            output.push(if self.observations < self.min_periods {
                f64::NAN
            } else if mean {
                pandas_mean(
                    self.sum,
                    self.observations,
                    self.negative,
                    self.consecutive_same,
                    self.previous,
                )
            } else if self.consecutive_same >= self.observations {
                self.previous * count_as_f64(self.observations)
            } else {
                self.sum
            });
        }
        output
    }
}

#[allow(clippy::too_many_arguments)]
#[allow(clippy::float_cmp)] // pandas uses exact equality to repair constant-window artifacts.
fn add_value(
    value: f64,
    observations: &mut usize,
    negative: &mut usize,
    sum: &mut f64,
    compensation: &mut f64,
    consecutive_same: &mut usize,
    previous: &mut f64,
) {
    if value.is_nan() {
        return;
    }
    *observations += 1;
    let adjusted = value - *compensation;
    let next = *sum + adjusted;
    *compensation = next - *sum - adjusted;
    *sum = next;
    if value.is_sign_negative() {
        *negative += 1;
    }
    if value == *previous {
        *consecutive_same += 1;
    } else {
        *consecutive_same = 1;
    }
    *previous = value;
}

fn remove_value(
    value: f64,
    observations: &mut usize,
    negative: &mut usize,
    sum: &mut f64,
    compensation: &mut f64,
) {
    if value.is_nan() {
        return;
    }
    *observations -= 1;
    let adjusted = -value - *compensation;
    let next = *sum + adjusted;
    *compensation = next - *sum - adjusted;
    *sum = next;
    if value.is_sign_negative() {
        *negative -= 1;
    }
}

fn pandas_mean(
    sum: f64,
    observations: usize,
    negative: usize,
    consecutive_same: usize,
    previous: f64,
) -> f64 {
    if consecutive_same >= observations {
        previous
    } else {
        let result = sum / count_as_f64(observations);
        if negative == 0 && result < 0.0 || negative == observations && result > 0.0 {
            0.0
        } else {
            result
        }
    }
}

#[derive(Debug)]
pub(super) struct ExtremeState {
    window: usize,
    min_periods: usize,
    maximum: bool,
    candidates: VecDeque<(usize, f64)>,
    finite: VecDeque<bool>,
    observations: usize,
    next_index: usize,
}

impl ExtremeState {
    fn new(window: usize, min_periods: usize, maximum: bool) -> Self {
        Self {
            window,
            min_periods,
            maximum,
            candidates: VecDeque::with_capacity(window),
            finite: VecDeque::with_capacity(window),
            observations: 0,
            next_index: 0,
        }
    }

    fn execute(&mut self, values: &[f64]) -> Vec<f64> {
        let mut output = Vec::with_capacity(values.len());
        for value in values.iter().copied() {
            if self.finite.len() == self.window && self.finite.pop_front().is_some_and(|item| item)
            {
                self.observations -= 1;
            }
            let start = self
                .next_index
                .saturating_add(1)
                .saturating_sub(self.window);
            while self
                .candidates
                .front()
                .is_some_and(|candidate| candidate.0 < start)
            {
                self.candidates.pop_front();
            }
            let valid = !value.is_nan();
            self.finite.push_back(valid);
            if valid {
                self.observations += 1;
                while self.candidates.back().is_some_and(|candidate| {
                    if self.maximum {
                        value >= candidate.1
                    } else {
                        value <= candidate.1
                    }
                }) {
                    self.candidates.pop_back();
                }
                self.candidates.push_back((self.next_index, value));
            }
            output.push(if self.observations >= self.min_periods {
                self.candidates
                    .front()
                    .map_or(f64::NAN, |candidate| candidate.1)
            } else {
                f64::NAN
            });
            self.next_index = self.next_index.saturating_add(1);
        }
        output
    }
}

fn integer(arguments: &Map<String, Value>, name: &str) -> Result<usize, VectorCoreError> {
    arguments
        .get(name)
        .ok_or_else(|| error(&format!("missing integer argument {name}")))
        .and_then(|value| bounded_integer(value, name))
}

fn bounded_integer(value: &Value, name: &str) -> Result<usize, VectorCoreError> {
    value
        .as_u64()
        .and_then(|item| usize::try_from(item).ok())
        .ok_or_else(|| error(&format!("invalid integer argument {name}")))
}

fn count_as_f64(count: usize) -> f64 {
    f64::from(u32::try_from(count).expect("rolling counts are bounded by MAX_WINDOW"))
}

fn error(message: &str) -> VectorCoreError {
    VectorCoreError::InvalidState(format!("pandas rolling: {message}"))
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn fixed_reducers_match_pandas_warmup_and_values() {
        let arguments = json!({"window": 3, "min_periods": 3, "center": false})
            .as_object()
            .expect("arguments")
            .clone();
        let values = [1.0, 2.0, 3.0, 4.0];
        let sum = execute_rolling("sum", &values, &arguments).expect("sum");
        let mean = execute_rolling("mean", &values, &arguments).expect("mean");
        let min = execute_rolling("min", &values, &arguments).expect("min");
        let max = execute_rolling("max", &values, &arguments).expect("max");
        assert!(sum[0].is_nan() && sum[1].is_nan());
        assert_eq!(&sum[2..], &[6.0, 9.0]);
        assert_eq!(&mean[2..], &[2.0, 3.0]);
        assert_eq!(&min[2..], &[1.0, 2.0]);
        assert_eq!(&max[2..], &[3.0, 4.0]);
    }

    #[test]
    fn center_and_unknown_reducer_fail_closed() {
        let centered = json!({"window": 3, "center": true})
            .as_object()
            .expect("arguments")
            .clone();
        assert!(execute_rolling("mean", &[1.0], &centered).is_err());
        let causal = json!({"window": 3}).as_object().expect("arguments").clone();
        assert!(execute_rolling("median", &[1.0], &causal).is_err());
    }

    #[test]
    fn rolling_state_is_exact_across_arbitrary_chunks_and_bounded() {
        let arguments = json!({"window": 4, "min_periods": 4, "center": false})
            .as_object()
            .expect("arguments")
            .clone();
        let values = [1.0, 1.0, 3.0, -0.0, 8.0, 13.0, 21.0, 34.0, 55.0];
        for reducer in ["sum", "mean", "min", "max"] {
            let expected = execute_rolling(reducer, &values, &arguments).expect("batch result");
            let mut state = stream(reducer, &arguments).expect("stream state");
            let mut actual = Vec::new();
            for chunk in [&values[..1], &values[1..3], &values[3..8], &values[8..]] {
                actual.extend(state.execute(chunk));
            }
            assert_eq!(actual.len(), expected.len());
            for (actual, expected) in actual.iter().zip(expected) {
                assert_eq!(actual.to_bits(), expected.to_bits(), "{reducer}");
            }
            assert!(state.retained() <= 8);
        }
    }
}
