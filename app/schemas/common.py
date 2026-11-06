"""Shared Pydantic base schemas.

The backend's JSON contract is camelCase (frontend-facing, AGENTS.md §1):
responses serialize with camelCase aliases, requests accept both alias and
pythonic names (``populate_by_name``). Request models additionally forbid
unknown fields so client typos fail fast with a 422.
"""

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict
from pydantic.alias_generators import to_camel


def _stringify_uuid(value: object) -> object:
    """Coerce UUID instances into their canonical string form.

    Args:
        value: Raw attribute value.

    Returns:
        String form for UUIDs; the value otherwise unchanged.
    """
    return str(value) if isinstance(value, UUID) else value


IdStr = Annotated[str, BeforeValidator(_stringify_uuid)]
"""Entity-reference string that tolerates UUID objects from the ORM."""


class BaseSchema(BaseModel):
    """Base for all wire schemas: camelCase aliases, ORM-friendly parsing."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class BaseRequest(BaseSchema):
    """Base for request bodies: rejects unknown fields."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )
