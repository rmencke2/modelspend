"""Scrub anything that looks like an API key from text before it can reach an error message, a log or an API response.

Provider SDK errors sometimes echo (part of) the key, e.g. "Incorrect API key provided: sk-proj-****abcd". Two layers:
  * every key/token this process was configured with is registered and removed verbatim
  * common key shapes (sk-..., sk-ant-..., AIza..., xai-..., Bearer ...) are removed by pattern, including masked forms
"""
from __future__ import annotations

import re

_SECRETS: set[str] = set()
_PATTERNS = [
    re.compile(r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_\-*]{4,}"),
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-*]{4,}"),
    re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_\-*]{8,}"),
    re.compile(r"(?<![A-Za-z0-9])xai-[A-Za-z0-9*]{6,}"),
    re.compile(r"(?i)(bearer|basic)\s+[A-Za-z0-9._~+/=\-*]{8,}"),
    re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|authorization|x-api-key)[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9._~+/=\-*]{8,}"),
]


def register(*values: str | None) -> None:
    for v in values:
        if v and len(v) >= 6:
            _SECRETS.add(v)


def scrub(text: str) -> str:
    for s in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(s, "[redacted]")
    for i, pat in enumerate(_PATTERNS):
        text = pat.sub((lambda m: m.group(1) + "[redacted]") if i == 5 else "[redacted]", text)
    return text
