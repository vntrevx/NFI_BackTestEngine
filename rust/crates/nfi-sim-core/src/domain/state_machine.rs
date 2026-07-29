//! Generic, data-driven state-machine program contract.

#![allow(clippy::module_name_repetitions)]

use std::collections::BTreeMap;

use serde::Deserialize;
use serde_json::Value;

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StateMachineProgram {
    pub schema_version: String,
    pub entrypoints: BTreeMap<String, StateMachineEntrypoint>,
    pub required_reads: Vec<StateMachineRead>,
    pub required_columns: Vec<String>,
    pub required_state_keys: Vec<String>,
    pub opcodes: Vec<String>,
    pub source_map: BTreeMap<String, StateMachineSourceLocation>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StateMachineEntrypoint {
    pub max_steps: usize,
    pub instructions: Vec<StateMachineInstruction>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StateMachineRead {
    pub source: StateMachineReadSource,
    pub key: String,
}

#[derive(Debug, Clone, Copy, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "snake_case")]
pub enum StateMachineReadSource {
    Candle,
    Wallet,
    Trade,
    Orders,
    CustomState,
    Input,
    Local,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StateMachineSourceLocation {
    pub path: String,
    pub line: usize,
    pub column: usize,
    pub end_line: usize,
    pub end_column: usize,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "opcode", rename_all = "snake_case", deny_unknown_fields)]
pub enum StateMachineInstruction {
    If {
        id: String,
        condition: StateMachineExpression,
        then_instructions: Vec<Self>,
        else_instructions: Vec<Self>,
    },
    SetLocal {
        id: String,
        name: String,
        value: StateMachineExpression,
    },
    SetState {
        id: String,
        key: String,
        value_type: StateMachineValueType,
        value: StateMachineExpression,
    },
    DeleteState {
        id: String,
        key: String,
    },
    Evaluate {
        id: String,
        expression: StateMachineExpression,
    },
    BoundedFor {
        id: String,
        variable: String,
        start: i64,
        stop: i64,
        max_iterations: usize,
        instructions: Vec<Self>,
    },
    Action {
        id: String,
        kind: StateMachineActionKind,
        stake: Option<StateMachineExpression>,
        tag: Option<StateMachineExpression>,
    },
}

impl StateMachineInstruction {
    #[must_use]
    pub fn id(&self) -> &str {
        match self {
            Self::If { id, .. }
            | Self::SetLocal { id, .. }
            | Self::SetState { id, .. }
            | Self::DeleteState { id, .. }
            | Self::Evaluate { id, .. }
            | Self::BoundedFor { id, .. }
            | Self::Action { id, .. } => id,
        }
    }
}

#[derive(Debug, Clone, Copy, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum StateMachineValueType {
    Null,
    Bool,
    Integer,
    Number,
    String,
    Json,
}

#[derive(Debug, Clone, Copy, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum StateMachineActionKind {
    AddEntry,
    PartialExit,
    Derisk,
    Buyback,
    Stop,
    Exit,
    NoOp,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum StateMachineExpression {
    Literal {
        value: Value,
    },
    Read {
        source: StateMachineReadSource,
        key: String,
        default: Option<Box<Self>>,
    },
    Unary {
        operator: StateMachineUnaryOperator,
        operand: Box<Self>,
    },
    Binary {
        operator: StateMachineBinaryOperator,
        left: Box<Self>,
        right: Box<Self>,
    },
    Boolean {
        operator: StateMachineBooleanOperator,
        values: Vec<Self>,
    },
    Compare {
        operator: StateMachineComparison,
        left: Box<Self>,
        right: Box<Self>,
    },
    ScalarCall {
        name: StateMachineScalarCall,
        arguments: Vec<Self>,
    },
}

#[derive(Debug, Clone, Copy, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StateMachineUnaryOperator {
    Not,
    Negative,
    Positive,
}

#[derive(Debug, Clone, Copy, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StateMachineBinaryOperator {
    Add,
    Subtract,
    Multiply,
    Divide,
    FloorDivide,
    Modulo,
    Power,
}

#[derive(Debug, Clone, Copy, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StateMachineBooleanOperator {
    And,
    Or,
}

#[derive(Debug, Clone, Copy, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StateMachineComparison {
    Equal,
    NotEqual,
    Less,
    LessEqual,
    Greater,
    GreaterEqual,
    Is,
    IsNot,
}

#[derive(Debug, Clone, Copy, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StateMachineScalarCall {
    Abs,
    Min,
    Max,
    Float,
    Int,
    Bool,
    Len,
}
