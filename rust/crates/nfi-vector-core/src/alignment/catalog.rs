//! Immutable, identity-exact source frames for compiled indicator programs.

use std::collections::BTreeMap;

use super::{FrameIdentity, NumericFrame, SourceLocation};
use crate::VectorCoreError;

/// Validated source frames keyed by their exact pair and timeframe.
///
/// The catalog never normalizes a pair, substitutes a timeframe, fills an
/// empty frame, or stores caller mutation overlays. A runtime may borrow the
/// same source repeatedly and keep each execution overlay outside the catalog.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct FrameCatalog {
    frames: BTreeMap<FrameIdentity, NumericFrame>,
}

impl FrameCatalog {
    /// Build a catalog from explicit key/frame entries.
    ///
    /// Keeping the key separate from the stored frame makes manifest or loader
    /// identity drift detectable before an indicator program starts.
    ///
    /// # Errors
    ///
    /// Returns an invalid-program error for an empty pair, a key/frame identity
    /// mismatch, a duplicate identity, or an invalid frame shape.
    pub fn new(
        entries: impl IntoIterator<Item = (FrameIdentity, NumericFrame)>,
    ) -> Result<Self, VectorCoreError> {
        let mut frames = BTreeMap::new();
        for (identity, frame) in entries {
            validate_entry(&identity, &frame)?;
            if frames.insert(identity.clone(), frame).is_some() {
                return Err(VectorCoreError::InvalidProgram(format!(
                    "frame catalog contains duplicate identity {} {}",
                    identity.pair,
                    identity.timeframe.as_str()
                )));
            }
        }
        Ok(Self { frames })
    }

    /// Number of exact pair/timeframe frames in the catalog.
    #[must_use]
    pub fn len(&self) -> usize {
        self.frames.len()
    }

    /// Whether the catalog has no source frames.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.frames.is_empty()
    }

    /// Iterate the validated identities in deterministic pair/timeframe order.
    pub fn identities(&self) -> impl Iterator<Item = &FrameIdentity> {
        self.frames.keys()
    }

    /// Resolve exactly one immutable source frame.
    ///
    /// An explicitly stored empty frame is returned unchanged. Call
    /// [`Self::lookup_non_empty`] when the compiled path cannot represent the
    /// strategy's empty-frame branch.
    ///
    /// # Errors
    ///
    /// Returns a source-located execution error when the exact identity is not
    /// present. No other pair or timeframe is considered.
    pub fn lookup(
        &self,
        identity: &FrameIdentity,
        source: &SourceLocation,
    ) -> Result<&NumericFrame, VectorCoreError> {
        self.frames.get(identity).ok_or_else(|| {
            source.error(format!(
                "frame catalog has no exact frame for {} {}",
                identity.pair,
                identity.timeframe.as_str()
            ))
        })
    }

    /// Resolve a frame and reject a present-but-empty source explicitly.
    ///
    /// # Errors
    ///
    /// Returns the source-located missing error from [`Self::lookup`], or a
    /// source-located empty-frame error. It never synthesizes candle rows.
    pub fn lookup_non_empty(
        &self,
        identity: &FrameIdentity,
        source: &SourceLocation,
    ) -> Result<&NumericFrame, VectorCoreError> {
        let frame = self.lookup(identity, source)?;
        if frame.timestamps_ms.is_empty() {
            return Err(source.error(format!(
                "frame catalog frame {} {} is empty",
                identity.pair,
                identity.timeframe.as_str()
            )));
        }
        Ok(frame)
    }
}

fn validate_entry(identity: &FrameIdentity, frame: &NumericFrame) -> Result<(), VectorCoreError> {
    if identity.pair.is_empty() || frame.identity.pair.is_empty() {
        return Err(VectorCoreError::InvalidProgram(
            "frame catalog identity has an empty pair".to_owned(),
        ));
    }
    if identity != &frame.identity {
        return Err(VectorCoreError::InvalidProgram(format!(
            "frame catalog key {} {} differs from stored frame identity {} {}",
            identity.pair,
            identity.timeframe.as_str(),
            frame.identity.pair,
            frame.identity.timeframe.as_str()
        )));
    }
    frame.validate().map_err(|error| {
        VectorCoreError::InvalidProgram(format!(
            "frame catalog frame {} {} has invalid shape: {error}",
            identity.pair,
            identity.timeframe.as_str()
        ))
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::alignment::Timeframe;

    fn identity(pair: &str, timeframe: &str) -> FrameIdentity {
        FrameIdentity::new(pair, Timeframe::parse(timeframe).expect("timeframe"))
            .expect("frame identity")
    }

    fn frame(pair: &str, timeframe: &str, values: Vec<Option<f64>>) -> NumericFrame {
        NumericFrame {
            identity: identity(pair, timeframe),
            timestamps_ms: (0..values.len())
                .map(|row| i64::try_from(row).expect("test row") * 60_000)
                .collect(),
            columns: BTreeMap::from([("close".to_owned(), values)]),
        }
    }

    fn source() -> SourceLocation {
        SourceLocation::new("n17", "strategy.py", 3353, 21)
    }

    #[test]
    fn rejects_duplicate_and_key_frame_identity_drift() {
        let key = identity("ETH/USDT", "1h");
        let first = frame("ETH/USDT", "1h", vec![Some(1.0)]);
        let second = frame("ETH/USDT", "1h", vec![Some(2.0)]);
        let duplicate = FrameCatalog::new([(key.clone(), first), (key.clone(), second)])
            .expect_err("duplicate identity");
        assert!(matches!(
            duplicate,
            VectorCoreError::InvalidProgram(message)
                if message.contains("duplicate identity ETH/USDT 1h")
        ));

        let drifted = FrameCatalog::new([(
            identity("ETH/USDT", "1h"),
            frame("BTC/USDT", "1h", vec![Some(1.0)]),
        )])
        .expect_err("key/frame drift");
        assert!(matches!(
            drifted,
            VectorCoreError::InvalidProgram(message)
                if message.contains("differs from stored frame identity")
        ));
    }

    #[test]
    fn rejects_invalid_frame_shape_before_storage() {
        let identity = identity("ETH/USDT", "1h");
        let invalid = NumericFrame {
            identity: identity.clone(),
            timestamps_ms: vec![0, 60_000],
            columns: BTreeMap::from([("close".to_owned(), vec![Some(1.0)])]),
        };
        let error = FrameCatalog::new([(identity, invalid)]).expect_err("invalid shape");
        assert!(matches!(
            error,
            VectorCoreError::InvalidProgram(message) if message.contains("invalid shape")
        ));
    }

    #[test]
    fn exact_lookup_never_substitutes_pair_or_timeframe() {
        let stored_identity = identity("ETH/USDT", "1h");
        let catalog = FrameCatalog::new([(
            stored_identity.clone(),
            frame("ETH/USDT", "1h", vec![Some(7.0)]),
        )])
        .expect("catalog");
        assert_eq!(catalog.len(), 1);
        assert_eq!(catalog.identities().collect::<Vec<_>>(), [&stored_identity]);
        assert_eq!(
            catalog
                .lookup(&stored_identity, &source())
                .expect("exact")
                .columns["close"],
            vec![Some(7.0)]
        );

        for missing in [identity("BTC/USDT", "1h"), identity("ETH/USDT", "4h")] {
            let error = catalog.lookup(&missing, &source()).expect_err("exact miss");
            assert!(matches!(
                error,
                VectorCoreError::Execution { node, message }
                    if node == "n17"
                        && message.starts_with("strategy.py:3353:21:")
                        && message.contains(&missing.pair)
                        && message.contains(missing.timeframe.as_str())
            ));
        }
    }

    #[test]
    fn empty_source_is_preserved_or_source_located_on_demand() {
        let identity = identity("ETH/USDT", "1h");
        let catalog = FrameCatalog::new([(identity.clone(), frame("ETH/USDT", "1h", Vec::new()))])
            .expect("empty catalog frame is explicit data");
        assert!(catalog
            .lookup(&identity, &source())
            .expect("explicit empty")
            .timestamps_ms
            .is_empty());
        let error = catalog
            .lookup_non_empty(&identity, &source())
            .expect_err("empty exact lane");
        assert!(matches!(
            error,
            VectorCoreError::Execution { node, message }
                if node == "n17"
                    && message == "strategy.py:3353:21: frame catalog frame ETH/USDT 1h is empty"
        ));
    }

    #[test]
    fn repeated_lookup_keeps_caller_overlays_outside_the_catalog() {
        let identity = identity("ETH/USDT", "1h");
        let catalog =
            FrameCatalog::new([(identity.clone(), frame("ETH/USDT", "1h", vec![Some(3.0)]))])
                .expect("catalog");

        let mut caller_overlay = catalog.lookup(&identity, &source()).expect("first").clone();
        caller_overlay.columns.get_mut("close").expect("close")[0] = Some(99.0);

        assert_eq!(caller_overlay.columns["close"], vec![Some(99.0)]);
        assert_eq!(
            catalog
                .lookup(&identity, &source())
                .expect("second")
                .columns["close"],
            vec![Some(3.0)]
        );
    }
}
