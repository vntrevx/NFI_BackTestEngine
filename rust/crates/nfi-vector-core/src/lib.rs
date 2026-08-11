//! Safe, deterministic execution substrate for Indicator, Signal, and Tag programs.
//!
//! This crate owns typed Arrow columns, causal execution planning, bounded
//! streaming state, source-ordered dataframe mutation, and generic vector
//! execution. Feather transport remains in `nfi-vector-io`.

#![forbid(unsafe_code)]
#![allow(clippy::module_name_repetitions)] // Public names stay explicit across crate boundaries.

pub mod alignment;
pub mod batch;
pub mod column;
pub mod engine;
pub mod error;
pub mod float;
pub mod kernels;
pub mod mutation;
pub mod program;
pub mod sink;
pub mod state;

pub use error::VectorCoreError;
