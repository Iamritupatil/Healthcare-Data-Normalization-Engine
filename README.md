# Healthcare Data Normalization Engine

A service that accepts patient records from different healthcare systems, converts them into one standard FHIR-based format, and keeps a complete, traceable history of every change, without ever silently dropping or altering clinical data.

## The problem I chose

**Problem 1: Healthcare Data Integration & Normalization Engine.**

Hospitals, clinics and labs send patient data in different shapes, with different field names, date formats, units and codes. This service accepts those records through an API or file upload and converts them into one standard format based on FHIR. It validates them, flags duplicates and conflicting values, and keeps both the original data and a record of every change, so any normalized value can be traced back to its source. Invalid records are quarantined, never dropped, and can be reprocessed once fixed.

I chose this problem because I had built SynthFix, a tool that cleans machine-learning datasets. This project showed me the key difference: in an ML dataset you can drop or overwrite a bad value, but in healthcare a silently "fixed" value can reach a doctor. That idea shaped the whole design. The full reasoning is in [the system design document](docs/system-design.md#1-the-problem-and-why-i-chose-it).

## What it does

- **Accepts** single records (JSON) and file uploads (CSV or JSON), from three synthetic sources that each format data differently.
- **Normalizes** field names, dates (only in declared formats; ambiguous dates are never guessed), time zones (to UTC), units (exact decimal math with versioned conversion rules) and codes, into FHIR-lite `Patient` and `Observation` records.
- **Validates** every record against a declared schema. Failures go to a quarantine with reason codes and can be reprocessed later.
- **Keeps everything:** the raw payload exactly as received, every version of a record, and a log entry for every changed field, recording the rule and rule version used.
- **Finds duplicates and conflicts:** repeated requests, corrected records (stored as new versions), the same measurement from two sources, and the same patient across systems. Patient matching never merges two people on name and birth date alone. An automatic match needs a strong identifier and must pass a final verifier that can only veto.
- **Isolates organizations** with forced Postgres row-level security and separate database roles.
- **Runs uploads in the background** with per-row isolation, retries, crash recovery and progress tracking.
- **Includes** a console UI, structured logs with request ids, rate limits, an audit log, automated tests (including security tests) and an evaluation script.

## Documentation

| Document | Contents |
| --- | --- |
| [System design](docs/system-design.md) | Architecture, processing, data model, API, matching, failure handling, storage, scalability, multi-tenancy, security, evaluation |
| [Tradeoff analysis](docs/tradeoffs.md) | Latency vs reliability, cost vs quality, simplicity vs scalability, build vs buy, MVP priorities, key assumptions |

## Quick start

Requirements: Docker with Compose.

```bash
cp .env.example .env
docker compose up -d --build
```

Then open:
- **Console UI:** http://localhost:8000/
- **Interactive API docs:** http://localhost:8000/docs

Run the guided demo, which walks through every feature in about two minutes:

```bash
docker compose run --rm tools python -m scripts.demo
```

<!-- MERGE: insert here, verbatim from the current README, the tested setup details:
     the PowerShell equivalent of the commands above, how to install uv, how to seed
     the demo org and save the keys to .demo.env, and how to load them in bash and PowerShell. -->

## Using the API

Every request needs an `X-API-Key` header. The key determines the organization and role (`ingest`, `reader` or `admin`).

<!-- MERGE: insert here, verbatim from the current README, the tested API examples
     in both bash and PowerShell: ingest a record, a quarantined record, a file upload
     and job status, lineage, and reprocessing. -->

Errors follow RFC 9457 ("Problem Details"): every error has the same JSON shape and includes the request id. Lists use cursor pagination. The full endpoint list is in [the API design section](docs/system-design.md#7-api-design).

## Console UI

The console at http://localhost:8000/ lets you try a single record, upload a file and watch the job, browse quarantine, review candidate matches, and look up any record with its full lineage. Paste an API key to connect; it is kept only for the browser session.

## Tests and checks

<!-- MERGE: insert here, verbatim from the current README, the tested commands for:
     the test suite, lint/format/type checks, migration check, browser tests
     (Chromium install first), the smoke script, and scripts/security.py. -->

CI runs lint, type checks, the migration check, the full test suite and the security checks on every push.

## Evaluation

`uv run python -m scripts.evaluate` generates 500 synthetic patients with known truth (including deliberate errors and namesakes), runs them through the real pipeline, and measures the results:

| Measure | Result |
| --- | --- |
| Data loss | None: 2,839 rows in = 2,809 normalized + 30 quarantined |
| Normalization accuracy | 100% for birth date, sex, weight, glucose and height; 99.4% for family name |
| Matching precision | 100%: 453 automatic matches, 0 wrong |
| Matching recall | 88.8% automatic; 100% including pairs sent to human review |
| Namesakes merged | 0 of 10 |

The same code generated and evaluated this data, so real-world numbers would be lower; the evaluation shows the logic works as designed. See [the evaluation section](docs/system-design.md#16-evaluation).

## Project structure

```text
app/
  api/            HTTP routes, auth, limits, RFC 9457 errors
  pipeline/       pure functions: mapping, normalization, validation, match scoring
  services/       storage and orchestration: ingest, versions, quarantine, matching, audit
  jobs/           background job runner and polling worker
  models/         canonical (FHIR-lite) models and database tables
  observability/  structured logging and request ids
  static/         console UI
mappings/         one YAML mapping per source
migrations/       Alembic migrations, including row-level security
scripts/          demo, synthetic data generator, evaluation, security checks
tests/            unit, integration and browser tests
deploy/           Postgres roles and the Caddy HTTPS proxy
docs/             system design and tradeoff analysis
```

## Assumptions

- Sources send structured CSV or JSON and can declare their date formats, time zone and codes in a mapping file.
- Each source gives its records a stable id, so corrections are recognized as new versions.
- Data volume per organization is moderate (up to low millions of records).
- Each organization has someone to review uncertain matches and quarantined records.
- Organizations never share patients through this system.
- All data is synthetic.

## Limitations

- Identifiers (national ids, member numbers) are stored in plain text, protected by encryption at rest, row-level security and access control, but not by field-level encryption.
- Conflicts and warnings are recorded but have no "resolved" workflow yet.
- Rate limits are kept in process memory, so several API instances would each count separately; failed authentication attempts are not rate-limited.
- Matching weights are set by hand, not learned from labelled data.
- Out of scope: streaming ingest, HL7v2 and FHIR input, blood pressure, error tracking.

## Possible improvements

1. Field-level encryption or tokenization for identifiers.
2. A resolution workflow for conflicts, and rules for choosing which value to show.
3. Matching weights learned from labelled data.
4. Error tracking and alerting.
5. HL7v2 and FHIR input adapters.
6. Streaming ingest, and SQS for background jobs.

Details and reasoning: [system design, section 18](docs/system-design.md#18-assumptions-limitations-and-future-work).

## Author

Ritu Patil
