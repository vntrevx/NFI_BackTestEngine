//! Embedded Binance isolated-Futures semantic extension.

/// Versioned Futures descriptor consumed by Python contract checks.
#[must_use]
pub const fn contract_json() -> &'static str {
    include_str!("futures_contract.json")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn embedded_futures_contract_is_valid_json() {
        let document: serde_json::Value =
            serde_json::from_str(contract_json()).expect("valid Futures contract");

        assert_eq!(document["schema_version"], "freqtrade-futures-contract-v1");
        assert_eq!(document["scope"]["margin_mode"], "isolated");
        assert_eq!(
            document["exit_collision"]["rejected_stop_does_not_fall_through_to_liquidation"],
            true
        );
    }
}
