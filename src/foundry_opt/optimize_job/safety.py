from __future__ import annotations

import re
from collections.abc import Mapping


class UnsafeCheckpointContentError(ValueError):
    """Raised when persisted checkpoint state could expose raw or secret content."""


_FORBIDDEN_KEY_PARTS = {
    "argument",
    "arguments",
    "body",
    "content",
    "conversation",
    "conversations",
    "data",
    "input",
    "inputs",
    "message",
    "messages",
    "output",
    "outputs",
    "payload",
    "prompt",
    "prompts",
    "queries",
    "query",
    "raw",
    "request",
    "requests",
    "response",
    "responses",
    "result",
    "results",
    "span",
    "spans",
    "text",
    "tool",
    "tools",
    "trace",
    "traces",
    "transcript",
    "transcripts",
}
_SECRET_PATTERNS = (
    re.compile(
        r"(?i)(?:authorization|api[_-]?key|client[_-]?secret|password|"
        r"access[_-]?token|refresh[_-]?token)\s*[:=]"
    ),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+"),
    re.compile(
        r"(?i)\b(?:query|input|prompt|message|content|body|payload|"
        r"request|result|response|output|tool|trace|transcript)"
        r"\s*[:=]\s*.+"
    ),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
        r"[A-Za-z0-9_-]{8,}\b"
    ),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{12,}\b"),
    re.compile(r"(?i)(?:AccountKey|SharedAccessKey)="),
)
_SECRET_IDENTIFIER_PREFIXES = (
    "ghp_",
    "gho_",
    "ghu_",
    "ghs_",
    "ghr_",
    "github_pat_",
    "xoxb-",
    "xoxp-",
)


def assert_safe_persisted_document(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise UnsafeCheckpointContentError(
                    "checkpoint mapping keys must be strings"
                )
            normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).lower()
            parts = set(filter(None, re.split(r"[^a-z0-9]+", normalized)))
            if parts & _FORBIDDEN_KEY_PARTS:
                raise UnsafeCheckpointContentError(
                    "checkpoint contains a forbidden raw-content field"
                )
            assert_safe_persisted_string(key, field="checkpoint field", limit=128)
            assert_safe_persisted_document(child)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            assert_safe_persisted_document(child)
        return
    if isinstance(value, str):
        assert_safe_persisted_string(value, field="checkpoint value")


def assert_safe_persisted_string(
    value: str,
    *,
    field: str,
    limit: int = 512,
) -> None:
    if not value or len(value) > limit:
        raise UnsafeCheckpointContentError(
            f"{field} must contain between 1 and {limit} characters"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise UnsafeCheckpointContentError(
            f"{field} cannot contain control characters"
        )
    lowered = value.lower()
    if lowered.startswith(_SECRET_IDENTIFIER_PREFIXES) or (
        lowered.startswith("sk-") and len(value) >= 11
    ):
        raise UnsafeCheckpointContentError(
            f"{field} resembles secret or credential content"
        )
    if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        raise UnsafeCheckpointContentError(
            f"{field} resembles secret or credential content"
        )


__all__ = [
    "UnsafeCheckpointContentError",
    "assert_safe_persisted_document",
    "assert_safe_persisted_string",
]
