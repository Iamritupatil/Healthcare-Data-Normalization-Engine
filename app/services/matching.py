"""Matching in the database: candidates, links, verdicts, human decisions and findings.

A link's status says what the system currently believes about a pair:
    pending_verification  passed the strong-identifier rule; waiting for the verifier
    review                needs a human
    match                 the same person (verifier agreed, or a human said so)
    different             a human said they are different people
Only 'match' links make records count as one person, anywhere in the system.
"""

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, text, tuple_
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.models.tables import Finding, IngestJob, MatchLink, Record
from app.pipeline import match
from app.pipeline.verify import VerificationResult
from app.services.records import is_current_version, same_source_record

NAMESAKE = "POSSIBLE_NAMESAKE"
DUPLICATE_RECORD = "DUPLICATE_RECORD"

# Follow 'match' links in both directions. UNION (not UNION ALL) stops at records already seen.
_MATCH_GROUP = text(
    """
    WITH RECURSIVE grp(id) AS (
        SELECT CAST(:record_id AS uuid)
        UNION
        SELECT CASE WHEN l.record_id = g.id THEN l.candidate_id ELSE l.record_id END
          FROM match_links AS l
          JOIN grp AS g ON l.record_id = g.id OR l.candidate_id = g.id
         WHERE l.status = 'match'
    )
    SELECT id FROM grp
    """
)


def match_group_ids(session: Session, record_id: UUID) -> list[UUID]:
    return list(session.scalars(_MATCH_GROUP, {"record_id": record_id}))


def name_frequency(
    session: Session, record: Record, features: match.PatientFeatures
) -> match.NameFrequency:
    """Share of the org's other patient records with the same family / given name.

    Counts are cached for the transaction (one upload chunk): close enough for scoring,
    and it avoids counting the same common names again for every row.
    """
    cache: dict[tuple[str, str], int] = session.info.setdefault("name_counts", {})
    if "patient_total" not in session.info:
        session.info["patient_total"] = session.scalar(
            select(func.count()).select_from(Record).where(Record.record_type == "Patient")
        )
    population = max(session.info["patient_total"] or 0, match.MIN_POPULATION)

    def share(column: InstrumentedAttribute[str | None], key: str | None) -> float | None:
        if not key:
            return None
        if (column.key, key) not in cache:
            cache[(column.key, key)] = (
                session.scalar(
                    select(func.count())
                    .select_from(Record)
                    .where(Record.record_type == "Patient", Record.id != record.id, column == key)
                )
                or 0
            )
        return round(cache[(column.key, key)] / population, 4)

    return match.NameFrequency(
        family=share(Record.family_key, features.family),
        given=share(Record.given_key, features.given_key),
    )


def match_patient(
    session: Session, org_id: UUID, record: Record, features: match.PatientFeatures
) -> list[UUID]:
    """Compare a new patient record with its block. Returns links waiting for the verifier."""
    blocks = []
    if features.birth_year is not None:
        blocks.append(Record.birth_year == features.birth_year)
    if features.family_prefix is not None:
        blocks.append(Record.family_prefix == features.family_prefix)
    if not blocks:
        return []
    candidates = session.scalars(
        select(Record).where(
            Record.record_type == "Patient",
            Record.id != record.id,
            or_(*blocks),
            ~same_source_record(Record, record.source, record.record_type, record.source_record_id),
            is_current_version(),
        )
    ).all()
    frequency = name_frequency(session, record, features)
    pending: list[UUID] = []
    for candidate in candidates:
        facts = match.compare(
            features, match.PatientFeatures.from_resource(candidate.resource), frequency
        )
        result = match.score(facts)
        decision = match.decide(result, facts)
        if decision == "no_match":
            if match.is_namesake(facts):
                add_namesake_finding(
                    session, org_id, record.id, candidate.id, "national ids differ"
                )
            continue
        link = MatchLink(
            id=uuid4(),
            org_id=org_id,
            record_id=record.id,
            candidate_id=candidate.id,
            points=result.points,
            breakdown=result.breakdown,
            facts=facts.model_dump(),
            status=decision,
            rule_version=match.MATCH_RULE_VERSION,
        )
        session.add(link)
        if decision == "pending_verification":
            pending.append(link.id)
    return pending


def enqueue_verification(
    session: Session, org_id: UUID, link_ids: list[UUID], created_by: UUID | None = None
) -> UUID:
    job = IngestJob(
        id=uuid4(),
        org_id=org_id,
        kind="verify",
        params={"link_ids": [str(link_id) for link_id in link_ids]},
        status="pending",
        total_rows=len(link_ids),
        created_by=created_by,
    )
    session.add(job)
    return job.id


def _pair_has_finding(session: Session, code: str, record_id: UUID, other_id: UUID) -> bool:
    either_way = or_(
        and_(Finding.record_id == record_id, Finding.other_record_id == other_id),
        and_(Finding.record_id == other_id, Finding.other_record_id == record_id),
    )
    return (
        session.scalar(select(Finding.id).where(Finding.code == code, either_way).limit(1))
        is not None
    )


def add_namesake_finding(
    session: Session, org_id: UUID, record_id: UUID, other_id: UUID, reason: str
) -> None:
    if _pair_has_finding(session, NAMESAKE, record_id, other_id):
        return
    session.add(
        Finding(
            id=uuid4(),
            org_id=org_id,
            kind="warning",
            code=NAMESAKE,
            record_id=record_id,
            other_record_id=other_id,
            details={"reason": f"same name and birth date, but {reason}"},
        )
    )


def _add_conflicts(session: Session, org_id: UUID, link: MatchLink) -> None:
    record = session.get_one(Record, link.record_id)
    candidate = session.get_one(Record, link.candidate_id)
    for conflict in match.find_conflicts(record.resource, candidate.resource):
        code = f"CONFLICT_{conflict.field.replace('.', '_').upper()}"
        if _pair_has_finding(session, code, record.id, candidate.id):
            continue
        session.add(
            Finding(
                id=uuid4(),
                org_id=org_id,
                kind="conflict",
                code=code,
                record_id=record.id,
                other_record_id=candidate.id,
                field=conflict.field,
                details={
                    "value": conflict.value,
                    "source": record.source,
                    "other_value": conflict.other_value,
                    "other_source": candidate.source,
                },
            )
        )


def apply_verdict(session: Session, link: MatchLink, result: VerificationResult) -> None:
    """The verifier can confirm a pending pair or send it to review; nothing else."""
    if link.status != "pending_verification":
        return
    link.verifier = result.verifier
    link.verdict = result.as_dict()
    link.decided_by = "verifier"
    link.decided_at = datetime.now(UTC)
    link.status = "match" if result.confirmed else "review"
    if link.status == "match":
        _add_conflicts(session, link.org_id, link)


def record_human_decision(
    session: Session, link: MatchLink, decision: Literal["same", "different"]
) -> None:
    link.status = "match" if decision == "same" else "different"
    link.decided_by = "human"
    link.decided_at = datetime.now(UTC)
    if decision == "same":
        _add_conflicts(session, link.org_id, link)
    elif match.same_name_and_birth_date(match.ComparisonFacts.model_validate(link.facts)):
        add_namesake_finding(
            session,
            link.org_id,
            link.record_id,
            link.candidate_id,
            "a reviewer decided they differ",
        )


def find_duplicate_measurement(
    session: Session, source: str, source_record_id: str, resource: dict[str, Any]
) -> Record | None:
    """The same measurement (patient, time, LOINC code) already stored, from any source.

    "Same patient" means the observation's subject, or any record matched to it.
    """
    subject = resource["subject"]["identifier"]["value"]
    owners = {(source, subject)}
    patient_id = session.scalar(
        select(Record.id)
        .where(
            Record.record_type == "Patient",
            Record.source == source,
            Record.source_record_id == subject,
        )
        .order_by(Record.received_at.desc())
        .limit(1)
    )
    if patient_id is not None:
        group = match_group_ids(session, patient_id)
        members = session.execute(
            select(Record.source, Record.source_record_id).where(Record.id.in_(group))
        )
        owners |= {(member_source, member_id) for member_source, member_id in members}
    return session.scalars(
        select(Record)
        .where(
            Record.record_type == "Observation",
            Record.code == resource["code"]["coding"][0]["code"],
            Record.effective_at == resource["effectiveDateTime"],
            tuple_(Record.source, Record.subject_key).in_(sorted(owners)),
            ~same_source_record(Record, source, "Observation", source_record_id),
        )
        .order_by(Record.received_at)
        .limit(1)
    ).first()


def add_duplicate_finding(
    session: Session,
    org_id: UUID,
    existing: Record,
    raw_record_id: UUID,
    source: str,
    source_record_id: str,
    resource: dict[str, Any],
) -> None:
    quantity = resource["valueQuantity"]
    existing_quantity = existing.resource["valueQuantity"]
    session.add(
        Finding(
            id=uuid4(),
            org_id=org_id,
            kind="warning",
            code=DUPLICATE_RECORD,
            record_id=existing.id,
            raw_record_id=raw_record_id,
            details={
                "source": source,
                "source_record_id": source_record_id,
                "value": quantity["value"],
                "unit": quantity["unit"],
                "existing_source": existing.source,
                "existing_value": existing_quantity["value"],
                "values_differ": quantity["value"] != existing_quantity["value"],
            },
        )
    )
