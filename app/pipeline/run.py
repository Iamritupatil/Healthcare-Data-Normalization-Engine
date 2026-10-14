"""Runs stages 2 to 4 (map, normalize, validate) for one record. Pure: data in, data out.

Every field error is collected rather than stopping at the first one, so a quarantined
record shows everything that is wrong with it at once.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.pipeline.core import (
    FieldError,
    NormalizationError,
    PipelineWarning,
    ReasonCode,
    Rule,
    Transformation,
    WarningCode,
)
from app.pipeline.ingest import is_blank, source_record_id
from app.pipeline.mapping import RecordType, RecordTypeMapping, SourceMapping
from app.pipeline.normalize import codes, dates, identifiers, names, units
from app.pipeline.validate import validate_resource


@dataclass
class Normalized:
    resource: dict[str, Any]
    source_record_id: str
    mapping_version: int
    transformations: list[Transformation]
    warnings: list[PipelineWarning]


@dataclass
class Rejected:
    errors: list[FieldError]
    mapping_version: int
    warnings: list[PipelineWarning] = field(default_factory=list)

    @property
    def reason_codes(self) -> list[str]:
        return sorted({error.code.value for error in self.errors})


PipelineResult = Normalized | Rejected

# Which top-level canonical field each mapping target ends up in.
_RESOURCE_KEY = {
    "identifier": "identifier",
    "subject": "subject",
    "name": "name",
    "name.family": "name",
    "name.given": "name",
    "birthDate": "birthDate",
    "gender": "gender",
    "phone": "telecom",
    "email": "telecom",
    "postalCode": "address",
    "nationalId": "identifier",
    "insuranceMemberId": "identifier",
    "code": "code",
    "value": "valueQuantity",
    "unit": "valueQuantity",
    "effectiveDateTime": "effectiveDateTime",
}


def run_pipeline(
    mapping: SourceMapping, record_type: RecordType, payload: Mapping[str, Any], *, today: date
) -> PipelineResult:
    run = _Run(mapping, record_type, today)
    for source_field, rule in run.rules.fields.items():
        raw = payload.get(source_field)
        if is_blank(raw):
            continue
        try:
            run.normalize(rule.to, source_field, raw)
        except NormalizationError as exc:
            run.errors.append(FieldError(rule.to, exc.code, exc.message, source_field))

    ignored = sorted(set(payload) - run.rules.fields.keys())
    if ignored:
        run.warn(
            "*",
            WarningCode.FIELD_IGNORED,
            f"source fields not in mapping v{mapping.version}: {', '.join(ignored)}",
        )

    resource = run.assemble()
    validated, validation_errors = validate_resource(record_type, resource)
    failed = {_RESOURCE_KEY[error.field] for error in run.errors}
    errors = run.errors + [
        error for error in validation_errors if error.field.split(".")[0] not in failed
    ]
    srid = source_record_id(payload, run.rules.source_record_id)
    if errors or validated is None or srid is None:
        if not errors:
            errors = [
                FieldError("identifier", ReasonCode.IDENTIFIER_MISSING, "no source record id")
            ]
        return Rejected(errors, mapping.version, run.warnings)
    return Normalized(validated, srid, mapping.version, run.transformations, run.warnings)


def _text(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, int) and not isinstance(raw, bool):
        return str(raw)
    raise NormalizationError(ReasonCode.FIELD_INVALID, f"expected text, got {type(raw).__name__}")


class _Run:
    def __init__(self, mapping: SourceMapping, record_type: RecordType, today: date) -> None:
        self.mapping = mapping
        self.record_type = record_type
        self.rules: RecordTypeMapping = mapping.record_types[record_type]
        self.today = today
        self.values: dict[str, Any] = {}
        self.errors: list[FieldError] = []
        self.warnings: list[PipelineWarning] = []
        self.transformations: list[Transformation] = []

    def warn(self, target: str, code: WarningCode, message: str) -> None:
        self.warnings.append(PipelineWarning(target, code, message))

    def _set(
        self,
        target: str,
        source_field: str | None,
        raw: Any,
        result: Any,
        rule: Rule,
        **details: Any,
    ) -> None:
        self.values[target] = result
        self.transformations.append(
            Transformation(target, source_field, rule, raw, result, details)
        )

    def normalize(self, target: str, source_field: str, raw: Any) -> None:
        mapping = self.mapping
        rule = self.rules.fields[source_field]

        if target in {"identifier", "subject"}:
            identifier = identifiers.normalize_identifier(raw)
            if identifier.was_numeric:
                self.warn(
                    target,
                    WarningCode.IDENTIFIER_WAS_NUMERIC,
                    "sent as a number; leading zeros may be lost",
                )
            self._set(target, source_field, raw, identifier.value, identifiers.IDENTIFIER_RULE)
        elif target == "value":
            self._set(target, source_field, raw, units.parse_decimal(raw), units.DECIMAL_RULE)
        elif target == "birthDate":
            parsed = dates.parse_date(_text(raw), mapping.date_formats, today=self.today)
            self._set(
                target, source_field, raw, parsed, dates.DATE_RULE, formats=mapping.date_formats
            )
        elif target == "effectiveDateTime":
            parsed_at = dates.parse_timestamp(
                _text(raw), mapping.datetime_formats, mapping.timezone
            )
            self._set(
                target,
                source_field,
                raw,
                parsed_at,
                dates.TIMESTAMP_RULE,
                formats=mapping.datetime_formats,
                timezone=mapping.timezone,
            )
        elif target == "name":
            name = names.parse_full_name(_text(raw), mapping.name_order)
            if not name.certain:
                self.warn(
                    target,
                    WarningCode.NAME_SPLIT_UNCERTAIN,
                    "kept as text only; could not split safely",
                )
            result = {"text": name.text, "family": name.family, "given": name.given}
            self._set(target, source_field, raw, result, names.NAME_RULE, order=mapping.name_order)
        elif target in {"name.family", "name.given"}:
            self._set(target, source_field, raw, names.name_part(_text(raw)), names.NAME_PART_RULE)
        elif target == "gender":
            gender = codes.map_code(_text(raw), rule.codes or {})
            self._set(target, source_field, raw, gender, codes.CODE_RULE)
        elif target == "code":
            concept = units.CONCEPTS[codes.map_code(_text(raw), rule.codes or {})]
            self._set(
                target, source_field, raw, concept.loinc, units.CONCEPT_RULE, concept=concept.key
            )
            self.values["concept"] = concept
        elif target == "unit":
            self._set(target, source_field, raw, units.canonical_unit(_text(raw)), units.UNIT_RULE)
        elif target == "phone":
            phone = identifiers.normalize_phone(_text(raw))
            if phone is None:
                self.warn(
                    target, WarningCode.PHONE_INVALID, "not a plausible phone number; omitted"
                )
            else:
                self._set(target, source_field, raw, phone, identifiers.PHONE_RULE)
        elif target == "email":
            email = identifiers.normalize_email(_text(raw))
            if email is None:
                self.warn(
                    target, WarningCode.EMAIL_INVALID, "not a plausible email address; omitted"
                )
            else:
                self._set(target, source_field, raw, email, identifiers.EMAIL_RULE)
        elif target in {"nationalId", "insuranceMemberId"}:
            external_id = identifiers.normalize_external_id(_text(raw))
            if external_id is None:
                self.warn(
                    target,
                    WarningCode.IDENTIFIER_INVALID,
                    "empty after removing separators; omitted",
                )
            else:
                self._set(
                    target,
                    source_field,
                    raw,
                    external_id,
                    identifiers.EXTERNAL_ID_RULE,
                    system=rule.system,
                )
        elif target == "postalCode":
            postal = identifiers.normalize_postal_code(_text(raw))
            self._set(target, source_field, raw, postal, identifiers.POSTAL_RULE)
        else:  # pragma: no cover - mapping validation rejects unknown targets
            raise AssertionError(f"unhandled target {target}")

    def assemble(self) -> dict[str, Any]:
        values = self.values
        resource: dict[str, Any] = {
            "resourceType": self.record_type,
            "meta": {"source": self.mapping.source, "mappingVersion": self.mapping.version},
        }
        if "identifier" in values:
            own_id = {"system": self.rules.identifier_system, "value": values["identifier"]}
            if self.record_type == "Patient":
                own_id["type"] = "MR"
            resource["identifier"] = [own_id]
        if self.record_type == "Patient":
            self._assemble_patient(resource)
        else:
            self._assemble_observation(resource)
        return resource

    def _assemble_patient(self, resource: dict[str, Any]) -> None:
        values = self.values
        if "name" in values:
            resource["name"] = {key: value for key, value in values["name"].items() if value}
        elif "name.family" in values or "name.given" in values:
            family = values.get("name.family")
            given = values["name.given"].split(" ") if "name.given" in values else []
            text = " ".join([*given, family] if family else given)
            resource["name"] = {"text": text, "given": given} | (
                {"family": family} if family else {}
            )
        for target in ("gender", "birthDate"):
            if target in values:
                resource[target] = values[target]
        for target, id_type in (("nationalId", "NI"), ("insuranceMemberId", "MB")):
            if target in values:
                system = self.rules.fields[self._source_of(target) or ""].system
                resource.setdefault("identifier", []).append(
                    {"system": system, "value": values[target], "type": id_type}
                )
        telecom = [
            {"system": kind, "value": values[kind]} for kind in ("phone", "email") if kind in values
        ]
        if telecom:
            resource["telecom"] = telecom
        if "postalCode" in values:
            resource["address"] = [{"postalCode": values["postalCode"]}]

    def _assemble_observation(self, resource: dict[str, Any]) -> None:
        values = self.values
        resource["status"] = "final"
        if "subject" in values:
            patient_system = self.mapping.record_types["Patient"].identifier_system
            resource["subject"] = {
                "identifier": {"system": patient_system, "value": values["subject"]}
            }
        if "effectiveDateTime" in values:
            resource["effectiveDateTime"] = values["effectiveDateTime"]
        concept: units.Concept | None = values.get("concept")
        if concept is not None:
            resource["code"] = {
                "coding": [
                    {"system": units.LOINC, "code": concept.loinc, "display": concept.display}
                ],
                "text": concept.display,
            }
        if concept is None or "value" not in values or "unit" not in values:
            return
        try:
            quantity = units.normalize_quantity(concept, values["value"], values["unit"])
        except NormalizationError as exc:
            self.errors.append(FieldError("unit", exc.code, exc.message, self._source_of("unit")))
            return
        if quantity.conversion is not None:
            conversion = quantity.conversion
            self.transformations.append(
                Transformation(
                    "valueQuantity",
                    None,
                    conversion.rule,
                    {"value": values["value"], "unit": values["unit"]},
                    {"value": quantity.value, "unit": quantity.unit},
                    {
                        "factor": str(conversion.factor),
                        "operation": conversion.operation,
                        "step": str(concept.step),
                    },
                )
            )
        resource["valueQuantity"] = {
            "value": quantity.value,
            "unit": quantity.unit,
            "system": units.UCUM,
            "code": quantity.unit,
        }

    def _source_of(self, target: str) -> str | None:
        return self.rules.target_fields().get(target)
