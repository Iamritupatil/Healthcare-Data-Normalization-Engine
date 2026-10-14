"""Uploaded files: CSV, or a JSON array of payloads.

CSV is parsed with the csv module, so every value stays a string ("00123" too). JSON is
parsed with the exact codec, so 70.10 stays 70.10 and each element is the same payload
POST /v1/records would take. Either way the whole file is checked here, before a job
exists: a file the worker could not store is a 400 now, never a failed job later.
"""

import csv
import io
import re
from dataclasses import dataclass
from typing import Any, Literal

from app import json_codec
from app.pipeline.mapping import RecordType, SourceMapping

EXTRA_COLUMNS_KEY = "_extra_columns"

UploadFormat = Literal["csv", "json"]
UPLOAD_FORMATS: tuple[UploadFormat, ...] = ("csv", "json")

# What browsers and tools actually send for these files. Windows browsers label CSV as
# application/vnd.ms-excel, and curl sends application/octet-stream for unknown extensions.
_CSV_TYPES = {"text/csv", "application/csv", "text/plain", "application/vnd.ms-excel"}
_JSON_TYPES = {"application/json", "text/json"}
_ANY_TYPES = {"", "application/octet-stream"}

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class UploadError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class ParsedUpload:
    header: list[str]
    rows: list[dict[str, Any]]


def upload_format(filename: str | None, content_type: str | None) -> UploadFormat:
    """CSV or JSON, from the file name; the declared type decides only when the name doesn't."""
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if media not in _CSV_TYPES | _JSON_TYPES | _ANY_TYPES:
        raise UploadError(415, "the file must be CSV (text/csv) or JSON (application/json)")
    name = (filename or "").lower()
    if name.endswith(".json"):
        return "json"
    if name.endswith(".csv"):
        return "csv"
    return "json" if media in _JSON_TYPES else "csv"


def safe_filename(filename: str | None) -> str:
    """The last path component, without control characters. It is only ever displayed."""
    name = re.split(r"[\\/]", filename or "")[-1]
    return _CONTROL.sub("", name)[:255] or "upload"


def parse_upload(content: bytes, upload_type: UploadFormat, *, max_rows: int) -> ParsedUpload:
    if upload_type == "json":
        return parse_json(content, max_rows=max_rows)
    return parse_csv(content, max_rows=max_rows)


def _decode(content: bytes, kind: str) -> str:
    try:
        # utf-8-sig drops the byte-order mark that Excel adds.
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise UploadError(400, f"the file must be UTF-8 encoded {kind}") from None
    # Postgres cannot store a NUL character anywhere, so one would fail the job later.
    if "\x00" in text:
        raise UploadError(400, "the file contains a NUL character")
    return text


def parse_csv(content: bytes, *, max_rows: int) -> ParsedUpload:
    text = _decode(content, "CSV")
    reader = csv.DictReader(io.StringIO(text, newline=""), restkey=EXTRA_COLUMNS_KEY)
    try:
        if not reader.fieldnames:
            raise UploadError(400, "the file is empty or has no header row")
        header = [name.strip() for name in reader.fieldnames]
        if len(set(header)) != len(header):
            raise UploadError(400, "the header row repeats a column name")
        reader.fieldnames = header
        rows: list[dict[str, Any]] = []
        for row in reader:
            if len(rows) >= max_rows:
                raise UploadError(413, f"the file has more than {max_rows} data rows")
            # Short rows fill missing columns with None; leave those keys out.
            rows.append({key: value for key, value in row.items() if value is not None})
    except csv.Error as exc:
        raise UploadError(400, f"the file is not valid CSV (line {reader.line_num})") from exc
    if not rows:
        raise UploadError(400, "the file has a header but no data rows")
    return ParsedUpload(header, rows)


def parse_json(content: bytes, *, max_rows: int) -> ParsedUpload:
    """A JSON array of objects, each one a payload as POST /v1/records would take it."""
    text = _decode(content, "JSON")
    try:
        data = json_codec.loads(text)
    except json_codec.UnstorableValue as exc:
        raise UploadError(400, f"the file cannot be stored: {exc}") from None
    except ValueError:
        raise UploadError(400, "the file is not valid JSON") from None
    if not isinstance(data, list):
        raise UploadError(400, "the file must be a JSON array of objects")
    if len(data) > max_rows:
        raise UploadError(413, f"the file has more than {max_rows} records")
    if not data:
        raise UploadError(400, "the file has no records")
    header: dict[str, None] = {}
    for number, row in enumerate(data, start=1):
        if not isinstance(row, dict):
            raise UploadError(400, f"record {number} is not a JSON object")
        try:
            json_codec.check_storable(row)
        except json_codec.UnstorableValue as exc:
            raise UploadError(400, f"record {number} cannot be stored: {exc}") from None
        header.update(dict.fromkeys(row))
    return ParsedUpload(list(header), data)


def check_header(parsed: ParsedUpload, mapping: SourceMapping, record_type: RecordType) -> None:
    """Fail the upload early when the id columns are missing, instead of quarantining every row."""
    required = mapping.record_types[record_type].source_record_id
    missing = [name for name in required if name not in parsed.header]
    if missing:
        raise UploadError(
            400,
            f"the file is missing the id column(s) {missing} required by "
            f"{mapping.source} v{mapping.version} for {record_type}",
        )
