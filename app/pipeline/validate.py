"""Stage 4: check the assembled record against the canonical model.

Pydantic error details include the offending input value. That value can be patient
data, so it is dropped here and never reaches logs or API responses.
"""

from typing import Any

from pydantic import ValidationError

from app.models.canonical import CANONICAL_MODELS
from app.pipeline.core import FieldError, ReasonCode

_ERROR_CODES = {
    "missing": ReasonCode.FIELD_MISSING,
    "extra_forbidden": ReasonCode.FIELD_UNEXPECTED,
}


def validate_resource(
    record_type: str, resource: dict[str, Any]
) -> tuple[dict[str, Any] | None, list[FieldError]]:
    model = CANONICAL_MODELS[record_type]
    try:
        validated = model.model_validate(resource)
    except ValidationError as exc:
        return None, [
            FieldError(
                field=".".join(str(part) for part in error["loc"]) or record_type,
                code=_ERROR_CODES.get(error["type"], ReasonCode.FIELD_INVALID),
                message=error["msg"],
            )
            for error in exc.errors(include_input=False, include_url=False)
        ]
    return validated.model_dump(mode="python", exclude_none=True), []
