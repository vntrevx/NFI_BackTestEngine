//! Cross-module simulator contract tests.

use super::io::FILE_BACKED_READ_BUFFER_BYTES;
use super::protections::{DrawdownMode, ProtectionHandler, ProtectionProgram, ProtectionTiming};
use super::validation::valid_nfi_managed_long_route;
use super::*;

mod support;
use support::*;

mod callbacks;
mod contracts;
mod futures;
mod io;
mod nfi_adjustment;
mod nfi_routing;
mod position;
mod protections;
mod simulation;
mod task10_short_grind;
mod task14_callback_program;
mod task14_callback_runtime;
mod task14_production_callbacks;
mod task15_scheduler;
mod task16_execution;
mod task9_long_grind;
