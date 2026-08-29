//! Owned and file-backed feature and candle series abstractions.

use std::borrow::Cow;
use std::rc::Rc;

use serde::Deserialize;
use serde_json::Value;

use super::FileBackedRows;
use crate::domain::Candle;
use crate::scalar_vm::{scalar_number, scalar_number_value};

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

    pub(crate) fn value(&self, index: usize) -> Option<Value> {
        match self {
            Self::Numbers(values) => scalar_number_value(*values.get(index)?),
            Self::Booleans(values) => values.get(index).copied().map(Value::Bool),
            Self::FileBacked {
                rows,
                feature_index,
                kind,
            } => match kind {
                FileBackedFeatureKind::Number => {
                    scalar_number_value(rows.feature_number(index, *feature_index).ok()??)
                }
                FileBackedFeatureKind::Boolean => rows
                    .feature_boolean(index, *feature_index)
                    .ok()?
                    .map(Value::Bool),
            },
        }
    }

    pub(crate) fn number(&self, index: usize) -> Option<f64> {
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
            } => rows.feature_number(index, *feature_index).ok().flatten(),
        }
    }

    pub(crate) fn boolean(&self, index: usize) -> Option<bool> {
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
            } => rows.feature_boolean(index, *feature_index).ok().flatten(),
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

    pub(crate) fn try_get(
        &self,
        index: usize,
    ) -> Result<Option<Cow<'_, Candle>>, crate::domain::SimError> {
        match self {
            Self::Owned(candles) => Ok(candles.get(index).map(Cow::Borrowed)),
            Self::FileBacked(rows) => rows.candle(index).map(|candle| candle.map(Cow::Owned)),
        }
    }

    #[must_use]
    pub fn get(&self, index: usize) -> Option<Cow<'_, Candle>> {
        self.try_get(index).ok().flatten()
    }

    #[must_use]
    pub fn timestamp_ms(&self, index: usize) -> Option<i64> {
        match self {
            Self::Owned(candles) => candles.get(index).map(|candle| candle.timestamp_ms),
            Self::FileBacked(rows) => rows.timestamp_ms(index).ok().flatten(),
        }
    }

    #[must_use]
    pub fn has_entry_signal(&self, index: usize) -> Option<bool> {
        match self {
            Self::Owned(candles) => candles
                .get(index)
                .map(|candle| candle.enter_long.is_some() || candle.enter_short.is_some()),
            Self::FileBacked(rows) => rows.has_entry_signal(index).ok().flatten(),
        }
    }

    #[must_use]
    pub fn next_entry_index(&self, start: usize) -> Option<usize> {
        match self {
            Self::Owned(candles) => {
                candles
                    .iter()
                    .enumerate()
                    .skip(start)
                    .find_map(|(index, candle)| {
                        (candle.enter_long.is_some() || candle.enter_short.is_some())
                            .then_some(index)
                    })
            }
            Self::FileBacked(rows) => rows.next_entry_index(start).ok().flatten(),
        }
    }

    pub(crate) fn backing_failure(&self) -> Option<crate::domain::SimError> {
        match self {
            Self::Owned(_) => None,
            Self::FileBacked(rows) => rows.failure(),
        }
    }

    pub(crate) fn install_entry_indices(&self, indices: Vec<usize>) {
        if let Self::FileBacked(rows) = self {
            rows.install_entry_indices(indices);
        }
    }

    pub(crate) fn try_last(&self) -> Result<Option<Cow<'_, Candle>>, crate::domain::SimError> {
        if let Some(index) = self.len().checked_sub(1) {
            self.try_get(index)
        } else {
            Ok(None)
        }
    }

    #[must_use]
    pub fn last(&self) -> Option<Cow<'_, Candle>> {
        self.try_last().ok().flatten()
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
