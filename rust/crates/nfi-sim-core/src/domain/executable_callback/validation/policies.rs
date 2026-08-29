use crate::domain::{
    CallbackAcceptedReturn, CallbackCadence, CallbackFallback, CallbackReturnClass,
    CallbackStatement,
};

pub(super) fn accepted_returns(name: &str) -> Vec<CallbackAcceptedReturn> {
    use CallbackAcceptedReturn as A;
    match name {
        "bot_loop_start" | "order_filled" => vec![A::None],
        "leverage" => vec![A::FiniteNumber],
        "custom_stake_amount" => vec![A::FinitePositiveNumber, A::Zero, A::None],
        "confirm_trade_entry" | "confirm_trade_exit" => vec![A::TruthyAccept, A::FalsyReject],
        "adjust_trade_position" => vec![
            A::None,
            A::Zero,
            A::PositiveNumber,
            A::NegativeNumber,
            A::NumberAndTag,
        ],
        "custom_stoploss" => vec![A::None, A::FiniteNumber],
        "custom_exit" => vec![A::None, A::False, A::True, A::NonEmptyString],
        _ => vec![A::LifecycleTransition],
    }
}
pub(super) fn expected_cadence(name: &str) -> CallbackCadence {
    match name {
        "bot_loop_start" => CallbackCadence::OncePerMainCandle,
        "leverage" | "custom_stake_amount" | "confirm_trade_entry" => {
            CallbackCadence::PerInitialEntry
        }
        "order_filled" => CallbackCadence::PerFill,
        "confirm_trade_exit" => CallbackCadence::PerExitCandidate,
        "loop_cadence_startup_lookback" => CallbackCadence::SyntheticLifecycle,
        _ => CallbackCadence::PerOpenTradeCandle,
    }
}
pub(super) fn valid_order(name: &str, phase: usize, after: &[String], before: &[String]) -> bool {
    let ordered = [
        "loop_cadence_startup_lookback",
        "bot_loop_start",
        "leverage",
        "custom_stake_amount",
        "confirm_trade_entry",
        "order_filled",
        "adjust_trade_position",
        "custom_stoploss",
        "custom_exit",
        "confirm_trade_exit",
    ];
    let Some(index) = ordered.iter().position(|item| *item == name) else {
        return false;
    };
    let expected_after = if index < 3 {
        Vec::new()
    } else {
        vec![ordered[index - 1].to_owned()]
    };
    phase == index && after == expected_after && before.is_empty()
}

pub(super) fn valid_fallback(name: &str, fallback: &CallbackFallback) -> bool {
    use CallbackReturnClass as R;
    match name {
        "loop_cadence_startup_lookback" => {
            fallback.class == R::LifecycleTransition
                && fallback.value.as_str() == Some("load_trim_execute")
        }
        "bot_loop_start" | "order_filled" | "custom_stoploss" => {
            fallback.class == R::None && fallback.value.is_null()
        }
        "leverage" => fallback.class == R::Leverage && fallback.value.as_f64() == Some(1.0),
        "custom_stake_amount" => {
            fallback.class == R::Stake && fallback.value.as_str() == Some("proposed_stake")
        }
        "confirm_trade_entry" | "confirm_trade_exit" => {
            fallback.class == R::Boolean && fallback.value.as_bool() == Some(true)
        }
        "adjust_trade_position" => {
            fallback.class == R::Adjustment
                && fallback.value.as_array().is_some_and(|items| {
                    items.as_slice()
                        == [
                            serde_json::Value::Null,
                            serde_json::Value::String(String::new()),
                        ]
                })
        }
        "custom_exit" => fallback.class == R::Boolean && fallback.value.as_bool() == Some(false),
        _ => false,
    }
}

pub(super) fn valid_statement_returns(name: &str, statements: &[CallbackStatement]) -> bool {
    statements.iter().all(|statement| match statement {
        CallbackStatement::Return { result, .. } => match name {
            "bot_loop_start" | "order_filled" => result.class == CallbackReturnClass::None,
            "leverage" => result.class == CallbackReturnClass::Leverage,
            "custom_stake_amount" => matches!(
                result.class,
                CallbackReturnClass::Stake | CallbackReturnClass::None
            ),
            "confirm_trade_entry" | "confirm_trade_exit" => {
                result.class == CallbackReturnClass::Boolean
            }
            "adjust_trade_position" => matches!(
                result.class,
                CallbackReturnClass::Adjustment | CallbackReturnClass::None
            ),
            "custom_stoploss" => matches!(
                result.class,
                CallbackReturnClass::Stoploss | CallbackReturnClass::None
            ),
            "custom_exit" => matches!(
                result.class,
                CallbackReturnClass::ExitReason | CallbackReturnClass::None
            ),
            _ => result.class == CallbackReturnClass::LifecycleTransition,
        },
        CallbackStatement::If {
            then, otherwise, ..
        } => valid_statement_returns(name, then) && valid_statement_returns(name, otherwise),
        CallbackStatement::ForRange { body, .. } => valid_statement_returns(name, body),
        _ => true,
    })
}
