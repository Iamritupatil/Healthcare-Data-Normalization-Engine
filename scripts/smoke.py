"""Black-box check of every API endpoint against a running server (API and worker).

    python -m scripts.smoke
    python -m scripts.smoke --base-url https://medvora.example.com --keys-file .demo.env

Without --keys-file it creates two fresh organisations, which needs the owner database
password (a local .env and the tools container have it). Prints one line per check and
exits non-zero if any fails. Rate limiting is left to the integration tests: tripping
the local limit takes hundreds of requests.
"""

import argparse
import json
import secrets
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

SAMPLES = Path(__file__).resolve().parents[1] / "app" / "static" / "samples"
GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


class Smoke:
    def __init__(self, base_url: str, keys: dict[str, str], other_keys: dict[str, str]) -> None:
        self.http = httpx.Client(base_url=base_url, timeout=30)
        self.keys = keys
        self.other = other_keys
        self.passed = 0
        self.failed: list[str] = []
        self.run = secrets.token_hex(3)

    # ---- plumbing ----

    def call(
        self,
        method: str,
        path: str,
        role: str | None = "admin",
        *,
        other: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        if role:
            headers["X-API-Key"] = (self.other if other else self.keys)[role]
        return self.http.request(method, path, headers=headers, **kwargs)

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  {GREEN}pass{RESET}  {name}")
        else:
            self.failed.append(name)
            print(f"  {RED}FAIL{RESET}  {name}  {DIM}{detail}{RESET}")
        return ok

    def expect(
        self, name: str, response: httpx.Response, status: int, problem: str | None = None
    ) -> Any:
        body: Any = None
        try:
            body = response.json()
        except ValueError:
            body = None
        detail = f"got {response.status_code}: {response.text[:200]}"
        ok = response.status_code == status
        if ok and problem:
            ok = (
                response.headers.get("content-type", "").startswith("application/problem+json")
                and isinstance(body, dict)
                and body.get("type", "").endswith(f"/problems/{problem}")
                and body.get("status") == status
                and bool(body.get("request_id"))
            )
            detail = f"not an RFC 9457 '{problem}' problem: {response.text[:200]}"
        self.check(name, ok, detail)
        return body

    def wait(self, what: str, condition: Callable[[], bool], seconds: float = 60) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.5)
        return self.check(f"{what} (within {seconds:.0f}s; is the worker running?)", False)

    def wait_for_job(self, job_id: str, role: str = "ingest") -> dict[str, Any]:
        job: dict[str, Any] = {}

        def finished() -> bool:
            nonlocal job
            job = self.call("GET", f"/v1/jobs/{job_id}", role).json()
            return job.get("status") not in ("pending", "running")

        self.wait(f"job {job_id[:8]} finishes", finished, seconds=120)
        return job

    def record(
        self, source: str, record_type: str, payload: dict[str, Any], role: str = "ingest"
    ) -> httpx.Response:
        body = {"source": source, "record_type": record_type, "payload": payload}
        return self.call("POST", "/v1/records", role, json=body)

    def upload(
        self, content: bytes, filename: str, media: str, source: str, record_type: str
    ) -> httpx.Response:
        return self.call(
            "POST",
            "/v1/uploads",
            "ingest",
            files={"file": (filename, content, media)},
            data={"source": source, "record_type": record_type},
        )

    # ---- checks ----

    def health_and_docs(self) -> None:
        print("\nHealth, docs and the dashboard")
        self.check("GET /health", self.call("GET", "/health", None).json() == {"status": "ok"})
        self.check(
            "GET /health/ready",
            self.call("GET", "/health/ready", None).json() == {"status": "ready"},
        )
        spec = self.call("GET", "/openapi.json", None)
        self.check(
            "GET /openapi.json lists the API",
            spec.status_code == 200 and "/v1/records" in spec.json()["paths"],
        )
        self.check("GET /docs", self.call("GET", "/docs", None).status_code == 200)
        page = self.call("GET", "/", None)
        self.check(
            "GET / serves the dashboard with a strict CSP",
            page.status_code == 200
            and "script-src 'self'" in page.headers.get("content-security-policy", ""),
        )
        traced = self.call("GET", "/health", None, headers={"X-Request-ID": f"smoke-{self.run}"})
        self.check(
            "X-Request-ID is echoed back", traced.headers.get("x-request-id") == f"smoke-{self.run}"
        )

    def auth(self) -> None:
        print("\nAuthentication and roles")
        self.expect("no key -> 401", self.call("GET", "/v1/me", None), 401, "unauthorized")
        self.expect(
            "wrong key -> 401",
            self.call("GET", "/v1/me", None, headers={"X-API-Key": "mvk_000000000000_" + "x" * 43}),
            401,
            "unauthorized",
        )
        me = self.expect("GET /v1/me", self.call("GET", "/v1/me", "reader"), 200)
        self.check("  names the role", bool(me) and me["role"] == "reader")
        sources = self.expect("GET /v1/sources", self.call("GET", "/v1/sources", "ingest"), 200)
        self.check("  lists the three sources", bool(sources) and len(sources["items"]) == 3)
        self.expect(
            "reader cannot ingest -> 403",
            self.record("source-a", "Patient", {}, "reader"),
            403,
            "forbidden",
        )
        self.expect(
            "ingest cannot read quarantine -> 403",
            self.call("GET", "/v1/quarantine", "ingest"),
            403,
            "forbidden",
        )
        self.expect(
            "reader cannot read metrics -> 403",
            self.call("GET", "/metrics", "reader"),
            403,
            "forbidden",
        )

    def records(self) -> None:
        print("\nRecords: ingest, idempotency, versions, lineage")
        r = self.run
        patient = {
            "mrn": f"S{r}01",
            "patient_name": "ROBERT MILLER",
            "dob": "03/07/1984",
            "sex": "M",
            "ssn": "912-34-5678",
            "phone": "(555) 201-3344",
            "zip": "10001",
        }
        created = self.record("source-a", "Patient", patient)
        body = self.expect("POST /v1/records -> 201", created, 201)
        self.a_id = body["record"]["id"] if body else ""
        self.check(
            "  Location header", created.headers.get("location") == f"/v1/records/{self.a_id}"
        )
        self.check(
            "  birth date normalized",
            bool(body) and body["record"]["resource"]["birthDate"] == "1984-03-07",
        )
        again = self.expect(
            "same payload again -> 200", self.record("source-a", "Patient", patient), 200
        )
        self.check(
            "  marked duplicate, same record",
            bool(again) and again["duplicate"] and again["record"]["id"] == self.a_id,
        )

        valid = self.expect(
            "POST /v1/validate (valid)",
            self.call(
                "POST",
                "/v1/validate",
                "ingest",
                json={"source": "source-a", "record_type": "Patient", "payload": patient},
            ),
            200,
        )
        self.check(
            "  valid, with transformations",
            bool(valid) and valid["valid"] and len(valid["transformations"]) > 3,
        )
        invalid = self.expect(
            "POST /v1/validate (invalid)",
            self.call(
                "POST",
                "/v1/validate",
                "ingest",
                json={
                    "source": "source-a",
                    "record_type": "Patient",
                    "payload": {**patient, "dob": "31/02/1990"},
                },
            ),
            200,
        )
        self.check(
            "  invalid, with reason",
            bool(invalid) and invalid["reason_codes"] == ["DATE_UNPARSEABLE"],
        )

        weight = (
            '{"source":"source-b","record_type":"Observation","payload":{"result_id":"S'
            + r
            + '-W","patient_id":"B-S'
            + r
            + '","test_code":"BODYWT","value":70.10,"uom":"kg",'
            '"collected_at":"2026-10-01T10:30:00"}}'
        )
        exact = self.call(
            "POST",
            "/v1/records",
            "ingest",
            content=weight,
            headers={"content-type": "application/json"},
        )
        self.check(
            "decimals stay exact (70.10)",
            exact.status_code == 201 and '"value":70.10' in exact.text,
            exact.text[:200],
        )
        self.check(
            "  Chicago 10:30 stored as 15:30 UTC",
            '"effectiveDateTime":"2026-10-01T15:30:00Z"' in exact.text,
        )

        self.expect(
            "changed record -> new version 201",
            self.record("source-a", "Patient", {**patient, "zip": "10002"}),
            201,
        )
        versions = self.expect(
            "GET /v1/records/{id}/versions",
            self.call("GET", f"/v1/records/{self.a_id}/versions", "reader"),
            200,
        )
        self.check(
            "  two versions, newest current",
            bool(versions)
            and [(v["version"], v["is_current"]) for v in versions["items"]]
            == [(2, True), (1, False)],
        )
        old = self.expect(
            "GET /v1/records/{id}", self.call("GET", f"/v1/records/{self.a_id}", "reader"), 200
        )
        self.check("  older version is not current", bool(old) and old["is_current"] is False)
        lineage = self.expect(
            "GET /v1/records/{id}/lineage",
            self.call("GET", f"/v1/records/{self.a_id}/lineage", "reader"),
            200,
        )
        steps = {s["field"]: s for s in (lineage or {}).get("transformations", [])}
        self.check(
            "  birth date traced to '03/07/1984' and date.parse v1",
            steps.get("birthDate", {}).get("original") == "03/07/1984"
            and steps["birthDate"]["rule"] == "date.parse",
        )
        self.check(
            "  raw payload kept as sent", bool(lineage) and lineage["raw"]["payload"] == patient
        )
        self.expect(
            "unknown record id -> 404",
            self.call("GET", "/v1/records/00000000-0000-0000-0000-000000000000", "reader"),
            404,
            "not-found",
        )
        self.expect(
            "malformed record id -> 400",
            self.call("GET", "/v1/records/not-a-uuid", "reader"),
            400,
            "invalid-request",
        )

        print("\nRecords: bad input and quarantine")
        self.expect(
            "malformed JSON -> 400",
            self.call(
                "POST",
                "/v1/records",
                "ingest",
                content=b"{",
                headers={"content-type": "application/json"},
            ),
            400,
            "bad-request",
        )
        self.expect(
            "missing record_type -> 400",
            self.call("POST", "/v1/records", "ingest", json={"source": "source-a", "payload": {}}),
            400,
            "bad-request",
        )
        self.expect(
            "unknown source -> 400",
            self.record("nowhere", "Patient", {"a": "b"}),
            400,
            "bad-request",
        )
        ambiguous = {
            "hospital_no": f"C{r}01",
            "full_name": "OKAFOR, Amara",
            "birth_date": "03/04/1985",
            "sex_code": "2",
        }
        held = self.expect(
            "ambiguous date -> 422 quarantined",
            self.record("source-c", "Patient", ambiguous),
            422,
            "validation-failed",
        )
        self.q_id = held["quarantine_id"] if held else ""
        self.check(
            "  reason DATE_AMBIGUOUS", bool(held) and held["reason_codes"] == ["DATE_AMBIGUOUS"]
        )
        item = self.expect(
            "GET /v1/quarantine/{id}",
            self.call("GET", f"/v1/quarantine/{self.q_id}", "reader"),
            200,
        )
        self.check("  status open", bool(item) and item["status"] == "open")
        fixed = self.expect(
            "corrected resend -> 201",
            self.record("source-c", "Patient", {**ambiguous, "birth_date": "1985-04-03"}),
            201,
        )
        settled = self.call("GET", f"/v1/quarantine/{self.q_id}", "reader").json()
        self.check(
            "  quarantined version superseded",
            settled.get("status") == "superseded"
            and bool(fixed)
            and settled.get("resolved_record_id") == fixed["record"]["id"],
        )
        unmapped = {"mrn": f"S{r}09", "patient_name": "Ana Silva", "dob": "05/05/1975", "sex": "X"}
        self.expect(
            "unmapped code -> 422",
            self.record("source-a", "Patient", unmapped),
            422,
            "validation-failed",
        )

    def matching(self) -> None:
        print("\nMatching, conflicts, namesakes, review, duplicates")
        r = self.run
        b = {
            "patient_id": f"B-S{r}",
            "first_name": "Bob",
            "last_name": "MILLER",
            "date_of_birth": "03-07-1984",
            "gender": "Male",
            "mobile": "+1 555-201-3344",
            "pincode": "10002",
        }
        b_body = self.expect(
            "same man from source B -> 201", self.record("source-b", "Patient", b), 201
        )
        b_id = b_body["record"]["id"] if b_body else ""

        def linked() -> bool:
            group = self.call("GET", f"/v1/patients/{b_id}/matches", "reader").json()
            return len(group.get("members", [])) >= 2

        if self.wait("worker verifies the A/B pair", linked):
            group = self.expect(
                "GET /v1/patients/{id}/matches",
                self.call("GET", f"/v1/patients/{b_id}/matches", "reader"),
                200,
            )
            self.check(
                "  linked as the same person",
                bool(group) and any(link["status"] == "match" for link in group["links"]),
            )
            self.check(
                "  day/month swap kept as a birth-date conflict",
                bool(group) and any(c["code"] == "CONFLICT_BIRTHDATE" for c in group["conflicts"]),
            )
        conflicts = self.expect(
            "GET /v1/findings?kind=conflict",
            self.call("GET", "/v1/findings", "reader", params={"kind": "conflict"}),
            200,
        )
        self.check("  conflict listed", bool(conflicts) and len(conflicts["items"]) >= 1)

        namesake = {
            "hospital_no": f"C{r}02",
            "full_name": "MILLER, Robert",
            "birth_date": "1984-03-07",
            "sex_code": "1",
            "ssn": "987-65-4321",
            "tel": "555 777 0000",
            "postcode": "10001",
        }
        self.expect(
            "namesake with another SSN -> 201", self.record("source-c", "Patient", namesake), 201
        )
        found = self.call(
            "GET", "/v1/findings", "reader", params={"code": "POSSIBLE_NAMESAKE"}
        ).json()
        self.check("  POSSIBLE_NAMESAKE finding, not a merge", len(found.get("items", [])) >= 1)

        self.record(
            "source-a",
            "Patient",
            {
                "mrn": f"S{r}03",
                "patient_name": "Priya Sharma",
                "dob": "11/23/1990",
                "sex": "F",
                "zip": "60601",
            },
        )
        self.record(
            "source-c",
            "Patient",
            {
                "hospital_no": f"C{r}03",
                "full_name": "SHARMA, Priya",
                "birth_date": "1990-11-23",
                "sex_code": "2",
                "postcode": "60601",
            },
        )
        queue = self.expect(
            "GET /v1/matches?status=review",
            self.call("GET", "/v1/matches", "reader", params={"status": "review"}),
            200,
        )
        pair = next(
            (
                i
                for i in (queue or {}).get("items", [])
                if {i["record"]["source_record_id"], i["candidate"]["source_record_id"]}
                == {f"S{r}03", f"C{r}03"}
            ),
            None,
        )
        self.check(
            "  same name and birth date without strong id waits for a person", pair is not None
        )
        if pair:
            self.expect(
                "reader cannot decide -> 403",
                self.call(
                    "POST",
                    f"/v1/matches/{pair['id']}/decision",
                    "reader",
                    json={"decision": "different"},
                ),
                403,
                "forbidden",
            )
            self.expect(
                "other org cannot decide -> 404",
                self.call(
                    "POST",
                    f"/v1/matches/{pair['id']}/decision",
                    "admin",
                    other=True,
                    json={"decision": "same"},
                ),
                404,
                "not-found",
            )
            decided = self.expect(
                "POST /v1/matches/{id}/decision",
                self.call(
                    "POST", f"/v1/matches/{pair['id']}/decision", json={"decision": "different"}
                ),
                200,
            )
            self.check(
                "  recorded as different people",
                bool(decided)
                and decided["status"] == "different"
                and decided["decided_by"] == "human",
            )

        glucose_a = {
            "obs_id": f"S{r}-GLU",
            "mrn": f"S{r}01",
            "test": "GLU",
            "result": "99",
            "units": "mg/dL",
            "taken_at": "10/01/2026 10:30",
        }
        first = self.expect(
            "glucose 99 mg/dL from A -> 201", self.record("source-a", "Observation", glucose_a), 201
        )
        self.check(
            "  converted to 5.5 mmol/L",
            bool(first) and first["record"]["resource"]["valueQuantity"]["value"] == 5.5,
        )
        glucose_b = {
            "result_id": f"B-S{r}-GLU",
            "patient_id": f"B-S{r}",
            "test_code": "FBS",
            "value": "5.5",
            "uom": "mmol/L",
            "collected_at": "2026-10-01T09:30:00",
        }
        dup = self.expect(
            "same test from B (Chicago time) -> 200",
            self.record("source-b", "Observation", glucose_b),
            200,
        )
        self.check(
            "  recognised as a duplicate measurement",
            bool(dup)
            and bool(first)
            and dup["status"] == "duplicate_record"
            and dup["duplicate_of"] == first["record"]["id"],
        )

    def uploads_and_jobs(self) -> None:
        print("\nUploads and jobs")
        sample = (SAMPLES / "source-a-patients.csv").read_bytes()
        response = self.upload(sample, "patients.csv", "text/csv", "source-a", "Patient")
        job = self.expect("POST /v1/uploads (CSV) -> 202", response, 202)
        self.check(
            "  Location header",
            bool(job) and response.headers.get("location") == f"/v1/jobs/{job['id']}",
        )
        if job:
            done = self.wait_for_job(job["id"])
            self.check(
                "  40 rows: 37 normalized, 3 quarantined",
                (done.get("status"), done.get("normalized_rows"), done.get("failed_rows"))
                == ("completed_with_errors", 37, 3),
                str(done),
            )
            self.job_id = job["id"]
            self.expect(
                "GET /v1/jobs/{id} as reader",
                self.call("GET", f"/v1/jobs/{job['id']}", "reader"),
                200,
            )
        again = self.expect(
            "same file again -> 202",
            self.upload(sample, "patients.csv", "text/csv", "source-a", "Patient"),
            202,
        )
        if again:
            redo = self.wait_for_job(again["id"])
            self.check("  every row already received", redo.get("duplicate_rows") == 40, str(redo))

        rows = [
            {"mrn": f"J{self.run}{n}", "patient_name": "Mei Chen", "dob": "02/14/1960", "sex": "F"}
            for n in range(3)
        ]
        as_json = self.expect(
            "POST /v1/uploads (JSON array) -> 202",
            self.upload(
                json.dumps(rows).encode(),
                "patients.json",
                "application/json",
                "source-a",
                "Patient",
            ),
            202,
        )
        if as_json:
            self.check("  completed", self.wait_for_job(as_json["id"]).get("status") == "completed")
        self.expect(
            "missing id column -> 400",
            self.upload(b"name,dob\nBob,01/01/1980\n", "x.csv", "text/csv", "source-a", "Patient"),
            400,
            "bad-request",
        )
        self.expect(
            "not CSV or JSON -> 415",
            self.upload(b"\x89PNG\r\n", "photo.png", "image/png", "source-a", "Patient"),
            415,
        )

    def quarantine_findings_metrics(self) -> None:
        print("\nQuarantine lists, reprocess, findings, metrics")
        listed = self.expect(
            "GET /v1/quarantine", self.call("GET", "/v1/quarantine", "reader"), 200
        )
        self.check("  has rows", bool(listed) and len(listed["items"]) > 0)
        page1 = self.call("GET", "/v1/quarantine", "reader", params={"limit": 2}).json()
        cursor = page1.get("next_cursor")
        page2 = (
            self.call(
                "GET", "/v1/quarantine", "reader", params={"limit": 2, "cursor": cursor}
            ).json()
            if cursor
            else {}
        )
        ids1 = {i["id"] for i in page1.get("items", [])}
        ids2 = {i["id"] for i in page2.get("items", [])}
        self.check(
            "cursor paging returns the next, different page",
            bool(cursor) and bool(ids2) and not ids1 & ids2,
        )
        self.expect(
            "bad cursor -> 400",
            self.call("GET", "/v1/quarantine", "reader", params={"cursor": "nope"}),
            400,
            "bad-request",
        )
        filtered = self.call(
            "GET",
            "/v1/quarantine",
            "reader",
            params={"reason_code": "CODE_UNMAPPED", "source": "source-a"},
        ).json()
        self.check(
            "filter by reason and source",
            bool(filtered.get("items"))
            and all(
                "CODE_UNMAPPED" in i["reason_codes"] and i["source"] == "source-a"
                for i in filtered["items"]
            ),
        )
        self.expect(
            "reprocess without a filter -> 400",
            self.call("POST", "/v1/quarantine/reprocess", json={}),
            400,
            "invalid-request",
        )
        self.expect(
            "reprocess as reader -> 403",
            self.call(
                "POST", "/v1/quarantine/reprocess", "reader", json={"reason_code": "CODE_UNMAPPED"}
            ),
            403,
            "forbidden",
        )
        job = self.expect(
            "POST /v1/quarantine/reprocess -> 202",
            self.call("POST", "/v1/quarantine/reprocess", json={"reason_code": "CODE_UNMAPPED"}),
            202,
        )
        if job:
            done = self.wait_for_job(job["id"], "admin")
            # The mapping did not change, so the items fail again and stay open.
            self.check(
                "  reprocess job ran (items still fail: mapping unchanged)",
                done.get("status") == "completed_with_errors"
                and done.get("processed_rows", 0) >= 1,
                str(done),
            )
        self.expect(
            "findings limit over 200 -> 400",
            self.call("GET", "/v1/findings", "reader", params={"limit": 500}),
            400,
            "invalid-request",
        )
        metrics = self.expect("GET /metrics", self.call("GET", "/metrics"), 200)
        if metrics:
            balanced = all(
                c["ingested"]
                == c["normalized"]
                + c["duplicate_records"]
                + c["quarantine"]["open"]
                + c["quarantine"]["superseded"]
                for c in metrics["by_source"].values()
            )
            self.check(
                "  every received record is accounted for, per source",
                balanced,
                json.dumps(metrics["by_source"]),
            )

    def isolation(self) -> None:
        print("\nOrganisation isolation")
        for path in (
            f"/v1/records/{self.a_id}",
            f"/v1/records/{self.a_id}/lineage",
            f"/v1/patients/{self.a_id}/matches",
            f"/v1/quarantine/{self.q_id}",
            f"/v1/jobs/{getattr(self, 'job_id', self.a_id)}",
        ):
            label = (
                path.replace(self.a_id, "{id}")
                .replace(self.q_id, "{id}")
                .replace(getattr(self, "job_id", "-"), "{id}")
            )
            self.expect(
                f"other org: GET {label} -> 404",
                self.call("GET", path, "reader", other=True),
                404,
                "not-found",
            )
        theirs = self.call("GET", "/v1/quarantine", "reader", other=True).json()
        self.check("other org's quarantine list is empty", theirs.get("items") == [])


def read_keys(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    env = dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    keys = {
        role: env[f"MEDVORA_{role.upper()}_KEY"].strip() for role in ("admin", "ingest", "reader")
    }
    other = {
        role: env.get(f"MEDVORA_OTHER_ORG_{role.upper()}_KEY", "").strip()
        for role in ("admin", "ingest", "reader")
    }
    return keys, other


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--keys-file", type=Path)
    args = parser.parse_args()
    if args.keys_file:
        keys, other = read_keys(args.keys_file)
    else:
        from scripts.seed import create_org

        _, keys = create_org(f"Smoke test {secrets.token_hex(2)}")
        _, other = create_org(f"Smoke test other org {secrets.token_hex(2)}")
    smoke = Smoke(args.base_url, keys, other)
    try:
        smoke.health_and_docs()
        smoke.auth()
        smoke.records()
        smoke.matching()
        smoke.uploads_and_jobs()
        smoke.quarantine_findings_metrics()
        smoke.isolation()
    except httpx.HTTPError as error:
        sys.exit(f"Could not reach {args.base_url}: {error}")
    total = smoke.passed + len(smoke.failed)
    print(f"\n{smoke.passed}/{total} checks passed")
    if smoke.failed:
        print(f"{RED}Failed:{RESET} " + "; ".join(smoke.failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
