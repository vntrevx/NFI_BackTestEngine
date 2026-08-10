//! Safe, deterministic execution substrate for `indicator-program-v1`.
//!
//! This crate owns typed Arrow columns, causal execution planning, bounded
//! streaming state, and generic vector execution. Feather transport remains in
//! `nfi-vector-io`; NFI-reachable indicator algorithms are added separately.

#![forbid(unsafe_code)]
#![allow(clippy::module_name_repetitions)] // Public names stay explicit across crate boundaries.

pub mod alignment;
pub mod batch;
pub mod column;
pub mod engine;
pub mod error;
pub mod float;
pub mod kernels;
pub mod program;
pub mod sink;
pub mod state;

pub use error::VectorCoreError;
