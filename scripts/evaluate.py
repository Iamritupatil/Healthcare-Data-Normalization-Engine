"""Evaluate normalization and matching against synthetic data with known answers.

    python -m scripts.evaluate --patients 500 --seed 7

The generator knows who every row really is, so this measures:
- No data loss: every input row ends normalized or rejected, never silently dropped.
- Normalization accuracy: normalized birth dates, sex, weights and glucose values equal
  the generator's ground truth (a golden set by construction).
- Matching: precision and recall of automatic matches over cross-record pairs, how many
  true pairs wait for review, and whether any namesake was ever auto-matched.

It runs the same pure pipeline and matching functions as the service, in memory, with
the rule verifier. No database is needed.
"""

import argparse
import re
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from itertools import combinations
from pathlib import Path
from typing import Any

from app.pipeline import match
from app.pipeline.mapping import RecordType, load_registry
from app.pipeline.normalize.names import match_key
from app.pipeline.run import Normalized, run_pipeline
from app.pipeline.verify import RuleVerifier, verify_safely
from scripts.generate_data import Person, generate

TODAY = date(2026, 10, 14)
_DIGITS = re.compile(r"\d+")


def person_number(source_record_id: str) -> int:
    """Source ids embed the generator's person number (B-00123, C000123, 0000123)."""
    found = _DIGITS.search(source_record_id)
    if found is None:
        raise ValueError(source_record_id)
    return int(found.group())


def evaluate(patients: int, bad: int, seed: int, namesakes: int) -> None:
    dataset = generate(patients, bad, seed, namesakes)
    truth: dict[int, Person] = {p.number: p for p in dataset.people + dataset.namesakes}
    registry = load_registry(Path("mappings"))

    rows_in = rejected = 0
    reasons: Counter[str] = Counter()
    checks: Counter[str] = Counter()
    misses: Counter[str] = Counter()
    normalized_patients: list[tuple[int, dict[str, Any]]] = []

    for filename, rows in dataset.files.items():
        source = filename.split("-patients")[0].split("-observations")[0]
        record_type: RecordType = "Patient" if "patients" in filename else "Observation"
        mapping = registry.active(source)
        for row in rows:
            rows_in += 1
            result = run_pipeline(mapping, record_type, row, today=TODAY)
            if not isinstance(result, Normalized):
                rejected += 1
                reasons.update(result.reason_codes)
                continue
            person = truth[person_number(result.source_record_id)]
            for name, ok in _check(record_type, result.resource, person).items():
                checks[name] += 1
                misses[name] += 0 if ok else 1
            if record_type == "Patient":
                normalized_patients.append((person.number, result.resource))

    normalized = rows_in - rejected
    print(f"Rows in: {rows_in}   normalized: {normalized}   rejected: {rejected}")
    print(f"No data loss: {'yes' if normalized + rejected == rows_in else 'NO'}")
    print(f"Rejected by reason: {dict(sorted(reasons.items()))}")
    print("\nNormalization accuracy against ground truth:")
    for name in sorted(checks):
        correct = checks[name] - misses[name]
        print(f"  {name:<14} {correct}/{checks[name]}  ({correct / checks[name]:.1%})")
    _evaluate_matching(normalized_patients, {p.number for p in dataset.namesakes})


def _check(record_type: str, resource: dict[str, Any], person: Person) -> dict[str, bool]:
    if record_type == "Patient":
        family = resource["name"].get("family") or ""
        return {
            "family name": match_key(family) == match_key(person.family),
            "sex": resource.get("gender") == person.gender,
            # B rows sometimes carry a deliberate day/month swap, which matching flags.
            "birth date": resource["birthDate"] == person.birth_date
            or resource["birthDate"]
            == person.birth_date.replace(month=person.birth_date.day, day=person.birth_date.month),
        }
    code = resource["code"]["coding"][0]["code"]
    value: Decimal = resource["valueQuantity"]["value"]
    if code == "29463-7":
        return {"weight (kg)": abs(value - person.weight_kg) <= Decimal("0.05")}
    if code == "14749-6":
        return {"glucose (mmol/L)": abs(value - person.glucose_mmol) <= Decimal("0.05")}
    return {"height (cm)": value == person.height_cm}


def _evaluate_matching(
    patients: list[tuple[int, dict[str, Any]]], namesake_numbers: set[int]
) -> None:
    features = [(number, match.PatientFeatures.from_resource(r)) for number, r in patients]
    family_counts = Counter(f.family for _, f in features)
    given_counts = Counter(f.given_key for _, f in features)
    population = max(len(features), match.MIN_POPULATION)

    blocks: dict[object, set[int]] = defaultdict(set)
    for index, (_, f) in enumerate(features):
        blocks[("year", f.birth_year)].add(index)
        blocks[("prefix", f.family_prefix)].add(index)
    pairs = {pair for members in blocks.values() for pair in combinations(sorted(members), 2)}

    outcome: Counter[str] = Counter()
    namesake_auto_matched = 0
    verifier = RuleVerifier()
    for i, j in pairs:
        (number_a, a), (number_b, b) = features[i], features[j]
        frequency = match.NameFrequency(
            family=(family_counts[a.family] - 1) / population,
            given=(given_counts[a.given_key] - 1) / population,
        )
        facts = match.compare(a, b, frequency)
        decision: str = match.decide(match.score(facts), facts)
        if decision == "pending_verification":
            confirmed = verify_safely(verifier, facts, timeout=5).confirmed
            decision = "match" if confirmed else "review"
        same = number_a == number_b
        outcome[f"{decision}:{'same' if same else 'different'}"] += 1
        involves_namesake = {number_a, number_b} & namesake_numbers
        if decision == "match" and not same and involves_namesake:
            namesake_auto_matched += 1

    true_pairs = sum(
        1 for i, j in combinations(range(len(features)), 2) if features[i][0] == features[j][0]
    )
    matched_same, matched_diff = outcome["match:same"], outcome["match:different"]
    precision = matched_same / (matched_same + matched_diff) if matched_same + matched_diff else 1.0
    recall = matched_same / true_pairs if true_pairs else 1.0
    found = (matched_same + outcome["review:same"]) / max(true_pairs, 1)
    print(
        f"\nMatching over {len(features)} normalized patient records ({len(pairs)} compared pairs):"
    )
    print(f"  true same-person pairs:      {true_pairs}")
    print(f"  auto-matched (correct):      {matched_same}")
    print(f"  auto-matched (wrong):        {matched_diff}")
    print(f"  sent to review (same/diff):  {outcome['review:same']}/{outcome['review:different']}")
    print(
        f"  precision {precision:.1%}   recall {recall:.1%}   "
        f"(recall counting review as found: {found:.1%})"
    )
    print(f"  namesakes auto-matched:      {namesake_auto_matched} (must be 0)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--patients", type=int, default=500)
    parser.add_argument("--bad", type=int, default=5)
    parser.add_argument("--namesakes", type=int, default=10)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    evaluate(args.patients, args.bad, args.seed, args.namesakes)


if __name__ == "__main__":
    main()
