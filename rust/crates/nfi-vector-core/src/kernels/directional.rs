//! Exact TA-Lib directional and range-position kernels.
//!
//! The update order and extrema tie handling here deliberately follow TA-Lib
//! v0.6.4.  Each function returns one value per input row; values before the
//! upstream lookback are canonical `f64::NAN`.

use std::collections::VecDeque;

use serde_json::{Map, Value};

use crate::VectorCoreError;

const TA_EPSILON: f64 = 0.000_000_000_000_01;
const MAX_PERIOD: usize = 100_000;

/// Period-bounded streaming state for directional TA-Lib operations.
#[derive(Debug)]
pub(super) enum DirectionalStream {
    Aroon(AroonStream),
    Willr(WillrStream),
    PlusDi(DirectionalIndexStream),
    MinusDi(DirectionalIndexStream),
    Adx(AdxStream),
}

impl DirectionalStream {
    /// Executes one Arrow-sized chunk and returns only that chunk's output rows.
    pub(super) fn execute(&mut self, inputs: &[&[f64]]) -> Result<Vec<Vec<f64>>, VectorCoreError> {
        match self {
            Self::Aroon(state) => {
                let rows = validate_stream_inputs("AROON", inputs, 2)?;
                let mut down = Vec::with_capacity(rows);
                let mut up = Vec::with_capacity(rows);
                for (&high, &low) in inputs[0].iter().zip(inputs[1]) {
                    let (current_down, current_up) = state.push(high, low)?;
                    down.push(current_down);
                    up.push(current_up);
                }
                Ok(vec![down, up])
            }
            Self::Willr(state) => {
                validate_stream_inputs("WILLR", inputs, 3)?;
                Ok(vec![inputs[0]
                    .iter()
                    .zip(inputs[1])
                    .zip(inputs[2])
                    .map(|((&high, &low), &close)| state.push(high, low, close))
                    .collect::<Result<Vec<_>, _>>()?])
            }
            Self::PlusDi(state) | Self::MinusDi(state) => {
                validate_stream_inputs("directional index", inputs, 3)?;
                Ok(vec![inputs[0]
                    .iter()
                    .zip(inputs[1])
                    .zip(inputs[2])
                    .map(|((&high, &low), &close)| state.push(high, low, close))
                    .collect::<Result<Vec<_>, _>>()?])
            }
            Self::Adx(state) => {
                validate_stream_inputs("ADX", inputs, 3)?;
                Ok(vec![inputs[0]
                    .iter()
                    .zip(inputs[1])
                    .zip(inputs[2])
                    .map(|((&high, &low), &close)| state.push(high, low, close))
                    .collect::<Result<Vec<_>, _>>()?])
            }
        }
    }

    /// Returns the number of currently retained input rows, not historical rows.
    #[must_use]
    pub(super) fn retained(&self) -> usize {
        match self {
            Self::Aroon(state) => state.values.len(),
            Self::Willr(state) => state.values.len(),
            Self::PlusDi(state) | Self::MinusDi(state) => state.retained(),
            Self::Adx(state) => state.retained(),
        }
    }
}

/// Builds streaming state for a supported directional operation.
pub(super) fn stream(
    name: &str,
    arguments: &Map<String, Value>,
) -> Result<Option<DirectionalStream>, VectorCoreError> {
    if !matches!(name, "AROON" | "WILLR" | "PLUS_DI" | "MINUS_DI" | "ADX") {
        return Ok(None);
    }
    let period = stream_period(arguments)?;
    match name {
        "AROON" => {
            validate_period("AROON", period, 2)?;
            Ok(Some(DirectionalStream::Aroon(AroonStream::new(period)?)))
        }
        "WILLR" => {
            validate_period("WILLR", period, 2)?;
            Ok(Some(DirectionalStream::Willr(WillrStream::new(period)?)))
        }
        "PLUS_DI" => {
            validate_period("PLUS_DI", period, 1)?;
            Ok(Some(DirectionalStream::PlusDi(
                DirectionalIndexStream::new(period, Direction::Plus),
            )))
        }
        "MINUS_DI" => {
            validate_period("MINUS_DI", period, 1)?;
            Ok(Some(DirectionalStream::MinusDi(
                DirectionalIndexStream::new(period, Direction::Minus),
            )))
        }
        "ADX" => {
            validate_period("ADX", period, 2)?;
            Ok(Some(DirectionalStream::Adx(AdxStream::new(period))))
        }
        _ => Ok(None),
    }
}

#[derive(Debug)]
pub(super) struct AroonStream {
    period: usize,
    seen: usize,
    values: VecDeque<(f64, f64)>,
    lowest_index: Option<usize>,
    highest_index: Option<usize>,
    lowest: f64,
    highest: f64,
}

impl AroonStream {
    fn new(period: usize) -> Result<Self, VectorCoreError> {
        let mut values = VecDeque::new();
        values.try_reserve_exact(period + 1).map_err(|error| {
            VectorCoreError::InvalidState(format!("AROON stream cannot retain its window: {error}"))
        })?;
        Ok(Self {
            period,
            seen: 0,
            values,
            lowest_index: None,
            highest_index: None,
            lowest: 0.0,
            highest: 0.0,
        })
    }

    fn push(&mut self, high: f64, low: f64) -> Result<(f64, f64), VectorCoreError> {
        let today = self.seen;
        self.advance_seen()?;
        self.values.push_back((high, low));
        if self.values.len() > self.period + 1 {
            self.values.pop_front();
        }
        if today < self.period {
            return Ok((f64::NAN, f64::NAN));
        }

        let trailing = today - self.period;
        if self.lowest_index.is_none_or(|index| index < trailing) {
            let mut index = trailing;
            self.lowest_index = Some(index);
            self.lowest = self.low_at(today, index);
            while index < today {
                index += 1;
                let value = self.low_at(today, index);
                if value <= self.lowest {
                    self.lowest_index = Some(index);
                    self.lowest = value;
                }
            }
        } else if low <= self.lowest {
            self.lowest_index = Some(today);
            self.lowest = low;
        }

        if self.highest_index.is_none_or(|index| index < trailing) {
            let mut index = trailing;
            self.highest_index = Some(index);
            self.highest = self.high_at(today, index);
            while index < today {
                index += 1;
                let value = self.high_at(today, index);
                if value >= self.highest {
                    self.highest_index = Some(index);
                    self.highest = value;
                }
            }
        } else if high >= self.highest {
            self.highest_index = Some(today);
            self.highest = high;
        }

        let factor = 100.0 / period_as_f64(self.period);
        let highest = self
            .highest_index
            .expect("AROON stream index is initialized");
        let lowest = self
            .lowest_index
            .expect("AROON stream index is initialized");
        Ok((
            factor * period_as_f64(self.period - (today - lowest)),
            factor * period_as_f64(self.period - (today - highest)),
        ))
    }

    fn high_at(&self, today: usize, index: usize) -> f64 {
        self.values[index - (today + 1 - self.values.len())].0
    }

    fn low_at(&self, today: usize, index: usize) -> f64 {
        self.values[index - (today + 1 - self.values.len())].1
    }

    fn advance_seen(&mut self) -> Result<(), VectorCoreError> {
        self.seen = self.seen.checked_add(1).ok_or_else(|| {
            VectorCoreError::InvalidState("AROON stream row count overflow".to_owned())
        })?;
        Ok(())
    }
}

#[derive(Debug)]
pub(super) struct WillrStream {
    period: usize,
    seen: usize,
    values: VecDeque<(f64, f64, f64)>,
    lowest_index: Option<usize>,
    highest_index: Option<usize>,
    lowest: f64,
    highest: f64,
    difference: f64,
}

impl WillrStream {
    fn new(period: usize) -> Result<Self, VectorCoreError> {
        let mut values = VecDeque::new();
        values.try_reserve_exact(period).map_err(|error| {
            VectorCoreError::InvalidState(format!("WILLR stream cannot retain its window: {error}"))
        })?;
        Ok(Self {
            period,
            seen: 0,
            values,
            lowest_index: None,
            highest_index: None,
            lowest: 0.0,
            highest: 0.0,
            difference: 0.0,
        })
    }

    fn push(&mut self, high: f64, low: f64, close: f64) -> Result<f64, VectorCoreError> {
        let today = self.seen;
        self.advance_seen()?;
        self.values.push_back((high, low, close));
        if self.values.len() > self.period {
            self.values.pop_front();
        }
        let lookback = self.period - 1;
        if today < lookback {
            return Ok(f64::NAN);
        }

        let trailing = today - lookback;
        if self.lowest_index.is_none_or(|index| index < trailing) {
            let mut index = trailing;
            self.lowest_index = Some(index);
            self.lowest = self.low_at(today, index);
            while index < today {
                index += 1;
                let value = self.low_at(today, index);
                if value < self.lowest {
                    self.lowest_index = Some(index);
                    self.lowest = value;
                }
            }
            self.difference = (self.highest - self.lowest) / -100.0;
        } else if low <= self.lowest {
            self.lowest_index = Some(today);
            self.lowest = low;
            self.difference = (self.highest - self.lowest) / -100.0;
        }

        if self.highest_index.is_none_or(|index| index < trailing) {
            let mut index = trailing;
            self.highest_index = Some(index);
            self.highest = self.high_at(today, index);
            while index < today {
                index += 1;
                let value = self.high_at(today, index);
                if value > self.highest {
                    self.highest_index = Some(index);
                    self.highest = value;
                }
            }
            self.difference = (self.highest - self.lowest) / -100.0;
        } else if high >= self.highest {
            self.highest_index = Some(today);
            self.highest = high;
            self.difference = (self.highest - self.lowest) / -100.0;
        }

        Ok(if self.difference == 0.0 {
            0.0
        } else {
            (self.highest - close) / self.difference
        })
    }

    fn high_at(&self, today: usize, index: usize) -> f64 {
        self.values[index - (today + 1 - self.values.len())].0
    }

    fn low_at(&self, today: usize, index: usize) -> f64 {
        self.values[index - (today + 1 - self.values.len())].1
    }

    fn advance_seen(&mut self) -> Result<(), VectorCoreError> {
        self.seen = self.seen.checked_add(1).ok_or_else(|| {
            VectorCoreError::InvalidState("WILLR stream row count overflow".to_owned())
        })?;
        Ok(())
    }
}

#[derive(Debug)]
pub(super) struct DirectionalIndexStream {
    period: usize,
    direction: Direction,
    seen: usize,
    previous: Option<(f64, f64, f64)>,
    previous_dm: f64,
    previous_tr: f64,
}

impl DirectionalIndexStream {
    const fn new(period: usize, direction: Direction) -> Self {
        Self {
            period,
            direction,
            seen: 0,
            previous: None,
            previous_dm: 0.0,
            previous_tr: 0.0,
        }
    }

    fn push(&mut self, high: f64, low: f64, close: f64) -> Result<f64, VectorCoreError> {
        let today = self.seen;
        self.advance_seen()?;
        let Some((previous_high, previous_low, previous_close)) = self.previous else {
            self.previous = Some((high, low, close));
            return Ok(f64::NAN);
        };
        let diff_plus = high - previous_high;
        let diff_minus = previous_low - low;
        let selected = self.selected(diff_plus, diff_minus);
        let directional_move = self.selected_move(diff_plus, diff_minus, selected);

        if self.period == 1 {
            self.previous = Some((high, low, close));
            return Ok(if selected {
                let range = true_range(high, low, previous_close);
                if ta_is_zero(range) {
                    0.0
                } else {
                    directional_move / range
                }
            } else {
                0.0
            });
        }
        if today < self.period {
            self.previous_dm += directional_move;
            let range = true_range(high, low, previous_close);
            self.previous_tr += range;
            self.previous = Some((high, low, close));
            return Ok(f64::NAN);
        }

        let period = period_as_f64(self.period);
        self.previous_dm = self.previous_dm - (self.previous_dm / period) + directional_move;
        let range = true_range(high, low, previous_close);
        self.previous_tr = self.previous_tr - (self.previous_tr / period) + range;
        self.previous = Some((high, low, close));
        Ok(if ta_is_zero(self.previous_tr) {
            0.0
        } else {
            100.0 * (self.previous_dm / self.previous_tr)
        })
    }

    fn selected(&self, diff_plus: f64, diff_minus: f64) -> bool {
        match self.direction {
            Direction::Plus => diff_plus > 0.0 && diff_plus > diff_minus,
            Direction::Minus => diff_minus > 0.0 && diff_plus < diff_minus,
        }
    }

    fn selected_move(&self, diff_plus: f64, diff_minus: f64, selected: bool) -> f64 {
        if !selected {
            return 0.0;
        }
        match self.direction {
            Direction::Plus => diff_plus,
            Direction::Minus => diff_minus,
        }
    }

    fn retained(&self) -> usize {
        usize::from(self.previous.is_some())
    }

    fn advance_seen(&mut self) -> Result<(), VectorCoreError> {
        self.seen = self.seen.checked_add(1).ok_or_else(|| {
            VectorCoreError::InvalidState("directional index stream row count overflow".to_owned())
        })?;
        Ok(())
    }
}

#[derive(Debug)]
pub(super) struct AdxStream {
    period: usize,
    seen: usize,
    previous: Option<(f64, f64, f64)>,
    previous_minus_dm: f64,
    previous_plus_dm: f64,
    previous_tr: f64,
    dx_total: f64,
    previous_adx: f64,
}

impl AdxStream {
    const fn new(period: usize) -> Self {
        Self {
            period,
            seen: 0,
            previous: None,
            previous_minus_dm: 0.0,
            previous_plus_dm: 0.0,
            previous_tr: 0.0,
            dx_total: 0.0,
            previous_adx: 0.0,
        }
    }

    fn push(&mut self, high: f64, low: f64, close: f64) -> Result<f64, VectorCoreError> {
        let today = self.seen;
        self.advance_seen()?;
        let Some((previous_high, previous_low, previous_close)) = self.previous else {
            self.previous = Some((high, low, close));
            return Ok(f64::NAN);
        };
        let diff_plus = high - previous_high;
        let diff_minus = previous_low - low;

        if today < self.period {
            self.add_initial_move(diff_plus, diff_minus);
            let range = true_range(high, low, previous_close);
            self.previous_tr += range;
            self.previous = Some((high, low, close));
            return Ok(f64::NAN);
        }

        self.smooth_move(diff_plus, diff_minus);
        let range = true_range(high, low, previous_close);
        let period = period_as_f64(self.period);
        self.previous_tr = self.previous_tr - (self.previous_tr / period) + range;
        self.previous = Some((high, low, close));
        if today < 2 * self.period {
            self.add_dx();
            if today < (2 * self.period) - 1 {
                return Ok(f64::NAN);
            }
            self.previous_adx = self.dx_total / period;
            return Ok(self.previous_adx);
        }

        self.update_adx();
        Ok(self.previous_adx)
    }

    fn add_initial_move(&mut self, diff_plus: f64, diff_minus: f64) {
        if diff_minus > 0.0 && diff_plus < diff_minus {
            self.previous_minus_dm += diff_minus;
        } else if diff_plus > 0.0 && diff_plus > diff_minus {
            self.previous_plus_dm += diff_plus;
        }
    }

    fn smooth_move(&mut self, diff_plus: f64, diff_minus: f64) {
        let period = period_as_f64(self.period);
        self.previous_minus_dm -= self.previous_minus_dm / period;
        self.previous_plus_dm -= self.previous_plus_dm / period;
        self.add_initial_move(diff_plus, diff_minus);
    }

    fn add_dx(&mut self) {
        if !ta_is_zero(self.previous_tr) {
            let minus_di = 100.0 * (self.previous_minus_dm / self.previous_tr);
            let plus_di = 100.0 * (self.previous_plus_dm / self.previous_tr);
            let directional_total = minus_di + plus_di;
            if !ta_is_zero(directional_total) {
                self.dx_total += 100.0 * ((minus_di - plus_di).abs() / directional_total);
            }
        }
    }

    fn update_adx(&mut self) {
        if !ta_is_zero(self.previous_tr) {
            let minus_di = 100.0 * (self.previous_minus_dm / self.previous_tr);
            let plus_di = 100.0 * (self.previous_plus_dm / self.previous_tr);
            let directional_total = minus_di + plus_di;
            if !ta_is_zero(directional_total) {
                let dx = 100.0 * ((minus_di - plus_di).abs() / directional_total);
                self.previous_adx = ((self.previous_adx * period_as_f64(self.period - 1)) + dx)
                    / period_as_f64(self.period);
            }
        }
    }

    fn retained(&self) -> usize {
        usize::from(self.previous.is_some())
    }

    fn advance_seen(&mut self) -> Result<(), VectorCoreError> {
        self.seen = self.seen.checked_add(1).ok_or_else(|| {
            VectorCoreError::InvalidState("ADX stream row count overflow".to_owned())
        })?;
        Ok(())
    }
}

/// Calculates TA-Lib AROON, returning `(down, up)`.
pub(super) fn aroon(
    high: &[f64],
    low: &[f64],
    period: usize,
) -> Result<(Vec<f64>, Vec<f64>), VectorCoreError> {
    validate_two_inputs("AROON", high, low)?;
    validate_period("AROON", period, 2)?;

    let mut down = nan_output(high.len());
    let mut up = nan_output(high.len());
    if high.len() <= period {
        return Ok((down, up));
    }

    let factor = 100.0 / period_as_f64(period);
    let mut lowest_index: Option<usize> = None;
    let mut highest_index: Option<usize> = None;
    let mut lowest = 0.0;
    let mut highest = 0.0;

    for (trailing, today) in (period..high.len()).enumerate() {
        let low_today = low[today];
        if lowest_index.is_none_or(|index| index < trailing) {
            let mut index = trailing;
            lowest_index = Some(index);
            lowest = low[index];
            while index < today {
                index += 1;
                let value = low[index];
                if value <= lowest {
                    lowest_index = Some(index);
                    lowest = value;
                }
            }
        } else if low_today <= lowest {
            lowest_index = Some(today);
            lowest = low_today;
        }

        let high_today = high[today];
        if highest_index.is_none_or(|index| index < trailing) {
            let mut index = trailing;
            highest_index = Some(index);
            highest = high[index];
            while index < today {
                index += 1;
                let value = high[index];
                if value >= highest {
                    highest_index = Some(index);
                    highest = value;
                }
            }
        } else if high_today >= highest {
            highest_index = Some(today);
            highest = high_today;
        }

        let highest_index = highest_index.expect("AROON highest index is initialized");
        let lowest_index = lowest_index.expect("AROON lowest index is initialized");
        up[today] = factor * period_as_f64(period - (today - highest_index));
        down[today] = factor * period_as_f64(period - (today - lowest_index));
    }

    Ok((down, up))
}

/// Calculates TA-Lib WILLR.
pub(super) fn willr(
    high: &[f64],
    low: &[f64],
    close: &[f64],
    period: usize,
) -> Result<Vec<f64>, VectorCoreError> {
    validate_ohlc("WILLR", high, low, close)?;
    validate_period("WILLR", period, 2)?;

    let mut output = nan_output(high.len());
    let lookback = period - 1;
    if high.len() <= lookback {
        return Ok(output);
    }

    let mut lowest_index: Option<usize> = None;
    let mut highest_index: Option<usize> = None;
    let mut lowest = 0.0;
    let mut highest = 0.0;
    let mut difference = 0.0;

    for (trailing, today) in (lookback..high.len()).enumerate() {
        let low_today = low[today];
        if lowest_index.is_none_or(|index| index < trailing) {
            let mut index = trailing;
            lowest_index = Some(index);
            lowest = low[index];
            while index < today {
                index += 1;
                let value = low[index];
                if value < lowest {
                    lowest_index = Some(index);
                    lowest = value;
                }
            }
            difference = (highest - lowest) / -100.0;
        } else if low_today <= lowest {
            lowest_index = Some(today);
            lowest = low_today;
            difference = (highest - lowest) / -100.0;
        }

        let high_today = high[today];
        if highest_index.is_none_or(|index| index < trailing) {
            let mut index = trailing;
            highest_index = Some(index);
            highest = high[index];
            while index < today {
                index += 1;
                let value = high[index];
                if value > highest {
                    highest_index = Some(index);
                    highest = value;
                }
            }
            difference = (highest - lowest) / -100.0;
        } else if high_today >= highest {
            highest_index = Some(today);
            highest = high_today;
            difference = (highest - lowest) / -100.0;
        }

        output[today] = if difference == 0.0 {
            0.0
        } else {
            (highest - close[today]) / difference
        };
    }

    Ok(output)
}

/// Calculates TA-Lib `PLUS_DI`.
pub(super) fn plus_di(
    high: &[f64],
    low: &[f64],
    close: &[f64],
    period: usize,
) -> Result<Vec<f64>, VectorCoreError> {
    validate_ohlc("PLUS_DI", high, low, close)?;
    validate_period("PLUS_DI", period, 1)?;
    Ok(directional_index(high, low, close, period, Direction::Plus))
}

/// Calculates TA-Lib `MINUS_DI`.
pub(super) fn minus_di(
    high: &[f64],
    low: &[f64],
    close: &[f64],
    period: usize,
) -> Result<Vec<f64>, VectorCoreError> {
    validate_ohlc("MINUS_DI", high, low, close)?;
    validate_period("MINUS_DI", period, 1)?;
    Ok(directional_index(
        high,
        low,
        close,
        period,
        Direction::Minus,
    ))
}

/// Calculates TA-Lib ADX.
pub(super) fn adx(
    high: &[f64],
    low: &[f64],
    close: &[f64],
    period: usize,
) -> Result<Vec<f64>, VectorCoreError> {
    validate_ohlc("ADX", high, low, close)?;
    validate_period("ADX", period, 2)?;

    let mut output = nan_output(high.len());
    let lookback = (2 * period) - 1;
    if high.len() <= lookback {
        return Ok(output);
    }

    let mut today = 0;
    let mut previous_high = high[today];
    let mut previous_low = low[today];
    let mut previous_close = close[today];
    let mut previous_minus_dm = 0.0;
    let mut previous_plus_dm = 0.0;
    let mut previous_tr = 0.0;
    let period_f64 = period_as_f64(period);
    let period_minus_one_f64 = period_as_f64(period - 1);

    for _ in 0..(period - 1) {
        today += 1;
        let current_high = high[today];
        let diff_plus = current_high - previous_high;
        previous_high = current_high;
        let current_low = low[today];
        let diff_minus = previous_low - current_low;
        previous_low = current_low;

        if diff_minus > 0.0 && diff_plus < diff_minus {
            previous_minus_dm += diff_minus;
        } else if diff_plus > 0.0 && diff_plus > diff_minus {
            previous_plus_dm += diff_plus;
        }

        let range = true_range(previous_high, previous_low, previous_close);
        previous_tr += range;
        previous_close = close[today];
    }

    let mut dx_total = 0.0;
    for _ in 0..period {
        today += 1;
        let current_high = high[today];
        let diff_plus = current_high - previous_high;
        previous_high = current_high;
        let current_low = low[today];
        let diff_minus = previous_low - current_low;
        previous_low = current_low;

        previous_minus_dm -= previous_minus_dm / period_f64;
        previous_plus_dm -= previous_plus_dm / period_f64;
        if diff_minus > 0.0 && diff_plus < diff_minus {
            previous_minus_dm += diff_minus;
        } else if diff_plus > 0.0 && diff_plus > diff_minus {
            previous_plus_dm += diff_plus;
        }

        let range = true_range(previous_high, previous_low, previous_close);
        previous_tr = previous_tr - (previous_tr / period_f64) + range;
        previous_close = close[today];

        if !ta_is_zero(previous_tr) {
            let minus_di = 100.0 * (previous_minus_dm / previous_tr);
            let plus_di = 100.0 * (previous_plus_dm / previous_tr);
            let directional_total = minus_di + plus_di;
            if !ta_is_zero(directional_total) {
                dx_total += 100.0 * ((minus_di - plus_di).abs() / directional_total);
            }
        }
    }

    let mut previous_adx = dx_total / period_f64;
    output[today] = previous_adx;

    while today + 1 < high.len() {
        today += 1;
        let current_high = high[today];
        let diff_plus = current_high - previous_high;
        previous_high = current_high;
        let current_low = low[today];
        let diff_minus = previous_low - current_low;
        previous_low = current_low;

        previous_minus_dm -= previous_minus_dm / period_f64;
        previous_plus_dm -= previous_plus_dm / period_f64;
        if diff_minus > 0.0 && diff_plus < diff_minus {
            previous_minus_dm += diff_minus;
        } else if diff_plus > 0.0 && diff_plus > diff_minus {
            previous_plus_dm += diff_plus;
        }

        let range = true_range(previous_high, previous_low, previous_close);
        previous_tr = previous_tr - (previous_tr / period_f64) + range;
        previous_close = close[today];

        if !ta_is_zero(previous_tr) {
            let minus_di = 100.0 * (previous_minus_dm / previous_tr);
            let plus_di = 100.0 * (previous_plus_dm / previous_tr);
            let directional_total = minus_di + plus_di;
            if !ta_is_zero(directional_total) {
                let dx = 100.0 * ((minus_di - plus_di).abs() / directional_total);
                previous_adx = ((previous_adx * period_minus_one_f64) + dx) / period_f64;
            }
        }
        output[today] = previous_adx;
    }

    Ok(output)
}

#[derive(Clone, Copy, Debug)]
enum Direction {
    Plus,
    Minus,
}

#[allow(clippy::too_many_lines)] // Keeps TA-Lib's ordered initialization and smoothing phases intact.
fn directional_index(
    high: &[f64],
    low: &[f64],
    close: &[f64],
    period: usize,
    direction: Direction,
) -> Vec<f64> {
    let mut output = nan_output(high.len());
    if high.len() <= period {
        return output;
    }

    if period == 1 {
        for today in 1..high.len() {
            let previous_high = high[today - 1];
            let previous_low = low[today - 1];
            let previous_close = close[today - 1];
            let current_high = high[today];
            let diff_plus = current_high - previous_high;
            let current_low = low[today];
            let diff_minus = previous_low - current_low;
            let range = true_range(current_high, current_low, previous_close);
            let selected = match direction {
                Direction::Plus => diff_plus > 0.0 && diff_plus > diff_minus,
                Direction::Minus => diff_minus > 0.0 && diff_plus < diff_minus,
            };
            output[today] = if selected && !ta_is_zero(range) {
                match direction {
                    Direction::Plus => diff_plus / range,
                    Direction::Minus => diff_minus / range,
                }
            } else {
                0.0
            };
        }
        return output;
    }

    let mut today = 0;
    let mut previous_high = high[today];
    let mut previous_low = low[today];
    let mut previous_close = close[today];
    let mut previous_dm = 0.0;
    let mut previous_tr = 0.0;
    let period_f64 = period_as_f64(period);

    for _ in 0..(period - 1) {
        today += 1;
        let current_high = high[today];
        let diff_plus = current_high - previous_high;
        previous_high = current_high;
        let current_low = low[today];
        let diff_minus = previous_low - current_low;
        previous_low = current_low;
        let selected = match direction {
            Direction::Plus => diff_plus > 0.0 && diff_plus > diff_minus,
            Direction::Minus => diff_minus > 0.0 && diff_plus < diff_minus,
        };
        if selected {
            previous_dm += match direction {
                Direction::Plus => diff_plus,
                Direction::Minus => diff_minus,
            };
        }
        let range = true_range(previous_high, previous_low, previous_close);
        previous_tr += range;
        previous_close = close[today];
    }

    today += 1;
    let current_high = high[today];
    let diff_plus = current_high - previous_high;
    previous_high = current_high;
    let current_low = low[today];
    let diff_minus = previous_low - current_low;
    previous_low = current_low;
    let selected = match direction {
        Direction::Plus => diff_plus > 0.0 && diff_plus > diff_minus,
        Direction::Minus => diff_minus > 0.0 && diff_plus < diff_minus,
    };
    previous_dm = previous_dm - (previous_dm / period_f64)
        + if selected {
            match direction {
                Direction::Plus => diff_plus,
                Direction::Minus => diff_minus,
            }
        } else {
            0.0
        };
    let range = true_range(previous_high, previous_low, previous_close);
    previous_tr = previous_tr - (previous_tr / period_f64) + range;
    previous_close = close[today];
    output[today] = if ta_is_zero(previous_tr) {
        0.0
    } else {
        100.0 * (previous_dm / previous_tr)
    };

    while today + 1 < high.len() {
        today += 1;
        let current_high = high[today];
        let diff_plus = current_high - previous_high;
        previous_high = current_high;
        let current_low = low[today];
        let diff_minus = previous_low - current_low;
        previous_low = current_low;
        let selected = match direction {
            Direction::Plus => diff_plus > 0.0 && diff_plus > diff_minus,
            Direction::Minus => diff_minus > 0.0 && diff_plus < diff_minus,
        };
        previous_dm = previous_dm - (previous_dm / period_f64)
            + if selected {
                match direction {
                    Direction::Plus => diff_plus,
                    Direction::Minus => diff_minus,
                }
            } else {
                0.0
            };
        let range = true_range(previous_high, previous_low, previous_close);
        previous_tr = previous_tr - (previous_tr / period_f64) + range;
        previous_close = close[today];
        output[today] = if ta_is_zero(previous_tr) {
            0.0
        } else {
            100.0 * (previous_dm / previous_tr)
        };
    }

    output
}

fn true_range(high: f64, low: f64, previous_close: f64) -> f64 {
    let mut range = high - low;
    let high_close = (high - previous_close).abs();
    if high_close > range {
        range = high_close;
    }
    let low_close = (low - previous_close).abs();
    if low_close > range {
        range = low_close;
    }
    range
}

fn ta_is_zero(value: f64) -> bool {
    -TA_EPSILON < value && value < TA_EPSILON
}

fn nan_output(len: usize) -> Vec<f64> {
    vec![f64::NAN; len]
}

fn period_as_f64(period: usize) -> f64 {
    f64::from(u32::try_from(period).expect("validated TA-Lib period fits in u32"))
}

fn stream_period(arguments: &Map<String, Value>) -> Result<usize, VectorCoreError> {
    arguments.get("timeperiod").map_or(Ok(14), |value| {
        value
            .as_u64()
            .and_then(|period| usize::try_from(period).ok())
            .ok_or_else(|| {
                VectorCoreError::InvalidState("invalid integer argument timeperiod".to_owned())
            })
    })
}

fn validate_stream_inputs(
    name: &str,
    inputs: &[&[f64]],
    expected_inputs: usize,
) -> Result<usize, VectorCoreError> {
    if inputs.len() != expected_inputs {
        return Err(VectorCoreError::InvalidState(format!(
            "TA-Lib {name} stream requires {expected_inputs} inputs, got {}",
            inputs.len()
        )));
    }
    let rows = inputs.first().map_or(0, |input| input.len());
    if inputs.iter().any(|input| input.len() != rows) {
        return Err(VectorCoreError::InvalidState(format!(
            "TA-Lib {name} stream input lengths differ"
        )));
    }
    Ok(rows)
}

fn validate_two_inputs(name: &str, first: &[f64], second: &[f64]) -> Result<(), VectorCoreError> {
    if first.len() != second.len() {
        return Err(VectorCoreError::InvalidProgram(format!(
            "{name} input lengths differ: {} and {}",
            first.len(),
            second.len()
        )));
    }
    Ok(())
}

fn validate_ohlc(
    name: &str,
    high: &[f64],
    low: &[f64],
    close: &[f64],
) -> Result<(), VectorCoreError> {
    validate_two_inputs(name, high, low)?;
    if high.len() != close.len() {
        return Err(VectorCoreError::InvalidProgram(format!(
            "{name} input lengths differ: high has {}, close has {}",
            high.len(),
            close.len()
        )));
    }
    Ok(())
}

fn validate_period(name: &str, period: usize, minimum: usize) -> Result<(), VectorCoreError> {
    if !(minimum..=MAX_PERIOD).contains(&period) {
        return Err(VectorCoreError::InvalidProgram(format!(
            "{name} period must be in {minimum}..={MAX_PERIOD}, got {period}"
        )));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use serde_json::{json, Map};

    use super::{adx, aroon, minus_di, plus_di, stream, willr};
    use crate::VectorCoreError;

    const HIGH: [f64; 12] = [
        10.0, 12.0, 11.0, 13.0, 14.0, 13.0, 15.0, 16.0, 15.0, 17.0, 18.0, 17.0,
    ];
    const LOW: [f64; 12] = [
        8.0, 9.0, 8.0, 10.0, 11.0, 10.0, 12.0, 13.0, 12.0, 14.0, 15.0, 14.0,
    ];
    const CLOSE: [f64; 12] = [
        9.0, 11.0, 9.0, 12.0, 13.0, 11.0, 14.0, 15.0, 13.0, 16.0, 17.0, 15.0,
    ];
    const NAN_BITS: u64 = f64::NAN.to_bits();

    #[test]
    fn canonical_nan_warmups_follow_talib_lookbacks() {
        let (down, up) = aroon(&HIGH, &LOW, 3).expect("valid AROON");
        let willr = willr(&HIGH, &LOW, &CLOSE, 3).expect("valid WILLR");
        let plus = plus_di(&HIGH, &LOW, &CLOSE, 3).expect("valid PLUS_DI");
        let minus = minus_di(&HIGH, &LOW, &CLOSE, 3).expect("valid MINUS_DI");
        let adx = adx(&HIGH, &LOW, &CLOSE, 3).expect("valid ADX");

        assert!(down[..3].iter().all(|value| value.to_bits() == NAN_BITS));
        assert!(up[..3].iter().all(|value| value.to_bits() == NAN_BITS));
        assert!(willr[..2].iter().all(|value| value.to_bits() == NAN_BITS));
        assert!(plus[..3].iter().all(|value| value.to_bits() == NAN_BITS));
        assert!(minus[..3].iter().all(|value| value.to_bits() == NAN_BITS));
        assert!(adx[..5].iter().all(|value| value.to_bits() == NAN_BITS));
    }

    #[test]
    fn aroon_and_willr_preserve_talib_tie_rules() {
        let high = [2.0, 3.0, 3.0, 2.0, 3.0];
        let low = [1.0, 0.0, 0.0, 1.0, 0.0];
        let close = [1.5, 1.5, 2.0, 1.5, 2.5];
        let (down, up) = aroon(&high, &low, 2).expect("valid AROON");
        let willr = willr(&high, &low, &close, 2).expect("valid WILLR");

        assert_eq!(down[2].to_bits(), 100.0_f64.to_bits());
        assert_eq!(up[2].to_bits(), 100.0_f64.to_bits());
        assert_eq!(down[3].to_bits(), 50.0_f64.to_bits());
        assert_eq!(up[3].to_bits(), 50.0_f64.to_bits());
        assert_eq!(willr[1].to_bits(), (-50.0_f64).to_bits());
        assert_eq!(willr[2].to_bits(), (-100.0_f64 / 3.0).to_bits());
    }

    #[test]
    fn directional_outputs_match_pinned_talib_bits() {
        let plus = plus_di(&HIGH, &LOW, &CLOSE, 3).expect("valid PLUS_DI");
        let minus = minus_di(&HIGH, &LOW, &CLOSE, 3).expect("valid MINUS_DI");
        let adx = adx(&HIGH, &LOW, &CLOSE, 3).expect("valid ADX");

        assert_bits(
            &plus[3..],
            &[
                0x4044_d555_5555_5556,
                0x4043_5555_5555_5555,
                0x4039_1bb4_a404_6ed2,
                0x4041_af39_f97c_6ad4,
                0x4041_5cad_1d42_a470,
                0x4037_89bf_5e4e_cf52,
                0x4040_f40d_4d13_d643,
                0x4040_dd99_9dac_b748,
                0x4037_1ee8_0d25_7e49,
            ],
        );
        assert_bits(
            &minus[3..],
            &[
                0x4020_aaaa_aaaa_aaab,
                0x4015_5555_5555_5556,
                0x402e_4d93_64d9_364e,
                0x4021_cfcb_e356_a2da,
                0x4018_5655_aac7_25f1,
                0x402d_b97c_661d_0902,
                0x4022_13dc_2208_4d3c,
                0x4019_17f9_7962_76ee,
                0x402d_921f_990d_d036,
            ],
        );
        assert_bits(
            &adx[5..],
            &[
                0x404b_dbf6_fdbf_6fdb,
                0x404c_88d5_b3f4_584d,
                0x404e_b847_f2be_85ed,
                0x4048_3ed2_a53b_d0db,
                0x4049_d086_9785_e5a0,
                0x404c_a62d_b0b4_fb03,
                0x4046_c3aa_b870_3d6b,
            ],
        );
    }

    #[test]
    fn zero_range_and_period_one_directional_index_follow_talib() {
        let flat = [1.0, 1.0, 1.0];
        let plus = plus_di(&flat, &flat, &flat, 1).expect("valid PLUS_DI");
        let minus = minus_di(&flat, &flat, &flat, 1).expect("valid MINUS_DI");
        assert_bits(&plus[1..], &[0, 0]);
        assert_bits(&minus[1..], &[0, 0]);
    }

    #[test]
    fn invalid_input_shapes_and_periods_fail_closed() {
        assert!(matches!(
            aroon(&HIGH[..2], &LOW[..1], 2),
            Err(VectorCoreError::InvalidProgram(_))
        ));
        assert!(matches!(
            willr(&HIGH, &LOW, &CLOSE[..11], 3),
            Err(VectorCoreError::InvalidProgram(_))
        ));
        assert!(matches!(
            plus_di(&HIGH, &LOW, &CLOSE, 0),
            Err(VectorCoreError::InvalidProgram(_))
        ));
        assert!(matches!(
            adx(&HIGH, &LOW, &CLOSE, 1),
            Err(VectorCoreError::InvalidProgram(_))
        ));
        assert!(matches!(
            aroon(&HIGH, &LOW, 100_001),
            Err(VectorCoreError::InvalidProgram(_))
        ));
    }

    #[test]
    fn streams_are_chunk_exact_for_every_directional_operation() {
        let mut arguments = Map::new();
        arguments.insert("timeperiod".to_owned(), json!(3));
        let chunks = [(0, 1), (1, 5), (5, 8), (8, HIGH.len())];

        let expected = [
            ("AROON", {
                let (down, up) = aroon(&HIGH, &LOW, 3).expect("valid AROON");
                vec![down, up]
            }),
            (
                "WILLR",
                vec![willr(&HIGH, &LOW, &CLOSE, 3).expect("valid WILLR")],
            ),
            (
                "PLUS_DI",
                vec![plus_di(&HIGH, &LOW, &CLOSE, 3).expect("valid PLUS_DI")],
            ),
            (
                "MINUS_DI",
                vec![minus_di(&HIGH, &LOW, &CLOSE, 3).expect("valid MINUS_DI")],
            ),
            ("ADX", vec![adx(&HIGH, &LOW, &CLOSE, 3).expect("valid ADX")]),
        ];

        for (name, expected) in expected {
            let mut state = stream(name, &arguments)
                .expect("valid stream arguments")
                .expect("directional stream");
            let mut actual = vec![Vec::new(); expected.len()];
            for (start, end) in chunks {
                let inputs: Vec<&[f64]> = if name == "AROON" {
                    vec![&HIGH[start..end], &LOW[start..end]]
                } else {
                    vec![&HIGH[start..end], &LOW[start..end], &CLOSE[start..end]]
                };
                let batch = state.execute(&inputs).expect("valid stream chunk");
                assert!(batch.iter().all(|output| output.len() == end - start));
                for (target, source) in actual.iter_mut().zip(batch) {
                    target.extend(source);
                }
            }
            for (actual, expected) in actual.iter().zip(expected) {
                assert_values_match(actual, &expected);
            }
        }
    }

    #[test]
    fn streams_retain_only_period_scale_state() {
        let mut arguments = Map::new();
        arguments.insert("timeperiod".to_owned(), json!(3));
        let high = (0..1_000).map(f64::from).collect::<Vec<_>>();
        let low = high.iter().map(|value| value - 1.0).collect::<Vec<_>>();
        let close = high.iter().map(|value| value - 0.5).collect::<Vec<_>>();

        for (name, expected_bound) in [
            ("AROON", 4),
            ("WILLR", 3),
            ("PLUS_DI", 1),
            ("MINUS_DI", 1),
            ("ADX", 1),
        ] {
            let mut state = stream(name, &arguments)
                .expect("valid stream arguments")
                .expect("directional stream");
            let inputs: Vec<&[f64]> = if name == "AROON" {
                vec![&high, &low]
            } else {
                vec![&high, &low, &close]
            };
            state.execute(&inputs).expect("valid stream chunk");
            assert!(state.retained() <= expected_bound, "{name}");
        }
    }

    fn assert_bits(actual: &[f64], expected: &[u64]) {
        assert_eq!(actual.len(), expected.len());
        for (actual, expected) in actual.iter().zip(expected) {
            assert_eq!(actual.to_bits(), *expected);
        }
    }

    fn assert_values_match(actual: &[f64], expected: &[f64]) {
        assert_eq!(actual.len(), expected.len());
        for (actual, expected) in actual.iter().zip(expected) {
            assert_eq!(actual.to_bits(), expected.to_bits());
        }
    }
}
