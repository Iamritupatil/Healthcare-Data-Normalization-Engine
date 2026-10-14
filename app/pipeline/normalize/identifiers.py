"""Identifiers stay strings: MRN "00123" must never become the number 123."""

import re
from dataclasses import dataclass
from typing import Any

from app.pipeline.core import NormalizationError, ReasonCode, Rule

IDENTIFIER_RULE = Rule("identifier.string", 1)
EXTERNAL_ID_RULE = Rule("identifier.compact", 1)
PHONE_RULE = Rule("phone.digits", 1)
EMAIL_RULE = Rule("email.lower", 1)
POSTAL_RULE = Rule("postal.clean", 1)

_NON_DIGITS = re.compile(r"\D")
_SEPARATORS = re.compile(r"[\s.\-/]")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class IdentifierResult:
    value: str
    was_numeric: bool


def normalize_identifier(raw: Any) -> IdentifierResult:
    if isinstance(raw, bool):
        raise NormalizationError(ReasonCode.FIELD_INVALID, "identifier must be a string")
    if isinstance(raw, int):
        # The sender already turned it into a number; any leading zeros are gone.
        return IdentifierResult(str(raw), was_numeric=True)
    if not isinstance(raw, str):
        raise NormalizationError(ReasonCode.FIELD_INVALID, "identifier must be a string")
    value = raw.strip()
    if not value:
        raise NormalizationError(ReasonCode.IDENTIFIER_MISSING, "identifier is empty")
    return IdentifierResult(value, was_numeric=False)


def normalize_external_id(raw: str) -> str | None:
    """National ids and member numbers are written with spaces, dots or dashes; compare without."""
    value = _SEPARATORS.sub("", raw).upper()
    return value or None


def normalize_email(raw: str) -> str | None:
    value = raw.strip().lower()
    return value if _EMAIL.fullmatch(value) else None


def normalize_phone(raw: str) -> str | None:
    """Digits only, keeping a leading +. Returns None when too short to be a phone number."""
    text = raw.strip()
    digits = _NON_DIGITS.sub("", text)
    if not 7 <= len(digits) <= 15:
        return None
    return f"+{digits}" if text.startswith("+") else digits


def normalize_postal_code(raw: str) -> str:
    return " ".join(raw.split()).upper()
