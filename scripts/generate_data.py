"""Synthetic patients and observations for sources A, B and C. Deterministic for a seed.

    python -m scripts.generate_data --patients 300 --bad 5 --out data

Every patient appears in source A; about 60% also appear in B and 30% in C, each written
the way that source writes things (date order, name order, units, code tables, time
zone). What the data exercises:

- Matching: B rows sometimes use a nickname, an upper-case surname, or a birth date with
  day and month swapped (linked, plus a conflict finding). Some have a dropped letter in
  the surname, a new postal code or no phone, which should send them to review.
- Namesakes: extra people in C share a name, birth date and sex with someone in A. With
  different national ids they must become POSSIBLE_NAMESAKE findings, otherwise review;
  never a match.
- Duplicate measurements: A and B both record the same glucose test at the same moment,
  in their own local times. Only correct UTC conversion reveals they are one measurement.
- Defects: `--bad` rows per file break in ways the pipeline must quarantine.

Identifiers are fake by construction: SSNs start with 9 (never issued), emails use the
reserved example.org domain, phone numbers use the 555 exchange.
"""

import argparse
import csv
import io
import random
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import TypedDict
from zoneinfo import ZoneInfo

GIVEN = [
    ("Robert", "male"),
    ("William", "male"),
    ("James", "male"),
    ("Michael", "male"),
    ("Thomas", "male"),
    ("Daniel", "male"),
    ("Aarav", "male"),
    ("Rohan", "male"),
    ("Arjun", "male"),
    ("Mateo", "male"),
    ("Kwame", "male"),
    ("Liam", "male"),
    ("Elizabeth", "female"),
    ("Margaret", "female"),
    ("Catherine", "female"),
    ("Susan", "female"),
    ("Patricia", "female"),
    ("Priya", "female"),
    ("Ananya", "female"),
    ("Sofía", "female"),
    ("Zoë", "female"),
    ("Chloé", "female"),
    ("Amara", "female"),
    ("Mei", "female"),
]
FAMILY = [
    "Miller",
    "Sharma",
    "Patel",
    "O'Brien",
    "Muñoz",
    "Nguyen",
    "Okafor",
    "Kowalski",
    "Smith",
    "Johnson",
    "Brown",
    "Iyer",
    "Reddy",
    "García",
    "Müller",
    "Rossi",
    "Kim",
    "Haddad",
    "Andersson",
    "Novak",
    "Mensah",
    "Chen",
    "Silva",
    "Fischer",
]
NICKNAMES = {
    "Robert": "Bob",
    "William": "Bill",
    "James": "Jim",
    "Michael": "Mike",
    "Thomas": "Tom",
    "Daniel": "Dan",
    "Elizabeth": "Liz",
    "Margaret": "Maggie",
    "Catherine": "Kate",
    "Susan": "Sue",
    "Patricia": "Pat",
}
ZONES = {
    "source-a": ZoneInfo("America/New_York"),
    "source-b": ZoneInfo("America/Chicago"),
    "source-c": ZoneInfo("America/Los_Angeles"),
}
LB_PER_KG = Decimal("0.45359237")
MGDL_PER_MMOLL = Decimal("18.016")
FIRST_TEST = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
NAMESAKE_NUMBER_START = 900_000


@dataclass(frozen=True)
class Person:
    number: int
    given: str
    family: str
    gender: str
    birth_date: date
    phone: str
    postal_code: str
    ssn: str
    email: str
    member_id: str
    weight_kg: Decimal
    height_cm: Decimal
    glucose_mmol: Decimal

    @property
    def glucose_at(self) -> datetime:
        return FIRST_TEST + timedelta(days=self.number % 30, minutes=self.number % 600)


def _ascii(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if c.isascii() and c.isalpha()).lower()


class Identity(TypedDict):
    phone: str
    postal_code: str
    ssn: str
    email: str
    member_id: str


def _identity(number: int, given: str, family: str, rng: random.Random) -> Identity:
    return {
        "phone": f"555{rng.randrange(1000000, 9999999)}",
        "postal_code": f"{rng.randrange(10000, 99999)}",
        "ssn": f"9{rng.randrange(10, 99)}-{rng.randrange(10, 99)}-{rng.randrange(1000, 9999)}",
        "email": f"{_ascii(given)}.{_ascii(family)}{number}@example.org",
        "member_id": f"BR{rng.randrange(10_000_000, 99_999_999)}",
    }


def make_people(count: int, rng: random.Random) -> list[Person]:
    people = []
    for number in range(1, count + 1):
        given, gender = rng.choice(GIVEN)
        family = rng.choice(FAMILY)
        people.append(
            Person(
                number=number,
                given=given,
                family=family,
                gender=gender,
                birth_date=date(1940, 1, 1) + timedelta(days=rng.randrange(0, 365 * 75)),
                weight_kg=Decimal(rng.randrange(450, 1100)) / 10,
                height_cm=Decimal(rng.randrange(1500, 1950)) / 10,
                glucose_mmol=Decimal(rng.randrange(40, 110)) / 10,
                **_identity(number, given, family, rng),
            )
        )
    return people


def make_namesakes(people: list[Person], count: int, rng: random.Random) -> list[Person]:
    """Different people who share a name, birth date and sex with someone in the list."""
    namesakes = []
    for index, twin in enumerate(rng.sample(people, min(count, len(people)))):
        number = NAMESAKE_NUMBER_START + index
        namesakes.append(
            replace(twin, number=number, **_identity(number, twin.given, twin.family, rng))
        )
    return namesakes


def _swap_day_month(value: date) -> date | None:
    if value.day > 12 or value.day == value.month:
        return None
    return value.replace(month=value.day, day=value.month)


def source_a_patient(p: Person, rng: random.Random) -> dict[str, str]:
    name = f"{p.given} {p.family}"
    return {
        "mrn": f"{p.number:07d}",
        "patient_name": name.upper() if rng.random() < 0.3 else name,
        "dob": p.birth_date.strftime("%m/%d/%Y"),
        "sex": "M" if p.gender == "male" else "F",
        "ssn": p.ssn if rng.random() < 0.7 else "",
        "phone": f"({p.phone[:3]}) {p.phone[3:6]}-{p.phone[6:]}",
        "email": p.email if rng.random() < 0.6 else "",
        "zip": p.postal_code,
    }


def _typo(name: str, rng: random.Random) -> str:
    position = rng.randrange(1, len(name) - 1)
    return name[:position] + name[position + 1 :]


def source_b_patient(p: Person, rng: random.Random) -> dict[str, str]:
    birth_date = p.birth_date
    if rng.random() < 0.1:
        birth_date = _swap_day_month(birth_date) or birth_date
    given = NICKNAMES.get(p.given, p.given) if rng.random() < 0.3 else p.given
    # Real-world noise: a dropped letter, a move since the last visit, no phone on file.
    family = _typo(p.family, rng) if rng.random() < 0.05 and len(p.family) > 5 else p.family
    postal_code = f"{rng.randrange(10000, 99999)}" if rng.random() < 0.08 else p.postal_code
    has_phone = rng.random() >= 0.1
    return {
        "patient_id": f"B-{p.number:05d}",
        "first_name": given,
        "last_name": family.upper() if rng.random() < 0.5 else family,
        "date_of_birth": birth_date.strftime("%d-%m-%Y"),
        "gender": p.gender.title(),
        "member_id": p.member_id,
        "mobile": f"+1 {p.phone[:3]}-{p.phone[3:6]}-{p.phone[6:]}" if has_phone else "",
        "email": p.email if rng.random() < 0.4 else "",
        "pincode": postal_code,
    }


def source_c_patient(p: Person, rng: random.Random) -> dict[str, str]:
    day_first = p.birth_date.day > 12 and rng.random() < 0.3
    return {
        "hospital_no": f"C{p.number:06d}",
        "full_name": f"{p.family.upper()}, {p.given}",
        "birth_date": p.birth_date.strftime("%d/%m/%Y" if day_first else "%Y-%m-%d"),
        "sex_code": "1" if p.gender == "male" else "2",
        "ssn": p.ssn if rng.random() < 0.9 else "",
        "tel": f"{p.phone[:3]} {p.phone[3:6]} {p.phone[6:]}",
        "postcode": p.postal_code,
    }


def _local(source: str, moment: datetime, fmt: str) -> str:
    return moment.astimezone(ZONES[source]).strftime(fmt)


def _quantize(value: Decimal, step: str) -> str:
    return str(value.quantize(Decimal(step), rounding=ROUND_HALF_UP))


def source_a_observations(p: Person) -> list[dict[str, str]]:
    fmt = "%m/%d/%Y %H:%M"
    return [
        {
            "obs_id": f"A-{p.number}-WT",
            "mrn": f"{p.number:07d}",
            "test": "WT",
            "units": "lbs",
            "result": _quantize(p.weight_kg / LB_PER_KG, "0.1"),
            "taken_at": _local("source-a", p.glucose_at + timedelta(hours=1), fmt),
        },
        {
            "obs_id": f"A-{p.number}-GLU",
            "mrn": f"{p.number:07d}",
            "test": "GLU",
            "units": "mg/dL",
            "result": _quantize(p.glucose_mmol * MGDL_PER_MMOLL, "1"),
            "taken_at": _local("source-a", p.glucose_at, fmt),
        },
    ]


def source_b_observations(p: Person) -> list[dict[str, str]]:
    fmt = "%Y-%m-%dT%H:%M:%S"
    return [
        {
            "result_id": f"B-{p.number}-1",
            "patient_id": f"B-{p.number:05d}",
            "test_code": "BODYWT",
            "uom": "kg",
            "value": str(p.weight_kg),
            "collected_at": _local("source-b", p.glucose_at + timedelta(hours=2), fmt),
        },
        # The same glucose test as source A's, at the same moment in Chicago local time.
        {
            "result_id": f"B-{p.number}-2",
            "patient_id": f"B-{p.number:05d}",
            "test_code": "FBS",
            "uom": "mmol/L",
            "value": str(p.glucose_mmol),
            "collected_at": _local("source-b", p.glucose_at, fmt),
        },
    ]


def source_c_observations(p: Person) -> list[dict[str, str]]:
    fmt = "%Y-%m-%d %H:%M"
    at = _local("source-c", p.glucose_at + timedelta(hours=3), fmt)
    return [
        {
            "accession": f"C-{p.number}-W",
            "hospital_no": f"C{p.number:06d}",
            "loinc": "29463-7",
            "unit": "kg",
            "value": str(p.weight_kg),
            "observed_at": at,
        },
        {
            "accession": f"C-{p.number}-H",
            "hospital_no": f"C{p.number:06d}",
            "loinc": "8302-2",
            "unit": "cm",
            "value": str(p.height_cm),
            "observed_at": at,
        },
    ]


Defect = Callable[[dict[str, str]], None]

# Each breaks one row in a way the pipeline must quarantine with a specific reason.
PATIENT_DEFECTS: dict[str, list[Defect]] = {
    "source-a": [
        lambda r: r.update(dob=""),  # FIELD_MISSING
        lambda r: r.update(dob="31/02/1990"),  # DATE_UNPARSEABLE
        lambda r: r.update(dob="01/01/2090"),  # DATE_OUT_OF_RANGE
        lambda r: r.update(sex="X"),  # CODE_UNMAPPED
        lambda r: r.update(patient_name=""),  # FIELD_MISSING (name)
    ],
    "source-b": [
        lambda r: r.update(date_of_birth="1990-13-45"),  # DATE_UNPARSEABLE
        lambda r: r.update(gender="M"),  # CODE_UNMAPPED
        lambda r: r.update(date_of_birth=""),  # FIELD_MISSING
    ],
    "source-c": [
        lambda r: r.update(birth_date="03/04/1985"),  # DATE_AMBIGUOUS
        lambda r: r.update(sex_code="9"),  # CODE_UNMAPPED
        lambda r: r.update(birth_date="1880-01-01"),  # DATE_OUT_OF_RANGE
    ],
}
OBSERVATION_DEFECTS: dict[str, list[Defect]] = {
    "source-a": [
        lambda r: r.update(units="stone"),  # UNIT_UNKNOWN
        lambda r: r.update(result="high"),  # VALUE_NOT_NUMERIC
        lambda r: r.update(test="XYZ"),  # CODE_UNMAPPED
        lambda r: r.update(taken_at="yesterday"),  # TIMESTAMP_UNPARSEABLE
        lambda r: r.update(taken_at="03/08/2026 02:30"),  # TIMESTAMP_NONEXISTENT (DST gap)
    ],
    "source-b": [lambda r: r.update(uom="kg/m2"), lambda r: r.update(value="")],
    "source-c": [lambda r: r.update(unit="mmol/L"), lambda r: r.update(observed_at="")],
}

PATIENT_ROWS = {
    "source-a": source_a_patient,
    "source-b": source_b_patient,
    "source-c": source_c_patient,
}
OBSERVATION_ROWS = {
    "source-a": source_a_observations,
    "source-b": source_b_observations,
    "source-c": source_c_observations,
}
SHARE = {"source-a": 1.0, "source-b": 0.6, "source-c": 0.3}


def members(source: str, people: list[Person], seed: int) -> list[Person]:
    """Who this source has seen. A separate generator keeps it stable across files."""
    rng = random.Random(f"{seed}-{source}-members")
    return [person for person in people if rng.random() < SHARE[source]]


def _break_rows(
    rows: list[dict[str, str]], defects: list[Defect], count: int, rng: random.Random
) -> None:
    positions = sorted(rng.sample(range(len(rows)), min(count, len(rows))))
    for index, position in enumerate(positions):
        defects[index % len(defects)](rows[position])


def patient_rows(
    source: str, people: list[Person], bad: int, seed: int, extra: list[Person] | None = None
) -> list[dict[str, str]]:
    rng = random.Random(f"{seed}-{source}-patients")
    rows = [
        PATIENT_ROWS[source](person, rng)
        for person in members(source, people, seed) + (extra or [])
    ]
    _break_rows(rows, PATIENT_DEFECTS[source], bad, rng)
    return rows


def observation_rows(
    source: str, people: list[Person], bad: int, seed: int
) -> list[dict[str, str]]:
    rng = random.Random(f"{seed}-{source}-observations")
    rows = [
        row for person in members(source, people, seed) for row in OBSERVATION_ROWS[source](person)
    ]
    _break_rows(rows, OBSERVATION_DEFECTS[source], bad, rng)
    return rows


def to_csv(rows: list[dict[str, str]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


@dataclass(frozen=True)
class Dataset:
    people: list[Person]
    namesakes: list[Person]
    files: dict[str, list[dict[str, str]]]


def generate(patients: int, bad: int, seed: int, namesakes: int = 5) -> Dataset:
    rng = random.Random(seed)
    people = make_people(patients, rng)
    lookalikes = make_namesakes(people, namesakes, rng)
    files: dict[str, list[dict[str, str]]] = {}
    for source in PATIENT_ROWS:
        extra = lookalikes if source == "source-c" else None
        files[f"{source}-patients.csv"] = patient_rows(source, people, bad, seed, extra)
        files[f"{source}-observations.csv"] = observation_rows(source, people, bad, seed)
    return Dataset(people, lookalikes, files)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--patients", type=int, default=300)
    parser.add_argument("--bad", type=int, default=5, help="defective rows per file")
    parser.add_argument("--namesakes", type=int, default=5)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", type=Path, default=Path("data"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    dataset = generate(args.patients, args.bad, args.seed, args.namesakes)
    for name, rows in dataset.files.items():
        (args.out / name).write_text(to_csv(rows), encoding="utf-8", newline="")
        print(f"wrote {args.out / name} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
