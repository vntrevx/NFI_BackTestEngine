"""Compile and execute the source-ordered Native signal contract."""

from .compiler import (
    SIGNAL_PROGRAM_VERSION,
    SignalProgramCompileError,
    compile_signal_program,
)
from .runtime import SignalProgramExecutionError, execute_signal_program
from .validation import validate_signal_program

__all__ = [
    "SIGNAL_PROGRAM_VERSION",
    "SignalProgramCompileError",
    "SignalProgramExecutionError",
    "compile_signal_program",
    "execute_signal_program",
    "validate_signal_program",
]
