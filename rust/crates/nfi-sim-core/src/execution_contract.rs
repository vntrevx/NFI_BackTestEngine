//! Embedded version of the exact Spot order and wallet contract.

/// Versioned execution descriptor consumed by Python contract checks.
#[must_use]
pub const fn contract_json() -> &'static str {
    include_str!("execution_contract.json")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn embedded_execution_contract_is_valid_json() {
        let document: serde_json::Value =
            serde_json::from_str(contract_json()).expect("valid execution contract");

        assert_eq!(
            document["schema_version"],
            "freqtrade-execution-contract-v1"
        );
        assert_eq!(
            document["wallet"]["mutation_order"],
            "serial-scheduler-event-order"
        );
        assert_eq!(
            document["precision"]["weighted_basis_division"],
            "ccxt-precise-truncate-18-decimals"
        );
    }
}
