"""Stage 5: patient matching.

Linking two different people mixes their medical histories, which is far worse than
leaving a duplicate, so the rules are deliberately cautious:

1. Only pairs that share a birth year or the first three letters of the family name are
   compared (blocking).
2. `compare` turns a pair into comparison facts: one outcome per field (agree, disagree,
   missing, ...). Facts never contain the values themselves.
3. `score` adds points per field. Agreement adds, disagreement subtracts, missing adds
   nothing: hospitals collect different fields, so absence is not evidence. Names that
   are common in the org earn fewer points.
4. `decide` applies the strong-identifier rule: a pair can only head towards an automatic
   match if it agrees on a national id, insurance member number, phone or email, and no
   name disagrees. Everything else above the review threshold waits for a human.
5. A pair that passes goes to a verifier (verify.py) that can only veto.

Points:
    family name      agree 3 (fewer if common), one typo apart 1, disagree -1
    given name       agree or known nickname 2 (fewer if common), disagree -1
    birth date       agree 4, day and month swapped 2, disagree -2
    gender           agree 1, disagree -1
    postal code      agree 1
    phone, email     agree 3 each
    national id      agree 6, disagree -10
    insurance id     agree 4, disagree -2
"""

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from app.pipeline.normalize.names import match_key

MATCH_RULE_VERSION = 2
MATCH_THRESHOLD = 9
REVIEW_THRESHOLD = 5
# Name frequencies are shares of the org's patients, with a floor on the population so a
# small org does not treat every name as common.
MIN_POPULATION = 200

STRONG_IDENTIFIERS = ("national_id", "insurance_member_id", "phone", "email")

Decision = Literal["no_match", "review", "pending_verification"]
Outcome = Literal["agree", "disagree", "missing"]

_NICKNAMES_FILE = Path(__file__).parent / "data" / "nicknames.yaml"


@lru_cache(maxsize=1)
def _nickname_index() -> dict[str, str]:
    with _NICKNAMES_FILE.open(encoding="utf-8") as handle:
        table: dict[str, list[str]] = yaml.safe_load(handle)
    index: dict[str, str] = {}
    for formal, nicknames in table.items():
        index.setdefault(match_key(formal), match_key(formal))
        for nickname in nicknames:
            index.setdefault(match_key(nickname), match_key(formal))
    return index


def canonical_given(name: str) -> str:
    key = match_key(name)
    return _nickname_index().get(key, key)


def _identifier(resource: dict[str, Any], id_type: str) -> tuple[str, str] | None:
    for identifier in resource.get("identifier", []):
        if identifier.get("type") == id_type:
            return identifier["system"], identifier["value"]
    return None


def _contact(resource: dict[str, Any], system: str) -> str | None:
    values = [t["value"] for t in resource.get("telecom", []) if t.get("system") == system]
    return values[0] if values else None


@dataclass(frozen=True)
class PatientFeatures:
    """Matching keys taken from a canonical Patient. Used in memory only, never stored."""

    family: str
    given: str
    middle_initial: str | None
    birth_date: date | None
    gender: str | None
    postal_code: str | None
    phone: str | None
    email: str | None
    national_id: tuple[str, str] | None
    insurance_member_id: tuple[str, str] | None

    @classmethod
    def from_resource(cls, resource: dict[str, Any]) -> "PatientFeatures":
        name = resource.get("name", {})
        given_names: list[str] = name.get("given") or []
        family = name.get("family")
        if family is None and not given_names:
            # Name kept as text only: best effort, last token as the family name.
            tokens = name.get("text", "").split()
            family = tokens[-1] if tokens else ""
            given_names = tokens[:-1]
        birth_date = resource.get("birthDate")
        if isinstance(birth_date, str):
            birth_date = date.fromisoformat(birth_date)
        phone = _contact(resource, "phone")
        postal_codes = [a["postalCode"] for a in resource.get("address", []) if a.get("postalCode")]
        gender = resource.get("gender")
        return cls(
            family=match_key(family or ""),
            given=match_key(given_names[0]) if given_names else "",
            middle_initial=match_key(given_names[1])[:1] or None if len(given_names) > 1 else None,
            birth_date=birth_date,
            gender=gender if gender in {"male", "female", "other"} else None,
            postal_code=postal_codes[0].replace(" ", "") if postal_codes else None,
            phone="".join(ch for ch in phone if ch.isdigit())[-10:] if phone else None,
            email=_contact(resource, "email"),
            national_id=_identifier(resource, "NI"),
            insurance_member_id=_identifier(resource, "MB"),
        )

    @property
    def birth_year(self) -> int | None:
        return self.birth_date.year if self.birth_date else None

    @property
    def family_prefix(self) -> str | None:
        return self.family[:3] if len(self.family) >= 3 else None

    @property
    def given_key(self) -> str | None:
        return canonical_given(self.given) if self.given else None


@dataclass(frozen=True)
class NameFrequency:
    """Share of the org's patients with this family / given name (None when unknown)."""

    family: float | None = None
    given: float | None = None


class ComparisonFacts(BaseModel):
    """What a pair has in common, field by field. Contains no names, dates or numbers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    family_name: Literal["agree", "close", "disagree", "missing"]
    family_name_frequency: float | None = None
    given_name: Literal["agree", "nickname", "disagree", "missing"]
    given_name_frequency: float | None = None
    middle_initial: Outcome
    birth_date: Literal["agree", "swapped", "disagree", "missing"]
    gender: Outcome
    postal_code: Outcome
    phone: Outcome
    email: Outcome
    national_id: Outcome
    insurance_member_id: Outcome


FACT_FIELDS = frozenset(ComparisonFacts.model_fields)


def _one_edit_apart(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1 or a == b:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b, strict=True)) == 1
    shorter, longer = sorted((a, b), key=len)
    return any(longer[:i] + longer[i + 1 :] == shorter for i in range(len(longer)))


def _swapped(a: date, b: date) -> bool:
    return a.year == b.year and a.month == b.day and a.day == b.month and a != b


def _outcome(a: object | None, b: object | None) -> Outcome:
    if a is None or b is None:
        return "missing"
    return "agree" if a == b else "disagree"


def _id_outcome(a: tuple[str, str] | None, b: tuple[str, str] | None) -> Outcome:
    # Numbers from different issuing systems cannot be compared.
    if a is None or b is None or a[0] != b[0]:
        return "missing"
    return "agree" if a[1] == b[1] else "disagree"


def compare(
    a: PatientFeatures, b: PatientFeatures, frequency: NameFrequency | None = None
) -> ComparisonFacts:
    frequency = frequency or NameFrequency()
    if not a.family or not b.family:
        family = "missing"
    elif a.family == b.family:
        family = "agree"
    elif min(len(a.family), len(b.family)) >= 5 and _one_edit_apart(a.family, b.family):
        family = "close"
    else:
        family = "disagree"

    if not a.given or not b.given:
        given = "missing"
    elif a.given == b.given:
        given = "agree"
    elif canonical_given(a.given) == canonical_given(b.given):
        given = "nickname"
    else:
        given = "disagree"

    if a.birth_date is None or b.birth_date is None:
        birth = "missing"
    elif a.birth_date == b.birth_date:
        birth = "agree"
    elif _swapped(a.birth_date, b.birth_date):
        birth = "swapped"
    else:
        birth = "disagree"

    return ComparisonFacts(
        family_name=family,
        family_name_frequency=frequency.family if family == "agree" else None,
        given_name=given,
        given_name_frequency=frequency.given if given in {"agree", "nickname"} else None,
        middle_initial=_outcome(a.middle_initial, b.middle_initial),
        birth_date=birth,
        gender=_outcome(a.gender, b.gender),
        postal_code=_outcome(a.postal_code, b.postal_code),
        phone=_outcome(a.phone, b.phone),
        email=_outcome(a.email, b.email),
        national_id=_id_outcome(a.national_id, b.national_id),
        insurance_member_id=_id_outcome(a.insurance_member_id, b.insurance_member_id),
    )


def name_points(full: int, frequency: float | None) -> int:
    """Full points for a rare name, fewer for one shared by 1% or 5% of patients."""
    if frequency is None or frequency < 0.01:
        return full
    if frequency < 0.05:
        return full - 1
    return max(1, full - 2)


_FIXED_POINTS: dict[str, dict[str, int]] = {
    "family_name": {"close": 1, "disagree": -1},
    "given_name": {"disagree": -1},
    "birth_date": {"agree": 4, "swapped": 2, "disagree": -2},
    "gender": {"agree": 1, "disagree": -1},
    "postal_code": {"agree": 1},
    "phone": {"agree": 3},
    "email": {"agree": 3},
    "national_id": {"agree": 6, "disagree": -10},
    "insurance_member_id": {"agree": 4, "disagree": -2},
}


@dataclass(frozen=True)
class MatchScore:
    points: int
    breakdown: dict[str, int]


def score(facts: ComparisonFacts) -> MatchScore:
    breakdown: dict[str, int] = {}
    if facts.family_name == "agree":
        breakdown["family_name"] = name_points(3, facts.family_name_frequency)
    if facts.given_name in {"agree", "nickname"}:
        breakdown["given_name"] = name_points(2, facts.given_name_frequency)
    for field, table in _FIXED_POINTS.items():
        points = table.get(getattr(facts, field))
        if points is not None:
            breakdown[field] = points
    return MatchScore(sum(breakdown.values()), breakdown)


def decide(result: MatchScore, facts: ComparisonFacts) -> Decision:
    """The one place that decides what a score means. Demographics alone never auto-match."""
    if result.points < REVIEW_THRESHOLD:
        return "no_match"
    strong = any(getattr(facts, field) == "agree" for field in STRONG_IDENTIFIERS)
    # Twins share a surname, birth date, phone and address; a different given name stops them.
    names_disagree = "disagree" in (facts.family_name, facts.given_name)
    if result.points >= MATCH_THRESHOLD and strong and not names_disagree:
        return "pending_verification"
    return "review"


def same_name_and_birth_date(facts: ComparisonFacts) -> bool:
    return (
        facts.family_name == "agree"
        and facts.given_name in {"agree", "nickname"}
        and facts.birth_date == "agree"
    )


def is_namesake(facts: ComparisonFacts) -> bool:
    """Same name and birth date, but national ids prove they are different people."""
    return same_name_and_birth_date(facts) and facts.national_id == "disagree"


@dataclass(frozen=True)
class Conflict:
    field: str
    value: Any
    other_value: Any


def find_conflicts(resource: dict[str, Any], other: dict[str, Any]) -> list[Conflict]:
    """Fields that disagree between two records already judged to be the same patient."""
    conflicts: list[Conflict] = []
    for field in ("birthDate", "gender"):
        value, other_value = resource.get(field), other.get(field)
        if value and other_value and str(value) != str(other_value):
            conflicts.append(Conflict(field, value, other_value))
    family = resource.get("name", {}).get("family")
    other_family = other.get("name", {}).get("family")
    if family and other_family and match_key(family) != match_key(other_family):
        conflicts.append(Conflict("name.family", family, other_family))
    return conflicts
