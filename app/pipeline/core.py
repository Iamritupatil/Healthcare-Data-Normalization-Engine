"""Types shared by every pipeline stage. Nothing here touches the web layer or the database."""

import dataclasses
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ReasonCode(StrEnum):
    """Permanent failures: the record goes to quarantine and is never retried automatically."""

    DATE_UNPARSEABLE = "DATE_UNPARSEABLE"
    DATE_AMBIGUOUS = "DATE_AMBIGUOUS"
    DATE_OUT_OF_RANGE = "DATE_OUT_OF_RANGE"
    TIMESTAMP_UNPARSEABLE = "TIMESTAMP_UNPARSEABLE"
    TIMESTAMP_AMBIGUOUS = "TIMESTAMP_AMBIGUOUS"
    TIMESTAMP_NONEXISTENT = "TIMESTAMP_NONEXISTENT"
    TIMEZONE_MISSING = "TIMEZONE_MISSING"
    UNIT_UNKNOWN = "UNIT_UNKNOWN"
    UNIT_INCOMPATIBLE = "UNIT_INCOMPATIBLE"
    VALUE_NOT_NUMERIC = "VALUE_NOT_NUMERIC"
    CODE_UNMAPPED = "CODE_UNMAPPED"
    IDENTIFIER_MISSING = "IDENTIFIER_MISSING"
    FIELD_MISSING = "FIELD_MISSING"
    FIELD_INVALID = "FIELD_INVALID"
    FIELD_UNEXPECTED = "FIELD_UNEXPECTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class WarningCode(StrEnum):
    """Accepted with a note: the record is stored and the warning becomes a finding."""

    NAME_SPLIT_UNCERTAIN = "NAME_SPLIT_UNCERTAIN"
    FIELD_IGNORED = "FIELD_IGNORED"
    IDENTIFIER_WAS_NUMERIC = "IDENTIFIER_WAS_NUMERIC"
    PHONE_INVALID = "PHONE_INVALID"
    EMAIL_INVALID = "EMAIL_INVALID"
    IDENTIFIER_INVALID = "IDENTIFIER_INVALID"


@dataclass(frozen=True)
class Rule:
    """A named, versioned normalization rule. Bump the version whenever behaviour changes."""

    name: str
    version: int


class NormalizationError(Exception):
    def __init__(self, code: ReasonCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class FieldError:
    field: str
    code: ReasonCode
    message: str
    source_field: str | None = None

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "field": self.field,
            "code": self.code.value,
            "message": self.message,
        }
        if self.source_field is not None:
            data["source_field"] = self.source_field
        return data


@dataclass(frozen=True)
class PipelineWarning:
    field: str
    code: WarningCode
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"field": self.field, "code": self.code.value, "message": self.message}


@dataclass(frozen=True)
class Transformation:
    """One entry of the transformation log: how a source value became a canonical value."""

    field: str
    source_field: str | None
    rule: Rule
    original: Any
    result: Any
    details: dict[str, Any] = dataclasses.field(default_factory=dict)
