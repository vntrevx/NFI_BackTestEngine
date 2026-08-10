//! Exact batch-local ports of the supported TA-Lib oscillator kernels.
//!
//! These functions use the TA-Lib v0.6.4 default global settings: no unstable
//! period and default compatibility. Outputs retain the input length, with the
//! unavailable lookback prefix represented by canonical quiet NaNs.

use std::collections::VecDeque;

use serde_json::{Map, Value};

use crate::error::VectorCoreError;

const MAX_PERIOD: usize = 100_000;
const TA_EPSILON: f64 = 0.000_000_000_000_01;

/// TA-Lib RSI with the default compatibility and unstable-period settings.
pub(super) fn rsi(in_real: &[f64], period: usize) -> Result<Vec<f64>, VectorCoreError> {
    validate_period("RSI", "period", period, 2)?;
    let mut out = unavailable(in_real.len());
    if in_real.len() <= period {
        return Ok(out);
    }

    let mut today = 0;
    let mut prev_value = in_real[today];
    let mut prev_gain = 0.0;
    let mut prev_loss = 0.0;
    today += 1;
    for _ in 0..period {
        let temp_value_1 = in_real[today];
        today += 1;
        let temp_value_2 = temp_value_1 - prev_value;
        prev_value = temp_value_1;
        if temp_value_2 < 0.0 {
            prev_loss -= temp_value_2;
        } else {
            prev_gain += temp_value_2;
        }
    }

    prev_loss /= period_as_f64(period);
    prev_gain /= period_as_f64(period);
    let total = prev_gain + prev_loss;
    out[period] = if is_ta_zero(total) {
        0.0
    } else {
        100.0 * (prev_gain / total)
    };

    while today < in_real.len() {
        let current_value = in_real[today];
        today += 1;
        let temp_value_2 = current_value - prev_value;
        prev_value = current_value;

        prev_loss *= period_as_f64(period - 1);
        prev_gain *= period_as_f64(period - 1);
        if temp_value_2 < 0.0 {
            prev_loss -= temp_value_2;
        } else {
            prev_gain += temp_value_2;
        }
        prev_loss /= period_as_f64(period);
        prev_gain /= period_as_f64(period);
        let total = prev_gain + prev_loss;
        out[today - 1] = if is_ta_zero(total) {
            0.0
        } else {
            100.0 * (prev_gain / total)
        };
    }

    Ok(out)
}

/// TA-Lib CCI using the original circular-buffer summation order.
pub(super) fn cci(
    high: &[f64],
    low: &[f64],
    close: &[f64],
    period: usize,
) -> Result<Vec<f64>, VectorCoreError> {
    let len = validate_price_inputs("CCI", high, low, close)?;
    validate_period("CCI", "period", period, 2)?;
    let mut out = unavailable(len);
    let lookback = period - 1;
    if len <= lookback {
        return Ok(out);
    }

    let mut circular = vec![0.0; period];
    let mut circular_index = 0;
    let mut today = 0;
    while today < lookback {
        circular[circular_index] = typical_price(high, low, close, today);
        circular_index = next_index(circular_index, period);
        today += 1;
    }

    while today < len {
        let last_value = typical_price(high, low, close, today);
        circular[circular_index] = last_value;

        let mut average = 0.0;
        for value in &circular {
            average += *value;
        }
        average /= period_as_f64(period);

        let mut absolute_deviation_sum = 0.0;
        for value in &circular {
            absolute_deviation_sum += (*value - average).abs();
        }
        let difference = last_value - average;
        out[today] = if difference != 0.0 && absolute_deviation_sum != 0.0 {
            difference / (0.015 * (absolute_deviation_sum / period_as_f64(period)))
        } else {
            0.0
        };

        circular_index = next_index(circular_index, period);
        today += 1;
    }

    Ok(out)
}

/// TA-Lib MFI with the documented zero-price-movement treatment.
pub(super) fn mfi(
    high: &[f64],
    low: &[f64],
    close: &[f64],
    volume: &[f64],
    period: usize,
) -> Result<Vec<f64>, VectorCoreError> {
    let len = validate_price_inputs("MFI", high, low, close)?;
    validate_length("MFI", "volume", volume.len(), len)?;
    validate_period("MFI", "period", period, 2)?;
    let mut out = unavailable(len);
    if len <= period {
        return Ok(out);
    }

    let mut money_flow = vec![(0.0, 0.0); period];
    let mut money_flow_index = 0;
    let mut today = 0;
    let mut prev_value = typical_price(high, low, close, today);
    let mut positive_sum = 0.0;
    let mut negative_sum = 0.0;
    today += 1;
    for _ in 0..period {
        let mut temp_value_1 = typical_price(high, low, close, today);
        let temp_value_2 = temp_value_1 - prev_value;
        prev_value = temp_value_1;
        temp_value_1 *= volume[today];
        today += 1;
        let flow = classify_money_flow(temp_value_1, temp_value_2);
        positive_sum += flow.0;
        negative_sum += flow.1;
        money_flow[money_flow_index] = flow;
        money_flow_index = next_index(money_flow_index, period);
    }
    out[period] = mfi_value(positive_sum, negative_sum);

    while today < len {
        let outgoing = money_flow[money_flow_index];
        positive_sum -= outgoing.0;
        negative_sum -= outgoing.1;

        let mut temp_value_1 = typical_price(high, low, close, today);
        let temp_value_2 = temp_value_1 - prev_value;
        prev_value = temp_value_1;
        temp_value_1 *= volume[today];
        today += 1;
        let flow = classify_money_flow(temp_value_1, temp_value_2);
        positive_sum += flow.0;
        negative_sum += flow.1;
        money_flow[money_flow_index] = flow;
        out[today - 1] = mfi_value(positive_sum, negative_sum);
        money_flow_index = next_index(money_flow_index, period);
    }

    Ok(out)
}

/// TA-Lib OBV, including its initial-volume output at index zero.
pub(super) fn obv(in_real: &[f64], volume: &[f64]) -> Result<Vec<f64>, VectorCoreError> {
    validate_length("OBV", "volume", volume.len(), in_real.len())?;
    if in_real.is_empty() {
        return Ok(Vec::new());
    }

    let mut out = Vec::with_capacity(in_real.len());
    let mut previous_obv = volume[0];
    let mut previous_value = in_real[0];
    for (value, volume) in in_real.iter().zip(volume) {
        if *value > previous_value {
            previous_obv += *volume;
        } else if *value < previous_value {
            previous_obv -= *volume;
        }
        out.push(previous_obv);
        previous_value = *value;
    }
    Ok(out)
}

/// TA-Lib STOCHF with the only NFI-reachable Fast-D moving average, SMA (type 0).
pub(super) fn stochf(
    high: &[f64],
    low: &[f64],
    close: &[f64],
    k_period: usize,
    d_period: usize,
) -> Result<(Vec<f64>, Vec<f64>), VectorCoreError> {
    let len = validate_price_inputs("STOCHF", high, low, close)?;
    validate_period("STOCHF", "fast_k_period", k_period, 1)?;
    validate_period("STOCHF", "fast_d_period", d_period, 1)?;
    let lookback_k = k_period - 1;
    let lookback_fast_d = d_period - 1;
    let lookback_total = lookback_k + lookback_fast_d;
    let mut fast_k = unavailable(len);
    let mut fast_d = unavailable(len);
    if len <= lookback_total {
        return Ok((fast_k, fast_d));
    }

    let mut trailing = 0;
    let mut today = lookback_k;
    let mut lowest_index = None;
    let mut highest_index = None;
    let mut lowest = 0.0;
    let mut highest = 0.0;
    let mut difference = 0.0;
    let mut raw_fast_k = Vec::with_capacity(len - today);

    while today < len {
        let mut value = low[today];
        if lowest_index.is_none_or(|index| index < trailing) {
            lowest_index = Some(trailing);
            lowest = low[trailing];
            let mut index = trailing;
            while index < today {
                index += 1;
                value = low[index];
                if value < lowest {
                    lowest_index = Some(index);
                    lowest = value;
                }
            }
            difference = (highest - lowest) / 100.0;
        } else if value <= lowest {
            lowest_index = Some(today);
            lowest = value;
            difference = (highest - lowest) / 100.0;
        }

        value = high[today];
        if highest_index.is_none_or(|index| index < trailing) {
            highest_index = Some(trailing);
            highest = high[trailing];
            let mut index = trailing;
            while index < today {
                index += 1;
                value = high[index];
                if value > highest {
                    highest_index = Some(index);
                    highest = value;
                }
            }
            difference = (highest - lowest) / 100.0;
        } else if value >= highest {
            highest_index = Some(today);
            highest = value;
            difference = (highest - lowest) / 100.0;
        }

        raw_fast_k.push(if difference == 0.0 {
            0.0
        } else {
            (close[today] - lowest) / difference
        });
        trailing += 1;
        today += 1;
    }

    let smoothed_fast_d = simple_moving_average(&raw_fast_k, d_period);
    for (offset, value) in smoothed_fast_d.iter().enumerate() {
        let output_index = lookback_total + offset;
        fast_k[output_index] = raw_fast_k[lookback_fast_d + offset];
        fast_d[output_index] = *value;
    }
    Ok((fast_k, fast_d))
}

/// TA-Lib ULTOSC after its period ordering and true-range priming steps.
pub(super) fn ultosc(
    high: &[f64],
    low: &[f64],
    close: &[f64],
    period_1: usize,
    period_2: usize,
    period_3: usize,
) -> Result<Vec<f64>, VectorCoreError> {
    let len = validate_price_inputs("ULTOSC", high, low, close)?;
    validate_period("ULTOSC", "period_1", period_1, 1)?;
    validate_period("ULTOSC", "period_2", period_2, 1)?;
    validate_period("ULTOSC", "period_3", period_3, 1)?;
    let (period_1, period_2, period_3) = sort_ultosc_periods(period_1, period_2, period_3);
    let mut out = unavailable(len);
    let lookback = period_3;
    if len <= lookback {
        return Ok(out);
    }

    let (mut a1_total, mut b1_total) = prime_ultosc_totals(high, low, close, lookback, period_1);
    let (mut a2_total, mut b2_total) = prime_ultosc_totals(high, low, close, lookback, period_2);
    let (mut a3_total, mut b3_total) = prime_ultosc_totals(high, low, close, lookback, period_3);
    let mut today = lookback;
    let mut trailing_1 = today - period_1 + 1;
    let mut trailing_2 = today - period_2 + 1;
    let mut trailing_3 = today - period_3 + 1;

    while today < len {
        let (buying_pressure, true_range) = ultosc_terms(high, low, close, today);
        a1_total += buying_pressure;
        a2_total += buying_pressure;
        a3_total += buying_pressure;
        b1_total += true_range;
        b2_total += true_range;
        b3_total += true_range;

        let mut value = 0.0;
        if !is_ta_zero(b1_total) {
            value += 4.0 * (a1_total / b1_total);
        }
        if !is_ta_zero(b2_total) {
            value += 2.0 * (a2_total / b2_total);
        }
        if !is_ta_zero(b3_total) {
            value += a3_total / b3_total;
        }

        let (buying_pressure, true_range) = ultosc_terms(high, low, close, trailing_1);
        a1_total -= buying_pressure;
        b1_total -= true_range;
        let (buying_pressure, true_range) = ultosc_terms(high, low, close, trailing_2);
        a2_total -= buying_pressure;
        b2_total -= true_range;
        let (buying_pressure, true_range) = ultosc_terms(high, low, close, trailing_3);
        a3_total -= buying_pressure;
        b3_total -= true_range;

        out[today] = 100.0 * (value / 7.0);
        today += 1;
        trailing_1 += 1;
        trailing_2 += 1;
        trailing_3 += 1;
    }
    Ok(out)
}

/// Bounded, exact TA-Lib oscillator state for an ordered stream of batches.
#[derive(Debug)]
pub(super) enum OscillatorStream {
    Rsi(RsiStream),
    Cci(CciStream),
    Mfi(MfiStream),
    Obv(ObvStream),
    Stochf(StochfStream),
    Ultosc(UltOscStream),
}

impl OscillatorStream {
    /// Execute one ordered batch and return only values for that batch.
    pub(super) fn execute(&mut self, inputs: &[&[f64]]) -> Result<Vec<Vec<f64>>, VectorCoreError> {
        match self {
            Self::Rsi(state) => Ok(vec![state.execute(single_input("RSI", inputs)?)?]),
            Self::Cci(state) => Ok(vec![state.execute(price_inputs("CCI", inputs)?)?]),
            Self::Mfi(state) => Ok(vec![state.execute(price_volume_inputs("MFI", inputs)?)?]),
            Self::Obv(state) => Ok(vec![state.execute(obv_inputs(inputs)?)?]),
            Self::Stochf(state) => {
                let (fast_k, fast_d) = state.execute(price_inputs("STOCHF", inputs)?)?;
                Ok(vec![fast_k, fast_d])
            }
            Self::Ultosc(state) => Ok(vec![state.execute(price_inputs("ULTOSC", inputs)?)?]),
        }
    }

    /// Number of scalar values retained between batches.
    #[must_use]
    pub(super) fn retained(&self) -> usize {
        match self {
            Self::Rsi(state) => state.retained(),
            Self::Cci(state) => state.retained(),
            Self::Mfi(state) => state.retained(),
            Self::Obv(state) => state.retained(),
            Self::Stochf(state) => state.retained(),
            Self::Ultosc(state) => state.retained(),
        }
    }
}

/// Create bounded streaming state for an implemented oscillator, if applicable.
pub(super) fn stream(
    name: &str,
    arguments: &Map<String, Value>,
) -> Result<Option<OscillatorStream>, VectorCoreError> {
    let state = match name {
        "RSI" => {
            let period = argument_period(arguments, "timeperiod", 14)?;
            validate_period("RSI", "period", period, 2)?;
            OscillatorStream::Rsi(RsiStream::new(period))
        }
        "CCI" => {
            let period = argument_period(arguments, "timeperiod", 14)?;
            validate_period("CCI", "period", period, 2)?;
            OscillatorStream::Cci(CciStream::new(period))
        }
        "MFI" => {
            let period = argument_period(arguments, "timeperiod", 14)?;
            validate_period("MFI", "period", period, 2)?;
            OscillatorStream::Mfi(MfiStream::new(period))
        }
        "OBV" => OscillatorStream::Obv(ObvStream::default()),
        "STOCHF" => {
            if argument_period(arguments, "fastd_matype", 0)? != 0 {
                return Err(VectorCoreError::InvalidState(
                    "TA-Lib STOCHF only supports exact SMA fast-D".to_owned(),
                ));
            }
            let k_period = argument_period(arguments, "fastk_period", 5)?;
            let d_period = argument_period(arguments, "fastd_period", 3)?;
            validate_period("STOCHF", "fast_k_period", k_period, 1)?;
            validate_period("STOCHF", "fast_d_period", d_period, 1)?;
            OscillatorStream::Stochf(StochfStream::new(k_period, d_period))
        }
        "ULTOSC" => {
            let period_1 = argument_period(arguments, "timeperiod1", 7)?;
            let period_2 = argument_period(arguments, "timeperiod2", 14)?;
            let period_3 = argument_period(arguments, "timeperiod3", 28)?;
            validate_period("ULTOSC", "period_1", period_1, 1)?;
            validate_period("ULTOSC", "period_2", period_2, 1)?;
            validate_period("ULTOSC", "period_3", period_3, 1)?;
            OscillatorStream::Ultosc(UltOscStream::new(period_1, period_2, period_3))
        }
        _ => return Ok(None),
    };
    Ok(Some(state))
}

#[derive(Debug)]
pub(super) struct RsiStream {
    period: usize,
    previous: Option<f64>,
    changes: usize,
    gain: f64,
    loss: f64,
    initialized: bool,
}

impl RsiStream {
    fn new(period: usize) -> Self {
        Self {
            period,
            previous: None,
            changes: 0,
            gain: 0.0,
            loss: 0.0,
            initialized: false,
        }
    }

    fn execute(&mut self, values: &[f64]) -> Result<Vec<f64>, VectorCoreError> {
        validate_period("RSI", "period", self.period, 2)?;
        let mut out = unavailable(values.len());
        for (index, value) in values.iter().enumerate() {
            let Some(previous) = self.previous else {
                self.previous = Some(*value);
                continue;
            };
            let change = *value - previous;
            self.previous = Some(*value);
            if self.initialized {
                self.loss *= period_as_f64(self.period - 1);
                self.gain *= period_as_f64(self.period - 1);
                if change < 0.0 {
                    self.loss -= change;
                } else {
                    self.gain += change;
                }
                self.loss /= period_as_f64(self.period);
                self.gain /= period_as_f64(self.period);
                out[index] = rsi_value(self.gain, self.loss);
            } else {
                if change < 0.0 {
                    self.loss -= change;
                } else {
                    self.gain += change;
                }
                self.changes += 1;
                if self.changes == self.period {
                    self.loss /= period_as_f64(self.period);
                    self.gain /= period_as_f64(self.period);
                    self.initialized = true;
                    out[index] = rsi_value(self.gain, self.loss);
                }
            }
        }
        Ok(out)
    }

    fn retained(&self) -> usize {
        usize::from(self.previous.is_some()) + 2
    }
}

#[derive(Debug)]
pub(super) struct CciStream {
    period: usize,
    values: Vec<f64>,
    next: usize,
    count: usize,
}

impl CciStream {
    fn new(period: usize) -> Self {
        Self {
            period,
            values: Vec::new(),
            next: 0,
            count: 0,
        }
    }

    fn execute(
        &mut self,
        (high, low, close): PriceInputs<'_>,
    ) -> Result<Vec<f64>, VectorCoreError> {
        let len = validate_price_inputs("CCI", high, low, close)?;
        validate_period("CCI", "period", self.period, 2)?;
        if self.values.is_empty() {
            self.values = vec![0.0; self.period];
        }
        let mut out = unavailable(len);
        for (index, ((high, low), close)) in high.iter().zip(low).zip(close).enumerate() {
            let last = (*high + *low + *close) / 3.0;
            self.values[self.next] = last;
            self.next = next_index(self.next, self.period);
            self.count = self.count.saturating_add(1);
            if self.count >= self.period {
                let mut average = 0.0;
                for value in &self.values {
                    average += *value;
                }
                average /= period_as_f64(self.period);
                let mut deviation = 0.0;
                for value in &self.values {
                    deviation += (*value - average).abs();
                }
                let difference = last - average;
                out[index] = if difference != 0.0 && deviation != 0.0 {
                    difference / (0.015 * (deviation / period_as_f64(self.period)))
                } else {
                    0.0
                };
            }
        }
        Ok(out)
    }

    fn retained(&self) -> usize {
        self.values.len()
    }
}

#[derive(Debug)]
pub(super) struct MfiStream {
    period: usize,
    previous: Option<f64>,
    values: Vec<(f64, f64)>,
    next: usize,
    count: usize,
    positive: f64,
    negative: f64,
}

impl MfiStream {
    fn new(period: usize) -> Self {
        Self {
            period,
            previous: None,
            values: Vec::new(),
            next: 0,
            count: 0,
            positive: 0.0,
            negative: 0.0,
        }
    }

    fn execute(
        &mut self,
        (high, low, close, volume): PriceVolumeInputs<'_>,
    ) -> Result<Vec<f64>, VectorCoreError> {
        let len = validate_price_inputs("MFI", high, low, close)?;
        validate_length("MFI", "volume", volume.len(), len)?;
        validate_period("MFI", "period", self.period, 2)?;
        if self.values.is_empty() {
            self.values = vec![(0.0, 0.0); self.period];
        }
        let mut out = unavailable(len);
        for index in 0..len {
            let typical = typical_price(high, low, close, index);
            let Some(previous) = self.previous else {
                self.previous = Some(typical);
                continue;
            };
            if self.count >= self.period {
                let outgoing = self.values[self.next];
                self.positive -= outgoing.0;
                self.negative -= outgoing.1;
            }
            let flow = classify_money_flow(typical * volume[index], typical - previous);
            self.previous = Some(typical);
            self.positive += flow.0;
            self.negative += flow.1;
            self.values[self.next] = flow;
            self.next = next_index(self.next, self.period);
            self.count = self.count.saturating_add(1);
            if self.count >= self.period {
                out[index] = mfi_value(self.positive, self.negative);
            }
        }
        Ok(out)
    }

    fn retained(&self) -> usize {
        usize::from(self.previous.is_some()) + (self.values.len() * 2) + 2
    }
}

#[derive(Debug, Default)]
pub(super) struct ObvStream {
    previous: Option<f64>,
    value: f64,
}

impl ObvStream {
    fn execute(&mut self, (values, volume): ObvInputs<'_>) -> Result<Vec<f64>, VectorCoreError> {
        validate_length("OBV", "volume", volume.len(), values.len())?;
        let mut out = Vec::with_capacity(values.len());
        for (value, volume) in values.iter().zip(volume) {
            if let Some(previous) = self.previous {
                if *value > previous {
                    self.value += *volume;
                } else if *value < previous {
                    self.value -= *volume;
                }
            } else {
                self.value = *volume;
            }
            self.previous = Some(*value);
            out.push(self.value);
        }
        Ok(out)
    }

    fn retained(&self) -> usize {
        usize::from(self.previous.is_some()) + 1
    }
}

#[derive(Debug)]
pub(super) struct StochfStream {
    k_period: usize,
    d_period: usize,
    prices: VecDeque<(f64, f64)>,
    raw_values: VecDeque<f64>,
    raw_total: f64,
    seen: usize,
    lowest_index: Option<usize>,
    highest_index: Option<usize>,
    lowest: f64,
    highest: f64,
    difference: f64,
}

impl StochfStream {
    fn new(k_period: usize, d_period: usize) -> Self {
        Self {
            k_period,
            d_period,
            prices: VecDeque::new(),
            raw_values: VecDeque::new(),
            raw_total: 0.0,
            seen: 0,
            lowest_index: None,
            highest_index: None,
            lowest: 0.0,
            highest: 0.0,
            difference: 0.0,
        }
    }

    fn execute(
        &mut self,
        (high, low, close): PriceInputs<'_>,
    ) -> Result<(Vec<f64>, Vec<f64>), VectorCoreError> {
        let len = validate_price_inputs("STOCHF", high, low, close)?;
        validate_period("STOCHF", "fast_k_period", self.k_period, 1)?;
        validate_period("STOCHF", "fast_d_period", self.d_period, 1)?;
        let mut fast_k = unavailable(len);
        let mut fast_d = unavailable(len);
        for index in 0..len {
            let today = self.seen;
            self.prices.push_back((high[index], low[index]));
            if self.prices.len() > self.k_period {
                self.prices.pop_front();
            }
            if self.prices.len() == self.k_period {
                let trailing = today + 1 - self.k_period;
                self.update_extrema(trailing, today);
                let raw = if self.difference == 0.0 {
                    0.0
                } else {
                    (close[index] - self.lowest) / self.difference
                };
                self.raw_total += raw;
                self.raw_values.push_back(raw);
                if self.raw_values.len() == self.d_period {
                    let average = self.raw_total / period_as_f64(self.d_period);
                    let oldest = self.raw_values.pop_front().expect("non-empty SMA state");
                    self.raw_total -= oldest;
                    fast_k[index] = raw;
                    fast_d[index] = average;
                }
            }
            self.seen = self.seen.saturating_add(1);
        }
        Ok((fast_k, fast_d))
    }

    fn update_extrema(&mut self, trailing: usize, today: usize) {
        let current_low = self.prices.back().expect("current price exists").1;
        if self.lowest_index.is_none_or(|index| index < trailing) {
            self.lowest_index = Some(trailing);
            self.lowest = self.prices.front().expect("window exists").1;
            for (offset, (_, value)) in self.prices.iter().enumerate().skip(1) {
                if *value < self.lowest {
                    self.lowest_index = Some(trailing + offset);
                    self.lowest = *value;
                }
            }
            self.difference = (self.highest - self.lowest) / 100.0;
        } else if current_low <= self.lowest {
            self.lowest_index = Some(today);
            self.lowest = current_low;
            self.difference = (self.highest - self.lowest) / 100.0;
        }

        let current_high = self.prices.back().expect("current price exists").0;
        if self.highest_index.is_none_or(|index| index < trailing) {
            self.highest_index = Some(trailing);
            self.highest = self.prices.front().expect("window exists").0;
            for (offset, (value, _)) in self.prices.iter().enumerate().skip(1) {
                if *value > self.highest {
                    self.highest_index = Some(trailing + offset);
                    self.highest = *value;
                }
            }
            self.difference = (self.highest - self.lowest) / 100.0;
        } else if current_high >= self.highest {
            self.highest_index = Some(today);
            self.highest = current_high;
            self.difference = (self.highest - self.lowest) / 100.0;
        }
    }

    fn retained(&self) -> usize {
        (self.prices.len() * 2) + self.raw_values.len() + 3
    }
}

#[derive(Debug)]
pub(super) struct UltOscStream {
    periods: (usize, usize, usize),
    previous: Option<f64>,
    seen: usize,
    pending: VecDeque<(f64, f64)>,
    totals: [(f64, f64); 3],
    windows: [VecDeque<(f64, f64)>; 3],
    initialized: bool,
}

impl UltOscStream {
    fn new(period_1: usize, period_2: usize, period_3: usize) -> Self {
        Self {
            periods: sort_ultosc_periods(period_1, period_2, period_3),
            previous: None,
            seen: 0,
            pending: VecDeque::new(),
            totals: [(0.0, 0.0); 3],
            windows: std::array::from_fn(|_| VecDeque::new()),
            initialized: false,
        }
    }

    fn execute(
        &mut self,
        (high, low, close): PriceInputs<'_>,
    ) -> Result<Vec<f64>, VectorCoreError> {
        let len = validate_price_inputs("ULTOSC", high, low, close)?;
        validate_period("ULTOSC", "period_1", self.periods.0, 1)?;
        validate_period("ULTOSC", "period_2", self.periods.1, 1)?;
        validate_period("ULTOSC", "period_3", self.periods.2, 1)?;
        let mut out = unavailable(len);
        for index in 0..len {
            let Some(previous) = self.previous else {
                self.previous = Some(close[index]);
                self.seen = 1;
                continue;
            };
            let term = ultosc_term(high[index], low[index], close[index], previous);
            self.previous = Some(close[index]);
            let today = self.seen;
            if self.initialized {
                for total in &mut self.totals {
                    total.0 += term.0;
                    total.1 += term.1;
                }
                out[index] = self.value();
                self.remove_trailing(term);
            } else if today < self.periods.2 {
                self.pending.push_back(term);
            } else {
                self.initialize(term);
                out[index] = self.value();
                self.finish_initialization(term);
                self.initialized = true;
                self.pending.clear();
            }
            self.seen = self.seen.saturating_add(1);
        }
        Ok(out)
    }

    fn initialize(&mut self, current: (f64, f64)) {
        for (index, period) in self.period_values().into_iter().enumerate() {
            let mut total = (0.0, 0.0);
            let retained = period - 1;
            for value in self.pending.iter().skip(self.pending.len() - retained) {
                total.0 += value.0;
                total.1 += value.1;
            }
            total.0 += current.0;
            total.1 += current.1;
            self.totals[index] = total;

            let mut window = VecDeque::new();
            if period > 1 {
                for value in self.pending.iter().skip(self.pending.len() - (period - 2)) {
                    window.push_back(*value);
                }
                window.push_back(current);
            }
            self.windows[index] = window;
        }
    }

    fn value(&self) -> f64 {
        let mut value = 0.0;
        if !is_ta_zero(self.totals[0].1) {
            value += 4.0 * (self.totals[0].0 / self.totals[0].1);
        }
        if !is_ta_zero(self.totals[1].1) {
            value += 2.0 * (self.totals[1].0 / self.totals[1].1);
        }
        if !is_ta_zero(self.totals[2].1) {
            value += self.totals[2].0 / self.totals[2].1;
        }
        100.0 * (value / 7.0)
    }

    fn remove_trailing(&mut self, current: (f64, f64)) {
        for (index, period) in self.period_values().into_iter().enumerate() {
            let outgoing = if period == 1 {
                current
            } else {
                self.windows[index]
                    .pop_front()
                    .expect("bounded ULTOSC window has a trailing term")
            };
            self.totals[index].0 -= outgoing.0;
            self.totals[index].1 -= outgoing.1;
            if period > 1 {
                self.windows[index].push_back(current);
            }
        }
    }

    fn finish_initialization(&mut self, current: (f64, f64)) {
        for (index, period) in self.period_values().into_iter().enumerate() {
            let outgoing = if period == 1 {
                current
            } else {
                self.pending[self.pending.len() - (period - 1)]
            };
            self.totals[index].0 -= outgoing.0;
            self.totals[index].1 -= outgoing.1;
        }
    }

    fn period_values(&self) -> [usize; 3] {
        [self.periods.0, self.periods.1, self.periods.2]
    }

    fn retained(&self) -> usize {
        let window_values = self.windows.iter().map(VecDeque::len).sum::<usize>();
        usize::from(self.previous.is_some()) + (self.pending.len() * 2) + (window_values * 2) + 6
    }
}

type PriceInputs<'a> = (&'a [f64], &'a [f64], &'a [f64]);
type PriceVolumeInputs<'a> = (&'a [f64], &'a [f64], &'a [f64], &'a [f64]);
type ObvInputs<'a> = (&'a [f64], &'a [f64]);

fn single_input<'a>(name: &str, inputs: &[&'a [f64]]) -> Result<&'a [f64], VectorCoreError> {
    if let [values] = inputs {
        Ok(values)
    } else {
        Err(stream_input_error(name, "requires exactly one input"))
    }
}

fn price_inputs<'a>(name: &str, inputs: &[&'a [f64]]) -> Result<PriceInputs<'a>, VectorCoreError> {
    if let [high, low, close] = inputs {
        Ok((high, low, close))
    } else {
        Err(stream_input_error(
            name,
            "requires high, low, and close inputs",
        ))
    }
}

fn price_volume_inputs<'a>(
    name: &str,
    inputs: &[&'a [f64]],
) -> Result<PriceVolumeInputs<'a>, VectorCoreError> {
    if let [high, low, close, volume] = inputs {
        Ok((high, low, close, volume))
    } else {
        Err(stream_input_error(
            name,
            "requires high, low, close, and volume inputs",
        ))
    }
}

fn obv_inputs<'a>(inputs: &[&'a [f64]]) -> Result<ObvInputs<'a>, VectorCoreError> {
    if let [real, volume] = inputs {
        Ok((real, volume))
    } else {
        Err(stream_input_error("OBV", "requires real and volume inputs"))
    }
}

fn argument_period(
    arguments: &Map<String, Value>,
    name: &str,
    default: usize,
) -> Result<usize, VectorCoreError> {
    arguments.get(name).map_or(Ok(default), |value| {
        value
            .as_u64()
            .and_then(|value| usize::try_from(value).ok())
            .ok_or_else(|| {
                VectorCoreError::InvalidState(format!("invalid integer argument {name}"))
            })
    })
}

fn stream_input_error(name: &str, message: &str) -> VectorCoreError {
    VectorCoreError::InvalidState(format!("TA-Lib {name}: {message}"))
}

fn rsi_value(gain: f64, loss: f64) -> f64 {
    let total = gain + loss;
    if is_ta_zero(total) {
        0.0
    } else {
        100.0 * (gain / total)
    }
}

fn ultosc_term(high: f64, low: f64, close: f64, previous_close: f64) -> (f64, f64) {
    let true_low = if low < previous_close {
        low
    } else {
        previous_close
    };
    let buying_pressure = close - true_low;
    let mut true_range = high - low;
    let mut temporary = (previous_close - high).abs();
    if temporary > true_range {
        true_range = temporary;
    }
    temporary = (previous_close - low).abs();
    if temporary > true_range {
        true_range = temporary;
    }
    (buying_pressure, true_range)
}

fn unavailable(len: usize) -> Vec<f64> {
    vec![f64::NAN; len]
}

fn validate_period(
    kernel: &str,
    parameter: &str,
    period: usize,
    minimum: usize,
) -> Result<(), VectorCoreError> {
    if (minimum..=MAX_PERIOD).contains(&period) {
        Ok(())
    } else {
        Err(VectorCoreError::InvalidState(format!(
            "{kernel} {parameter} must be in {minimum}..={MAX_PERIOD}, got {period}"
        )))
    }
}

fn validate_price_inputs(
    kernel: &str,
    high: &[f64],
    low: &[f64],
    close: &[f64],
) -> Result<usize, VectorCoreError> {
    let len = high.len();
    validate_length(kernel, "low", low.len(), len)?;
    validate_length(kernel, "close", close.len(), len)?;
    Ok(len)
}

fn validate_length(
    kernel: &str,
    name: &str,
    actual: usize,
    expected: usize,
) -> Result<(), VectorCoreError> {
    if actual == expected {
        Ok(())
    } else {
        Err(VectorCoreError::InvalidState(format!(
            "{kernel} {name} length {actual} does not match expected length {expected}"
        )))
    }
}

fn is_ta_zero(value: f64) -> bool {
    (-TA_EPSILON < value) && (value < TA_EPSILON)
}

fn typical_price(high: &[f64], low: &[f64], close: &[f64], index: usize) -> f64 {
    (high[index] + low[index] + close[index]) / 3.0
}

fn next_index(index: usize, length: usize) -> usize {
    if index + 1 == length {
        0
    } else {
        index + 1
    }
}

fn period_as_f64(period: usize) -> f64 {
    f64::from(u32::try_from(period).expect("validated TA-Lib period fits u32"))
}

fn classify_money_flow(value: f64, change: f64) -> (f64, f64) {
    if change < 0.0 {
        (0.0, value)
    } else if change > 0.0 {
        (value, 0.0)
    } else {
        (0.0, 0.0)
    }
}

fn mfi_value(positive_sum: f64, negative_sum: f64) -> f64 {
    let total = positive_sum + negative_sum;
    if total < 1.0 {
        0.0
    } else {
        100.0 * (positive_sum / total)
    }
}

fn simple_moving_average(values: &[f64], period: usize) -> Vec<f64> {
    let lookback = period - 1;
    if values.len() <= lookback {
        return Vec::new();
    }
    let mut total = 0.0;
    let mut trailing = 0;
    let mut today = 0;
    while today < lookback {
        total += values[today];
        today += 1;
    }

    let mut out = Vec::with_capacity(values.len() - lookback);
    while today < values.len() {
        total += values[today];
        let current = total;
        total -= values[trailing];
        out.push(current / period_as_f64(period));
        trailing += 1;
        today += 1;
    }
    out
}

fn sort_ultosc_periods(period_1: usize, period_2: usize, period_3: usize) -> (usize, usize, usize) {
    let periods = [period_1, period_2, period_3];
    let mut used = [false; 3];
    let mut sorted = [0; 3];
    for slot in &mut sorted {
        let mut longest_period = 0;
        let mut longest_index = 0;
        for input_index in 0..3 {
            if !used[input_index] && periods[input_index] > longest_period {
                longest_period = periods[input_index];
                longest_index = input_index;
            }
        }
        used[longest_index] = true;
        *slot = longest_period;
    }
    (sorted[2], sorted[1], sorted[0])
}

fn prime_ultosc_totals(
    high: &[f64],
    low: &[f64],
    close: &[f64],
    start: usize,
    period: usize,
) -> (f64, f64) {
    let mut buying_pressure_total = 0.0;
    let mut true_range_total = 0.0;
    for day in start - period + 1..start {
        let (buying_pressure, true_range) = ultosc_terms(high, low, close, day);
        buying_pressure_total += buying_pressure;
        true_range_total += true_range;
    }
    (buying_pressure_total, true_range_total)
}

fn ultosc_terms(high: &[f64], low: &[f64], close: &[f64], day: usize) -> (f64, f64) {
    let temp_low = low[day];
    let temp_high = high[day];
    let previous_close = close[day - 1];
    let true_low = if temp_low < previous_close {
        temp_low
    } else {
        previous_close
    };
    let buying_pressure = close[day] - true_low;
    let mut true_range = temp_high - temp_low;
    let mut temp = (previous_close - temp_high).abs();
    if temp > true_range {
        true_range = temp;
    }
    temp = (previous_close - temp_low).abs();
    if temp > true_range {
        true_range = temp;
    }
    (buying_pressure, true_range)
}

#[cfg(test)]
mod tests {
    use super::*;

    const HIGH: [f64; 16] = [
        10.0, 11.0, 12.0, 13.0, 12.5, 14.0, 15.0, 14.5, 16.0, 17.0, 16.5, 18.0, 19.0, 18.5, 20.0,
        21.0,
    ];
    const LOW: [f64; 16] = [
        8.0, 9.0, 10.0, 11.0, 10.5, 12.0, 13.0, 12.5, 14.0, 15.0, 14.5, 16.0, 17.0, 16.5, 18.0,
        19.0,
    ];
    const CLOSE: [f64; 16] = [
        9.0, 10.5, 10.5, 12.5, 11.0, 13.5, 13.5, 13.0, 15.5, 16.0, 15.0, 17.5, 18.0, 17.0, 19.5,
        20.0,
    ];
    const VOLUME: [f64; 16] = [
        100.0, 120.0, 80.0, 110.0, 90.0, 130.0, 140.0, 100.0, 150.0, 160.0, 125.0, 170.0, 180.0,
        140.0, 190.0, 200.0,
    ];

    #[test]
    fn exact_bits_match_pinned_python_talib_for_supported_oscillators() {
        rsi_matches_pinned_bits();
        cci_matches_pinned_bits();
        mfi_matches_pinned_bits();
        obv_matches_pinned_bits();
        stochf_matches_pinned_bits();
        ultosc_matches_pinned_bits();
    }

    fn rsi_matches_pinned_bits() {
        assert_bits(
            &rsi(&CLOSE, 3).expect("valid RSI"),
            &[
                0x7ff8_0000_0000_0000,
                0x7ff8_0000_0000_0000,
                0x7ff8_0000_0000_0000,
                0x4059_0000_0000_0000,
                0x404e_6f4d_e9bd_37a8,
                0x4054_0e10_e10e_10e1,
                0x4054_0e10_e10e_10e1,
                0x4050_678c_f19e_33c6,
                0x4055_5da4_c9e1_98a1,
                0x4055_e6f7_0872_dc4a,
                0x404e_5c53_a205_511e,
                0x4054_6f42_0fca_c0cd,
                0x4055_10ea_f6d9_3545,
                0x404d_c6b8_fdc6_679e,
                0x4054_2f2a_5d21_0807,
                0x4054_d692_4e7f_1db6,
            ],
        );
    }

    fn cci_matches_pinned_bits() {
        assert_bits(
            &cci(&HIGH, &LOW, &CLOSE, 3).expect("valid CCI"),
            &[
                0x7ff8_0000_0000_0000,
                0x7ff8_0000_0000_0000,
                0x4054_d555_5555_555a,
                0x4059_0000_0000_0001,
                0xc02e_c4ec_4ec4_ec3a,
                0x4059_0000_0000_0007,
                0x4052_44ec_4ec4_ec51,
                0xc03c_9249_2492_4900,
                0x4058_ffff_ffff_fff4,
                0x4053_71c7_1c71_c71d,
                0xc040_aaaa_aaaa_aa9b,
                0x4058_ffff_ffff_fffa,
                0x4053_71c7_1c71_c71e,
                0xc040_aaaa_aaaa_aacd,
                0x4058_ffff_ffff_fff4,
                0x4053_71c7_1c71_c71c,
            ],
        );
    }

    fn mfi_matches_pinned_bits() {
        assert_bits(
            &mfi(&HIGH, &LOW, &CLOSE, &VOLUME, 3).expect("valid MFI"),
            &[
                0x7ff8_0000_0000_0000,
                0x7ff8_0000_0000_0000,
                0x7ff8_0000_0000_0000,
                0x4059_0000_0000_0000,
                0x4051_17d0_5f41_7d06,
                0x4052_bc11_9c9e_8193,
                0x4053_89a4_6bd8_e1ee,
                0x4052_4f0d_7256_d8b0,
                0x4052_fd13_77a6_be0f,
                0x4053_9897_1180_08da,
                0x4051_e72a_268c_6aec,
                0x4052_8538_b73e_99d2,
                0x4053_10e8_7cb2_97a5,
                0x4051_eef4_05c3_dd37,
                0x4052_7b87_e82a_2b8c,
                0x4052_f979_535b_1181,
            ],
        );
    }

    fn obv_matches_pinned_bits() {
        assert_bits(
            &obv(&CLOSE, &VOLUME).expect("valid OBV"),
            &[
                0x4059_0000_0000_0000,
                0x406b_8000_0000_0000,
                0x406b_8000_0000_0000,
                0x4074_a000_0000_0000,
                0x406e_0000_0000_0000,
                0x4077_2000_0000_0000,
                0x4077_2000_0000_0000,
                0x4070_e000_0000_0000,
                0x407a_4000_0000_0000,
                0x4082_2000_0000_0000,
                0x407c_7000_0000_0000,
                0x4083_8800_0000_0000,
                0x4089_2800_0000_0000,
                0x4084_c800_0000_0000,
                0x408a_b800_0000_0000,
                0x4090_7c00_0000_0000,
            ],
        );
    }

    fn stochf_matches_pinned_bits() {
        let (fast_k, fast_d) = stochf(&HIGH, &LOW, &CLOSE, 3, 2).expect("valid STOCHF");
        assert_bits(
            &fast_k,
            &[
                0x7ff8_0000_0000_0000,
                0x7ff8_0000_0000_0000,
                0x7ff8_0000_0000_0000,
                0x4055_e000_0000_0000,
                0x4040_aaaa_aaaa_aaab,
                0x4055_6db6_db6d_b6db,
                0x4050_aaaa_aaaa_aaab,
                0x4040_aaaa_aaaa_aaab,
                0x4055_6db6_db6d_b6db,
                0x4053_71c7_1c71_c71d,
                0x4040_aaaa_aaaa_aaab,
                0x4055_6db6_db6d_b6db,
                0x4053_71c7_1c71_c71d,
                0x4040_aaaa_aaaa_aaab,
                0x4055_6db6_db6d_b6db,
                0x4053_71c7_1c71_c71d,
            ],
        );
        assert_bits(
            &fast_d,
            &[
                0x7ff8_0000_0000_0000,
                0x7ff8_0000_0000_0000,
                0x7ff8_0000_0000_0000,
                0x4052_c000_0000_0000,
                0x404e_3555_5555_5556,
                0x404d_c30c_30c3_0c31,
                0x4053_0c30_c30c_30c4,
                0x4049_0000_0000_0002,
                0x404d_c30c_30c3_0c32,
                0x4054_6fbe_fbef_befc,
                0x404b_c71c_71c7_1c72,
                0x404d_c30c_30c3_0c30,
                0x4054_6fbe_fbef_befc,
                0x404b_c71c_71c7_1c72,
                0x404d_c30c_30c3_0c30,
                0x4054_6fbe_fbef_befc,
            ],
        );
    }

    fn ultosc_matches_pinned_bits() {
        assert_bits(
            &ultosc(&HIGH, &LOW, &CLOSE, 2, 3, 4).expect("valid ULTOSC"),
            &[
                0x7ff8_0000_0000_0000,
                0x7ff8_0000_0000_0000,
                0x7ff8_0000_0000_0000,
                0x7ff8_0000_0000_0000,
                0x404a_3f78_ac3f_78ad,
                0x404e_cd50_3354_0cd4,
                0x404c_6bca_1af2_86bc,
                0x4041_75d7_5d75_d75e,
                0x404c_9249_2492_4924,
                0x404f_bc14_e5e0_a730,
                0x4046_72f0_5397_829d,
                0x404d_f2f0_5397_829e,
                0x4050_10d5_9fa3_1ec4,
                0x4046_72f0_5397_829d,
                0x404d_f2f0_5397_829e,
                0x4050_10d5_9fa3_1ec4,
            ],
        );
    }

    #[test]
    fn rejects_invalid_periods_and_mismatched_inputs() {
        assert!(rsi(&CLOSE, 1).is_err());
        assert!(cci(&HIGH[..3], &LOW[..2], &CLOSE[..3], 2).is_err());
        assert!(mfi(&HIGH[..3], &LOW[..3], &CLOSE[..3], &VOLUME[..2], 2).is_err());
        assert!(obv(&CLOSE[..3], &VOLUME[..2]).is_err());
        assert!(stochf(&HIGH, &LOW, &CLOSE, 0, 3).is_err());
        assert!(ultosc(&HIGH, &LOW, &CLOSE, 1, 0, 3).is_err());
    }

    #[test]
    fn ties_and_zero_ranges_follow_talib_branches() {
        let high = [2.0, 2.0, 2.0, 2.0];
        let low = [2.0, 2.0, 2.0, 2.0];
        let close = [2.0, 2.0, 2.0, 2.0];
        let volume = [1.0, 2.0, 3.0, 4.0];
        assert_eq!(
            mfi(&high, &low, &close, &volume, 2).expect("MFI")[2].to_bits(),
            0.0_f64.to_bits()
        );
        let (fast_k, fast_d) = stochf(&high, &low, &close, 2, 2).expect("STOCHF");
        assert_eq!(fast_k[2].to_bits(), 0.0_f64.to_bits());
        assert_eq!(fast_d[2].to_bits(), 0.0_f64.to_bits());
        assert_eq!(
            obv(&close, &volume)
                .expect("OBV")
                .iter()
                .map(|value| value.to_bits())
                .collect::<Vec<_>>(),
            vec![1.0_f64.to_bits(); 4]
        );
    }

    #[test]
    fn streams_match_batch_bits_across_arbitrary_chunks() {
        let rsi_first = [&CLOSE[..2]];
        let rsi_second = [&CLOSE[2..7]];
        let rsi_third = [&CLOSE[7..11]];
        let rsi_last = [&CLOSE[11..]];
        assert_columns_bits(
            &[rsi(&CLOSE, 3).expect("batch RSI")],
            &collect_stream(
                "RSI",
                &arguments(&[("timeperiod", 3)]),
                &[&rsi_first, &rsi_second, &rsi_third, &rsi_last],
            ),
        );

        let price_first = [&HIGH[..2], &LOW[..2], &CLOSE[..2]];
        let price_second = [&HIGH[2..7], &LOW[2..7], &CLOSE[2..7]];
        let price_third = [&HIGH[7..11], &LOW[7..11], &CLOSE[7..11]];
        let price_last = [&HIGH[11..], &LOW[11..], &CLOSE[11..]];
        let price_chunks: [&[&[f64]]; 4] = [
            &price_first[..],
            &price_second[..],
            &price_third[..],
            &price_last[..],
        ];
        assert_columns_bits(
            &[cci(&HIGH, &LOW, &CLOSE, 3).expect("batch CCI")],
            &collect_stream("CCI", &arguments(&[("timeperiod", 3)]), &price_chunks),
        );
        assert_columns_bits(
            &[ultosc(&HIGH, &LOW, &CLOSE, 2, 3, 4).expect("batch ULTOSC")],
            &collect_stream(
                "ULTOSC",
                &arguments(&[("timeperiod1", 2), ("timeperiod2", 3), ("timeperiod3", 4)]),
                &price_chunks,
            ),
        );
        assert_columns_bits(
            &{
                let (fast_k, fast_d) = stochf(&HIGH, &LOW, &CLOSE, 3, 2).expect("batch STOCHF");
                vec![fast_k, fast_d]
            },
            &collect_stream(
                "STOCHF",
                &arguments(&[("fastk_period", 3), ("fastd_period", 2)]),
                &price_chunks,
            ),
        );

        let volume_first = [&HIGH[..2], &LOW[..2], &CLOSE[..2], &VOLUME[..2]];
        let volume_second = [&HIGH[2..7], &LOW[2..7], &CLOSE[2..7], &VOLUME[2..7]];
        let volume_third = [&HIGH[7..11], &LOW[7..11], &CLOSE[7..11], &VOLUME[7..11]];
        let volume_last = [&HIGH[11..], &LOW[11..], &CLOSE[11..], &VOLUME[11..]];
        assert_columns_bits(
            &[mfi(&HIGH, &LOW, &CLOSE, &VOLUME, 3).expect("batch MFI")],
            &collect_stream(
                "MFI",
                &arguments(&[("timeperiod", 3)]),
                &[
                    &volume_first[..],
                    &volume_second[..],
                    &volume_third[..],
                    &volume_last[..],
                ],
            ),
        );

        let obv_first = [&CLOSE[..2], &VOLUME[..2]];
        let obv_second = [&CLOSE[2..7], &VOLUME[2..7]];
        let obv_third = [&CLOSE[7..11], &VOLUME[7..11]];
        let obv_last = [&CLOSE[11..], &VOLUME[11..]];
        assert_columns_bits(
            &[obv(&CLOSE, &VOLUME).expect("batch OBV")],
            &collect_stream(
                "OBV",
                &Map::new(),
                &[
                    &obv_first[..],
                    &obv_second[..],
                    &obv_third[..],
                    &obv_last[..],
                ],
            ),
        );
    }

    #[test]
    fn streams_retain_only_period_scale_state() {
        let price = [&HIGH[..], &LOW[..], &CLOSE[..]];
        let volume = [&HIGH[..], &LOW[..], &CLOSE[..], &VOLUME[..]];
        let cases = [
            ("RSI", arguments(&[("timeperiod", 3)]), vec![&CLOSE[..]], 3),
            ("CCI", arguments(&[("timeperiod", 3)]), price.to_vec(), 3),
            ("MFI", arguments(&[("timeperiod", 3)]), volume.to_vec(), 9),
            ("OBV", Map::new(), vec![&CLOSE[..], &VOLUME[..]], 2),
            (
                "STOCHF",
                arguments(&[("fastk_period", 3), ("fastd_period", 2)]),
                price.to_vec(),
                10,
            ),
            (
                "ULTOSC",
                arguments(&[("timeperiod1", 2), ("timeperiod2", 3), ("timeperiod3", 4)]),
                price.to_vec(),
                19,
            ),
        ];
        for (name, arguments, inputs, maximum) in cases {
            let mut state = stream(name, &arguments)
                .expect("valid stream arguments")
                .expect("implemented stream");
            state.execute(&inputs).expect("execute stream");
            assert!(
                state.retained() <= maximum,
                "{name} retained too much state"
            );
        }
    }

    fn arguments(items: &[(&str, u64)]) -> Map<String, Value> {
        items
            .iter()
            .map(|(name, value)| ((*name).to_owned(), Value::from(*value)))
            .collect()
    }

    fn collect_stream(
        name: &str,
        arguments: &Map<String, Value>,
        chunks: &[&[&[f64]]],
    ) -> Vec<Vec<f64>> {
        let mut state = stream(name, arguments)
            .expect("valid stream arguments")
            .expect("implemented stream");
        let mut output = Vec::new();
        for inputs in chunks {
            let current = state.execute(inputs).expect("execute stream chunk");
            if output.is_empty() {
                output = vec![Vec::new(); current.len()];
            }
            for (full, current) in output.iter_mut().zip(current) {
                full.extend(current);
            }
        }
        output
    }

    fn assert_columns_bits(expected: &[Vec<f64>], actual: &[Vec<f64>]) {
        assert_eq!(expected.len(), actual.len());
        for (expected, actual) in expected.iter().zip(actual) {
            assert_bits(
                actual,
                &expected
                    .iter()
                    .map(|value| value.to_bits())
                    .collect::<Vec<_>>(),
            );
        }
    }

    fn assert_bits(actual: &[f64], expected: &[u64]) {
        assert_eq!(actual.len(), expected.len());
        for (index, (actual, expected)) in actual.iter().zip(expected).enumerate() {
            assert_eq!(actual.to_bits(), *expected, "bit mismatch at index {index}");
        }
    }
}
