from __future__ import annotations

import re
from typing import Literal

from pydantic import field_validator

from foundry_opt.models import FrozenModel
from foundry_opt.optimize_job.safety import (
    UnsafeCheckpointContentError,
    assert_safe_persisted_string,
)


_VERIFICATION_CHECK_LINE = re.compile(r"^(?P<kind>command|check)\s*:\s*(?P<value>.+)$")


def _safe_text(value: object, *, field: str, limit: int = 512) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    try:
        assert_safe_persisted_string(normalized, field=field, limit=limit)
    except UnsafeCheckpointContentError as exc:
        raise ValueError(str(exc)) from exc
    return normalized


class VerificationDatasetInput(FrozenModel):
    dataset_id_or_uri: str

    @field_validator("dataset_id_or_uri")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        normalized = _safe_text(value, field="dataset_id_or_uri")
        if any(character.isspace() for character in normalized):
            raise ValueError("dataset_id_or_uri must not contain whitespace")
        return normalized

    @property
    def is_uri(self) -> bool:
        return self.dataset_id_or_uri.startswith("azureai://")


class VerificationCheckSpec(FrozenModel):
    kind: Literal["command", "check"]
    value: str

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: str) -> str:
        return _safe_text(value, field="value")

    @property
    def casefold_key(self) -> tuple[str, str]:
        return (self.kind, self.value.casefold())

    def render(self) -> str:
        return f"{self.kind}: {self.value}"

    @classmethod
    def parse_line(cls, value: str) -> "VerificationCheckSpec":
        match = _VERIFICATION_CHECK_LINE.fullmatch(value.strip())
        if match is None:
            raise ValueError(
                "verification check entry must use command: <text> or check: <text>"
            )
        return cls(kind=match.group("kind"), value=match.group("value"))


__all__ = [
    "VerificationCheckSpec",
    "VerificationDatasetInput",
]
