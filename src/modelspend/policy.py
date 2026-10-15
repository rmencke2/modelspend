"""Routing policy: profiles, tier requirements and suitability assessment.

Everything here is data plus small pure functions, documented in docs/routing-policy.md.
No success probabilities are computed or assumed. Suitability is one of:
  measured  - enough evaluation results exist and their Wilson lower bound clears the profile bar
  prior     - editorial tier in the catalog meets the requirement (allowed only for some profiles)
  none      - not suitable
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from .catalog.models import EvidenceItem, Model, wilson_lower_bound
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


# ------------------------------------------------------------------------------------------------ custom evidence bars
BASES = ("lower_bound", "observed")
MAX_N = 100_000
MAX_RATE_LOWER_BOUND = 0.999


@lru_cache(maxsize=256)
def min_samples_for(rate: float) -> int:
    """Smallest number of cases for which even a PERFECT record has a Wilson 95% lower bound >= rate (so the bar can be cleared at all)."""
    for n in range(1, MAX_N + 1):
        if wilson_lower_bound(n, n) >= rate:
            return n
    raise ValueError(f"a lower-bound bar of {rate} needs more than {MAX_N} cases")


@dataclass(frozen=True)
class Threshold:
    """A user-defined evidence bar for one task category (or '*' for all). Any field may be left unset: it then falls back to '*', then to the profile."""

    min_n: int | None = None
    min_pass_rate: float | None = None
    basis: str | None = None  # lower_bound (default: 95% Wilson lower bound must clear the rate) | observed (passes / n must clear it)
    require_evidence: bool | None = None  # True: never choose a model for this category on its tier prior alone

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in (("min_n", self.min_n), ("min_pass_rate", self.min_pass_rate), ("basis", self.basis),
                                  ("require_evidence", self.require_evidence)) if v is not None}


def parse_threshold(category: str, raw: dict[str, Any]) -> tuple[str, Threshold]:
    cat = str(category).strip().lower()
    if cat != "*" and cat not in {c.value for c in TaskCategory if c != TaskCategory.unknown}:
        raise ValueError(f"unknown category '{category}'; use '*' or one of {', '.join(c.value for c in TaskCategory if c != TaskCategory.unknown)}")
    extra = set(raw) - {"min_n", "min_pass_rate", "basis", "require_evidence"}
    if extra:
        raise ValueError(f"unknown threshold field(s) for '{cat}': {', '.join(sorted(extra))}")
    n, rate, basis, req = raw.get("min_n"), raw.get("min_pass_rate"), raw.get("basis"), raw.get("require_evidence")
    if n is not None and (isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_N):
        raise ValueError(f"min_n for '{cat}' must be a whole number between 1 and {MAX_N}")
    if basis is not None and basis not in BASES:
        raise ValueError(f"basis for '{cat}' must be one of {', '.join(BASES)}")
    if rate is not None:
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0 < rate <= 1:
            raise ValueError(f"min_pass_rate for '{cat}' must be a number above 0 and at most 1 (for example 0.95)")
        if (basis or "lower_bound") == "lower_bound":
            if rate > MAX_RATE_LOWER_BOUND:
                raise ValueError(f"a lower-bound bar above {MAX_RATE_LOWER_BOUND} cannot be demonstrated with a realistic number of cases; use basis=observed")
            min_samples_for(float(rate))
    if req is not None and not isinstance(req, bool):
        raise ValueError(f"require_evidence for '{cat}' must be true or false")
    if n is None and rate is None and basis is None and req is None:
        raise ValueError(f"set at least one of min_n, min_pass_rate, basis, require_evidence for '{cat}'")
    return cat, Threshold(n, float(rate) if rate is not None else None, basis, req)


def parse_thresholds(raw: dict[str, Any] | None) -> dict[str, Threshold]:
    out: dict[str, Threshold] = {}
    for cat, spec in (raw or {}).items():
        if not isinstance(spec, dict):
            raise ValueError(f"thresholds['{cat}'] must be an object")
        c, t = parse_threshold(cat, spec)
        out[c] = t
    return out


@dataclass(frozen=True)
class Bar:
    """The evidence bar that applies to a (category, profile): what a model's measured results must show."""

    n: int
    rate: float
    basis: str
    require_evidence: bool
    source: str  # "profile" | "settings: <category>" | "settings: *" (the strictest source that set anything)

    def describe(self) -> str:
        what = "95% lower bound" if self.basis == "lower_bound" else "observed pass rate"
        return f"n >= {self.n} and {what} >= {self.rate:g}" + (" (evidence required, no tier priors)" if self.require_evidence else "")


def bar_for(pol: ProfilePolicy, cat: TaskCategory, thresholds: dict[str, Threshold] | None) -> Bar:
    """Resolve field by field: the category's own setting, else '*', else the profile's built-in bar."""
    ts = [t for t in ((thresholds or {}).get(cat.value), (thresholds or {}).get("*")) if t is not None]

    def pick(field: str, default: Any) -> tuple[Any, str | None]:
        for key, t in zip((cat.value, "*"), ((thresholds or {}).get(cat.value), (thresholds or {}).get("*"))):
            if t is not None and getattr(t, field) is not None:
                return getattr(t, field), key
        return default, None
    n, s1 = pick("min_n", pol.min_n)
    rate, s2 = pick("min_pass_rate", pol.min_lower_bound)
    basis, s3 = pick("basis", "lower_bound")
    user_req, s4 = pick("require_evidence", None)
    req = (not pol.allow_prior) or bool(user_req)  # a setting can only TIGHTEN this: high_assurance always requires evidence
    src = sorted({x for x in (s1, s2, s3, s4) if x}, key=lambda k: (k == "*", k))
    return Bar(n, rate, basis, bool(req), "profile" if not ts or not src else "settings: " + ", ".join(src))


def judge_evidence(ev: EvidenceItem | None, bar: Bar) -> tuple[str, str, float | None]:
    """(verdict, detail, lower bound). verdict: none | needs_more_cases | bar_unreachable | clears | below_bar."""
    if ev is None or ev.n <= 0:
        return "none", f"evidence insufficient (n=0, need {bar.n})", None
    lb = wilson_lower_bound(ev.passes, ev.n)
    if ev.n < bar.n:
        return "needs_more_cases", f"evidence insufficient (n={ev.n}, need {bar.n})", lb
    if bar.basis == "lower_bound":
        perfect = wilson_lower_bound(ev.n, ev.n)
        if perfect < bar.rate:
            need = min_samples_for(bar.rate)
            return ("bar_unreachable",
                    f"n={ev.n} cannot demonstrate a lower bound of {bar.rate:g} even if every case passes (a perfect record gives {perfect:.2f}); needs at least {need} cases",
                    lb)
        if lb >= bar.rate:
            return "clears", f"{ev.passes}/{ev.n} passed on '{ev.dataset}' (95% lower bound {lb:.2f} >= {bar.rate:g})", lb
        return "below_bar", f"measured {ev.passes}/{ev.n} on '{ev.dataset}' (lower bound {lb:.2f} < {bar.rate:g})", lb
    obs = ev.passes / ev.n
    if obs >= bar.rate:
        return "clears", f"{ev.passes}/{ev.n} passed on '{ev.dataset}' (observed {obs:.2f} >= {bar.rate:g}; 95% lower bound {lb:.2f})", lb
    return "below_bar", f"measured {ev.passes}/{ev.n} on '{ev.dataset}' (observed {obs:.2f} < {bar.rate:g})", lb


@dataclass
class Suitability:
    suitable: bool
    basis: str  # measured | prior | none
    detail: str
    lower_bound: float | None = None


def assess(model: Model, cat: TaskCategory, profile: Profile, hard: bool, thresholds: dict[str, Threshold] | None = None) -> Suitability:
    pol = PROFILES[profile]
    need = required_tier(cat, profile, hard)
    bar = bar_for(pol, cat, thresholds)
    ev = model.evidence.get(cat.value)
    verdict, detail, lb = judge_evidence(ev, bar)
    note = f" [bar from {bar.source}]" if bar.source != "profile" else ""
    if verdict == "clears":
        return Suitability(True, "measured", detail + note, lb)
    if verdict == "below_bar":
        return Suitability(False, "measured", detail + note, lb)
    have = detail + note  # insufficient evidence: fall back to the tier prior only if both the profile and the bar allow it
    if bar.require_evidence:
        who = pol.name.value if not pol.allow_prior else "your settings"
        return Suitability(False, "none", f"{who} require measured evidence; {have}")
    if model.tier is None:
        return Suitability(False, "none", f"no tier prior and {have}")
    if model.tier >= need:
        return Suitability(True, "prior", f"editorial tier {model.tier} >= required {need}; {have}")
    return Suitability(False, "none", f"editorial tier {model.tier} < required {need}; {have}")
