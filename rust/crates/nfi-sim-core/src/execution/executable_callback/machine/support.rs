use std::collections::BTreeMap;

use serde_json::Value;

use super::super::types::{CallbackDeltaOperation, CallbackInvocation, CallbackTypedDelta};
use crate::domain::{
    canonical_callback_json_sha256, CallbackReturnClass, CallbackStatement as S,
    ExecutableCallbackError as Error, ExecutableCallbackProgram,
};

#[derive(Clone, Copy)]
pub(super) enum RuntimeKind {
    Transition,
    Return,
    Register,
    Steps,
    Missing,
    Observation,
    Transaction,
}
pub(super) fn runtime_error(
    program: &ExecutableCallbackProgram,
    invocation: &CallbackInvocation,
    instruction: Option<String>,
    kind: RuntimeKind,
) -> Error {
    error(
        &source_id(program, &invocation.callback),
        &invocation.callback,
        instruction,
        invocation.timestamp_ms,
        kind,
    )
}
pub(super) fn error(
    source_id: &str,
    callback: &str,
    instruction_id: Option<String>,
    timestamp_ms: i64,
    kind: RuntimeKind,
) -> Error {
    match kind {
        RuntimeKind::Transition => Error::ExecutableCallbackInvalidTransition {
            source_id: source_id.to_owned(),
            callback: callback.to_owned(),
            instruction_id,
            timestamp_ms,
        },
        RuntimeKind::Return => Error::ExecutableCallbackInvalidReturn {
            source_id: source_id.to_owned(),
            callback: callback.to_owned(),
            instruction_id,
            timestamp_ms,
        },
        RuntimeKind::Register => Error::ExecutableCallbackRegisterType {
            source_id: source_id.to_owned(),
            callback: callback.to_owned(),
            instruction_id,
            timestamp_ms,
        },
        RuntimeKind::Steps => Error::ExecutableCallbackStepLimit {
            source_id: source_id.to_owned(),
            callback: callback.to_owned(),
            instruction_id,
            timestamp_ms,
        },
        RuntimeKind::Missing => Error::ExecutableCallbackMissingInput {
            source_id: source_id.to_owned(),
            callback: callback.to_owned(),
            instruction_id,
            timestamp_ms,
        },
        RuntimeKind::Observation => Error::ExecutableCallbackObservation {
            source_id: source_id.to_owned(),
            callback: callback.to_owned(),
            instruction_id,
            timestamp_ms,
        },
        RuntimeKind::Transaction => Error::ExecutableCallbackTransaction {
            source_id: source_id.to_owned(),
            callback: callback.to_owned(),
            instruction_id,
            timestamp_ms,
        },
    }
}
pub(super) fn identity(value: &S) -> (&str, &[String]) {
    match value {
        S::Let {
            id, predicate_ids, ..
        }
        | S::SetRegister {
            id, predicate_ids, ..
        }
        | S::SetRegisterItem {
            id, predicate_ids, ..
        }
        | S::SetCustomState {
            id, predicate_ids, ..
        }
        | S::DeleteCustomState {
            id, predicate_ids, ..
        }
        | S::If {
            id, predicate_ids, ..
        }
        | S::ForRange {
            id, predicate_ids, ..
        }
        | S::Return {
            id, predicate_ids, ..
        }
        | S::RaiseCallback {
            id, predicate_ids, ..
        }
        | S::EmitObservation {
            id, predicate_ids, ..
        } => (id, predicate_ids),
    }
}
pub(super) fn delta(
    operation: CallbackDeltaOperation,
    key: &str,
    before: Option<Value>,
    after: Option<Value>,
    id: &str,
    predicates: &[String],
) -> CallbackTypedDelta {
    CallbackTypedDelta {
        operation,
        key: key.to_owned(),
        before,
        after,
        producer_instruction_id: id.to_owned(),
        predicate_ids: predicates.to_vec(),
    }
}
pub(super) fn source_id(program: &ExecutableCallbackProgram, callback: &str) -> String {
    let method = format!("method:{callback}");
    program
        .identity
        .source_closure
        .iter()
        .find(|item| item.logical_method_id == method)
        .or_else(|| program.identity.source_closure.first())
        .map_or_else(String::new, |item| item.source_id.clone())
}
pub(super) fn map_fingerprint(values: &BTreeMap<String, Value>) -> Result<String, Error> {
    let value =
        serde_json::to_value(values).map_err(|error| Error::InvalidExecutableCallbackProgram {
            reason: error.to_string(),
        })?;
    canonical_callback_json_sha256(&value)
}
pub(super) fn normalize_none(class: CallbackReturnClass, value: Option<Value>) -> Option<Value> {
    if class == CallbackReturnClass::None && value.as_ref().is_none_or(Value::is_null) {
        None
    } else {
        value
    }
}
pub(super) fn normalize_return(
    class: CallbackReturnClass,
    value: Option<Value>,
) -> (CallbackReturnClass, Option<Value>) {
    match (class, value) {
        (
            CallbackReturnClass::Stake
            | CallbackReturnClass::Stoploss
            | CallbackReturnClass::Adjustment
            | CallbackReturnClass::ExitReason,
            Some(Value::Null),
        )
        | (CallbackReturnClass::None, None | Some(Value::Null)) => {
            (CallbackReturnClass::None, None)
        }
        (CallbackReturnClass::ExitReason, Some(value @ Value::Bool(_))) => {
            (CallbackReturnClass::Boolean, Some(value))
        }
        (class, value) => (class, value),
    }
}
pub(super) fn valid_return(
    callback: &str,
    class: CallbackReturnClass,
    value: Option<&Value>,
) -> bool {
    use CallbackReturnClass as R;
    let shape = match class {
        R::None => value.is_none(),
        R::Boolean => value.is_some_and(Value::is_boolean),
        R::Number | R::Stake | R::Leverage | R::Stoploss => {
            value.and_then(Value::as_f64).is_some_and(f64::is_finite)
        }
        R::String | R::ExitReason | R::LifecycleTransition => {
            value.and_then(Value::as_str).is_some()
        }
        R::Adjustment => value.is_none_or(|item| {
            item.as_f64().is_some_and(f64::is_finite)
                || item.as_array().is_some_and(|items| {
                    !items.is_empty()
                        && items.len() <= 2
                        && (items[0].is_null() || items[0].as_f64().is_some_and(f64::is_finite))
                })
        }),
    };
    shape
        && match callback {
            "bot_loop_start" | "order_filled" => class == R::None,
            "leverage" => matches!(class, R::Leverage | R::Number),
            "custom_stake_amount" => matches!(class, R::Stake | R::Number | R::None),
            "confirm_trade_entry" | "confirm_trade_exit" => class == R::Boolean,
            "adjust_trade_position" => matches!(class, R::Adjustment | R::Number | R::None),
            "custom_stoploss" => matches!(class, R::Stoploss | R::Number | R::None),
            "custom_exit" => matches!(class, R::ExitReason | R::String | R::Boolean | R::None),
            "loop_cadence_startup_lookback" => class == R::LifecycleTransition,
            _ => false,
        }
}
