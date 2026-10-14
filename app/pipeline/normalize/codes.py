"""Code tables declared in mapping files (e.g. sex codes to FHIR administrative gender)."""

from collections.abc import Mapping

from app.pipeline.core import NormalizationError, ReasonCode, Rule

CODE_RULE = Rule("code.map", 1)
FHIR_GENDERS = frozenset({"male", "female", "other", "unknown"})


def map_code(raw: str, table: Mapping[str, str]) -> str:
    text = raw.strip()
    if text in table:
        return table[text]
    folded = {key.casefold(): value for key, value in table.items()}
    if text.casefold() in folded:
        return folded[text.casefold()]
    raise NormalizationError(ReasonCode.CODE_UNMAPPED, "code is not in the mapping's code table")
