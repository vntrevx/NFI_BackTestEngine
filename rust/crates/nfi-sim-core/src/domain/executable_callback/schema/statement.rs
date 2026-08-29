use serde::{Deserialize, Serialize};

use super::{CallbackExpression, CallbackReturn};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
pub enum Statement {
    Let {
        id: String,
        predicate_ids: Vec<String>,
        name: String,
        value: CallbackExpression,
    },
    SetRegister {
        id: String,
        predicate_ids: Vec<String>,
        register_id: String,
        value: CallbackExpression,
    },
    SetRegisterItem {
        id: String,
        predicate_ids: Vec<String>,
        register_id: String,
        key: CallbackExpression,
        value: CallbackExpression,
    },
    SetCustomState {
        id: String,
        predicate_ids: Vec<String>,
        key: String,
        value: CallbackExpression,
    },
    DeleteCustomState {
        id: String,
        predicate_ids: Vec<String>,
        key: String,
    },
    If {
        id: String,
        predicate_ids: Vec<String>,
        condition: CallbackExpression,
        then: Vec<Self>,
        otherwise: Vec<Self>,
    },
    ForRange {
        id: String,
        predicate_ids: Vec<String>,
        target: String,
        bounds: Vec<CallbackExpression>,
        body: Vec<Self>,
    },
    Return {
        id: String,
        predicate_ids: Vec<String>,
        result: CallbackReturn,
    },
    RaiseCallback {
        id: String,
        predicate_ids: Vec<String>,
        exception_class: String,
        message: CallbackExpression,
    },
    EmitObservation {
        id: String,
        predicate_ids: Vec<String>,
        channel: String,
        payload: CallbackExpression,
    },
}
