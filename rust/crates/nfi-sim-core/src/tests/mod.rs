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
