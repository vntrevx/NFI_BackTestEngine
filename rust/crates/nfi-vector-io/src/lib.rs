//! Verified columnar input boundary for analyzed strategy vectors.
//!
//! Python remains responsible for running the real strategy's vector methods.
//! This crate reads their immutable Feather output directly, so neither Python
//! nor JSON duplicates every candle and callback feature before simulation.
//! The simulator core intentionally does not depend on Arrow or the filesystem.

mod decode;
mod failures;
mod loader;
mod row;
mod schema;
mod values;

pub use failures::VectorInputError;
pub use loader::{load_vector_manifest, load_vector_manifest_profiled, VectorLoadProfile};

/// Version of the compact manifest consumed by this crate.
pub const VECTOR_MANIFEST_SCHEMA_VERSION: &str = "1.2.0";
