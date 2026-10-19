"""Catalog schema. Unknown values are None, never guessed."""
from __future__ import annotations

import math
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .. import registry


class PriceTier(BaseModel):
    """Whole-request price override when the prompt exceeds `prompt_tokens_over`."""

    prompt_tokens_over: int
    input_per_mtok: float
    output_per_mtok: float
    cached_input_per_mtok: float | None = None
    cache_write_per_mtok: float | None = None


class Pricing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    currency: str = "USD"
    input_per_mtok: float | None = None
    output_per_mtok: float | None = None
    cached_input_per_mtok: float | None = None
    cache_write_per_mtok: float | None = None
    effective_date: date | None = None
    verified_on: date | None = None
    source_url: str | None = None
    # True: provider-reported output tokens already include reasoning tokens (do not add twice).
    # False: reasoning tokens are reported separately and billed as output (adapter sums them).
    reasoning_in_output: bool = True
    long_context_tiers: list[PriceTier] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)  # free-text billing conditions
    additional_charges: list[str] = Field(default_factory=list)  # e.g. per-call tool fees; not used by ModelSpend
    origin: Literal["catalog", "user_override"] = "catalog"

    @property
    def known(self) -> bool:
        return self.input_per_mtok is not None and self.output_per_mtok is not None


class Limits(BaseModel):
    context_tokens: int | None = None
    max_output_tokens: int | None = None


class Capabilities(BaseModel):
    """True/False when verified, None when unknown. Unknown never satisfies a requirement."""

    text: bool | None = True
    streaming: bool | None = None
    structured_output: bool | None = None
    tool_calling: bool | None = None
    temperature: bool | None = None  # accepts a temperature parameter
    reasoning: Literal["none", "optional", "always_on"] | None = None
    input_modalities: list[str] = Field(default_factory=lambda: ["text"])


class EvidenceItem(BaseModel):
    """Measured pass counts for one task category on a named dataset."""

    n: int
    passes: int
    dataset: str
    measured_on: date | None = None
    source: str = "modelspend eval"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    id: str
    display_name: str | None = None
    availability: Literal["available", "preview", "deprecated", "unavailable", "unverified"] = "unverified"
    verified_on: date | None = None
    availability_source_url: str | None = None
    limits: Limits = Field(default_factory=Limits)
    capabilities: Capabilities = Field(default_factory=Capabilities)
    pricing: Pricing | None = None
    # Editorial capability tier: 1 small/fast, 2 mid, 3 frontier. A *prior*, not a measurement.
    tier: Literal[1, 2, 3] | None = None
    tier_note: str | None = None
    evidence: dict[str, EvidenceItem] = Field(default_factory=dict)
    synthetic: bool = False  # True only for test/simulation fixtures
    notes: str | None = None

    @property
    def ref(self) -> str:
        return f"{self.provider}/{self.id}"


def wilson_lower_bound(passes: int, n: int, z: float = 1.96) -> float:
    if n <= 0:
        return 0.0
    p = passes / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / d)


class CatalogFile(BaseModel):
    catalog_version: str
    description: str = ""
    models: list[Model] = Field(default_factory=list)

    def validate_providers(self) -> None:
        for m in self.models:
            if m.provider not in registry.all_providers():
                raise ValueError(f"{m.ref}: unknown provider")
