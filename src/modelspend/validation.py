"""Deterministic validators (cheap, preferred) plus an optional cost-accounted model judge.

A passing validator proves only what it checks: valid JSON is not a correct answer, and a
judge model is itself fallible (see docs/validation-and-escalation.md).
"""
from __future__ import annotations

import ast
import asyncio
import json
import re
from typing import Any, Awaitable, Callable

import jsonschema
from referencing import Registry
from referencing.exceptions import NoSuchResource

from .types import RouteRequest, ValidationReport, ValidationResult, ValidatorSpec


def _deny_retrieval(uri: str):
    raise NoSuchResource(ref=uri)


def validate_schema_local(instance: Any, schema: dict) -> None:
    """jsonschema validation in THIS process. Never fetches a URL (a schema is request-supplied data; jsonschema resolves remote
    `$ref`s by default, a blind SSRF), so only local references ('#/...') are allowed."""
    for ref in _refs(schema):
        if not ref.startswith("#"):
            raise jsonschema.exceptions.SchemaError(f"only local $ref values (starting with '#') are allowed, got {ref[:80]!r}")
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    cls(schema, registry=Registry(retrieve=_deny_retrieval)).validate(instance)


def _has_patterns(node: Any) -> bool:
    if isinstance(node, dict):
        return any(k in ("pattern", "patternProperties") or _has_patterns(v) for k, v in node.items())
    if isinstance(node, list):
        return any(_has_patterns(v) for v in node)
    return False


def validate_schema(instance: Any, schema: dict) -> None:
    """Like validate_schema_local, but a schema that contains `pattern`/`patternProperties` (regexes, which can backtrack
    catastrophically) is evaluated in a worker process with a hard timeout."""
    if _has_patterns(schema):
        from . import safe_regex
        try:
            safe_regex.validate_schema(instance, schema)
        except safe_regex.RegexTimeout as e:
            raise ValueError(f"schema pattern {e}") from e
        return
    validate_schema_local(instance, schema)


def _refs(node: Any):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("$ref", "$dynamicRef", "$recursiveRef") and isinstance(v, str):
                yield v
            else:
                yield from _refs(v)
    elif isinstance(node, list):
        for v in node:
            yield from _refs(v)


def _json(text: str) -> Any:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", t).strip()
    return json.loads(t)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower()).strip(" .!\"'")


def run_spec(spec: ValidatorSpec, text: str, sandbox_runner: Callable[[str, dict], tuple[bool, str]] | None = None) -> ValidationResult:
    t = spec.type
    name = spec.name or t
    extra = spec.model_extra or {}
    try:
        if t == "json_schema":
            validate_schema(_json(text), extra["schema"])
            return ValidationResult(name=name, passed=True)
        if t == "valid_json":
            _json(text)
            return ValidationResult(name=name, passed=True)
        if t == "required_fields":
            obj = _json(text)
            missing = [f for f in extra["fields"] if not isinstance(obj, dict) or f not in obj]
            return ValidationResult(name=name, passed=not missing, detail=f"missing: {missing}" if missing else "")
        if t == "allowed_labels":
            ok = _norm(text) in {_norm(x) for x in extra["labels"]}
            return ValidationResult(name=name, passed=ok, detail="" if ok else f"'{text.strip()[:40]}' not in allowed labels")
        if t == "exact":
            ok = _norm(text) == _norm(str(extra["expected"]))
            return ValidationResult(name=name, passed=ok, detail="" if ok else "output differs from expected")
        if t == "json_equals":
            ok = _json(text) == extra["expected"]
            return ValidationResult(name=name, passed=ok, detail="" if ok else "JSON differs from expected")
        if t == "contains":
            low = text.lower()
            miss = [s for s in extra["substrings"] if s.lower() not in low]
            return ValidationResult(name=name, passed=not miss, detail=f"missing: {miss}" if miss else "")
        if t == "not_contains":
            low = text.lower()
            bad = [s for s in extra["substrings"] if s.lower() in low]
            return ValidationResult(name=name, passed=not bad, detail=f"forbidden present: {bad}" if bad else "")
        if t == "regex":
            from . import safe_regex
            try:
                ok = safe_regex.search(str(extra["pattern"]), text)
            except safe_regex.RegexTimeout as e:
                return ValidationResult(name=name, passed=False, detail=f"regex {e}")
            return ValidationResult(name=name, passed=ok, detail="" if ok else "pattern not matched")
        if t == "max_chars":
            ok = len(text) <= int(extra["value"])
            return ValidationResult(name=name, passed=ok, detail="" if ok else f"{len(text)} > {extra['value']} chars")
        if t == "min_chars":
            ok = len(text.strip()) >= int(extra["value"])
            return ValidationResult(name=name, passed=ok, detail="" if ok else f"{len(text.strip())} < {extra['value']} chars")
        if t == "python_syntax":
            code = _strip_fence(text)
            ast.parse(code)  # parse only; never executed
            return ValidationResult(name=name, passed=True)
        if t == "python_tests":
            if sandbox_runner is None:
                return ValidationResult(name=name, passed=False, detail="no sandbox available; generated code is never run on the host")
            ok, detail = sandbox_runner(_strip_fence(text), extra)
            return ValidationResult(name=name, passed=ok, detail=detail)
        return ValidationResult(name=name, passed=False, detail=f"unknown validator type '{t}'")
    except KeyError as e:
        return ValidationResult(name=name, passed=False, detail=f"validator misconfigured: missing {e}")
    except Exception as e:  # noqa: BLE001 - validators must never crash a request
        return ValidationResult(name=name, passed=False, detail=f"{type(e).__name__}: {str(e)[:160]}")


def _strip_fence(text: str) -> str:
    m = re.search(r"```(?:python|py)?\n(.*?)```", text, re.S)
    return m.group(1) if m else text


JudgeFn = Callable[[str], Awaitable[ValidationResult]]


async def validate(
    req: RouteRequest,
    text: str,
    *,
    truncated: bool = False,
    judge: JudgeFn | None = None,
    sandbox_runner: Callable[[str, dict], tuple[bool, str]] | None = None,
) -> ValidationReport:
    results: list[ValidationResult] = []
    if req.response_schema:
        results.append(await asyncio.to_thread(run_spec, ValidatorSpec(type="json_schema", schema=req.response_schema), text, sandbox_runner))  # type: ignore[call-arg]
    for spec in req.validators:
        results.append(await asyncio.to_thread(run_spec, spec, text, sandbox_runner))
    for i, fn in enumerate(req.callable_validators):
        nm = getattr(fn, "__name__", f"callable_{i}")
        try:
            r = fn(text)
            ok, detail = (bool(r[0]), str(r[1])) if isinstance(r, tuple) else (bool(r), "")
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"{type(e).__name__}: {e}"
        results.append(ValidationResult(name=nm, kind="callable", passed=ok, detail=detail))
    if truncated and results:  # truncation only counts as evidence when a validator is configured
        results.insert(0, ValidationResult(name="not_truncated", passed=False, detail="output hit the token limit"))
    # The judge is only consulted if every deterministic check passed (it costs money).
    if req.judge and judge is not None and all(r.passed for r in results):
        results.append(await judge(text))
    if not results:
        if req.acceptance_criteria:
            return ValidationReport(status="unverified_criteria", note="acceptance criteria supplied but no validator configured")
        return ValidationReport(status="not_configured")
    status = "passed" if all(r.passed for r in results) else "failed"
    note = None
    if status == "passed" and req.acceptance_criteria and not any(r.kind == "judge" for r in results):
        note = "passed configured validators; acceptance criteria are not individually verified"
    return ValidationReport(status=status, results=results, note=note)
