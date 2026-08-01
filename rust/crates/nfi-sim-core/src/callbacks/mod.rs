//! Compiled callback evaluation and dataframe feature projection.

mod evaluation;
mod projection;

pub(crate) use evaluation::{
    evaluate_adjustment_bundle, evaluate_custom_exit_bundle, feature_bool_at, feature_number_at,
    scalar_trade_value,
};
#[cfg(test)]
pub(crate) use projection::insert_feature_window;
pub(crate) use projection::insert_projected_feature_window;
