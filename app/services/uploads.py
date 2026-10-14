"""CSV uploads, parsed with the csv module so every value stays a string ("00123" too)."""

import csv
import io
from dataclasses import dataclass

from app.pipeline.mapping import RecordType, SourceMapping

EXTRA_COLUMNS_KEY = "_extra_columns"


class UploadError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class ParsedCsv:
    header: list[str]
    rows: list[dict[str, str | list[str]]]


def parse_csv(content: bytes, *, max_rows: int) -> ParsedCsv:
    try:
        # utf-8-sig drops the byte-order mark that Excel adds.
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise UploadError(400, "the file must be UTF-8 encoded CSV") from None
    reader = csv.DictReader(io.StringIO(text, newline=""), restkey=EXTRA_COLUMNS_KEY)
    try:
        if not reader.fieldnames:
            raise UploadError(400, "the file is empty or has no header row")
        header = [name.strip() for name in reader.fieldnames]
        if len(set(header)) != len(header):
            raise UploadError(400, "the header row repeats a column name")
        reader.fieldnames = header
        rows: list[dict[str, str | list[str]]] = []
        for row in reader:
            if len(rows) >= max_rows:
                raise UploadError(413, f"the file has more than {max_rows} data rows")
            # Short rows fill missing columns with None; leave those keys out.
            rows.append({key: value for key, value in row.items() if value is not None})
    except csv.Error as exc:
        raise UploadError(400, f"the file is not valid CSV (line {reader.line_num})") from exc
    if not rows:
        raise UploadError(400, "the file has a header but no data rows")
    return ParsedCsv(header, rows)


def check_header(parsed: ParsedCsv, mapping: SourceMapping, record_type: RecordType) -> None:
    """Fail the upload early when the id columns are missing, instead of quarantining every row."""
    required = mapping.record_types[record_type].source_record_id
    missing = [name for name in required if name not in parsed.header]
    if missing:
        raise UploadError(
            400,
            f"the header is missing the id column(s) {missing} required by "
            f"{mapping.source} v{mapping.version} for {record_type}",
        )
