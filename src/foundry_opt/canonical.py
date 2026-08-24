from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from foundry_opt.optimize_job.safety import (
    UnsafeCheckpointContentError,
    assert_safe_persisted_document,
)


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def safe_persisted_document(value: object) -> object:
    assert_safe_persisted_document(value)
    return value


def redact_persisted_document(value: object) -> object:
    safe_persisted_document(value)
    if isinstance(value, Mapping):
        return {key: redact_persisted_document(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [redact_persisted_document(child) for child in value]
    if isinstance(value, list):
        return [redact_persisted_document(child) for child in value]
    return value


__all__ = [
    "UnsafeCheckpointContentError",
    "canonical_json_bytes",
    "canonical_sha256",
    "redact_persisted_document",
    "safe_persisted_document",
]
