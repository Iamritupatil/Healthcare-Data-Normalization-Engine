"""Measurements: concepts, units and versioned conversion factors.

All arithmetic uses `Decimal`. Values already in the canonical unit keep the sender's
precision; converted values are rounded once, at the end, to the concept's declared step.

Converting glucose from mg/dL to mmol/L also changes the LOINC code, because LOINC
codes encode the property being measured (mass concentration vs. substance concentration).
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final, Literal

from app.pipeline.core import NormalizationError, ReasonCode, Rule

LOINC: Final = "http://loinc.org"
UCUM: Final = "http://unitsofmeasure.org"

DECIMAL_RULE = Rule("decimal.parse", 1)
UNIT_RULE = Rule("unit.alias", 1)
CONCEPT_RULE = Rule("concept.map", 1)


@dataclass(frozen=True)
class Concept:
    key: str
    loinc: str
    display: str
    unit: str
    step: Decimal


CONCEPTS: dict[str, Concept] = {
    concept.key: concept
    for concept in (
        Concept("body-weight", "29463-7", "Body weight", "kg", Decimal("0.01")),
        Concept("body-height", "8302-2", "Body height", "cm", Decimal("0.1")),
        Concept(
            "glucose",
            "14749-6",
            "Glucose [Moles/volume] in Serum or Plasma",
            "mmol/L",
            Decimal("0.1"),
        ),
        Concept("heart-rate", "8867-4", "Heart rate", "/min", Decimal("1")),
    )
}

# Spellings seen in source systems, mapped to UCUM codes. Lookups are case-insensitive,
# except that UCUM itself is case-sensitive, so we only fold the alias, never the result.
UNIT_ALIASES: dict[str, str] = {
    "kg": "kg",
    "kgs": "kg",
    "kilogram": "kg",
    "kilograms": "kg",
    "g": "g",
    "lb": "[lb_av]",
    "lbs": "[lb_av]",
    "pound": "[lb_av]",
    "pounds": "[lb_av]",
    "[lb_av]": "[lb_av]",
    "cm": "cm",
    "m": "m",
    "in": "[in_i]",
    "inch": "[in_i]",
    "inches": "[in_i]",
    "[in_i]": "[in_i]",
    "mg/dl": "mg/dL",
    "mmol/l": "mmol/L",
    "/min": "/min",
    "bpm": "/min",
    "beats/min": "/min",
}


@dataclass(frozen=True)
class Conversion:
    rule: Rule
    factor: Decimal
    operation: Literal["multiply", "divide"]


CONVERSIONS: dict[tuple[str, str], Conversion] = {
    ("body-weight", "[lb_av]"): Conversion(
        Rule("unit.lb_av_to_kg", 1), Decimal("0.45359237"), "multiply"
    ),
    ("body-weight", "g"): Conversion(Rule("unit.g_to_kg", 1), Decimal("0.001"), "multiply"),
    ("body-height", "[in_i]"): Conversion(Rule("unit.in_i_to_cm", 1), Decimal("2.54"), "multiply"),
    ("body-height", "m"): Conversion(Rule("unit.m_to_cm", 1), Decimal("100"), "multiply"),
    # Molar mass of glucose is 180.16 g/mol, so 1 mmol/L = 18.016 mg/dL.
    ("glucose", "mg/dL"): Conversion(
        Rule("unit.glucose_mgdl_to_mmoll", 1), Decimal("18.016"), "divide"
    ),
}


@dataclass(frozen=True)
class Quantity:
    value: Decimal
    unit: str
    conversion: Conversion | None


def parse_decimal(raw: Any) -> Decimal:
    if isinstance(raw, bool):
        raise NormalizationError(ReasonCode.VALUE_NOT_NUMERIC, "is a boolean, not a number")
    if isinstance(raw, Decimal | int):
        value = Decimal(raw)
    elif isinstance(raw, float):
        value = Decimal(repr(raw))
    elif isinstance(raw, str):
        try:
            value = Decimal(raw.strip())
        except InvalidOperation:
            raise NormalizationError(ReasonCode.VALUE_NOT_NUMERIC, "is not a number") from None
    else:
        raise NormalizationError(ReasonCode.VALUE_NOT_NUMERIC, "is not a number")
    if not value.is_finite():
        raise NormalizationError(ReasonCode.VALUE_NOT_NUMERIC, "is not a finite number")
    return value


def canonical_unit(raw: str) -> str:
    unit = UNIT_ALIASES.get(raw.strip().casefold())
    if unit is None:
        raise NormalizationError(ReasonCode.UNIT_UNKNOWN, "unit is not recognised")
    return unit


def normalize_quantity(concept: Concept, value: Decimal, unit: str) -> Quantity:
    if unit == concept.unit:
        return Quantity(value, unit, None)
    conversion = CONVERSIONS.get((concept.key, unit))
    if conversion is None:
        raise NormalizationError(
            ReasonCode.UNIT_INCOMPATIBLE,
            f"cannot convert {unit} to {concept.unit} for {concept.key}",
        )
    if conversion.operation == "multiply":
        converted = value * conversion.factor
    else:
        converted = value / conversion.factor
    return Quantity(
        converted.quantize(concept.step, rounding=ROUND_HALF_UP), concept.unit, conversion
    )
