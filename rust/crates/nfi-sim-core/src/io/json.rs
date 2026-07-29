//! Canonical JSON parsing and result serialization.

use crate::domain::{SimulationInput, SimulationResult};

/// Parse one simulator document for both native frontends.
///
/// The IR compiler flattens Python `elif` chains, so the normal JSON recursion
/// limit remains an input-safety boundary. Keeping this parser in the core
/// prevents the CLI and Python extension from drifting to different
/// acceptance rules.
///
/// # Errors
///
/// Returns the original JSON/type error, including trailing input.
pub fn parse_simulation_input(encoded: &[u8]) -> Result<SimulationInput, serde_json::Error> {
    serde_json::from_slice(encoded)
}

/// Serialize one simulator result using the canonical compact JSON surface.
///
/// # Errors
///
/// Returns the serializer error if a result cannot be represented as JSON.
pub fn serialize_simulation_result(
    result: &SimulationResult,
) -> Result<Vec<u8>, serde_json::Error> {
    serde_json::to_vec(result)
}
