//! Bounded callback, stake, confirmation, and scalar program contracts.

use std::collections::BTreeMap;

use serde::Deserialize;
use serde_json::Value;

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CallbackProgram {
    #[serde(default)]
    pub order_filled: Option<OrderFilledProgram>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OrderFilledProgram {
    pub initial_successful_entry_writes: Vec<CustomDataWrite>,
    pub order_tag_actions: BTreeMap<String, Vec<CustomDataWrite>>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CustomDataWrite {
    pub key: String,
    pub value: Value,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StakeProgram {
    pub statements: Vec<StakeStatement>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum StakeStatement {
    #[serde(rename = "let")]
    Let {
        name: String,
        value: StakeExpression,
    },
    #[serde(rename = "if")]
    If {
        condition: StakeExpression,
        then: Vec<StakeStatement>,
        otherwise: Vec<StakeStatement>,
    },
    #[serde(rename = "for")]
    For {
        name: String,
        iterable: StakeExpression,
        body: Vec<StakeStatement>,
    },
    Return {
        value: StakeExpression,
    },
}

#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum StakeExpression {
    Literal {
        value: Value,
    },
    Variable {
        name: String,
    },
    Multiply {
        left: Box<Self>,
        right: Box<Self>,
    },
    And {
        values: Vec<Self>,
    },
    Or {
        values: Vec<Self>,
    },
    Equal {
        left: Box<Self>,
        right: Box<Self>,
    },
    Greater {
        left: Box<Self>,
        right: Box<Self>,
    },
    Choose {
        condition: Box<Self>,
        then: Box<Self>,
        otherwise: Box<Self>,
    },
    Index {
        value: Box<Self>,
        index: Box<Self>,
    },
    SplitWords {
        value: Box<Self>,
    },
    StakeClampMin {
        multiplier: Box<Self>,
    },
    AllIn {
        items: Box<Self>,
        container: Box<Self>,
    },
    AnyIn {
        items: Box<Self>,
        container: Box<Self>,
    },
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ConfirmProgram {
    pub statements: Vec<Value>,
    pub functions: BTreeMap<String, ConfirmFunction>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ConfirmFunction {
    pub parameters: Vec<String>,
    pub statements: Vec<Value>,
}

/// Compact arena program used by large, pure trade-decision functions.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScalarDecisionProgram {
    pub schema_version: String,
    pub opcode: String,
    pub parameters: Vec<String>,
    pub expressions: Vec<Value>,
    pub statements: Vec<Value>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScalarProgramBundle {
    pub schema_version: String,
    pub entry: String,
    pub programs: BTreeMap<String, ScalarDecisionProgram>,
}
