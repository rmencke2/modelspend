"""Routing policy: profiles, tier requirements and suitability assessment.

Everything here is data plus small pure functions, documented in docs/routing-policy.md.
No success probabilities are computed or assumed. Suitability is one of:
  measured  - enough evaluation results exist and their Wilson lower bound clears the profile bar
  prior     - editorial tier in the catalog meets the requirement (allowed only for some profiles)
  none      - not suitable
"""
from __future__ import annotations

from dataclasses import dataclass

from .catalog.models import Model, wilson_lower_bound
from .types import Profile, TaskCategory

POLICY_VERSION = "2026.10-1"


@dataclass(frozen=True)
class ProfilePolicy:
    name: Profile
    description: str
    tier_delta: int  # added to the category's base tier requirement
    allow_prior: bool  # may a model be chosen on its editorial tier alone?
    min_n: int  # evaluation samples needed for measured evidence to count
    min_lower_bound: float  # Wilson 95% lower bound the measured pass rate must clear
    escalation_default: int
    require_validation: bool


PROFILES: dict[Profile, ProfilePolicy] = {
    Profile.economy: ProfilePolicy(
        Profile.economy,
        "Cheapest model whose tier prior (or modest measured evidence) fits routine work. No quality escalation by default.",
        tier_delta=-1, allow_prior=True, min_n=10, min_lower_bound=0.65, escalation_default=0, require_validation=False,
    ),
    Profile.balanced: ProfilePolicy(
        Profile.balanced,
        "Cheapest model meeting the category's base tier, or measured evidence with lower bound >= 0.80 over >= 20 samples. At most one escalation on validator-evidenced failure.",
        tier_delta=0, allow_prior=True, min_n=20, min_lower_bound=0.80, escalation_default=1, require_validation=False,
    ),
    Profile.high_assurance: ProfilePolicy(
        Profile.high_assurance,
        "Requires measured evidence (>= 40 samples, lower bound >= 0.90) for the category AND configured validation. Tier priors are not accepted.",
        tier_delta=1, allow_prior=False, min_n=40, min_lower_bound=0.90, escalation_default=1, require_validation=True,
    ),
}

# Base capability tier each category needs (1 small/fast, 2 mid, 3 frontier). Editorial priors.
BASE_TIER: dict[TaskCategory, int] = {
    TaskCategory.classification: 1,
    TaskCategory.extraction: 1,
    TaskCategory.rewriting: 1,
    TaskCategory.summarization: 1,
    TaskCategory.translation: 1,
    TaskCategory.qa_context: 1,
    TaskCategory.code: 2,
    TaskCategory.analysis: 3,
    TaskCategory.unknown: 2,  # conservative fallback when the task cannot be categorized
}

# Output-length assumptions (tokens) used for *expected* cost. Worst case always uses the output cap.
EXPECTED_OUTPUT_TOKENS: dict[TaskCategory, int] = {
    TaskCategory.classification: 16,
    TaskCategory.extraction: 200,
    TaskCategory.rewriting: 0,  # scales with input, see routing.expected_output
    TaskCategory.summarization: 250,
    TaskCategory.translation: 0,  # scales with input
    TaskCategory.qa_context: 200,
    TaskCategory.code: 700,
    TaskCategory.analysis: 1000,
    TaskCategory.unknown: 400,
}


def required_tier(cat: TaskCategory, profile: Profile, hard: bool) -> int:
    t = BASE_TIER[cat] + PROFILES[profile].tier_delta + (1 if hard else 0)
    return max(1, min(3, t))


@dataclass
class Suitability:
    suitable: bool
    basis: str  # measured | prior | none
    detail: str
    lower_bound: float | None = None


def assess(model: Model, cat: TaskCategory, profile: Profile, hard: bool) -> Suitability:
    pol = PROFILES[profile]
    need = required_tier(cat, profile, hard)
    ev = model.evidence.get(cat.value)
    if ev is not None and ev.n >= pol.min_n:
        lb = wilson_lower_bound(ev.passes, ev.n)
        if lb >= pol.min_lower_bound:
            return Suitability(True, "measured", f"{ev.passes}/{ev.n} passed on '{ev.dataset}' (95% lower bound {lb:.2f} >= {pol.min_lower_bound})", lb)
        return Suitability(False, "measured", f"measured {ev.passes}/{ev.n} on '{ev.dataset}' (lower bound {lb:.2f} < {pol.min_lower_bound})", lb)
    have = f"evidence insufficient (n={ev.n if ev else 0}, need {pol.min_n})"
    if not pol.allow_prior:
        return Suitability(False, "none", f"{pol.name.value} requires measured evidence; {have}")
    if model.tier is None:
        return Suitability(False, "none", f"no tier prior and {have}")
    if model.tier >= need:
        return Suitability(True, "prior", f"editorial tier {model.tier} >= required {need}; {have}")
    return Suitability(False, "none", f"editorial tier {model.tier} < required {need}; {have}")
