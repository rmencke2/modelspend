"""Public request/response types shared by the SDK, CLI and server."""
from __future__ import annotations

import json
from enum import Enum
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import UnsupportedInput

PROVIDERS = ("openai", "anthropic", "google", "xai")
_ALIASES = {"gemini": "google", "claude": "anthropic", "grok": "xai"}


def normalize_provider(name: str) -> str:
    n = name.strip().lower()
    n = _ALIASES.get(n, n)
    if n not in PROVIDERS:
        raise ValueError(f"unknown provider '{name}'; supported: {', '.join(PROVIDERS)}")
    return n


class TaskCategory(str, Enum):
    classification = "classification"
    extraction = "extraction"
    rewriting = "rewriting"
    summarization = "summarization"
    translation = "translation"
    qa_context = "qa_context"
    code = "code"
    analysis = "analysis"
    unknown = "unknown"


class Profile(str, Enum):
    economy = "economy"
    balanced = "balanced"
    high_assurance = "high_assurance"


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: str  # JSON string, as in the OpenAI format


class ToolSpec(BaseModel):
    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    @field_validator("content", mode="before")
    @classmethod
    def _flatten(cls, v: Any) -> Any:
        if v is None or isinstance(v, str):
            return v
        if isinstance(v, list):
            out = []
            for part in v:
                if isinstance(part, dict) and part.get("type") in ("text", "input_text") and "text" in part:
                    out.append(part["text"])
                else:
                    ptype = part.get("type") if isinstance(part, dict) else type(part).__name__
                    raise UnsupportedInput(
                        f"content part of type '{ptype}' is not supported; this release handles text only"
                    )
            return "".join(out)
        raise UnsupportedInput(f"unsupported message content type: {type(v).__name__}")


class ValidatorSpec(BaseModel):
    """Declarative deterministic validator. See validation.py for the supported types."""

    model_config = ConfigDict(extra="allow")
    type: str
    name: str | None = None


class Usage(BaseModel):
    """Normalized token usage.

    input_tokens        uncached input, billed at the input rate
    cache_read_tokens   input served from cache, billed at the cached-input rate
    cache_write_tokens  input written to cache, billed at the cache-write rate
    output_tokens       TOTAL billable output, *including* reasoning tokens
    reasoning_tokens    memo only: the reasoning portion already inside output_tokens
    """

    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def __add__(self, o: "Usage") -> "Usage":
        return Usage(**{k: getattr(self, k) + getattr(o, k) for k in type(self).model_fields})


class RouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    messages: list[Message]
    task_type: TaskCategory | None = None
    hard: bool = False  # caller hint: this task needs stronger reasoning
    acceptance_criteria: list[str] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)
    profile: Profile = Profile.balanced
    max_budget_usd: float | None = Field(default=None, gt=0)
    max_budget_tokens: int | None = Field(default=None, gt=0)  # cap on total tokens (input + output) across ALL attempts
    allow_unpriced: bool = False  # permit models with no catalog pricing; cost is then reported as unknown
    latency_target_ms: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0)
    response_schema: dict[str, Any] | None = None
    validators: list[ValidatorSpec] = Field(default_factory=list)
    callable_validators: list[Callable[[str], Any]] = Field(default_factory=list, exclude=True)
    judge: bool = False  # optional model-based judge; costs money, see docs
    escalation_limit: int | None = Field(default=None, ge=0)
    sensitive: bool = False  # restrict to providers the operator approved for sensitive data
    allow_models: list[str] = Field(default_factory=list)  # "provider/model-id"
    deny_models: list[str] = Field(default_factory=list)
    tools: list[ToolSpec] = Field(default_factory=list)
    tool_choice: str | None = None
    provider_options: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("providers")
    @classmethod
    def _providers(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for p in v:
            n = normalize_provider(p)
            if n not in out:
                out.append(n)
        return out

    @field_validator("messages")
    @classmethod
    def _messages(cls, v: list[Message]) -> list[Message]:
        if not v:
            raise ValueError("messages must not be empty")
        if not any(m.role == "user" for m in v) and not any(m.role == "tool" for m in v):
            raise ValueError("messages must contain at least one user message")
        return v

    @model_validator(mode="after")
    def _opts(self) -> "RouteRequest":
        for p in self.provider_options:
            if normalize_provider(p) not in self.providers:
                raise ValueError(f"provider_options['{p}'] given for a provider that is not in the allowlist")
        return self

    def has_validation(self) -> bool:
        return bool(self.validators or self.callable_validators or self.response_schema or self.judge)


class AttemptRecord(BaseModel):
    index: int
    kind: Literal["generation", "classification", "judge"] = "generation"
    purpose: Literal["initial", "retry", "failover", "escalation", "routing", "validation"] = "initial"
    provider: str
    model: str
    outcome: str  # ok | operational_error | refused | skipped_budget
    error_kind: str | None = None
    usage: Usage = Field(default_factory=Usage)
    estimated_cost_usd: float | None = None
    cost_usd: float | None = 0.0  # None = model has no pricing, cost unknown (tokens are still exact)
    cost_basis: Literal["reported_usage", "uncertain_upper_estimate", "none", "unpriced"] = "reported_usage"
    latency_ms: int = 0
    note: str | None = None


class ValidationResult(BaseModel):
    name: str
    kind: Literal["deterministic", "callable", "judge"] = "deterministic"
    passed: bool
    detail: str = ""


class ValidationReport(BaseModel):
    # passed | failed | not_configured | unverified_criteria | skipped
    status: str
    results: list[ValidationResult] = Field(default_factory=list)
    note: str | None = None


class Candidate(BaseModel):
    provider: str
    model: str
    tier: int | None
    suitability_basis: Literal["measured", "prior", "none"]
    suitability_detail: str
    evidence_lower_bound: float | None = None
    input_tokens_est: int
    expected_output_tokens: int
    worst_case_output_tokens: int
    expected_total_tokens: int = 0
    worst_case_total_tokens: int = 0
    expected_cost_usd: float | None = None  # None when the model has no pricing
    worst_case_cost_usd: float | None = None
    pricing_warnings: list[str] = Field(default_factory=list)
    rank: int | None = None


class Exclusion(BaseModel):
    provider: str
    model: str
    stage: str
    reason: str


class TaskInfo(BaseModel):
    category: TaskCategory
    basis: Literal["hint", "rules", "cache", "model", "fallback"]
    confidence: Literal["high", "low"]
    required_tier: int
    note: str = ""


class Plan(BaseModel):
    request_id: str
    task: TaskInfo
    profile: Profile
    candidates: list[Candidate]  # suitable candidates, ranked cheapest first
    unsuitable: list[Candidate] = Field(default_factory=list)  # eligible but lacking suitability evidence
    excluded: list[Exclusion] = Field(default_factory=list)
    input_tokens: int
    token_method: str
    escalation_limit: int
    ranking_basis: str = "expected_cost"  # expected_cost | tier_then_tokens (used whenever any candidate is unpriced)
    assumptions: list[str] = Field(default_factory=list)
    uncertainty: list[str] = Field(default_factory=list)
    catalog_version: str
    policy_version: str
    explanation: str = ""


class RouteResult(BaseModel):
    request_id: str
    status: Literal["ok", "validation_failed", "refused", "truncated"]
    text: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    provider: str
    model: str
    task: TaskInfo
    profile: Profile
    explanation: str
    estimated_cost_usd: float | None  # pre-call estimate for the first attempt (None if unpriced)
    actual_cost_usd: float | None  # from provider-reported usage across ALL attempts; None if any attempt was unpriced
    uncertain_cost_usd: float = 0.0  # upper-bound charge for failed calls whose billing is unknown
    cost_note: str = (
        "actual_cost_usd is computed from provider-reported token usage and catalog prices; "
        "your provider invoice is authoritative."
    )
    usage: Usage  # token usage summed over ALL attempts (exact, as reported by the providers)
    total_tokens: int = 0
    budget_tokens: int | None = None
    budget_tokens_remaining: int | None = None
    attempts: list[AttemptRecord]
    retries: int = 0
    failovers: int = 0
    escalations: int = 0
    validation: ValidationReport
    budget_usd: float | None = None
    budget_remaining_usd: float | None = None
    uncertainty: list[str] = Field(default_factory=list)
    catalog_version: str
    policy_version: str
    latency_ms: int = 0

    def json_metadata(self) -> dict[str, Any]:
        return json.loads(self.model_dump_json(exclude={"text"}))


class StreamEvent(BaseModel):
    type: Literal["route", "delta", "done", "error"]
    text: str | None = None
    plan: Plan | None = None
    result: RouteResult | None = None
    error: dict[str, Any] | None = None
