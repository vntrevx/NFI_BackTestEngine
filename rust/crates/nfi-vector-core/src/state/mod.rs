//! Bounded state that survives Arrow record-batch boundaries.

mod rolling;

pub use rolling::{RollingWindowState, ShiftState, StateProfile};
