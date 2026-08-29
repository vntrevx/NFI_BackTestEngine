use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::CallbackExpression;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
pub enum Expression {
    Literal {
        value: Value,
    },
    ReadInput {
        name: String,
    },
    ReadLocal {
        name: String,
    },
    ReadRegister {
        register_id: String,
    },
    ReadTrade {
        field: String,
    },
    ReadOrder {
        field: String,
    },
    ReadCandle {
        field: String,
    },
    ReadWallet {
        field: String,
    },
    ReadCustomState {
        key: String,
        default: Box<Self>,
    },
    MapGet {
        value: Box<Self>,
        key: Box<Self>,
        default: Box<Self>,
    },
    Record {
        fields: Vec<CallbackRecordField>,
    },
    List {
        items: Vec<Self>,
    },
    Tuple {
        items: Vec<Self>,
    },
    Index {
        value: Box<Self>,
        index: Box<Self>,
    },
    Unary {
        operator: CallbackUnaryOperator,
        value: Box<Self>,
    },
    Binary {
        operator: CallbackBinaryOperator,
        left: Box<Self>,
        right: Box<Self>,
    },
    Compare {
        left: Box<Self>,
        comparisons: Vec<CallbackComparison>,
    },
    And {
        values: Vec<Self>,
    },
    Or {
        values: Vec<Self>,
    },
    Choose {
        condition: Box<Self>,
        then: Box<Self>,
        otherwise: Box<Self>,
    },
    CallBuiltin {
        name: CallbackBuiltin,
        args: Vec<Self>,
    },
    TimestampMs {
        value: Box<Self>,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(untagged)]
pub enum CallbackRecordField {
    Named(CallbackNamedField),
    Spread(CallbackSpreadField),
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CallbackNamedField {
    pub name: String,
    pub value: CallbackExpression,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CallbackSpreadField {
    pub spread: CallbackExpression,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CallbackComparison {
    pub operator: CallbackCompareOperator,
    pub right: CallbackExpression,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CallbackUnaryOperator {
    Not,
    Neg,
    Pos,
}
#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CallbackBinaryOperator {
    Add,
    Sub,
    Mul,
    Div,
    Mod,
}
#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CallbackCompareOperator {
    Eq,
    Ne,
    Lt,
    Le,
    Gt,
    Ge,
    Is,
    IsNot,
    In,
    NotIn,
}
#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CallbackBuiltin {
    Min,
    Max,
    Len,
    Int,
    Float,
    Bool,
}
