//! Validate a Python-compiled indicator program at the Rust boundary.

use std::error::Error;
use std::fs;
use std::io::{Error as IoError, ErrorKind};

use nfi_vector_core::program::IndicatorProgram;

fn main() -> Result<(), Box<dyn Error>> {
    let path = std::env::args_os().nth(1).ok_or_else(|| {
        IoError::new(
            ErrorKind::InvalidInput,
            "usage: validate_indicator_program <indicator-program.json>",
        )
    })?;
    let encoded = fs::read_to_string(path)?;
    let program = IndicatorProgram::from_json(&encoded)?;
    println!(
        "validated {}: {} functions, {} nodes, fingerprint {}",
        program.schema_version,
        program.functions.len(),
        program.nodes.len(),
        program.fingerprint
    );
    Ok(())
}
