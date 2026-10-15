# Healthcare Data Normalization Engine

A service that accepts patient records from different healthcare systems, converts them into one standard FHIR-based format, and keeps a complete, traceable history of every change, without ever silently dropping or altering clinical data.

**Author:** Ritu Patil

## The problem I chose

**Problem 1: Healthcare Data Integration & Normalization Engine.**

Hospitals, clinics and labs send patient data in different shapes, with different field names, date formats, units and codes. This service accepts those records through an API or file upload and converts them into one standard format based on FHIR. It validates them, flags duplicates and conflicting values, and keeps both the original data and a record of every change, so any normalized value can be traced back to its source. Invalid records are quarantined, never dropped, and can be reprocessed once fixed.

I chose this problem because I had built SynthFix, a tool that cleans machine-learning datasets. This project showed me the key difference: in an ML dataset you can drop or overwrite a bad value, but in healthcare a silently "fixed" value can reach a doctor. That idea shaped the whole design.

## Features

- **Ingest** single records (JSON) or file uploads (CSV or JSON) from three synthetic sources that each format data differently.
- **Normalize** field names, dates, time zones, units and codes into FHIR-lite `Patient` and `Observation` records. Ambiguous dates are quarantined, never guessed.
- **Validate** every record against a declared schema; failures go to a quarantine with reason codes and can be reprocessed.
- **Keep full history:** the raw payload as received, every version of a record, and a log entry for every changed field.
- **Detect duplicates and conflicts**, including the same patient across systems. Two people are never merged on name and birth date alone.
- **Isolate organizations** with forced Postgres row-level security.
- **Process uploads in the background** with retries, crash recovery and progress tracking.
- **Console UI**, structured logs, rate limits, an audit log, automated tests and an evaluation script.

## Design documents

The design is documented separately, in two PDFs submitted with this repository:

- **System Design** (`system-design.pdf`): architecture, processing, data model, API, matching, failure handling, storage, scalability, multi-tenancy, security and evaluation.
- **Tradeoff Analysis** (`tradeoffs.pdf`): latency vs reliability, cost vs quality, simplicity vs scalability, build vs buy, MVP priorities and key assumptions.

## Tech stack

Python 3.12, FastAPI, Pydantic, PostgreSQL, SQLAlchemy, Alembic, Docker Compose, pytest.

## Quick start

Requirements: Docker with Compose. To run the scripts and tests on your machine, also install [uv](https://docs.astral.sh/uv/).

**bash (macOS / Linux):**

```bash
cp .env.example .env
docker compose up -d --build
```

**PowerShell (Windows):**

```powershell
Copy-Item .env.example .env
docker compose up -d --build
```

Then open:

- **Console UI:** http://localhost:8000/
- **Interactive API docs:** http://localhost:8000/docs

Run the guided demo, which walks through every feature in about two minutes:

```bash
docker compose run --rm tools python -m scripts.demo
```

Create a demo organization and API keys:

```bash
docker compose run --rm tools python -m scripts.seed
```

To stop everything: `docker compose down`. To also delete the database: `docker compose down -v`.

## Using the API

Every request needs an `X-API-Key` header. The key decides the organization and the role (`ingest`, `reader` or `admin`). The full list of endpoints, with a "Try it out" button for each, is at http://localhost:8000/docs.

**bash:**

```bash
export API_KEY="<admin key>"
curl -s http://localhost:8000/v1/me -H "X-API-Key: $API_KEY"
curl -s "http://localhost:8000/v1/quarantine?limit=10" -H "X-API-Key: $API_KEY"
curl -s http://localhost:8000/v1/records/<record-id>/lineage -H "X-API-Key: $API_KEY"
```

**PowerShell:**

```powershell
$h = @{ "X-API-Key" = "<admin key>" }
Invoke-RestMethod http://localhost:8000/v1/me -Headers $h
Invoke-RestMethod "http://localhost:8000/v1/quarantine?limit=10" -Headers $h
Invoke-RestMethod http://localhost:8000/v1/records/<record-id>/lineage -Headers $h
```

Main endpoints:

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/records` | Ingest one record |
| `POST /v1/validate` | Check a record without storing it |
| `POST /v1/uploads` | Upload a CSV or JSON file; returns a job id |
| `GET /v1/jobs/{id}` | Upload progress |
| `GET /v1/records/{id}/lineage` | A record's raw payload and every change made to it |
| `GET /v1/quarantine` | Records that failed, with reasons |
| `POST /v1/quarantine/reprocess` | Re-run quarantined records with the current rules |
| `GET /v1/matches?status=review` | Patient matches waiting for a person to decide |
| `GET /v1/findings` | Conflicts and warnings |

Errors follow RFC 9457 ("Problem Details"), so every error has the same JSON shape and includes a request id.

## Console UI

The console at http://localhost:8000/ lets you try a single record, upload a file and watch the job, browse quarantine, review candidate patient matches, and look up any record with its full history. Paste an API key to connect; it is kept only for the browser session.

## Tests and checks

```bash
uv sync
uv run pytest                      # unit and integration tests (needs the Docker stack running)
uv run ruff check .                # lint
uv run mypy .                      # type checks
uv run alembic check               # migrations match the models
uv run python -m scripts.security  # code and dependency security scans
```

Browser tests (install Chromium first):

```bash
uv run playwright install chromium
uv run pytest tests/e2e
```

CI runs lint, type checks, the migration check, the test suite and the security checks on every push.

## Evaluation

```bash
uv run python -m scripts.evaluate
```

Runs 500 synthetic people (938 patient records across the three sources, 2,839 rows with observations) through the real pipeline, with deliberate errors and namesakes mixed in:

| Measure | Result |
| --- | --- |
| Data loss | None: all 2,839 rows ended as normalized (2,809) or quarantined (30) |
| Normalization accuracy | 100% for birth date, sex, weight, glucose and height; 99.4% for family name |
| Matching precision | 100%: 453 automatic matches, 0 wrong |
| Matching recall | 88.8% automatic; 100% including pairs sent to human review |
| Namesakes merged | 0 of 10 |

The same code generated and evaluated this data, so real-world numbers would be lower.

## Project structure

```text
app/
  api/            HTTP routes, auth, limits, errors
  pipeline/       mapping, normalization, validation, match scoring (no database code)
  services/       storage: ingest, versions, quarantine, matching, audit
  jobs/           background job worker
  models/         canonical models and database tables
  observability/  logging and request ids
  static/         console UI
mappings/         one YAML mapping per source
migrations/       database migrations
scripts/          demo, seed, data generator, evaluation, security checks
tests/            unit, integration and browser tests
deploy/           database roles and HTTPS proxy config
```

## Assumptions

- Sources send structured CSV or JSON and declare their date formats, time zone and codes in a mapping file.
- Each source gives its records a stable id, so corrections are recognized as new versions.
- Data volume per organization is moderate (up to low millions of records).
- Someone at each organization reviews uncertain matches and quarantined records.
- All data is synthetic.

## Limitations

- Identifiers such as national ids are stored in plain text, protected by encryption at rest and access control, but not by field-level encryption.
- Conflicts and warnings are recorded but can't be marked as resolved yet.
- Rate limits are kept in memory, so they don't work across several API instances.
- Matching weights are set by hand, not learned from labelled data.
- Not built: streaming ingest, HL7v2 and FHIR input, blood pressure, error tracking.

## Possible improvements

1. Field-level encryption for identifiers.
2. A workflow to resolve conflicts and choose which value to show.
3. Matching weights learned from labelled data.
4. Error tracking and alerting.
5. HL7v2 and FHIR input.
6. Streaming ingest, and a managed queue for background jobs.
