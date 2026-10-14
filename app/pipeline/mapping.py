"""Stage 2: per-source mapping files.

A mapping file is a translation dictionary for one sending system: which source field
feeds which canonical field, and how the source writes dates, names and codes. Files are
never edited in place; a change is a new version, so every log row points at the rules
that actually ran. Layout: mappings/<source>/v<version>.yaml.
"""

from collections.abc import Iterable
from pathlib import Path
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.pipeline.normalize.codes import FHIR_GENDERS
from app.pipeline.normalize.names import NameOrder
from app.pipeline.normalize.units import CONCEPTS

RecordType = Literal["Patient", "Observation"]
RECORD_TYPES: tuple[RecordType, ...] = ("Patient", "Observation")

TARGETS: dict[RecordType, frozenset[str]] = {
    "Patient": frozenset(
        {
            "identifier",
            "name",
            "name.family",
            "name.given",
            "birthDate",
            "gender",
            "phone",
            "email",
            "postalCode",
            "nationalId",
            "insuranceMemberId",
        }
    ),
    "Observation": frozenset(
        {"identifier", "subject", "code", "value", "unit", "effectiveDateTime"}
    ),
}
REQUIRED_TARGETS: dict[RecordType, frozenset[str]] = {
    "Patient": frozenset({"identifier", "birthDate"}),
    "Observation": frozenset(
        {"identifier", "subject", "code", "value", "unit", "effectiveDateTime"}
    ),
}
CODED_TARGETS = frozenset({"gender", "code"})
# Identifiers from outside the source system; comparable across sources when systems match.
SYSTEM_TARGETS = frozenset({"nationalId", "insuranceMemberId"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FieldRule(_Strict):
    to: str
    codes: dict[str, str] | None = None
    system: str | None = None


class RecordTypeMapping(_Strict):
    identifier_system: str = Field(min_length=1)
    source_record_id: list[str] = Field(min_length=1)
    fields: dict[str, FieldRule] = Field(min_length=1)

    def target_fields(self) -> dict[str, str]:
        """Canonical target -> source field name."""
        return {rule.to: name for name, rule in self.fields.items()}


class SourceMapping(_Strict):
    source: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    version: int = Field(ge=1)
    description: str = ""
    timezone: str | None = None
    date_formats: list[str] = Field(default_factory=lambda: ["%Y-%m-%d"], min_length=1)
    datetime_formats: list[str] = Field(default_factory=list)
    name_order: NameOrder = "given_family"
    record_types: dict[RecordType, RecordTypeMapping] = Field(min_length=1)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(f"unknown time zone {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _check_fields(self) -> Self:
        for record_type, mapping in self.record_types.items():
            seen: set[str] = set()
            for source_field, rule in mapping.fields.items():
                where = f"{record_type}.fields.{source_field}"
                if rule.to not in TARGETS[record_type]:
                    raise ValueError(f"{where}: unknown target {rule.to!r}")
                if rule.to in seen:
                    raise ValueError(f"{where}: target {rule.to!r} is mapped twice")
                seen.add(rule.to)
                if (rule.to in CODED_TARGETS) != (rule.codes is not None):
                    raise ValueError(
                        f"{where}: 'codes' is required for {sorted(CODED_TARGETS)} only"
                    )
                if (rule.to in SYSTEM_TARGETS) != (rule.system is not None):
                    raise ValueError(
                        f"{where}: 'system' is required for {sorted(SYSTEM_TARGETS)} only"
                    )
                if rule.to == "gender" and rule.codes:
                    bad = set(rule.codes.values()) - FHIR_GENDERS
                    if bad:
                        raise ValueError(f"{where}: not FHIR genders: {sorted(bad)}")
                if rule.to == "code" and rule.codes:
                    bad = set(rule.codes.values()) - CONCEPTS.keys()
                    if bad:
                        raise ValueError(f"{where}: unknown concepts: {sorted(bad)}")
            missing = REQUIRED_TARGETS[record_type] - seen
            if missing:
                raise ValueError(f"{record_type}: required targets not mapped: {sorted(missing)}")
            if record_type == "Patient" and not seen & {"name", "name.family", "name.given"}:
                raise ValueError("Patient: map either 'name' or 'name.family'/'name.given'")
            if record_type == "Observation" and "Patient" not in self.record_types:
                raise ValueError("Observation needs a Patient mapping for the subject identifier")
            unknown_keys = set(mapping.source_record_id) - mapping.fields.keys()
            if unknown_keys:
                raise ValueError(f"{record_type}.source_record_id: unmapped fields {unknown_keys}")
        return self


class UnknownSourceError(LookupError):
    pass


class MappingRegistry:
    """Parsed mappings cached in memory by (source, version). The highest version is active."""

    def __init__(self, mappings: Iterable[SourceMapping]) -> None:
        self._by_source: dict[str, dict[int, SourceMapping]] = {}
        for mapping in mappings:
            versions = self._by_source.setdefault(mapping.source, {})
            if mapping.version in versions:
                raise ValueError(f"duplicate mapping {mapping.source} v{mapping.version}")
            versions[mapping.version] = mapping

    def sources(self) -> list[str]:
        return sorted(self._by_source)

    def active(self, source: str) -> SourceMapping:
        versions = self._by_source.get(source)
        if not versions:
            raise UnknownSourceError(source)
        return versions[max(versions)]

    def get(self, source: str, version: int) -> SourceMapping:
        try:
            return self._by_source[source][version]
        except KeyError:
            raise UnknownSourceError(f"{source} v{version}") from None


def load_mapping_file(path: Path) -> SourceMapping:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    mapping = SourceMapping.model_validate(data)
    if path.parent.name != mapping.source or path.stem != f"v{mapping.version}":
        raise ValueError(f"{path}: file must live at {mapping.source}/v{mapping.version}.yaml")
    return mapping


def load_registry(directory: Path) -> MappingRegistry:
    files = sorted(directory.glob("*/v*.yaml"))
    if not files:
        raise FileNotFoundError(f"no mapping files under {directory}")
    return MappingRegistry(load_mapping_file(path) for path in files)
