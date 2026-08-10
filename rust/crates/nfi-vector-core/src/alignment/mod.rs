//! Generic, causal alignment of base and informative candle frames.
//!
//! This ports the timestamp rule of Freqtrade 2026.5.1's
//! `merge_informative_pair`: an informative candle becomes visible only after
//! it has closed. Frames are deliberately explicit about their `(pair,
//! timeframe)` identity, so equal column names from different markets cannot
//! be joined accidentally.

mod batch;
mod fill;
mod model;
mod stream;
mod support;

pub use batch::merge;
pub use fill::{forward_fill, ForwardFillStream};
pub use model::{FrameIdentity, MergeSpec, MergedFrame, NumericFrame, SourceLocation, Timeframe};
pub use stream::MergeStream;

#[cfg(test)]
use model::days_from_civil;
#[cfg(test)]
mod tests;
