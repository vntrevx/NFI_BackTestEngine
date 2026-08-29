"""Project-specific error types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class NfiBacktestError(Exception):
    """Base class for expected command failures."""


class SpecValidationError(NfiBacktestError):
    """A versioned fixture or trade surface does not satisfy its contract."""


class InputBoundaryError(SpecValidationError, ValueError):
    """An untrusted input exceeded a resource, archive, or path boundary."""


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


@dataclass(frozen=True, slots=True)
class PackagedRegistryCurrentRefError(SpecValidationError):
    """A packaged registry lacks a fresh exact current-ref proof."""

    code: str
    observation_method: str
    observation_status: str
    repository: str
    ref: str
    packaged_commit: str
    observed_commit: str | None

    @property
    def evidence(self) -> dict[str, Any]:
        """Return deterministic machine evidence for the failed promotion proof."""
        return {
            "schema_version": "packaged-semantic-registry-current-ref-proof-v1",
            "code": self.code,
            "observation_method": self.observation_method,
            "observation_status": self.observation_status,
            "repository": self.repository,
            "ref": self.ref,
            "packaged_commit": self.packaged_commit,
            "observed_commit": self.observed_commit,
            "native_promotion": False,
        }

    def __str__(self) -> str:
        """Render stable machine evidence at command boundaries."""
        import json

        return json.dumps(self.evidence, sort_keys=True, separators=(",", ":"))
