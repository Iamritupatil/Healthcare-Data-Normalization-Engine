"""Canonical record shapes: a small, strict subset of FHIR R4 Patient and Observation.

Field names follow FHIR so the output is familiar to anyone who knows the standard.
`extra="forbid"` turns any unexpected field into a validation error instead of letting
it slip through.
"""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.pipeline.normalize.units import LOINC, UCUM


class _Canonical(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# HL7 v2 table 0203 identifier types: medical record number, national id, member number.
IdentifierType = Literal["MR", "NI", "MB"]


class Identifier(_Canonical):
    system: str = Field(min_length=1)
    value: str = Field(min_length=1)
    type: IdentifierType | None = None


class HumanName(_Canonical):
    text: str = Field(min_length=1)
    family: str | None = None
    given: list[str] = Field(default_factory=list)


class ContactPoint(_Canonical):
    system: Literal["phone", "email"]
    value: str = Field(min_length=1)


class Address(_Canonical):
    postalCode: str = Field(min_length=1)


class Meta(_Canonical):
    source: str
    mappingVersion: int


class Patient(_Canonical):
    resourceType: Literal["Patient"] = "Patient"
    identifier: list[Identifier] = Field(min_length=1)
    name: HumanName
    gender: Literal["male", "female", "other", "unknown"] | None = None
    birthDate: date
    telecom: list[ContactPoint] = Field(default_factory=list)
    address: list[Address] = Field(default_factory=list)
    meta: Meta


class Coding(_Canonical):
    system: Literal["http://loinc.org"] = LOINC
    code: str
    display: str


class CodeableConcept(_Canonical):
    coding: list[Coding] = Field(min_length=1)
    text: str


class Reference(_Canonical):
    identifier: Identifier


class Quantity(_Canonical):
    value: Decimal
    unit: str
    system: Literal["http://unitsofmeasure.org"] = UCUM
    code: str


class Observation(_Canonical):
    resourceType: Literal["Observation"] = "Observation"
    identifier: list[Identifier] = Field(min_length=1)
    status: Literal["final"] = "final"
    code: CodeableConcept
    subject: Reference
    effectiveDateTime: AwareDatetime
    valueQuantity: Quantity
    meta: Meta


CANONICAL_MODELS: dict[str, type[_Canonical]] = {"Patient": Patient, "Observation": Observation}
