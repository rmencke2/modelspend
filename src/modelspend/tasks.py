"""Deterministic task categorization.

Order of authority: explicit `task_type` hint > classification cache > keyword rules > fallback.
Only the head and tail of the last user message are scanned (fenced code and the middle of long
text are ignored for keywords) so that retrieved/pasted content in the middle cannot steer routing.
Prompt LENGTH is deliberately not used as a difficulty signal.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass

from .policy import BASE_TIER
from .types import Message, TaskCategory

WINDOW = 600

_RULES: list[tuple[TaskCategory, int, re.Pattern]] = [
    (TaskCategory.translation, 3, re.compile(r"\btranslat(e|ion|ing)\b|\binto (french|spanish|german|danish|swedish|italian|english|portuguese|dutch|japanese|chinese)\b", re.I)),
    (TaskCategory.summarization, 3, re.compile(r"\bsummari[sz](e|ation)\b|\bsummary\b|\btl;?dr\b|\bcondense\b|\bkey points\b", re.I)),
    (TaskCategory.classification, 3, re.compile(r"\bclassif(y|ication)\b|\bcategori[sz]e\b|\bsentiment\b|\bwhich category\b|\bchoose one of\b|\bone of the (following )?(labels|categories|options)\b|\blabel (this|the|each)\b", re.I)),
    (TaskCategory.extraction, 3, re.compile(r"\bextract\b|\bpull out\b|\bparse\b|\bidentify all\b|\blist all (the )?(names|dates|entities)\b", re.I)),
    (TaskCategory.rewriting, 3, re.compile(r"\brewrite\b|\brephrase\b|\bparaphrase\b|\bproofread\b|\bfix (the )?grammar\b|\bmake (it|this) (more )?(formal|concise|friendly|polite|shorter)\b", re.I)),
    (TaskCategory.code, 3, re.compile(r"\bdef \w+\(|\bfunction\b.*\b(write|implement|fix)\b|\b(write|implement|fix|debug|refactor)\b.*\b(function|method|class|script|code|bug|query)\b|\btraceback\b|\bstack ?trace\b|\bunit tests?\b|\bsyntax error\b", re.I)),
    (TaskCategory.analysis, 2, re.compile(r"\banaly[sz]e\b|\bcompare\b|\btrade-?offs?\b|\bevaluate\b|\bstrategy\b|\bpros and cons\b|\broot cause\b|\bassess\b|\brecommend\b", re.I)),
    (TaskCategory.qa_context, 2, re.compile(r"\b(based on|using|according to|from) (only )?(the )?(following|above|provided|given) (context|document|text|passage|article)\b|\bcontext:|\bdocument:", re.I)),
]
_FENCE = re.compile(r"```.*?```", re.S)


@dataclass
class Classification:
    category: TaskCategory
    basis: str  # hint | rules | cache | model | fallback
    confidence: str  # high | low
    note: str = ""
    ambiguous: bool = False


def scan_text(messages: list[Message]) -> str:
    last = next((m.content or "" for m in reversed(messages) if m.role == "user"), "")
    return last if len(last) <= 2 * WINDOW else last[:WINDOW] + "\n" + last[-WINDOW:]


def classify_rules(messages: list[Message]) -> Classification:
    text = scan_text(messages)
    has_fence = "```" in (next((m.content or "" for m in reversed(messages) if m.role == "user"), ""))
    stripped = _FENCE.sub(" ", text)
    scores: dict[TaskCategory, int] = {}
    for cat, w, rx in _RULES:
        if rx.search(stripped):
            scores[cat] = scores.get(cat, 0) + w
    if has_fence:
        scores[TaskCategory.code] = scores.get(TaskCategory.code, 0) + 2
    if scores.get(TaskCategory.qa_context) and "?" in stripped:
        scores[TaskCategory.qa_context] += 1
    if not scores:
        return Classification(TaskCategory.unknown, "fallback", "low", "no category signals; conservative fallback applies", True)
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], -BASE_TIER[kv[0]]))
    top, second = ranked[0], (ranked[1] if len(ranked) > 1 else None)
    if second and top[1] - second[1] < 2:
        # ambiguous: pick the category with the HIGHER requirement (conservative)
        cands = [top[0], second[0]]
        pick = max(cands, key=lambda c: BASE_TIER[c])
        return Classification(pick, "rules", "low", f"ambiguous between {cands[0].value} and {cands[1].value}; chose the stricter", True)
    return Classification(top[0], "rules", "high", "")


def privacy_key(salt: bytes, messages: list[Message]) -> str:
    """Salted HMAC of the scanned text: a cache key that cannot be reversed to the prompt."""
    return hmac.new(salt, scan_text(messages).strip().lower().encode(), hashlib.sha256).hexdigest()
