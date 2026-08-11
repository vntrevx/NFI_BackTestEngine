//! Exact, operation-driven indicator kernels reachable from current NFI source.
//!
//! Function names and parameters are TA-Lib semantics, never strategy identities.

mod directional;
mod moving;
mod native;
mod oscillator;
mod rolling;

pub use native::{
    absolute_difference, chaikin_money_flow, hourly_inside_bar, safe_percent_change,
    utc_opening_range, AbsoluteDifferenceStream, ChaikinMoneyFlowStream, HourlyInsideBarOutput,
    HourlyInsideBarStream, SafePercentChangeStream, UtcOpeningRangeOutput, UtcOpeningRangeStream,
};
pub use rolling::execute_rolling;
pub(crate) use rolling::{stream as rolling_stream, RollingStream};

#[cfg(test)]
mod tests;

use serde_json::{Map, Value};

use crate::VectorCoreError;

/// Full-length output columns from one exact indicator invocation.
#[derive(Clone, Debug, PartialEq)]
pub struct KernelOutput {
    names: &'static [&'static str],
    columns: Vec<Vec<f64>>,
}

/// Operation-bound, bounded TA-Lib state carried between Arrow batches.
#[derive(Debug)]
pub(crate) struct TalibStream {
    name: String,
    inner: TalibStreamInner,
}

#[derive(Debug)]
enum TalibStreamInner {
    Moving(moving::MovingStream),
    Directional(directional::DirectionalStream),
    Oscillator(oscillator::OscillatorStream),
}

impl TalibStream {
    pub(crate) fn new(name: &str, arguments: &Map<String, Value>) -> Result<Self, VectorCoreError> {
        let inner = if let Some(state) = moving::stream(name, arguments)? {
            TalibStreamInner::Moving(state)
        } else if let Some(state) = directional::stream(name, arguments)? {
            TalibStreamInner::Directional(state)
        } else if let Some(state) = oscillator::stream(name, arguments)? {
            TalibStreamInner::Oscillator(state)
        } else {
            return Err(kernel_error(
                name,
                "operation is not in the exact streaming registry",
            ));
        };
        Ok(Self {
            name: name.to_owned(),
            inner,
        })
    }

    pub(crate) fn execute(&mut self, inputs: &[&[f64]]) -> Result<KernelOutput, VectorCoreError> {
        let rows = validate_inputs(&self.name, inputs)?;
        let columns = match &mut self.inner {
            TalibStreamInner::Moving(state) => state.execute(inputs)?,
            TalibStreamInner::Directional(state) => state.execute(inputs)?,
            TalibStreamInner::Oscillator(state) => state.execute(inputs)?,
        };
        if columns.iter().any(|column| column.len() != rows) {
            return Err(kernel_error(
                &self.name,
                "streaming output length differs from its input",
            ));
        }
        Ok(KernelOutput::new(output_names(&self.name)?, columns))
    }

    pub(crate) fn retained(&self) -> usize {
        match &self.inner {
            TalibStreamInner::Moving(state) => state.retained(),
            TalibStreamInner::Directional(state) => state.retained(),
            TalibStreamInner::Oscillator(state) => state.retained(),
        }
    }
}

impl KernelOutput {
    fn new(names: &'static [&'static str], columns: Vec<Vec<f64>>) -> Self {
        Self { names, columns }
    }

    #[must_use]
    pub const fn names(&self) -> &'static [&'static str] {
        self.names
    }

    #[must_use]
    pub fn columns(&self) -> &[Vec<f64>] {
        &self.columns
    }

    /// Select one named output without relying on tuple position in callers.
    #[must_use]
    pub fn column(&self, name: &str) -> Option<&[f64]> {
        self.names
            .iter()
            .position(|candidate| *candidate == name)
            .and_then(|index| self.columns.get(index).map(Vec::as_slice))
    }
}

/// Execute one pinned TA-Lib-compatible operation over complete input columns.
///
/// This is deliberately a generic operation API. It does not know an NFI class,
/// Signal, pair, timerange, strategy hash, or expected result.
///
/// # Errors
///
/// Returns a fail-closed error for an unknown function, unsupported MA type,
/// invalid parameter, input arity, or unequal column length.
pub fn execute_talib(
    name: &str,
    inputs: &[&[f64]],
    arguments: &Map<String, Value>,
) -> Result<KernelOutput, VectorCoreError> {
    let rows = validate_inputs(name, inputs)?;
    let output = execute_moving(name, inputs, arguments)?
        .or(execute_directional(name, inputs, arguments)?)
        .or(execute_oscillator(name, inputs, arguments)?)
        .ok_or_else(|| kernel_error(name, "operation is not in the exact kernel registry"))?;
    if output.columns.iter().any(|column| column.len() != rows) {
        return Err(kernel_error(
            name,
            "kernel output length differs from its input",
        ));
    }
    Ok(output)
}

fn execute_moving(
    name: &str,
    inputs: &[&[f64]],
    arguments: &Map<String, Value>,
) -> Result<Option<KernelOutput>, VectorCoreError> {
    let output = match name {
        "BBANDS" => {
            if integer(arguments, "matype", 0)? != 0 {
                return Err(kernel_error(
                    name,
                    "only exact TA-Lib SMA bands are implemented",
                ));
            }
            let (upper, middle, lower) = moving::bbands_sma(
                input(inputs, 0)?,
                integer(arguments, "timeperiod", 5)?,
                number(arguments, "nbdevup", 2.0)?,
                number(arguments, "nbdevdn", 2.0)?,
            )?;
            KernelOutput::new(
                &["upperband", "middleband", "lowerband"],
                vec![upper, middle, lower],
            )
        }
        "EMA" => single(moving::ema(
            input(inputs, 0)?,
            integer(arguments, "timeperiod", 30)?,
        )?),
        "MAX" => single(moving::max(
            input(inputs, 0)?,
            integer(arguments, "timeperiod", 30)?,
        )?),
        "MIN" => single(moving::min(
            input(inputs, 0)?,
            integer(arguments, "timeperiod", 30)?,
        )?),
        "ROC" => single(moving::roc(
            input(inputs, 0)?,
            integer(arguments, "timeperiod", 10)?,
        )?),
        "SMA" => single(moving::sma(
            input(inputs, 0)?,
            integer(arguments, "timeperiod", 30)?,
        )?),
        "STDDEV" => single(moving::stddev(
            input(inputs, 0)?,
            integer(arguments, "timeperiod", 5)?,
            number(arguments, "nbdev", 1.0)?,
        )?),
        "SUM" => single(moving::sum(
            input(inputs, 0)?,
            integer(arguments, "timeperiod", 30)?,
        )?),
        _ => return Ok(None),
    };
    Ok(Some(output))
}

fn execute_directional(
    name: &str,
    inputs: &[&[f64]],
    arguments: &Map<String, Value>,
) -> Result<Option<KernelOutput>, VectorCoreError> {
    let output = match name {
        "ADX" => KernelOutput::new(
            &["real"],
            vec![directional::adx(
                input(inputs, 0)?,
                input(inputs, 1)?,
                input(inputs, 2)?,
                integer(arguments, "timeperiod", 14)?,
            )?],
        ),
        "AROON" => {
            let (down, up) = directional::aroon(
                input(inputs, 0)?,
                input(inputs, 1)?,
                integer(arguments, "timeperiod", 14)?,
            )?;
            KernelOutput::new(&["aroondown", "aroonup"], vec![down, up])
        }
        "MINUS_DI" => KernelOutput::new(
            &["real"],
            vec![directional::minus_di(
                input(inputs, 0)?,
                input(inputs, 1)?,
                input(inputs, 2)?,
                integer(arguments, "timeperiod", 14)?,
            )?],
        ),
        "PLUS_DI" => KernelOutput::new(
            &["real"],
            vec![directional::plus_di(
                input(inputs, 0)?,
                input(inputs, 1)?,
                input(inputs, 2)?,
                integer(arguments, "timeperiod", 14)?,
            )?],
        ),
        "WILLR" => KernelOutput::new(
            &["real"],
            vec![directional::willr(
                input(inputs, 0)?,
                input(inputs, 1)?,
                input(inputs, 2)?,
                integer(arguments, "timeperiod", 14)?,
            )?],
        ),
        _ => return Ok(None),
    };
    Ok(Some(output))
}

fn execute_oscillator(
    name: &str,
    inputs: &[&[f64]],
    arguments: &Map<String, Value>,
) -> Result<Option<KernelOutput>, VectorCoreError> {
    let output = match name {
        "CCI" => single(oscillator::cci(
            input(inputs, 0)?,
            input(inputs, 1)?,
            input(inputs, 2)?,
            integer(arguments, "timeperiod", 14)?,
        )?),
        "MFI" => single(oscillator::mfi(
            input(inputs, 0)?,
            input(inputs, 1)?,
            input(inputs, 2)?,
            input(inputs, 3)?,
            integer(arguments, "timeperiod", 14)?,
        )?),
        "OBV" => single(oscillator::obv(input(inputs, 0)?, input(inputs, 1)?)?),
        "RSI" => single(oscillator::rsi(
            input(inputs, 0)?,
            integer(arguments, "timeperiod", 14)?,
        )?),
        "STOCHF" => {
            if integer(arguments, "fastd_matype", 0)? != 0 {
                return Err(kernel_error(
                    name,
                    "only exact TA-Lib SMA fast-D is implemented",
                ));
            }
            let (fast_k, fast_d) = oscillator::stochf(
                input(inputs, 0)?,
                input(inputs, 1)?,
                input(inputs, 2)?,
                integer(arguments, "fastk_period", 5)?,
                integer(arguments, "fastd_period", 3)?,
            )?;
            KernelOutput::new(&["fastk", "fastd"], vec![fast_k, fast_d])
        }
        "ULTOSC" => single(oscillator::ultosc(
            input(inputs, 0)?,
            input(inputs, 1)?,
            input(inputs, 2)?,
            integer(arguments, "timeperiod1", 7)?,
            integer(arguments, "timeperiod2", 14)?,
            integer(arguments, "timeperiod3", 28)?,
        )?),
        _ => return Ok(None),
    };
    Ok(Some(output))
}

fn single(column: Vec<f64>) -> KernelOutput {
    KernelOutput::new(&["real"], vec![column])
}

fn output_names(name: &str) -> Result<&'static [&'static str], VectorCoreError> {
    match name {
        "AROON" => Ok(&["aroondown", "aroonup"]),
        "BBANDS" => Ok(&["upperband", "middleband", "lowerband"]),
        "STOCHF" => Ok(&["fastk", "fastd"]),
        "ADX" | "CCI" | "EMA" | "MAX" | "MFI" | "MIN" | "MINUS_DI" | "OBV" | "PLUS_DI" | "ROC"
        | "RSI" | "SMA" | "STDDEV" | "SUM" | "ULTOSC" | "WILLR" => Ok(&["real"]),
        _ => Err(kernel_error(name, "operation has no exact output contract")),
    }
}

fn validate_inputs(name: &str, inputs: &[&[f64]]) -> Result<usize, VectorCoreError> {
    let Some(first) = inputs.first() else {
        return Err(kernel_error(name, "indicator requires at least one input"));
    };
    if inputs.iter().any(|input| input.len() != first.len()) {
        return Err(kernel_error(name, "indicator input lengths differ"));
    }
    Ok(first.len())
}

fn input<'a>(inputs: &'a [&[f64]], index: usize) -> Result<&'a [f64], VectorCoreError> {
    inputs
        .get(index)
        .copied()
        .ok_or_else(|| VectorCoreError::InvalidState(format!("missing indicator input {index}")))
}

fn integer(
    arguments: &Map<String, Value>,
    name: &str,
    default: usize,
) -> Result<usize, VectorCoreError> {
    arguments.get(name).map_or(Ok(default), |value| {
        value
            .as_u64()
            .and_then(|item| usize::try_from(item).ok())
            .ok_or_else(|| {
                VectorCoreError::InvalidState(format!("invalid integer argument {name}"))
            })
    })
}

fn number(
    arguments: &Map<String, Value>,
    name: &str,
    default: f64,
) -> Result<f64, VectorCoreError> {
    arguments.get(name).map_or(Ok(default), |value| {
        value
            .as_f64()
            .filter(|item| item.is_finite())
            .ok_or_else(|| {
                VectorCoreError::InvalidState(format!("invalid numeric argument {name}"))
            })
    })
}

fn kernel_error(name: &str, message: &str) -> VectorCoreError {
    VectorCoreError::InvalidState(format!("TA-Lib {name}: {message}"))
}
