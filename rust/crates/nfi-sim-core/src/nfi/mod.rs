//! NFI X7 compiled strategy state machines and route dispatch.

mod adjustment;
mod dispatch;
mod dispatch_plan;
mod exit;
mod legacy_grind;
mod rebuy;
mod regular_adjustment;
mod state;

pub(crate) use adjustment::{
    evaluate_nfi_position_adjustment as evaluate_nfi_system_v3_adjustment, AdjustmentState,
};
pub(crate) use dispatch::evaluate_nfi_position_adjustment;
#[cfg(test)]
pub(crate) use exit::nfi_inline_profile_exit;
pub(crate) use exit::{
    evaluate_nfi_exit, CustomExitDecision, NFI_LONG_EXIT_PROGRAMS,
    NFI_LONG_EXIT_PROGRAMS_WITHOUT_DESCENDING, NFI_SHORT_EXIT_PROGRAMS,
};
pub(crate) use legacy_grind::evaluate_nfi_legacy_grind_adjustment;
pub(crate) use rebuy::{
    compiled_rebuy_delegates, evaluate_nfi_rebuy_adjustment, evaluate_nfi_short_rebuy_adjustment,
};
pub(crate) use regular_adjustment::{evaluate_nfi_regular_adjustment, RegularAdjustmentOutcome};
#[cfg(test)]
pub(crate) use state::NfiProfitSnapshot;
pub(crate) use state::{nfi_profit_snapshot, PositionAdjustmentRequest, ProfitTarget};
