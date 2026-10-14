# Medvora

A healthcare record normalization engine. Hospitals send patients and lab results in
their own formats (month-first or day-first dates, pounds or kilograms, `M`/`F` or
`1`/`2`, "Robert Miller" or "MILLER, Robert"). Medvora turns every record into one
canonical, FHIR-like shape, quarantines what it cannot trust (with the reasons), traces
every value back to what was received, and links records that describe the same patient
without ever merging two different people.

```text
            ┌──────────┐   ┌─────────┐   ┌───────────┐   ┌──────────┐   ┌─────────────┐
 record ──▶ │ 1 ingest │──▶│ 2 map   │──▶│ 3 normal- │──▶│ 4 valid- │──▶│ 6 store     │
 or CSV     │ raw +    │   │ YAML per│   │ ize dates,│   │ ate      │   │ + lineage   │
            │ hash     │   │ source  │   │ units,... │   │ Pydantic │   └──────┬──────┘
            └──────────┘   └─────────┘   └───────────┘   └────┬─────┘          │ patients
                 │ seen before? return                         │ fails          ▼
                 └─ the earlier result                         ▼          ┌─────────────┐
                                                        quarantine with   │ 5 match +   │
                                                        reason codes      │ verify (veto│
                                                                          │ only) ──▶ review queue
                                                                          └─────────────┘
```

## Quick start

Requires Docker with Compose.

```bash
cp .env.example .env        # then replace every change-me value (see the comment at the top)
docker compose up -d        # Postgres, migrations, API on http://localhost:8000, worker
docker compose run --rm tools python -m scripts.demo
```

The demo creates two fresh organisations and walks through every feature in about two
minutes: ingest, idempotency, matching across sources, conflicts, namesakes, the review
queue, quarantine and correction, unit and time-zone conversion, duplicate measurements,
lineage, a CSV upload, cursor paging, org isolation, roles and metrics.

Interactive API docs: <http://localhost:8000/docs> (use **Authorize** with an API key).

To get keys for your own calls:

```bash
docker compose run --rm tools python -m scripts.seed --name "My Hospital Network"
# prints MEDVORA_ADMIN_KEY=..., MEDVORA_INGEST_KEY=..., MEDVORA_READER_KEY=... (shown once)
```

## API

Every request sends `X-API-Key`. `X-Request-ID` is optional; one is generated if missing,
and it is always returned and written on every log line. Every error is an
[RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem document
(`application/problem+json`) with a `request_id`.

| Method | Path | Role | Returns |
| --- | --- | --- | --- |
| `GET` | `/health`, `/health/ready` | none | Process up; `ready` also checks the database |
| `POST` | `/v1/records` | ingest | `201` stored, `200` already received (or a measurement already stored from another source), `422` quarantined |
| `POST` | `/v1/validate` | ingest | Dry run: the canonical record or the errors; stores nothing |
| `POST` | `/v1/uploads` | ingest | `202` with a job; `413` over 20 MB or 100,000 rows |
| `GET` | `/v1/jobs/{id}` | ingest, reader | Progress: total, processed, normalized, failed, duplicate rows |
| `GET` | `/v1/records/{id}` | reader | The canonical record, and whether it is the current version |
| `GET` | `/v1/records/{id}/lineage` | reader | Raw payload, every transformation with its rule version, mapping version |
| `GET` | `/v1/records/{id}/versions` | reader | All versions of the same source record |
| `GET` | `/v1/patients/{id}/matches` | reader | Match group, candidate links with scores and facts, open conflicts |
| `GET` | `/v1/matches?status=review` | reader | Candidate pairs waiting for a human |
| `POST` | `/v1/matches/{id}/decision` | admin | A reviewer's `same` / `different` decision (audited) |
| `GET` | `/v1/quarantine` | reader | Filter by `source`, `reason_code`, `status`, `source_record_id` |
| `GET` | `/v1/quarantine/{id}` | reader | One quarantined record |
| `POST` | `/v1/quarantine/reprocess` | admin | Re-run open items (by ids or filter) with the current mapping; returns a job |
| `GET` | `/v1/findings` | reader | Conflicts and warnings (namesakes, duplicate measurements, ...) |
| `GET` | `/metrics` | admin | Counts by source and status for your organisation |

Lists are cursor-paginated: `?limit=` (default 50, max 200) and `?cursor=` from the
previous page's `next_cursor`, which is `null` on the last page.

**Roles.** `ingest` keys belong to sending systems and cannot read patient data back;
`reader` keys read; `admin` keys do both and also reprocess and decide matches. Another
organisation's ids return `404`, never `403`, so ids do not leak.

### Examples

```bash
API=http://localhost:8000

# Ingest a patient from source A
curl -s $API/v1/records -H "X-API-Key: $MEDVORA_INGEST_KEY" -H "Content-Type: application/json" -d '{
  "source": "source-a", "record_type": "Patient",
  "payload": {"mrn": "0012345", "patient_name": "ROBERT MILLER", "dob": "03/07/1984",
              "sex": "M", "phone": "(555) 201-3344", "zip": "10001"}}'

# A record that cannot be trusted: 422 with every reason, and it is kept in quarantine
curl -s $API/v1/records -H "X-API-Key: $MEDVORA_INGEST_KEY" -H "Content-Type: application/json" -d '{
  "source": "source-c", "record_type": "Patient",
  "payload": {"hospital_no": "C1", "full_name": "OKAFOR, Amara", "birth_date": "03/04/1985", "sex_code": "9"}}'

# Upload a CSV and poll the job
python -m scripts.generate_data --patients 300 --bad 5 --out data
curl -s $API/v1/uploads -H "X-API-Key: $MEDVORA_INGEST_KEY" \
  -F file=@data/source-a-patients.csv -F source=source-a -F record_type=Patient
curl -s $API/v1/jobs/<job id> -H "X-API-Key: $MEDVORA_INGEST_KEY"

# Where did this value come from?
curl -s $API/v1/records/<record id>/lineage -H "X-API-Key: $MEDVORA_READER_KEY"

# Fix a mapping (new version file), restart api and worker, then re-run what failed
curl -s $API/v1/quarantine/reprocess -H "X-API-Key: $MEDVORA_ADMIN_KEY" \
  -H "Content-Type: application/json" -d '{"reason_code": "CODE_UNMAPPED"}'
```

## How it works, and why

### Organisation isolation is enforced by Postgres

Every table carries `org_id` and has a row-level security policy, with `FORCE ROW LEVEL
SECURITY` so even the table owner is filtered. Three database roles keep it honest:

| Role | Logs in | Can |
| --- | --- | --- |
| `medvora_owner` | migrations, seed | Owns the schema. Still filtered by RLS. |
| `medvora_app` | API and worker | Only the grants it needs. Not the owner, no `BYPASSRLS`. Cannot update or delete raw records, canonical records or the audit log. |
| `medvora_system` | never | Owns the two `SECURITY DEFINER` functions that must see across orgs: API-key lookup (the org is unknown until the key is checked) and job claiming (workers serve every org). |

The org is set per transaction with `set_config('app.org_id', ..., true)`, the
transaction-local form of `SET LOCAL`, so a pooled connection cannot carry one org's id
into the next request. With no org set, every policy matches nothing. The API and worker
containers never receive the owner password. Tests prove each of these against a real
database.

### Nothing is lost, everything is traceable

- **Raw first.** Every payload is stored as received (append-only) before anything else.
- **Idempotent.** A SHA-256 of the canonical JSON of source, type and payload is unique
  per org. Re-sending returns the earlier result; key order and whitespace do not matter.
- **Versions.** Each record has a `source_record_id` (for example the MRN). Same id with
  new content is a new version, kept beside the old one; the latest received is current.
  A good new version settles older quarantined versions (`superseded`).
- **Lineage.** Every normalized field logs source field, original value, result, rule
  name and rule version, plus the mapping version that ran.
- **Quarantine.** Failures keep every reason (`DATE_AMBIGUOUS`, `UNIT_UNKNOWN`, ...) with
  the canonical field and the source column. Reprocessing re-runs from the raw payload
  with the current mapping.
- **Accounting.** Per source, `ingested = normalized + duplicate_records +
  quarantine.open + quarantine.superseded` (see `/metrics`).

### Normalization rules

Mapping files (`mappings/<source>/v<N>.yaml`) are validated at startup, so a typo stops
the app instead of corrupting data. They are never edited in place: a fix is a new version.

| Value | Rule |
| --- | --- |
| Dates | Parsed only with the source's declared formats. A value that fits two declared formats differently (03/04/1985 as d/m and m/d) is `DATE_AMBIGUOUS`. Before 1900 or in the future is rejected. Birth dates are never shifted through a time zone. |
| Timestamps | Read in the source's declared zone (an explicit offset wins) and stored in UTC. Local times that do not exist or occur twice because of daylight saving are rejected, not guessed. |
| Numbers | `Decimal` end to end, including JSON parsing, storage (JSONB numeric) and responses: `70.10` stays `70.10`. Values already in the canonical unit keep the sender's precision; converted values are rounded once to a declared step. |
| Units | UCUM codes. Conversion factors are versioned rules (lb→kg × 0.45359237, glucose mg/dL→mmol/L ÷ 18.016). Glucose in mmol/L uses LOINC 14749-6, the substance-concentration code that matches the unit. |
| Names | Unicode NFC, whitespace collapsed, ALL-CAPS title-cased, split by the source's declared order. Anything risky to split is kept as `name.text` with a warning. |
| Identifiers | Always strings; leading zeros survive. National ids and member numbers are compared without separators, and only within the same issuing system. |

### Matching is cautious by design

Merging two different people's histories is worse than leaving a duplicate. So:

1. **Blocking.** Only records sharing a birth year or a family-name prefix are compared.
2. **Comparison facts.** Each pair becomes per-field outcomes (`agree`, `disagree`,
   `missing`, `swapped`, `nickname`, ...). Facts contain no patient data.
3. **Scoring.** Agreement adds points and disagreement subtracts. Missing adds nothing,
   because hospitals collect different fields. Names common in the org count for less.
4. **Strong-identifier rule.** A pair can only head for an automatic match if it agrees
   on a national id, insurance member number, phone or email, and no name disagrees
   (twins share a surname, birth date, phone and address). Everything else that scores
   high enough waits for a human.
5. **Veto-only verifier.** Pairs that pass go to a background verifier that can confirm or
   send to review, never promote. The default `RuleVerifier` vetoes on a different middle
   initial, postal code or sex. An optional LLM verifier (`MATCH_VERIFIER=llm`) receives
   only the comparison facts. Timeouts, errors, invalid or off-schema answers all count as
   `unsure`, which means review.
6. **Namesakes and duplicates.** Same name and birth date but different national ids
   creates a `POSSIBLE_NAMESAKE` finding (also when a reviewer decides "different"). The
   same measurement (patient, time, LOINC code) from two sources is stored once, with a
   `DUPLICATE_RECORD` finding; only correct time-zone handling reveals it as the same.

A match only adds a link; it never edits either record. Conflicting fields between
matched records become findings with both values.

### Uploads and jobs

The API stores the file with a job row and returns `202`. A worker claims jobs with
`FOR UPDATE SKIP LOCKED` (several workers never take the same job) and processes 500 rows
per transaction, saving progress in the same transaction. Each row runs in a savepoint,
so one bad row cannot fail the chunk. Transient errors (lost connection, deadlock) retry
the whole chunk after 1 s, 4 s and 16 s; data errors never retry. A worker that dies is
detected by its stale heartbeat and its job is taken over. Replaying a chunk is harmless
because ingestion is idempotent. On `SIGTERM` the worker finishes the current chunk and
puts the job back.

`JobRunner` is the seam for a managed queue: the API only calls `runner.submit()`, and
any consumer calls the same `process_job()`.

### Observability and safety

- Structured JSON logs with the request id, org id and key id on every line. Logs hold
  ids, codes and counts only: never names, dates or payloads, and never exception
  messages from the database or validation libraries, which can echo row values.
- Every read of patient data and every decision is written to an append-only audit log
  in the same transaction.
- API keys are `mvk_<prefix>_<secret>`: looked up by the public prefix, verified as an
  HMAC-SHA256 with a server-side pepper, compared in constant time, never stored in plain.
- Per-key rate limits (100 records/minute, 10 uploads/hour) return `429` with
  `Retry-After`. Request bodies are capped while they stream (1 MB, 20 MB for uploads),
  including chunked requests that send no `Content-Length`.

## Development

```bash
uv sync                                   # Python 3.12 and all dependencies
docker compose up -d db                   # Postgres on 127.0.0.1:5432
uv run alembic upgrade head
uv run pytest                             # unit + integration tests (skips integration if no DB)
uv run ruff check . && uv run ruff format --check . && uv run mypy app scripts migrations tests
uv run uvicorn --factory app.main:create_app --reload
uv run python -m app.jobs.worker
```

Integration tests run the real app against Postgres as the real roles, in a separate
`medvora_test` database (created by the role bootstrap), so they never touch your data
and can run while the stack is up. Each test creates its own organisations, so
row-level security keeps tests apart. CI (GitHub Actions) runs
lint, strict type checks, `alembic check` (migrations must match the models), a
downgrade/upgrade round trip, the full test suite against a Postgres service, and a
Docker build.

### Adding a source or fixing a mapping

Add `mappings/<source>/v1.yaml` (see the existing three). To change one, copy it to
`v<N+1>.yaml`, edit, restart the API and worker, then reprocess the affected quarantine
items. Old versions stay so lineage keeps pointing at the rules that actually ran.

## Evaluation

`python -m scripts.evaluate` runs the pipeline and matcher over synthetic data whose
ground truth is known. Source B carries realistic noise (nicknames, swapped day and
month, dropped letters, moves, missing phones), and source C contains deliberate
namesakes. With the defaults (500 patients, seed 7):

| Measure | Result |
| --- | --- |
| No data loss (rows in = normalized + rejected) | yes: 2,839 = 2,809 + 30 |
| Birth date, sex, weight, glucose, height vs ground truth | 100% |
| Family name vs ground truth | 99.4% (the misses are typos as sent; matching scores them as "close") |
| Matching precision (automatic matches) | 100%, 0 wrong links |
| Matching recall, automatic | 88.8% |
| Matching recall, automatic or sent to review | 100% |
| Namesakes automatically matched | 0 |

The trade-off is deliberate: pairs the rules cannot prove go to a person, not to a merge.

## Deploying (AWS demo)

One EC2 instance runs the same containers as local, with Caddy in front for HTTPS.

1. Launch an instance with EBS encryption and an Elastic IP. Security group: 80 and 443
   only; use SSM Session Manager instead of SSH.
2. Install Docker and the Compose plugin, clone the repository.
3. Point a subdomain at the Elastic IP; set `DOMAIN` in `.env`.
4. Create `.env` from `.env.example` with strong random secrets; `chmod 600 .env`.
5. `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d`
   (migrations run automatically before the API and worker start).
6. `docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm tools python -m scripts.seed --with-other-org > .demo.env`
7. From your laptop: `uv run python -m scripts.demo --base-url https://<your domain> --keys-file .demo.env`
8. Afterwards: terminate the instance, release the Elastic IP, delete snapshots.

Only Caddy publishes ports; Postgres and the API are reachable only inside the Compose
network. What production would change:

| Part | Demo | Production |
| --- | --- | --- |
| Compute | One EC2 instance | ECS Fargate, several API and worker tasks |
| Database | Postgres container | Amazon RDS (backups, encryption, failover) |
| Job queue | Postgres job table | Amazon SQS, behind the same `JobRunner` |
| Secrets | `.env` on the server | AWS Secrets Manager |
| HTTPS | Caddy | Load balancer with an ACM certificate |
| Logs | Docker logs | CloudWatch |
| Rate limits | In process memory | Redis, shared by all API tasks |

## Assumptions

- Each source declares its date formats, time zone and code tables; nothing is guessed.
- A source record id (MRN, accession number, ...) identifies a record within its source.
  A payload with new content and the same id is a corrected version, not a new record.
- The most recently *received* version is current; sources do not send a reliable
  last-updated time.
- National ids are comparable only when two sources use the same issuing system.
- Observations in scope: body weight, height, glucose and heart rate, each with one
  canonical unit. Clinical plausibility ranges are not checked.
- Data in this repository is synthetic: SSNs start with 9 (never issued), emails use
  `example.org`, phone numbers the 555 exchange.

## Limitations

- **Concurrent matching.** Two transactions ingesting the same patient at the same moment
  do not see each other's uncommitted rows and may not link. A periodic re-match sweep
  or per-block advisory locks would close this.
- **Duplicate measurements depend on order.** An observation is recognised as a
  duplicate only if the patients were already matched when it arrived.
- **Name frequencies are approximate** within an upload chunk (counted once per chunk).
- **Rate limits are per process.** Several API instances would need Redis.
- **Mapping changes need a restart** of the API and worker.
- **Resent payloads after a mapping fix** return the earlier (quarantined) result,
  because the content is identical; use `/v1/quarantine/reprocess`.
- **Failed API-key attempts are not rate limited** per client address; put that at the
  edge (Caddy or a WAF).
- Blood pressure (two components) and other compound observations are not modelled.

## Next steps

- Re-match sweep after verification, so late links also catch duplicate measurements.
- Survivorship: a golden patient view choosing the best value per field across a group.
- Admin endpoints for API keys and mapping uploads (validated, versioned, hot-reloaded).
- SQS runner and consumer; S3 for uploaded files instead of the job row.
- OpenTelemetry traces alongside the request id; Prometheus-format metrics.
- A hand-labelled golden set from a pilot hospital to replace synthetic evaluation.

## Project layout

```text
app/
  pipeline/     pure functions: ingest hash, mapping, normalize/, validate, match, verify, run
  models/       canonical.py (FHIR-lite Pydantic models), tables.py (SQLAlchemy)
  services/     the database side: ingest, matching, records, uploads, audit, auth, verifiers
  api/          routes, dependencies, RFC 9457 errors, pagination, limits
  jobs/         JobRunner, process_job, the polling worker
  observability/ logging, request ids, request middleware
mappings/       one directory per source, one YAML file per version
migrations/     Alembic migrations (rls.py: shared RLS and role helpers)
scripts/        seed, generate_data, demo, evaluate
tests/          unit/ (no database) and integration/ (real Postgres)
deploy/         Postgres role bootstrap, Caddyfile
```

`pipeline/` imports neither FastAPI nor the database: every stage is a function from
data to data, tested in milliseconds. `services/` holds all storage; `api/` only
translates HTTP.
