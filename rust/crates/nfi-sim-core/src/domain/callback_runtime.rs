//! Typed callback cadence and entrypoint-atomic state boundary.

use super::{
    CallbackOutcome, CallbackPhase, CallbackRuntimeError, CallbackSemanticEvent,
    CallbackTransaction, CallbackVisibility, CALLBACK_TRACE_SCHEMA_VERSION,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Error {
    InvalidTransition {
        from: CallbackPhase,
        to: CallbackPhase,
    },
    InvalidOutcome {
        phase: CallbackPhase,
        outcome: CallbackOutcome,
    },
}

/// Resolve the callback-controlled same-candle exit competition.
///
/// A custom-exit value wins over a reached stop. `None` and a contained
/// callback exception fall through to the stop candidate.
///
/// # Errors
///
/// Returns [`CallbackRuntimeError`] when supplied a return shape that
/// `custom_exit` cannot produce.
pub fn same_candle_exit_winner(
    custom_exit: CallbackOutcome,
    stop_reached: bool,
) -> Result<Option<CallbackPhase>, CallbackRuntimeError> {
    match (custom_exit, stop_reached) {
        (CallbackOutcome::Value, _) => Ok(Some(CallbackPhase::CustomExit)),
        (CallbackOutcome::None | CallbackOutcome::Exception, true) => {
            Ok(Some(CallbackPhase::CustomStoploss))
        }
        (CallbackOutcome::None | CallbackOutcome::Exception, false) => Ok(None),
        (CallbackOutcome::Accepted | CallbackOutcome::Rejected, _) => {
            Err(CallbackRuntimeError::InvalidOutcome {
                phase: CallbackPhase::CustomExit,
                outcome: custom_exit,
            })
        }
    }
}

pub struct Runtime {
    candle_index: usize,
    phase: CallbackPhase,
    outcome: Option<CallbackOutcome>,
    visibility: CallbackVisibility,
    events: Vec<CallbackSemanticEvent>,
}

impl Runtime {
    #[must_use]
    pub fn new(candle_index: usize, visibility: CallbackVisibility) -> Self {
        Self {
            candle_index,
            phase: CallbackPhase::CandleStart,
            outcome: None,
            visibility,
            events: Vec::new(),
        }
    }

    #[must_use]
    pub fn visibility(&self) -> &CallbackVisibility {
        &self.visibility
    }

    #[must_use]
    pub fn events(&self) -> &[CallbackSemanticEvent] {
        &self.events
    }

    /// Record a callback whose mutation was committed by Native execution.
    ///
    /// # Errors
    ///
    /// Returns [`CallbackRuntimeError`] for an out-of-order phase or an
    /// outcome shape that the callback cannot return.
    pub fn record(
        &mut self,
        phase: CallbackPhase,
        outcome: CallbackOutcome,
        visibility: CallbackVisibility,
    ) -> Result<(), CallbackRuntimeError> {
        self.validate_transition(phase)?;
        Self::validate_outcome(phase, outcome)?;
        self.visibility = visibility;
        self.push(phase, outcome, CallbackTransaction::Committed, None);
        Ok(())
    }

    /// Record a production callback with its explicit transaction result.
    ///
    /// # Errors
    ///
    /// Returns [`CallbackRuntimeError`] for an invalid phase or outcome.
    pub fn record_transaction(
        &mut self,
        phase: CallbackPhase,
        outcome: CallbackOutcome,
        transaction: CallbackTransaction,
        visibility: CallbackVisibility,
        diagnostic: Option<String>,
    ) -> Result<(), CallbackRuntimeError> {
        self.validate_transition(phase)?;
        Self::validate_outcome(phase, outcome)?;
        self.visibility = visibility;
        self.push(phase, outcome, transaction, diagnostic);
        Ok(())
    }

    /// Run one callback against a private state copy and commit only success.
    ///
    /// # Errors
    ///
    /// Returns [`CallbackRuntimeError`] for an out-of-order phase or an
    /// outcome shape that the callback cannot return. Callback exceptions are
    /// semantic events, not runtime errors, and roll back the private copy.
    pub fn invoke<F>(
        &mut self,
        phase: CallbackPhase,
        callback: F,
    ) -> Result<CallbackSemanticEvent, CallbackRuntimeError>
    where
        F: FnOnce(&mut CallbackVisibility) -> Result<CallbackOutcome, String>,
    {
        self.validate_transition(phase)?;
        let mut candidate = self.visibility.clone();
        let (outcome, transaction, diagnostic) = match callback(&mut candidate) {
            Ok(outcome) => {
                Self::validate_outcome(phase, outcome)?;
                self.visibility = candidate;
                (outcome, CallbackTransaction::Committed, None)
            }
            Err(diagnostic) => (
                CallbackOutcome::Exception,
                CallbackTransaction::RolledBack,
                Some(diagnostic),
            ),
        };
        Ok(self.push(phase, outcome, transaction, diagnostic))
    }

    fn push(
        &mut self,
        phase: CallbackPhase,
        outcome: CallbackOutcome,
        transaction: CallbackTransaction,
        diagnostic: Option<String>,
    ) -> CallbackSemanticEvent {
        self.phase = phase;
        self.outcome = Some(outcome);
        let event = CallbackSemanticEvent {
            schema_version: CALLBACK_TRACE_SCHEMA_VERSION,
            candle_index: self.candle_index,
            sequence: self.events.len(),
            phase,
            outcome,
            transaction,
            visibility: self.visibility.clone(),
            diagnostic,
        };
        self.events.push(event.clone());
        event
    }

    fn validate_transition(&self, to: CallbackPhase) -> Result<(), CallbackRuntimeError> {
        let valid = match self.phase {
            CallbackPhase::CandleStart => matches!(
                to,
                CallbackPhase::StakeSizing
                    | CallbackPhase::OrderFilled
                    | CallbackPhase::PositionAdjustment
                    | CallbackPhase::CustomStoploss
                    | CallbackPhase::CandleAfter
            ),
            CallbackPhase::StakeSizing => to == CallbackPhase::Leverage,
            CallbackPhase::Leverage => matches!(
                to,
                CallbackPhase::EntryConfirmation | CallbackPhase::CandleAfter
            ),
            CallbackPhase::EntryConfirmation => match self.outcome {
                Some(CallbackOutcome::Accepted) => to == CallbackPhase::OrderFilled,
                Some(CallbackOutcome::Rejected | CallbackOutcome::Exception) => {
                    to == CallbackPhase::CandleAfter
                }
                Some(CallbackOutcome::Value | CallbackOutcome::None) | None => false,
            },
            CallbackPhase::OrderFilled => matches!(
                to,
                CallbackPhase::PositionAdjustment
                    | CallbackPhase::CustomStoploss
                    | CallbackPhase::CandleAfter
            ),
            CallbackPhase::PositionAdjustment => matches!(
                to,
                CallbackPhase::OrderFilled
                    | CallbackPhase::CustomStoploss
                    | CallbackPhase::CandleAfter
            ),
            CallbackPhase::CustomStoploss => matches!(
                to,
                CallbackPhase::CustomExit
                    | CallbackPhase::ExitConfirmation
                    | CallbackPhase::CandleAfter
            ),
            CallbackPhase::CustomExit => match self.outcome {
                Some(CallbackOutcome::Value) => matches!(
                    to,
                    CallbackPhase::ExitConfirmation | CallbackPhase::CandleAfter
                ),
                Some(CallbackOutcome::None | CallbackOutcome::Exception) => matches!(
                    to,
                    CallbackPhase::ExitConfirmation | CallbackPhase::CandleAfter
                ),
                Some(CallbackOutcome::Accepted | CallbackOutcome::Rejected) | None => false,
            },
            CallbackPhase::ExitConfirmation => matches!(
                to,
                CallbackPhase::ExitConfirmation
                    | CallbackPhase::StakeSizing
                    | CallbackPhase::CandleAfter
            ),
            CallbackPhase::CandleAfter => false,
        };
        if valid {
            Ok(())
        } else {
            Err(CallbackRuntimeError::InvalidTransition {
                from: self.phase,
                to,
            })
        }
    }

    fn validate_outcome(
        phase: CallbackPhase,
        outcome: CallbackOutcome,
    ) -> Result<(), CallbackRuntimeError> {
        let valid = match phase {
            CallbackPhase::CandleStart => false,
            CallbackPhase::StakeSizing
            | CallbackPhase::Leverage
            | CallbackPhase::PositionAdjustment
            | CallbackPhase::CustomStoploss
            | CallbackPhase::CustomExit => matches!(
                outcome,
                CallbackOutcome::Value | CallbackOutcome::None | CallbackOutcome::Exception
            ),
            CallbackPhase::EntryConfirmation | CallbackPhase::ExitConfirmation => matches!(
                outcome,
                CallbackOutcome::Accepted | CallbackOutcome::Rejected | CallbackOutcome::Exception
            ),
            CallbackPhase::OrderFilled | CallbackPhase::CandleAfter => matches!(
                outcome,
                CallbackOutcome::Accepted | CallbackOutcome::Exception
            ),
        };
        if valid {
            Ok(())
        } else {
            Err(CallbackRuntimeError::InvalidOutcome { phase, outcome })
        }
    }
}
