//! NFI trade-manager schema and route validation.

use std::collections::BTreeSet;

use crate::domain::{
    CompiledAdjustmentExecutionMode, CompiledAdjustmentOperation, CompiledAdjustmentProgram,
    CompiledLegacyComparison, CompiledLegacyEntryStakeBasis, CompiledLegacyExitStakeBasis,
    CompiledLegacyGrindExecutionMode, CompiledLegacyGrindSide, CompiledLegacyGrindTransition,
    CompiledLegacyRetryPolicy, CompiledLegacyThresholdDivisor, CompiledLegacyWalletGuard,
    CompiledOrderSequence, CompiledOrderSide, CompiledPartialFillPolicy,
    CompiledRegularContinuationGuard, CompiledRegularContinuationKind,
    CompiledRegularExecutionMode, CompiledRegularTransition, CompiledSystemAdjustmentActionKind,
    CompiledSystemAdjustmentExecutionMode, CompiledSystemAdjustmentInputKind,
    CompiledSystemAdjustmentProgram, CompiledSystemAdjustmentSide, ManagedExitExecutionMode,
    ManagedExitInlinePosition, ManagedExitRoute, ManagedExitStateOperation,
    ManagedExitStateProgram, ManagedExitStopPolicy, ManagedExitTagMatcher, ManagedExitTagOperator,
    NfiLongGrindRoute, NfiManagedLongProfile, NfiManagedLongRoute, NfiX7AdjustmentPolicy,
    NfiX7PositionAdjustment, NfiX7TradeManager, PortfolioConfig, SimError,
};

use super::adjustment::{
    valid_nfi_adjustment_constants, valid_nfi_adjustment_policy, valid_nfi_legacy_grind_constants,
    valid_nfi_rebuy_constants, valid_nfi_regular_adjustment_constants,
};
use super::config::{
    uses_full_futures_manager_contract, valid_legacy_futures_fallback, valid_scalar_program,
};

mod adjustment_routes;
mod finalization;
mod grind_routes;
mod identity_routes;
mod legacy_clusters;
mod legacy_grind;
mod legacy_policy;
mod managed_exit_contract;
mod managed_long_exit;
mod managed_short_exit;
mod program_inventory;
mod rebuy;
mod rebuy_routes;
mod regular;
mod routes;
mod shared;
mod system_adjustment;

pub(crate) use legacy_grind::valid_versioned_legacy_grind_program;
use managed_long_exit::valid_managed_exit_program;
use managed_short_exit::valid_managed_short_exit_program;
use rebuy::{valid_adjustment_source_callback, valid_versioned_rebuy_program};
use regular::valid_versioned_regular_adjustment_program;
pub(crate) use routes::valid_nfi_managed_long_route;
use routes::valid_nfi_managed_short_route;
#[cfg(test)]
pub(crate) use system_adjustment::valid_system_adjustment_binding_level;
use system_adjustment::valid_versioned_system_adjustment_program;

pub(crate) fn validate_nfi_trade_manager(
    config: &PortfolioConfig,
    manager: &NfiX7TradeManager,
) -> Result<(), SimError> {
    let routes = identity_routes::summarize(manager);
    let managed_exits_are_valid = valid_managed_exit_program(manager);
    let short_exits_are_valid = valid_managed_short_exit_program(manager);
    let grind = grind_routes::summarize(manager, &routes.managed_tags, &routes.short_tags);
    let programs_are_valid = program_inventory::is_valid(manager);
    let adjustments =
        adjustment_routes::summarize(manager, &routes.managed_tags, &routes.short_tags);
    let rebuys = rebuy_routes::summarize(manager);
    let final_contract_is_valid = finalization::static_contract_is_valid(config, manager);

    let static_contracts_are_valid = routes.is_valid()
        && managed_exits_are_valid
        && short_exits_are_valid
        && grind.is_valid()
        && programs_are_valid
        && adjustments.is_valid()
        && rebuys.is_valid()
        && final_contract_is_valid;
    if !static_contracts_are_valid || !finalization::runtime_is_valid(manager) {
        return Err(SimError::InvalidNfiTradeManager);
    }
    Ok(())
}
