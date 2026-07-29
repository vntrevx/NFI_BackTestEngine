//! Freqtrade-compatible protection programs and pair-lock state.

mod contract;
mod state;

pub use contract::{
    DrawdownMode, PairLockState, ProtectionHandler, ProtectionProgram, ProtectionTiming,
};
pub(crate) use state::ProtectionState;
