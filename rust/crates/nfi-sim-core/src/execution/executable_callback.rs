//! Bounded run-scoped executable callback interpreter.

#[path = "executable_callback/machine.rs"]
mod machine;
#[path = "executable_callback/types.rs"]
mod types;
#[path = "executable_callback/value.rs"]
mod value;

pub use machine::CallbackProgramRuntime;
pub use types::*;
