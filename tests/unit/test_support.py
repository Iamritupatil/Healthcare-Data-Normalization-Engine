"""JSON codec, pagination cursors, request ids, rate limiting, CSV parsing, synthetic data."""

import random
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import exc as sa_exc

from app import json_codec
from app.api.errors import BadRequest, RateLimited
from app.api.limits import RateLimiter
from app.api.pagination import decode_cursor, encode_cursor
from app.jobs.runner import with_retries
from app.observability.request_id import accept_or_create
from app.services.uploads import UploadError, parse_csv
from scripts.generate_data import generate, make_people, patient_rows


class TestJsonCodec:
    def test_decimals_round_trip_exactly(self):
        data = json_codec.loads('{"weight": 70.10, "count": 3}')
        assert data == {"weight": Decimal("70.10"), "count": 3}
        assert json_codec.dumps(data) == '{"weight":70.10,"count":3}'

    def test_rejects_nan(self):
        with pytest.raises(ValueError, match="NaN is not valid JSON"):
            json_codec.loads('{"x": NaN}')

    def test_datetimes_are_written_as_utc(self):
        moment = datetime(2026, 10, 1, 14, 30, tzinfo=UTC)
        assert json_codec.dumps({"at": moment}) == '{"at":"2026-10-01T14:30:00Z"}'

    def test_sorted_output_for_hashing(self):
        assert json_codec.dumps({"b": 1, "a": "é"}, sort_keys=True) == '{"a":"é","b":1}'


class TestCursor:
    def test_round_trip(self):
        at, row_id = datetime(2026, 10, 1, 1, 2, 3, 456789, tzinfo=UTC), uuid4()
        assert decode_cursor(encode_cursor(at, row_id)) == (at, row_id)

    def test_garbage_is_a_bad_request(self):
        with pytest.raises(BadRequest):
            decode_cursor("not-a-cursor")


@pytest.mark.parametrize(
    ("incoming", "kept"),
    [("abc-123_X.y", True), ("bad id with spaces", False), ("x" * 65, False), (None, False)],
)
def test_request_ids_are_reused_only_when_safe(incoming, kept):
    result = accept_or_create(incoming)
    assert (result == incoming) is kept


def test_rate_limiter_blocks_then_resets():
    now = [0.0]
    limiter = RateLimiter(clock=lambda: now[0])
    for _ in range(3):
        limiter.hit("records", "key", limit=3, window_seconds=60)
    with pytest.raises(RateLimited) as exc:
        limiter.hit("records", "key", limit=3, window_seconds=60)
    assert exc.value.headers == {"Retry-After": "60"}
    limiter.hit("records", "other-key", limit=3, window_seconds=60)
    now[0] = 61
    limiter.hit("records", "key", limit=3, window_seconds=60)


class TestCsv:
    def test_values_stay_strings_and_bom_is_dropped(self):
        parsed = parse_csv("﻿mrn,dob\n00123,03/07/1984\n".encode(), max_rows=10)
        assert parsed.header == ["mrn", "dob"]
        assert parsed.rows == [{"mrn": "00123", "dob": "03/07/1984"}]

    @pytest.mark.parametrize(
        ("content", "status"),
        [
            (b"", 400),
            (b"mrn,mrn\n1,2\n", 400),
            (b"mrn\n", 400),
            (b"mrn\n1\n2\n3\n", 413),
            ("mrn\n\xff\n".encode("latin-1"), 400),
        ],
    )
    def test_bad_files_are_rejected(self, content, status):
        with pytest.raises(UploadError) as exc:
            parse_csv(content, max_rows=2)
        assert exc.value.status == status


class TestSyntheticData:
    def test_is_deterministic(self):
        assert generate(50, 3, seed=1).files == generate(50, 3, seed=1).files

    def test_row_counts_and_defects(self):
        people = make_people(1000, random.Random(7))
        rows = patient_rows("source-a", people, bad=5, seed=7)
        assert len(rows) == 1000
        assert len({row["mrn"] for row in rows}) == 1000

    def test_identifiers_are_clearly_fake(self):
        dataset = generate(20, 0, seed=3)
        assert all(person.ssn.startswith("9") for person in dataset.people)
        assert all(person.email.endswith("@example.org") for person in dataset.people)


class TestRetries:
    def _failing(self, errors: list[Exception]):
        calls = []

        def action() -> str:
            calls.append(1)
            if errors:
                raise errors.pop(0)
            return "done"

        return action, calls

    def test_transient_errors_are_retried_with_backoff(self):
        transient = sa_exc.OperationalError("SELECT 1", {}, Exception("connection lost"))
        action, calls = self._failing([transient, transient])
        waits: list[float] = []
        assert with_retries(action, (1.0, 4.0, 16.0), sleep=waits.append) == "done"
        assert (len(calls), waits) == (3, [1.0, 4.0])

    def test_gives_up_after_three_retries(self):
        transient = sa_exc.OperationalError("SELECT 1", {}, Exception("down"))
        action, calls = self._failing([transient] * 5)
        with pytest.raises(sa_exc.OperationalError):
            with_retries(action, (1.0, 4.0, 16.0), sleep=lambda _: None)
        assert len(calls) == 4

    def test_permanent_errors_are_never_retried(self):
        action, calls = self._failing([ValueError("bad data")])
        with pytest.raises(ValueError, match="bad data"):
            with_retries(action, (1.0, 4.0, 16.0), sleep=lambda _: None)
        assert len(calls) == 1
