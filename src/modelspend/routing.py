"""Planning: hard constraints -> task category -> cost estimates -> suitability -> ranking.

`Router.plan` performs no network calls and no generation; it backs both route-preview and
execution. Request text is treated as task data: nothing in message content can alter the
provider allowlist, budget, profile or constraints (those come only from typed request fields).
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Callable

from .catalog import Catalog, pricing_age_days
from .catalog.models import Model
from .config import Config
from .errors import (
    BudgetTooLow, InvalidRequest, NoEligibleModel, ProviderAllowlistRequired, ValidationRequired,
)
from .policy import EXPECTED_OUTPUT_TOKENS, POLICY_VERSION, PROFILES, Suitability, assess, required_tier
from .pricing import cost_for, estimate_cost
from .tasks import Classification, classify_rules
from .tokens import estimate_input_tokens, reservation_input_tokens
from .types import (
    Candidate, Exclusion, Message, Plan, RouteRequest, TaskCategory, TaskInfo, Usage,
)

# Provider-option allowlists: only these namespaced keys may be forwarded, per provider.
ALLOWED_PROVIDER_OPTIONS: dict[str, set[str]] = {
    "openai": {"reasoning_effort", "seed", "top_p", "frequency_penalty", "presence_penalty", "stop", "user"},
    "xai": {"reasoning_effort", "seed", "top_p", "frequency_penalty", "presence_penalty", "stop", "user"},
    "anthropic": {"top_p", "top_k", "stop_sequences"},
    "google": {"top_p", "top_k", "seed", "stop_sequences", "presence_penalty", "frequency_penalty"},
}


def validate_provider_options(req: RouteRequest) -> None:
    for prov, opts in req.provider_options.items():
        bad = set(opts) - ALLOWED_PROVIDER_OPTIONS[prov]
        if bad:
            raise InvalidRequest(
                f"provider_options['{prov}'] has unsupported key(s) {sorted(bad)}; "
                f"allowed: {sorted(ALLOWED_PROVIDER_OPTIONS[prov])}"
            )


def expected_output(cat: TaskCategory, last_user_tokens: int, cap: int) -> int:
    base = EXPECTED_OUTPUT_TOKENS[cat]
    if cat in (TaskCategory.rewriting, TaskCategory.translation):
        base = max(64, int(last_user_tokens * 1.1))
    return max(1, min(base, cap))


class Router:
    def __init__(
        self,
        catalog: Catalog,
        config: Config,
        available_providers: set[str] | None = None,
        latency_lookup: Callable[[str, str], tuple[float, int] | None] | None = None,
        today: Callable[[], date] = date.today,
    ):
        self.catalog = catalog
        self.cfg = config
        self.available = available_providers if available_providers is not None else set(config.api_keys)
        self.latency_lookup = latency_lookup
        self.today = today

    # ------------------------------------------------------------------------------------
    def resolve_providers(self, req: RouteRequest) -> list[str]:
        provs = req.providers or list(self.cfg.default_providers)
        if not provs:
            raise ProviderAllowlistRequired(
                "an explicit provider allowlist is required (request.providers or MODELSPEND_PROVIDERS)"
            )
        return provs

    def plan(
        self,
        req: RouteRequest,
        *,
        classification: Classification | None = None,
        request_id: str | None = None,
        require_streaming: bool = False,
        budget_remaining: float | None = None,
        tokens_remaining: int | None = None,
    ) -> Plan:
        providers = self.resolve_providers(req)
        validate_provider_options(req)
        pol = PROFILES[req.profile]
        if pol.require_validation and not req.has_validation() and not req.pin:
            raise ValidationRequired(
                "the high_assurance profile requires configured validation "
                "(response_schema, validators, callable validators or judge=true)"
            )
        if req.tools and req.response_schema:
            raise InvalidRequest("tools and response_schema cannot be combined in this release")

        in_tok, method = estimate_input_tokens(req.messages, req.response_schema, req.tools)
        reserve_in = reservation_input_tokens(in_tok, method)
        last_user = next((m.content or "" for m in reversed(req.messages) if m.role == "user"), "")
        last_user_tok = estimate_input_tokens([Message(role="user", content=last_user)])[0]
        cap = req.max_output_tokens or self.cfg.default_max_output_tokens
        budget = req.max_budget_usd if budget_remaining is None else budget_remaining
        tok_budget = req.max_budget_tokens if tokens_remaining is None else tokens_remaining
        unpriced_ok = req.allow_unpriced or self.cfg.allow_unpriced
        sens_ok = set(self.cfg.sensitive_providers)
        allow = req.allow_models or self.cfg.allow_models  # request-level list wins over the user's default
        deny = set(req.deny_models) | set(self.cfg.deny_models)

        # ---- 2. hard constraints -------------------------------------------------------
        excluded: list[Exclusion] = []
        eligible: list[Model] = []

        def drop(m: Model, stage: str, reason: str) -> None:
            excluded.append(Exclusion(provider=m.provider, model=m.id, stage=stage, reason=reason))

        models = self.catalog.for_providers(providers)
        if req.pin:
            models = [m for m in models if m.ref == req.pin]
            if not models:
                raise NoEligibleModel(
                    f"pinned model '{req.pin}' is not in the catalog for the allowed providers ({', '.join(providers)}). "
                    "Add it with `modelspend catalog sync --add-new --only <id> --exact --write` (it may stay unpriced: tokens are still exact).")
        for m in models:
            if m.provider not in self.available:
                drop(m, "provider", "no API key configured for this provider")
            elif req.sensitive and m.provider not in sens_ok:
                drop(m, "privacy", "request is marked sensitive and provider is not approved in MODELSPEND_SENSITIVE_PROVIDERS")
            elif req.pin and m.ref in deny:
                drop(m, "policy", "disabled (deny_models: request or your settings)")
            elif req.pin:
                eligible.append(m)  # observe-only: the caller chose this model explicitly, so no suitability/price/limit gating
            elif allow and m.ref not in allow:
                drop(m, "policy", "not in allow_models")
            elif m.ref in deny:
                drop(m, "policy", "disabled (deny_models: request or your settings)")
            elif m.availability not in ("available",) and not (m.availability == "preview" and self.cfg.allow_preview_models):
                drop(m, "availability", f"availability is '{m.availability}'")
            elif (m.pricing is None or not m.pricing.known) and not unpriced_ok:
                drop(m, "pricing", "pricing unknown; excluded from automatic cost optimization (allow_unpriced=true to run it anyway, with cost reported as unknown)")
            elif (m.pricing is None or not m.pricing.known) and budget is not None:
                drop(m, "budget", "a USD budget needs known pricing; use max_budget_tokens for unpriced models")
            elif m.pricing is not None and m.pricing.known and m.pricing.currency != "USD":
                drop(m, "pricing", f"price currency {m.pricing.currency} is not USD")
            elif self.cfg.exclude_stale_pricing and m.pricing is not None and m.pricing.known and self._stale(m):
                drop(m, "pricing", "pricing is stale and MODELSPEND_EXCLUDE_STALE_PRICING is set")
            elif m.limits.context_tokens is None or m.limits.max_output_tokens is None:
                drop(m, "limits", "context/output limits unknown")
            elif m.capabilities.input_modalities != ["text"] and "text" not in m.capabilities.input_modalities:
                drop(m, "capability", "no text input support")
            elif req.response_schema and m.capabilities.structured_output is not True:
                drop(m, "capability", "structured output not verified for this model")
            elif req.tools and m.capabilities.tool_calling is not True:
                drop(m, "capability", "tool calling not verified for this model")
            elif require_streaming and m.capabilities.streaming is not True:
                drop(m, "capability", "streaming not verified for this model")
            elif req.temperature is not None and m.capabilities.temperature is not True:
                drop(m, "capability", "temperature parameter not verified as supported")
            elif req.max_output_tokens and req.max_output_tokens > m.limits.max_output_tokens:
                drop(m, "limits", f"requested output {req.max_output_tokens} exceeds model max {m.limits.max_output_tokens}")
            elif reserve_in + cap > m.limits.context_tokens:
                drop(m, "context", f"needs ~{reserve_in + cap} tokens, context is {m.limits.context_tokens}")
            else:
                eligible.append(m)

        # ---- 3. task category ----------------------------------------------------------
        if req.task_type is not None and req.task_type != TaskCategory.unknown:
            cl = Classification(req.task_type, "hint", "high")
        else:
            cl = classification or classify_rules(req.messages)
        need = required_tier(cl.category, req.profile, req.hard)
        task = TaskInfo(category=cl.category, basis=cl.basis, confidence=cl.confidence,  # type: ignore[arg-type]
                        required_tier=need, note=cl.note)

        # ---- 4/5. cost estimate, budget, suitability, ranking --------------------------
        suitable: list[Candidate] = []
        unsuitable: list[Candidate] = []
        for m in eligible:
            out_cap = min(cap, m.limits.max_output_tokens or cap)
            exp_out = expected_output(cl.category, last_user_tok, out_cap)
            if m.capabilities.reasoning == "always_on":
                exp_out = min(out_cap, exp_out + self.cfg.reasoning_overhead_tokens)
            priced = m.pricing is not None and m.pricing.known
            worst = cost_for(m, Usage(input_tokens=reserve_in, output_tokens=out_cap)) if priced else None
            expct = estimate_cost(m, in_tok, exp_out) if priced else None
            worst_tok, exp_tok = reserve_in + out_cap, in_tok + exp_out
            if tok_budget is not None and worst_tok > tok_budget:
                drop(m, "budget", f"worst-case {worst_tok} tokens exceeds token budget {tok_budget}")
                continue
            if budget is not None and worst is not None and worst > budget + 1e-12:
                drop(m, "budget", f"worst-case cost ${worst:.6f} exceeds budget ${budget:.6f}")
                continue
            if self.latency_lookup and req.latency_target_ms and not req.pin:
                obs = self.latency_lookup(m.provider, m.id)
                if obs and obs[1] >= 5 and obs[0] > req.latency_target_ms:
                    drop(m, "latency", f"observed p50 {obs[0]:.0f}ms (n={obs[1]}) exceeds target {req.latency_target_ms}ms")
                    continue
            s = (Suitability(True, "none", "pinned by the caller (observe-only: no suitability check)") if req.pin
                 else assess(m, cl.category, req.profile, req.hard))
            cand = Candidate(
                provider=m.provider, model=m.id, tier=m.tier, suitability_basis=s.basis,  # type: ignore[arg-type]
                suitability_detail=s.detail, evidence_lower_bound=s.lower_bound,
                input_tokens_est=in_tok, expected_output_tokens=exp_out, worst_case_output_tokens=out_cap,
                expected_cost_usd=expct, worst_case_cost_usd=worst, expected_total_tokens=exp_tok,
                worst_case_total_tokens=worst_tok, pricing_warnings=self._pricing_warnings(m),
            )
            (suitable if s.suitable else unsuitable).append(cand)

        # Rank by expected cost when every candidate is priced. If ANY candidate is unpriced, costs are not
        # comparable, so rank by (smallest suitable tier, fewest expected tokens) - a documented proxy for "cheapest".
        all_priced = all(c.expected_cost_usd is not None for c in suitable + unsuitable)
        if all_priced:
            key = lambda c: (c.expected_cost_usd, -(c.evidence_lower_bound or 0), -(c.tier or 0), c.provider, c.model)  # noqa: E731
        else:
            key = lambda c: (c.tier or 9, c.expected_total_tokens, -(c.evidence_lower_bound or 0), c.provider, c.model)  # noqa: E731
        suitable.sort(key=key)
        for i, c in enumerate(suitable, 1):
            c.rank = i
        unsuitable.sort(key=key)

        limit = 0 if req.pin else (req.escalation_limit if req.escalation_limit is not None else pol.escalation_default)
        plan = Plan(
            request_id=request_id or uuid.uuid4().hex[:12], task=task, profile=req.profile,
            candidates=suitable, unsuitable=unsuitable, excluded=excluded, input_tokens=in_tok,
            token_method=method, escalation_limit=limit,
            ranking_basis="expected_cost" if all_priced else "tier_then_tokens",
            assumptions=self._assumptions(cl.category, cap, method),
            uncertainty=self._uncertainty(req, cl, suitable, method),
            catalog_version=self.catalog.version, policy_version=POLICY_VERSION,
        )
        plan.explanation = self._explain(plan, req)
        if req.pin and suitable:
            plan.explanation = (f"Pinned by the caller: running {req.pin} exactly (observe-only: no routing, failover or escalation); "
                                f"tokens, cost and tags are recorded. ~{suitable[0].expected_total_tokens} tokens expected, up to {suitable[0].worst_case_total_tokens}.")
        if not suitable:
            if any(e.stage == "budget" for e in excluded) and not unsuitable:
                raise BudgetTooLow(plan.explanation, plan=plan)
            raise NoEligibleModel(plan.explanation, plan=plan)
        return plan

    # ------------------------------------------------------------------------------------
    def _stale(self, m: Model) -> bool:
        age = pricing_age_days(m, self.today())
        return age is None or age > self.cfg.max_pricing_age_days

    def _pricing_warnings(self, m: Model) -> list[str]:
        w = []
        if m.pricing is None or not m.pricing.known:
            return ["no pricing in catalog: cost unknown (token usage is still tracked)"]
        age = pricing_age_days(m, self.today())
        if age is None:
            w.append("pricing verification date unknown")
        elif age > self.cfg.max_pricing_age_days:
            w.append(f"pricing last verified {age} days ago (stale after {self.cfg.max_pricing_age_days})")
        if m.pricing and m.pricing.origin == "user_override":
            w.append("price from user override")
        return w

    def _assumptions(self, cat: TaskCategory, cap: int, method: str) -> list[str]:
        return [
            f"input tokens estimated by {method}; reservations add a safety margin when approximate",
            f"expected output for '{cat.value}' assumed {EXPECTED_OUTPUT_TOKENS[cat] or 'proportional to input'} tokens (cap {cap}); worst case assumes the cap",
            "no prompt-cache hits assumed (cache reads, if any, only lower the final cost)",
            "reasoning tokens are included in output tokens and not counted twice",
        ]

    def _uncertainty(self, req: RouteRequest, cl: Classification, suitable: list[Candidate], method: str) -> list[str]:
        u = []
        if cl.confidence == "low":
            u.append(f"task category uncertain ({cl.note or 'low confidence'}); applied the conservative policy")
        if suitable and suitable[0].suitability_basis == "prior":
            u.append("selected model is chosen on an editorial tier prior; no sufficient measured evidence for this category")
        if "approx" in method:
            u.append("token counts are approximate")
        if req.latency_target_ms:
            u.append("latency target is only enforced from observed history (>=5 samples); otherwise unverified")
        if req.acceptance_criteria and not req.has_validation():
            u.append("acceptance criteria were supplied but no validator is configured; they cannot be checked")
        return u

    def _explain(self, p: Plan, req: RouteRequest) -> str:
        t = p.task
        parts = [
            f"Task '{t.category.value}' ({t.basis}, {t.confidence} confidence) under profile '{p.profile.value}' needs tier >= {t.required_tier} or sufficient measured evidence.",
            f"{len(p.candidates) + len(p.unsuitable)} models passed hard constraints; {len(p.candidates)} suitable; {len(p.excluded)} excluded.",
        ]
        if p.candidates:
            c = p.candidates[0]
            parts.append(
                (f"Selected {c.provider}/{c.model}: lowest estimated cost ${c.expected_cost_usd:.6f} "
                 f"(worst case ${c.worst_case_cost_usd:.6f})" if p.ranking_basis == "expected_cost" else
                 f"Selected {c.provider}/{c.model}: smallest suitable tier, then fewest tokens (pricing unknown for some candidates, "
                 f"so cost is not used for ranking)")
                + f"; ~{c.expected_total_tokens} tokens expected, up to {c.worst_case_total_tokens}; "
                f"basis: {c.suitability_basis} - {c.suitability_detail}."
            )
        else:
            parts.append("No suitable candidate.")
            if p.unsuitable:
                parts.append("Closest: " + "; ".join(f"{c.provider}/{c.model} ({c.suitability_detail})" for c in p.unsuitable[:3]) + ".")
            if p.excluded:
                parts.append("Excluded: " + "; ".join(f"{e.provider}/{e.model} [{e.stage}: {e.reason}]" for e in p.excluded[:6]) + ".")
        return " ".join(parts)
