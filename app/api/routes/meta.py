"""Who am I, and what does each source send. Used by the dashboard; useful to any client."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response

from app.api.deps import Registry, get_principal
from app.api.responses import ExactJSONResponse
from app.db import org_transaction
from app.models.tables import Org
from app.services.auth import Principal

router = APIRouter(prefix="/v1", tags=["account"])

Authenticated = Annotated[Principal, Depends(get_principal)]


@router.get("/me", summary="The organisation and role behind this API key")
def me(principal: Authenticated) -> Response:
    with org_transaction(principal.org_id) as session:
        org = session.get_one(Org, principal.org_id)
        body = {
            "org_id": principal.org_id,
            "org_name": org.name,
            "role": principal.role,
            "api_key_id": principal.api_key_id,
        }
    return ExactJSONResponse(body)


@router.get("/sources", summary="Known sources, their active mapping and the columns they send")
def sources(principal: Authenticated, registry: Registry) -> Response:
    items: list[dict[str, Any]] = []
    for name in registry.sources():
        mapping = registry.active(name)
        items.append(
            {
                "source": mapping.source,
                "version": mapping.version,
                "description": mapping.description.strip(),
                "timezone": mapping.timezone,
                "record_types": {
                    record_type: {
                        "columns": list(rules.fields),
                        "id_columns": rules.source_record_id,
                    }
                    for record_type, rules in mapping.record_types.items()
                },
            }
        )
    return ExactJSONResponse({"items": items})
