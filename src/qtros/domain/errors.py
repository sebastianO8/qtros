"""Exception hierarchy for the Quantitative Trading Research OS.

Every exception raised deliberately by this system descends from :class:`QtrosError`.
A single root means a caller can distinguish "the system detected a problem and said
so" from "the system hit an unhandled bug", which is the distinction that matters
when a multi-year backtest aborts at minute forty.

Design rules:
    * Exceptions carry structured context, never only a formatted string. The context
      dictionary is emitted verbatim into the structured log, so a failure is
      queryable after the fact rather than only readable.
    * No exception in this module is raised for control flow. Every one of them means
      a human must look at something.
"""

from __future__ import annotations

from typing import Any


class QtrosError(Exception):
    """Root of every deliberate exception in the system.

    Args:
        message: Human-readable description of what went wrong.
        **context: Structured key/value detail attached to the error and forwarded
            to the structured logger. Values should be JSON-serialisable.
    """

    def __init__(self, message: str, **context: Any) -> None:  # noqa: ANN401
        super().__init__(message)
        self.message: str = message
        self.context: dict[str, Any] = dict(context)

    def __str__(self) -> str:
        """Return the message with any structured context appended."""
        if not self.context:
            return self.message
        rendered: str = " ".join(f"{key}={value!r}" for key, value in sorted(self.context.items()))
        return f"{self.message} [{rendered}]"


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


class ConfigurationError(QtrosError):
    """Configuration could not be loaded, resolved, or validated."""


# --------------------------------------------------------------------------------------
# Layer 0 — data
# --------------------------------------------------------------------------------------


class DataError(QtrosError):
    """Base class for all Layer 0 failures."""


class WarehouseError(DataError):
    """The DuckDB warehouse could not be opened, migrated, or written."""


class SourceValidationError(DataError):
    """A raw data source failed validation and was not ingested.

    Raised by source registration for: paths outside the declared input root, paths
    resolving inside an output directory (the Cycle 1 self-ingestion guard), missing
    files, header/schema mismatches, and undeclared timezone offsets.
    """


class IngestionError(DataError):
    """A registered source failed during parsing or loading."""


class TimezoneError(DataError):
    """A timestamp could not be resolved to an unambiguous UTC instant.

    Raised for nonexistent local times (DST spring-forward) and ambiguous local times
    (DST fall-back) when the source declares :attr:`DstPolicy.STRICT_RAISE`.
    """


class CoverageError(DataError):
    """Requested data exists but does not meet the caller's coverage requirements.

    Distinct from :class:`IngestionError`: the data loaded correctly, it is simply
    not good enough to backtest on.
    """


class SchemaContractError(DataError):
    """A frame violated the MarketFrame schema contract.

    Naive timestamps, non-monotonic ordering, duplicate timestamps, impossible OHLC
    relationships, or missing required columns. These are corruption, not gaps: the
    system rejects them rather than repairing them.
    """


class FingerprintError(DataError):
    """A dataset fingerprint could not be computed deterministically."""


# --------------------------------------------------------------------------------------
# Layer 0.5 — features
# --------------------------------------------------------------------------------------


class FeatureError(QtrosError):
    """Base class for all Layer 0.5 failures."""


class LookaheadDetected(FeatureError):
    """A feature's value at bar *t* changed when data after *t* was removed.

    This is Directive 1 failing, detected mechanically rather than by review. It is
    always fatal: a frame carrying look-ahead bias must never reach a strategy, however
    convenient that would be in the middle of a debugging session.
    """


class DetectorVerificationError(FeatureError):
    """The lag audit failed to detect a known non-causal control.

    Means the detector itself is broken, which invalidates every pass result from the
    same run — not just the control's.
    """