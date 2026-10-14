"""A two-minute tour of every feature, against a running API and worker.

    docker compose run --rm tools python -m scripts.demo
    uv run python -m scripts.demo --base-url https://medvora.example.com --keys-file .demo.env

Without --keys-file it creates two fresh demo organisations first (this needs the owner
database password, which the tools container has). With --keys-file, use a fresh org per
run: records from an earlier run would otherwise be found as duplicates.
"""

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from scripts.generate_data import generate, to_csv

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"


class Demo:
    def __init__(self, base_url: str, keys: dict[str, str], other_org_key: str) -> None:
        self.http = httpx.Client(base_url=base_url, timeout=30)
        self.keys = keys
        self.other_org_key = other_org_key
        self.step = 0

    def title(self, text: str) -> None:
        self.step += 1
        print(f"\n{BOLD}{self.step}. {text}{RESET}")

    def call(
        self,
        method: str,
        path: str,
        role: str | None = "admin",
        *,
        quiet: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        if role:
            headers["X-API-Key"] = self.keys[role]
        response = self.http.request(method, path, headers=headers, **kwargs)
        if not quiet:
            print(f"{DIM}{method} {path} -> {response.status_code}{RESET}")
        return response

    def show(self, data: Any, keys: list[str] | None = None) -> None:
        if keys is not None and isinstance(data, dict):
            data = {key: data[key] for key in keys if key in data}
        print(json.dumps(data, indent=2, ensure_ascii=False)[:1500])

    def wait_for(self, check: Callable[[], bool], what: str, seconds: float = 30) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(0.5)
        raise SystemExit(f"timed out waiting for {what}; is the worker running?")

    def post(self, record_type: str, source: str, payload: dict[str, Any]) -> httpx.Response:
        body = {"source": source, "record_type": record_type, "payload": payload}
        return self.call("POST", "/v1/records", "ingest", json=body)


ROBERT_A = {
    "mrn": "0012345",
    "patient_name": "ROBERT MILLER",
    "dob": "03/07/1984",
    "sex": "M",
    "ssn": "912-34-5678",
    "phone": "(555) 201-3344",
    "zip": "10001",
}
ROBERT_B = {
    "patient_id": "B-12345",
    "first_name": "Bob",
    "last_name": "MILLER",
    "date_of_birth": "03-07-1984",  # day and month swapped by the clinic
    "gender": "Male",
    "member_id": "BR55501234",
    "mobile": "+1 555-201-3344",
    "pincode": "10001",
}


def run(demo: Demo, public_url: str) -> None:
    demo.title("The API is up and the database answers")
    demo.show(demo.call("GET", "/health/ready", role=None).json())

    demo.title("Ingest a patient from source A (US hospital: m/d/Y dates, upper-case names)")
    first = demo.post("Patient", "source-a", ROBERT_A)
    record_a = first.json()["record"]
    demo.show(record_a["resource"])

    demo.title("Send the identical payload again: idempotent, nothing new is stored")
    again = demo.post("Patient", "source-a", ROBERT_A).json()
    demo.show(again, ["status", "duplicate"])

    demo.title("The same man from source B: nickname, split names, day/month swapped")
    record_b = demo.post("Patient", "source-b", ROBERT_B).json()["record"]
    print("Shared phone number is a strong identifier, so the pair goes to the verifier...")

    def matched() -> bool:
        path = f"/v1/patients/{record_a['id']}/matches"
        group = demo.call("GET", path, "reader", quiet=True).json()
        return len(group["members"]) == 2

    demo.wait_for(matched, "the worker to verify the match")
    group = demo.call("GET", f"/v1/patients/{record_a['id']}/matches", "reader").json()
    link = group["links"][0]
    demo.show({"status": link["status"], "points": link["points"], "facts": link["facts"]})
    print("Linked, and the birth date disagreement is kept as a conflict with both values:")
    demo.show([c["details"] for c in group["conflicts"]])

    demo.title("A namesake in source C: same name and birth date, different SSN")
    demo.post(
        "Patient",
        "source-c",
        {
            "hospital_no": "C55501",
            "full_name": "MILLER, Robert",
            "birth_date": "1984-03-07",
            "sex_code": "1",
            "ssn": "987-65-4321",
            "tel": "555 777 0000",
            "postcode": "10001",
        },
    )
    namesakes = demo.call("GET", "/v1/findings", "reader", params={"code": "POSSIBLE_NAMESAKE"})
    demo.show([f["details"] for f in namesakes.json()["items"]])

    demo.title("Same name and birth date but no strong identifier: a human decides")
    demo.post(
        "Patient",
        "source-a",
        {
            "mrn": "0012399",
            "patient_name": "Priya Sharma",
            "dob": "11/23/1990",
            "sex": "F",
            "zip": "60601",
        },
    )
    demo.post(
        "Patient",
        "source-c",
        {
            "hospital_no": "C55502",
            "full_name": "SHARMA, Priya",
            "birth_date": "1990-11-23",
            "sex_code": "2",
            "postcode": "60601",
        },
    )
    review = demo.call("GET", "/v1/matches", "reader", params={"status": "review"}).json()["items"]
    demo.show({"status": review[0]["status"], "breakdown": review[0]["breakdown"]})
    decided = demo.call(
        "POST", f"/v1/matches/{review[0]['id']}/decision", json={"decision": "different"}
    )
    demo.show(decided.json(), ["status", "decided_by"])

    demo.title("A record that cannot be normalized is quarantined, with every reason")
    bad = demo.post(
        "Patient",
        "source-c",
        {
            "hospital_no": "C55503",
            "full_name": "OKAFOR, Amara",
            "birth_date": "03/04/1985",
            "sex_code": "9",
        },
    )
    demo.show(bad.json(), ["type", "status", "detail", "quarantine_id", "errors", "request_id"])

    demo.title("The corrected record arrives: it is stored and settles the quarantined one")
    fixed = demo.post(
        "Patient",
        "source-c",
        {
            "hospital_no": "C55503",
            "full_name": "OKAFOR, Amara",
            "birth_date": "1985-04-03",
            "sex_code": "2",
        },
    )
    item = demo.call("GET", f"/v1/quarantine/{bad.json()['quarantine_id']}", "reader").json()
    demo.show({"new_record": fixed.json()["record"]["id"], "quarantine_status": item["status"]})

    demo.title("Units and time zones: the same glucose test from two hospitals")
    glucose_a = demo.post(
        "Observation",
        "source-a",
        {
            "obs_id": "A-GLU-1",
            "mrn": "0012345",
            "test": "GLU",
            "result": "99",
            "units": "mg/dL",
            "taken_at": "10/01/2026 10:30",
        },
    )
    demo.show(glucose_a.json()["record"]["resource"]["valueQuantity"])
    print("Source B wrote the same test in Chicago local time (one hour behind New York):")
    glucose_b = demo.post(
        "Observation",
        "source-b",
        {
            "result_id": "B-GLU-1",
            "patient_id": "B-12345",
            "test_code": "FBS",
            "value": 5.5,
            "uom": "mmol/L",
            "collected_at": "2026-10-01T09:30:00",
        },
    )
    demo.show(glucose_b.json(), ["status", "duplicate_of"])

    demo.title("Lineage: every field back to its raw value and the rule that changed it")
    lineage = demo.call("GET", f"/v1/records/{record_b['id']}/lineage", "reader").json()
    demo.show(
        [
            {k: step[k] for k in ("field", "source_field", "original", "result", "rule")}
            for step in lineage["transformations"][:5]
        ]
    )

    demo.title("Upload a CSV: 200 synthetic patients with 5 broken rows")
    rows = generate(200, 5, seed=int(time.time()) % 10_000).files["source-a-patients.csv"]
    for row in rows:
        row["mrn"] = f"U{row['mrn']}"  # keep clear of the records above
    upload = demo.call(
        "POST",
        "/v1/uploads",
        "ingest",
        files={"file": ("patients.csv", to_csv(rows).encode(), "text/csv")},
        data={"source": "source-a", "record_type": "Patient"},
    )
    job_id = upload.json()["id"]

    def finished() -> bool:
        status = demo.call("GET", f"/v1/jobs/{job_id}", "ingest", quiet=True).json()["status"]
        return status not in ("pending", "running")

    demo.wait_for(finished, "the upload job", seconds=180)
    demo.show(
        demo.call("GET", f"/v1/jobs/{job_id}", "ingest").json(),
        ["status", "total_rows", "normalized_rows", "failed_rows"],
    )

    demo.title("The quarantine list pages with a cursor")
    page = demo.call("GET", "/v1/quarantine", "reader", params={"limit": 3}).json()
    demo.show(
        {
            "reasons": [item["reason_codes"] for item in page["items"]],
            "next_cursor": page["next_cursor"],
        }
    )

    if demo.other_org_key:
        demo.title("Org isolation: another organisation's key sees a 404, not the record")
        other = demo.http.get(
            f"/v1/records/{record_a['id']}", headers={"X-API-Key": demo.other_org_key}
        )
        demo.show(other.json(), ["status", "title", "request_id"])

    demo.title("Roles: a reader cannot ingest")
    forbidden = demo.call(
        "POST",
        "/v1/records",
        "reader",
        json={"source": "source-a", "record_type": "Patient", "payload": ROBERT_A},
    )
    demo.show(forbidden.json(), ["status", "title", "detail"])

    demo.title("Metrics for this organisation")
    demo.show(demo.call("GET", "/metrics").json())
    print(f"\n{BOLD}Done.{RESET} Interactive API docs: {public_url.rstrip('/')}/docs")


def read_keys_file(path: Path) -> dict[str, str]:
    values = dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    return {key.strip(): value.strip() for key, value in values.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base-url", default=os.environ.get("MEDVORA_BASE_URL", "http://localhost:8000")
    )
    parser.add_argument(
        "--public-url",
        default=os.environ.get("MEDVORA_PUBLIC_URL"),
        help="the API as a browser reaches it, if not --base-url (inside Docker it is not)",
    )
    parser.add_argument("--keys-file", type=Path, help="MEDVORA_*_KEY lines from scripts.seed")
    args = parser.parse_args()

    if args.keys_file:
        env = read_keys_file(args.keys_file)
        keys = {role: env[f"MEDVORA_{role.upper()}_KEY"] for role in ("admin", "ingest", "reader")}
        other_key = env.get("MEDVORA_OTHER_ORG_READER_KEY", "")
    else:
        from scripts.seed import create_org

        _, keys = create_org("Demo Hospital Network")
        _, other_keys = create_org("Another Hospital Network")
        other_key = other_keys["reader"]
    try:
        run(Demo(args.base_url, keys, other_key), args.public_url or args.base_url)
    except httpx.HTTPError as error:
        sys.exit(f"Could not reach the API at {args.base_url}: {error}")


if __name__ == "__main__":
    main()
