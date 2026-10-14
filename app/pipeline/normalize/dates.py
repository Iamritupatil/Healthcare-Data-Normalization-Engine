"""Dates and timestamps.

Birth dates are calendar dates and are never shifted through a time zone: converting
"1984-03-07" to UTC could move it to the 6th. Timestamps are read in the source's declared
zone (unless the value carries its own offset) and stored in UTC.
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.pipeline.core import NormalizationError, ReasonCode, Rule

DATE_RULE = Rule("date.parse", 1)
TIMESTAMP_RULE = Rule("timestamp.to_utc", 1)
MIN_YEAR = 1900


def _parse_naive(text: str, formats: Sequence[str]) -> set[datetime]:
    results: set[datetime] = set()
    for fmt in formats:
        try:
            results.add(datetime.strptime(text, fmt))  # noqa: DTZ007  zone is applied by the caller
        except ValueError:
            continue
    return results


def parse_date(raw: str, formats: Sequence[str], *, today: date) -> date:
    """Parse with the source's declared formats.

    A value is ambiguous only when a source declares several formats and the value fits
    more than one of them with different results, e.g. 03/04/1985 under d/m/Y and m/d/Y.
    """
    candidates = {value.date() for value in _parse_naive(raw.strip(), formats)}
    if not candidates:
        raise NormalizationError(
            ReasonCode.DATE_UNPARSEABLE, f"does not match the declared formats {list(formats)}"
        )
    if len(candidates) > 1:
        raise NormalizationError(
            ReasonCode.DATE_AMBIGUOUS, "matches more than one declared format with different dates"
        )
    parsed = candidates.pop()
    if parsed.year < MIN_YEAR or parsed > today:
        raise NormalizationError(
            ReasonCode.DATE_OUT_OF_RANGE, f"must be between {MIN_YEAR}-01-01 and today"
        )
    return parsed


def parse_timestamp(raw: str, formats: Sequence[str], timezone: str | None) -> datetime:
    """Return an aware UTC datetime."""
    text = raw.strip()
    try:
        explicit = datetime.fromisoformat(text)
    except ValueError:
        explicit = None
    if explicit is not None and explicit.tzinfo is not None:
        return explicit.astimezone(UTC)

    candidates = _parse_naive(text, formats)
    if not candidates:
        raise NormalizationError(
            ReasonCode.TIMESTAMP_UNPARSEABLE, f"does not match the declared formats {list(formats)}"
        )
    if len(candidates) > 1:
        raise NormalizationError(
            ReasonCode.TIMESTAMP_AMBIGUOUS, "matches more than one declared format"
        )
    if timezone is None:
        raise NormalizationError(
            ReasonCode.TIMEZONE_MISSING, "value has no offset and the source declares no time zone"
        )
    naive = candidates.pop()
    zone = ZoneInfo(timezone)
    earlier = naive.replace(tzinfo=zone, fold=0)
    later = naive.replace(tzinfo=zone, fold=1)
    if earlier.utcoffset() != later.utcoffset():
        round_trip = earlier.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
        if round_trip != naive:
            raise NormalizationError(
                ReasonCode.TIMESTAMP_NONEXISTENT,
                f"local time does not exist in {timezone} (daylight saving gap)",
            )
        raise NormalizationError(
            ReasonCode.TIMESTAMP_AMBIGUOUS,
            f"local time occurs twice in {timezone} (daylight saving overlap)",
        )
    return earlier.astimezone(UTC)
