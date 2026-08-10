//! Bounded streaming state for lagged and rolling indicator inputs.
//!
//! These containers deliberately store input values verbatim.  In particular, a
//! present `NaN` is still present, and signed zero is never normalised.  Indicator
//! reductions belong to their own kernels; this module only maintains the causal,
//! batch-spanning state those kernels need.

use std::collections::VecDeque;

use crate::VectorCoreError;

/// A compact description of the memory retained by a state container.
///
/// `capacity` is the configured hard bound, while `peak` is the greatest number
/// of retained observations since the state was created.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct StateProfile {
    /// Number of observations currently retained.
    pub retained: usize,
    /// Maximum number of observations this state can retain.
    pub capacity: usize,
    /// Greatest retained count observed over this state's lifetime.
    pub peak: usize,
}

impl StateProfile {
    /// Number of observations currently retained.
    #[must_use]
    pub const fn retained(self) -> usize {
        self.retained
    }

    /// Maximum number of observations this state can retain.
    #[must_use]
    pub const fn capacity(self) -> usize {
        self.capacity
    }

    /// Greatest retained count observed over this state's lifetime.
    #[must_use]
    pub const fn peak(self) -> usize {
        self.peak
    }
}

/// Bounded state for a causal shift by a fixed, positive lag.
#[derive(Clone, Debug)]
pub struct ShiftState {
    lag: usize,
    values: VecDeque<Option<f64>>,
    peak: usize,
}

impl ShiftState {
    /// Creates state for a shift by `lag` observations.
    ///
    /// A zero lag has no delayed state and is rejected so callers cannot
    /// accidentally use this type for a non-causal identity operation.
    ///
    /// # Errors
    ///
    /// Returns an invalid-state error for a zero or unallocatable lag.
    pub fn new(lag: usize) -> Result<Self, VectorCoreError> {
        if lag == 0 {
            return Err(VectorCoreError::InvalidState(
                "shift lag must be positive".to_owned(),
            ));
        }

        let mut values = VecDeque::new();
        values.try_reserve_exact(lag).map_err(|error| {
            VectorCoreError::InvalidState(format!("shift lag cannot be retained: {error}"))
        })?;

        Ok(Self {
            lag,
            values,
            peak: 0,
        })
    }

    /// Returns the configured shift lag.
    #[must_use]
    pub const fn lag(&self) -> usize {
        self.lag
    }

    /// Adds one observation and returns the observation exactly `lag` rows ago.
    ///
    /// Before `lag` observations have been seen, this returns `None`.  A prior
    /// null also returns `None`, matching nullable Arrow value semantics.
    pub fn push(&mut self, value: Option<f64>) -> Option<f64> {
        let shifted = if self.values.len() == self.lag {
            self.values.pop_front().flatten()
        } else {
            None
        };

        self.values.push_back(value);
        self.peak = self.peak.max(self.values.len());
        shifted
    }

    /// Alias for [`Self::push`] that emphasizes advancing a streaming state.
    pub fn advance(&mut self, value: Option<f64>) -> Option<f64> {
        self.push(value)
    }

    /// Returns the number of observations currently retained.
    #[must_use]
    pub fn len(&self) -> usize {
        self.values.len()
    }

    /// Returns whether no observations have been retained yet.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.values.is_empty()
    }

    /// Returns bounded-state accounting for this shift.
    #[must_use]
    pub fn profile(&self) -> StateProfile {
        StateProfile {
            retained: self.values.len(),
            capacity: self.lag,
            peak: self.peak,
        }
    }
}

/// Bounded, ordered state for a fixed-width rolling input window.
#[derive(Clone, Debug)]
pub struct RollingWindowState {
    window: usize,
    values: VecDeque<Option<f64>>,
    valid_count: usize,
    peak: usize,
}

impl RollingWindowState {
    /// Creates state for a rolling window with a positive width.
    ///
    /// # Errors
    ///
    /// Returns an invalid-state error for a zero or unallocatable window.
    pub fn new(window: usize) -> Result<Self, VectorCoreError> {
        if window == 0 {
            return Err(VectorCoreError::InvalidState(
                "rolling window must be positive".to_owned(),
            ));
        }

        let mut values = VecDeque::new();
        values.try_reserve_exact(window).map_err(|error| {
            VectorCoreError::InvalidState(format!("rolling window cannot be retained: {error}"))
        })?;

        Ok(Self {
            window,
            values,
            valid_count: 0,
            peak: 0,
        })
    }

    /// Returns the configured rolling-window width.
    #[must_use]
    pub const fn window(&self) -> usize {
        self.window
    }

    /// Adds one observation, evicting and returning the oldest observation when
    /// the window is already full.
    ///
    /// The outer `Option` reports whether an observation was evicted.  The inner
    /// `Option` preserves whether that evicted observation was null.
    pub fn push(&mut self, value: Option<f64>) -> Option<Option<f64>> {
        let evicted = if self.values.len() == self.window {
            self.values.pop_front()
        } else {
            None
        };

        if evicted.flatten().is_some() {
            self.valid_count -= 1;
        }
        if value.is_some() {
            self.valid_count += 1;
        }
        self.values.push_back(value);
        self.peak = self.peak.max(self.values.len());
        evicted
    }

    /// Alias for [`Self::push`] that emphasizes advancing a streaming state.
    pub fn advance(&mut self, value: Option<f64>) -> Option<Option<f64>> {
        self.push(value)
    }

    /// Iterates over the current window from oldest to newest without copying it.
    #[must_use]
    pub fn iter(&self) -> impl ExactSizeIterator<Item = &Option<f64>> + DoubleEndedIterator + '_ {
        self.values.iter()
    }

    /// Iterates over copied current values from oldest to newest.
    ///
    /// This is convenient for kernels that consume scalar values while keeping
    /// storage bounded by the configured window.
    #[must_use]
    pub fn values(&self) -> impl ExactSizeIterator<Item = Option<f64>> + DoubleEndedIterator + '_ {
        self.values.iter().copied()
    }

    /// Returns the number of observations currently retained.
    #[must_use]
    pub fn len(&self) -> usize {
        self.values.len()
    }

    /// Returns whether no observations have been retained yet.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.values.is_empty()
    }

    /// Returns the number of non-null observations in the current window.
    #[must_use]
    pub const fn valid_count(&self) -> usize {
        self.valid_count
    }

    /// Reports whether at least `min_periods` non-null observations are present.
    ///
    /// A minimum must be positive and cannot exceed the configured window.
    ///
    /// # Errors
    ///
    /// Returns an invalid-state error when `min_periods` is zero or wider than
    /// the configured window.
    pub fn is_ready(&self, min_periods: usize) -> Result<bool, VectorCoreError> {
        self.validate_min_periods(min_periods)?;
        Ok(self.valid_count >= min_periods)
    }

    /// Alias for [`Self::is_ready`].
    ///
    /// # Errors
    ///
    /// Returns the same invalid-state errors as [`Self::is_ready`].
    pub fn ready(&self, min_periods: usize) -> Result<bool, VectorCoreError> {
        self.is_ready(min_periods)
    }

    /// Returns bounded-state accounting for this rolling window.
    #[must_use]
    pub fn profile(&self) -> StateProfile {
        StateProfile {
            retained: self.values.len(),
            capacity: self.window,
            peak: self.peak,
        }
    }

    fn validate_min_periods(&self, min_periods: usize) -> Result<(), VectorCoreError> {
        if min_periods == 0 || min_periods > self.window {
            return Err(VectorCoreError::InvalidState(format!(
                "min_periods must be in 1..={} for a rolling window",
                self.window
            )));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::{RollingWindowState, ShiftState};
    use crate::VectorCoreError;

    #[test]
    fn shift_continues_across_batch_boundaries() {
        let mut state = ShiftState::new(2).expect("positive lag");

        let first_batch = [Some(10.0), Some(20.0)];
        let second_batch = [Some(30.0), Some(40.0)];

        assert_eq!(first_batch.map(|value| state.push(value)), [None, None]);
        assert_eq!(
            second_batch.map(|value| state.push(value)),
            [Some(10.0), Some(20.0)]
        );
        assert_eq!(state.profile().retained, 2);
    }

    #[test]
    fn state_preserves_null_nan_and_negative_zero() {
        let nan = f64::from_bits(0x7ff8_0000_0000_0001);
        let negative_zero = -0.0;
        let mut shift = ShiftState::new(1).expect("positive lag");

        assert_eq!(shift.push(None), None);
        assert_eq!(shift.push(Some(nan)), None);
        assert!(shift.push(Some(negative_zero)).is_some_and(f64::is_nan));
        assert_eq!(
            shift.push(None).expect("negative zero").to_bits(),
            negative_zero.to_bits()
        );

        let mut window = RollingWindowState::new(3).expect("positive window");
        window.push(None);
        window.push(Some(nan));
        window.push(Some(negative_zero));
        let values: Vec<_> = window.values().collect();
        assert_eq!(values[0], None);
        assert!(values[1].expect("NaN").is_nan());
        assert_eq!(
            values[2].expect("negative zero").to_bits(),
            negative_zero.to_bits()
        );
        assert_eq!(window.valid_count(), 2);
    }

    #[test]
    fn rolling_window_evicts_oldest_values_in_order() {
        let mut state = RollingWindowState::new(3).expect("positive window");
        assert_eq!(state.push(Some(1.0)), None);
        assert_eq!(state.push(None), None);
        assert_eq!(state.push(Some(3.0)), None);
        assert_eq!(state.push(Some(4.0)), Some(Some(1.0)));
        assert_eq!(state.push(Some(5.0)), Some(None));
        assert_eq!(
            state.values().collect::<Vec<_>>(),
            [Some(3.0), Some(4.0), Some(5.0)]
        );
        assert_eq!(state.valid_count(), 3);
    }

    #[test]
    fn readiness_uses_non_null_min_periods() {
        let mut state = RollingWindowState::new(3).expect("positive window");
        state.push(Some(1.0));
        state.push(None);
        assert!(!state.is_ready(2).expect("valid min_periods"));
        state.push(Some(3.0));
        assert!(state.is_ready(2).expect("valid min_periods"));
        assert!(!state.is_ready(3).expect("valid min_periods"));
    }

    #[test]
    fn long_stream_never_exceeds_configured_memory_bound() {
        let mut shift = ShiftState::new(7).expect("positive lag");
        let mut rolling = RollingWindowState::new(11).expect("positive window");

        for row in 0..10_000 {
            let value = (row % 5 != 0).then_some(f64::from(row));
            shift.push(value);
            rolling.push(value);
            assert!(shift.profile().retained <= shift.profile().capacity);
            assert!(rolling.profile().retained <= rolling.profile().capacity);
        }

        assert_eq!(shift.profile().peak, 7);
        assert_eq!(rolling.profile().peak, 11);
    }

    #[test]
    fn zero_bounds_and_invalid_min_periods_are_rejected() {
        assert!(matches!(
            ShiftState::new(0),
            Err(VectorCoreError::InvalidState(_))
        ));
        assert!(matches!(
            RollingWindowState::new(0),
            Err(VectorCoreError::InvalidState(_))
        ));

        let state = RollingWindowState::new(2).expect("positive window");
        assert!(matches!(
            state.is_ready(0),
            Err(VectorCoreError::InvalidState(_))
        ));
        assert!(matches!(
            state.is_ready(3),
            Err(VectorCoreError::InvalidState(_))
        ));
    }
}
