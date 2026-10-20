"""Exception hierarchy. Every error carries a stable `code` used by the HTTP layer."""
from __future__ import annotations

from typing import Any

from .redact import scrub


class ModelSpendError(Exception):
    code = "modelspend_error"
    http_status = 500

    def __init__(self, message: str, **details: Any):
        message = scrub(message)
        super().__init__(message)
        self.message = message
        self.details = details


class InvalidRequest(ModelSpendError):
    code = "invalid_request"
    http_status = 400


class UnsupportedInput(InvalidRequest):
    code = "unsupported_input"


class ProviderAllowlistRequired(InvalidRequest):
    code = "provider_allowlist_required"


class ValidationRequired(InvalidRequest):
    code = "validation_required"


class BudgetTooLow(InvalidRequest):
    code = "budget_too_low"


class NoEligibleModel(ModelSpendError):
    code = "no_eligible_model"
    http_status = 422


class AllAttemptsFailed(ModelSpendError):
    code = "all_attempts_failed"
    http_status = 502


class StreamInterrupted(ModelSpendError):
    """Raised/emitted when a stream fails after output began. The model is never swapped."""

    code = "stream_interrupted"
    http_status = 502


class SandboxUnavailable(ModelSpendError):
    code = "sandbox_unavailable"


class ProviderError(Exception):
    """Normalized provider failure.

    kind: auth | quota | rate_limit | timeout | connection | server | bad_request |
          unsupported | unknown.  `retryable` kinds may be retried / failed over.
    `maybe_billed` is True when the provider may have processed (and charged for) the request.
    """

    RETRYABLE = {"rate_limit", "timeout", "connection", "server"}
    FAILOVER = RETRYABLE | {"auth", "quota"}  # another provider may succeed; "quota" (no credits) is never retried on the same one
    MAYBE_BILLED = {"timeout", "server", "unknown"}

    def __init__(self, provider: str, kind: str, message: str, status_code: int | None = None):
        message = scrub(message)  # provider errors can echo (part of) the API key
        super().__init__(f"[{provider}] {kind}: {message}")
        self.provider = provider
        self.kind = kind
        self.message = message
        self.status_code = status_code
        self.retry_after: float | None = None  # set by the classifier when the provider says how long to wait

    @property
    def retryable(self) -> bool:
        return self.kind in self.RETRYABLE

    @property
    def maybe_billed(self) -> bool:
        return self.kind in self.MAYBE_BILLED
