"""Project-specific error types."""


class NfiBacktestError(Exception):
    """Base class for expected command failures."""


class SpecValidationError(NfiBacktestError):
    """A versioned fixture or trade surface does not satisfy its contract."""


class NormalizationError(NfiBacktestError):
    """A Freqtrade export cannot be normalized without guessing."""


class BenchmarkError(NfiBacktestError):
    """A benchmark could not be measured reproducibly."""


class BranchCoverageError(BenchmarkError):
    """A bounded fixture completed but did not reach its required branch."""


class DiscoveryInfrastructureError(BenchmarkError):
    """A discovery dependency failed before strategy semantics could be assessed."""

    def __init__(
        self,
        message: str,
        *,
        external_http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.external_http_status = external_http_status


class TraceError(NfiBacktestError):
    """A canonical state trace is malformed or cannot be compared."""


class StrategyAnalysisError(NfiBacktestError):
    """A strategy cannot enter the compiled engine without approximation."""
