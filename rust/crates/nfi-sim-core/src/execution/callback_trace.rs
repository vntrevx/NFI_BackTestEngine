//! Production callback semantic-event collection at execution boundaries.

use std::cell::RefCell;
use std::collections::BTreeMap;

use serde_json::Value;

use crate::domain::{
    CallbackInvocation, CallbackOutcome, CallbackPhase, CallbackProgramResult,
    CallbackProgramRuntime, CallbackReturnClass, CallbackRuntime, CallbackSemanticEvent,
    CallbackTransaction, CallbackVisibility, ExecutableCallbackError, ExecutableCallbackProgram,
    SimError,
};
use crate::portfolio::OpenTrade;

struct TraceSession {
    runtime: CallbackRuntime,
    custom_state: BTreeMap<String, Value>,
}

thread_local! {
    static SESSION: RefCell<Option<TraceSession>> = const { RefCell::new(None) };
}

pub(crate) fn begin(
    candle_index: usize,
    feature_index: Option<usize>,
    wallet_available: f64,
    trade: Option<&OpenTrade>,
) -> Result<(), SimError> {
    let order_count = trade.map_or(0, |value| value.orders.len());
    let custom_state = trade.map_or_else(BTreeMap::new, |value| value.custom_data.clone());
    let visibility = CallbackVisibility {
        feature_index,
        order_count,
        wallet_available,
        custom_state_generation: 0,
    };
    with_session(|slot| {
        *slot = Some(TraceSession {
            runtime: CallbackRuntime::new(candle_index, visibility),
            custom_state,
        });
        Ok(())
    })
}

pub(crate) struct ExecutableCallbacks<'program, 'runtime, 'events> {
    program: &'program ExecutableCallbackProgram,
    runtime: &'runtime mut CallbackProgramRuntime,
    events: &'events mut Vec<CallbackProgramResult>,
}

impl<'program, 'runtime, 'events> ExecutableCallbacks<'program, 'runtime, 'events> {
    pub(crate) fn new(
        program: &'program ExecutableCallbackProgram,
        runtime: &'runtime mut CallbackProgramRuntime,
        events: &'events mut Vec<CallbackProgramResult>,
    ) -> Self {
        Self {
            program,
            runtime,
            events,
        }
    }

    pub(crate) fn invoke(
        &mut self,
        invocation: &CallbackInvocation,
        custom_state: &mut BTreeMap<String, Value>,
    ) -> Result<CallbackProgramResult, ExecutableCallbackError> {
        let event = self
            .runtime
            .invoke(self.program, invocation, custom_state)?;
        if !valid_executable_transition(self.events.last(), &event) {
            return Err(
                ExecutableCallbackError::ExecutableCallbackInvalidTransition {
                    source_id: event.source_id.clone(),
                    callback: event.callback_name.clone(),
                    instruction_id: None,
                    timestamp_ms: invocation.timestamp_ms,
                },
            );
        }
        self.events.push(event.clone());
        Ok(event)
    }
}

fn valid_executable_transition(
    previous: Option<&CallbackProgramResult>,
    next: &CallbackProgramResult,
) -> bool {
    if next.callback_name == "bot_loop_start" {
        return true;
    }
    let Some(previous) = previous else {
        return matches!(
            next.callback_name.as_str(),
            "loop_cadence_startup_lookback"
                | "bot_loop_start"
                | "leverage"
                | "custom_stake_amount"
                | "order_filled"
                | "adjust_trade_position"
                | "custom_stoploss"
        );
    };
    match previous.callback_name.as_str() {
        "loop_cadence_startup_lookback" => next.callback_name == "bot_loop_start",
        "bot_loop_start" => matches!(
            next.callback_name.as_str(),
            "leverage" | "custom_stake_amount" | "adjust_trade_position" | "custom_stoploss"
        ),
        "leverage" => next.callback_name == "custom_stake_amount",
        "custom_stake_amount" => {
            next.callback_name == "confirm_trade_entry"
                || (next.callback_name == "custom_stake_amount"
                    && previous.return_class == CallbackReturnClass::Stake
                    && previous.return_value.as_ref().and_then(Value::as_f64) == Some(0.0))
        }
        "confirm_trade_entry" | "confirm_trade_exit" => {
            previous.return_class == CallbackReturnClass::Boolean
                && previous.return_value.as_ref().and_then(Value::as_bool) == Some(true)
                && next.callback_name == "order_filled"
        }
        "order_filled" => matches!(
            next.callback_name.as_str(),
            "leverage" | "custom_stake_amount" | "adjust_trade_position" | "custom_stoploss"
        ),
        "adjust_trade_position" => {
            matches!(
                next.callback_name.as_str(),
                "order_filled" | "custom_stoploss"
            )
        }
        "custom_stoploss" => matches!(
            next.callback_name.as_str(),
            "adjust_trade_position" | "custom_stoploss" | "custom_exit"
        ),
        "custom_exit" => next.callback_name == "confirm_trade_exit",
        _ => false,
    }
}

pub(crate) fn record_current(
    phase: CallbackPhase,
    outcome: CallbackOutcome,
) -> Result<(), SimError> {
    with_session(|slot| {
        let Some(session) = slot.as_mut() else {
            return Ok(());
        };
        let visibility = session.runtime.visibility().clone();
        session
            .runtime
            .record(phase, outcome, visibility)
            .map_err(runtime_error)
    })
}

pub(crate) fn record_trade_current(
    phase: CallbackPhase,
    outcome: CallbackOutcome,
    trade: &OpenTrade,
) -> Result<(), SimError> {
    record_trade_transaction_current(phase, outcome, CallbackTransaction::Committed, trade, None)
}

pub(crate) fn record_trade_transaction_current(
    phase: CallbackPhase,
    outcome: CallbackOutcome,
    transaction: CallbackTransaction,
    trade: &OpenTrade,
    diagnostic: Option<String>,
) -> Result<(), SimError> {
    with_session(|slot| {
        let Some(session) = slot.as_mut() else {
            return Ok(());
        };
        let wallet = session.runtime.visibility().wallet_available;
        record_trade_in_session(
            session,
            phase,
            outcome,
            transaction,
            wallet,
            trade,
            diagnostic,
        )
    })
}

pub(crate) fn record_trade(
    phase: CallbackPhase,
    outcome: CallbackOutcome,
    transaction: CallbackTransaction,
    wallet_available: f64,
    trade: &OpenTrade,
    diagnostic: Option<String>,
) -> Result<(), SimError> {
    with_session(|slot| {
        let Some(session) = slot.as_mut() else {
            return Ok(());
        };
        record_trade_in_session(
            session,
            phase,
            outcome,
            transaction,
            wallet_available,
            trade,
            diagnostic,
        )
    })
}

fn record_trade_in_session(
    session: &mut TraceSession,
    phase: CallbackPhase,
    outcome: CallbackOutcome,
    transaction: CallbackTransaction,
    wallet_available: f64,
    trade: &OpenTrade,
    diagnostic: Option<String>,
) -> Result<(), SimError> {
    let generation = session.runtime.visibility().custom_state_generation
        + u64::from(session.custom_state != trade.custom_data);
    if transaction == CallbackTransaction::Committed {
        session.custom_state = trade.custom_data.clone();
    }
    let visibility = CallbackVisibility {
        feature_index: session.runtime.visibility().feature_index,
        order_count: trade.orders.len(),
        wallet_available,
        custom_state_generation: generation,
    };
    session
        .runtime
        .record_transaction(phase, outcome, transaction, visibility, diagnostic)
        .map_err(runtime_error)
}

pub(crate) fn finish() -> Result<Vec<CallbackSemanticEvent>, SimError> {
    with_session(|slot| {
        let Some(mut session) = slot.take() else {
            return Ok(Vec::new());
        };
        let visibility = session.runtime.visibility().clone();
        session
            .runtime
            .record(
                CallbackPhase::CandleAfter,
                CallbackOutcome::Accepted,
                visibility,
            )
            .map_err(runtime_error)?;
        Ok(session.runtime.events().to_vec())
    })
}

fn with_session<T>(
    operation: impl FnOnce(&mut Option<TraceSession>) -> Result<T, SimError>,
) -> Result<T, SimError> {
    SESSION
        .try_with(|cell| {
            let mut slot = cell
                .try_borrow_mut()
                .map_err(|_| SimError::InvalidCallbackRuntime)?;
            operation(&mut slot)
        })
        .map_err(|_| SimError::InvalidCallbackRuntime)?
}

fn runtime_error(_: crate::domain::CallbackRuntimeError) -> SimError {
    SimError::InvalidCallbackRuntime
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::CallbackProgramTransaction;

    fn event(
        name: &str,
        class: CallbackReturnClass,
        value: Option<Value>,
    ) -> CallbackProgramResult {
        CallbackProgramResult {
            schema_version: "callback-semantic-trace-v2",
            callback_contract_fingerprint: "a".repeat(64),
            callback_execution_ir_fingerprint: "b".repeat(64),
            callback_name: name.to_owned(),
            source_id: format!("sha256:{}", "c".repeat(64)),
            program_fingerprint: "d".repeat(64),
            return_class: class,
            return_value: value,
            transaction: CallbackProgramTransaction::Committed,
            exception: None,
            predicate_ids: Vec::new(),
            register_deltas: Vec::new(),
            custom_state_deltas: Vec::new(),
            observations: Vec::new(),
            register_before_fingerprint: "e".repeat(64),
            register_after_fingerprint: "e".repeat(64),
            custom_state_fingerprint: "f".repeat(64),
        }
    }

    #[test]
    fn authenticated_v2_accepts_futures_leverage_then_stake() {
        let leverage = event(
            "leverage",
            CallbackReturnClass::Leverage,
            Some(Value::from(2.0)),
        );
        let stake = event(
            "custom_stake_amount",
            CallbackReturnClass::Stake,
            Some(Value::from(100.0)),
        );
        assert!(valid_executable_transition(None, &leverage));
        assert!(valid_executable_transition(Some(&leverage), &stake));
    }
}
