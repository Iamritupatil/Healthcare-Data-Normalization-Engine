from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.pipeline.core import NormalizationError, ReasonCode
from app.pipeline.normalize import codes, dates, identifiers, names, units

TODAY = date(2026, 10, 14)


class TestDates:
    def test_parses_with_the_declared_format(self):
        assert dates.parse_date("03/07/1984", ["%m/%d/%Y"], today=TODAY) == date(1984, 3, 7)

    def test_value_fitting_two_declared_formats_differently_is_ambiguous(self):
        with pytest.raises(NormalizationError) as exc:
            dates.parse_date("03/04/1990", ["%d/%m/%Y", "%m/%d/%Y"], today=TODAY)
        assert exc.value.code is ReasonCode.DATE_AMBIGUOUS

    def test_same_result_under_both_formats_is_not_ambiguous(self):
        assert dates.parse_date("03/03/1990", ["%d/%m/%Y", "%m/%d/%Y"], today=TODAY) == date(
            1990, 3, 3
        )

    def test_no_guessing_when_the_format_is_not_declared(self):
        with pytest.raises(NormalizationError) as exc:
            dates.parse_date("03/04/1990", ["%Y-%m-%d"], today=TODAY)
        assert exc.value.code is ReasonCode.DATE_UNPARSEABLE

    @pytest.mark.parametrize("value", ["01/01/2090", "01/01/1899", "10/15/2026"])
    def test_future_and_pre_1900_dates_are_out_of_range(self, value):
        with pytest.raises(NormalizationError) as exc:
            dates.parse_date(value, ["%m/%d/%Y"], today=TODAY)
        assert exc.value.code is ReasonCode.DATE_OUT_OF_RANGE


class TestTimestamps:
    def test_local_time_is_read_in_the_source_zone(self):
        # 10:30 in New York in October is 14:30 UTC (daylight time, UTC-4).
        result = dates.parse_timestamp("10/01/2026 10:30", ["%m/%d/%Y %H:%M"], "America/New_York")
        assert result == datetime(2026, 10, 1, 14, 30, tzinfo=UTC)

    def test_same_wall_clock_differs_by_zone(self):
        fmt = ["%Y-%m-%d %H:%M"]
        chicago = dates.parse_timestamp("2026-10-01 10:30", fmt, "America/Chicago")
        kolkata = dates.parse_timestamp("2026-10-01 10:30", fmt, "Asia/Kolkata")
        assert chicago == datetime(2026, 10, 1, 15, 30, tzinfo=UTC)
        assert kolkata == datetime(2026, 10, 1, 5, 0, tzinfo=UTC)

    def test_explicit_offset_wins_over_the_declared_zone(self):
        result = dates.parse_timestamp("2026-10-01T10:30:00+05:30", [], "America/New_York")
        assert result == datetime(2026, 10, 1, 5, 0, tzinfo=UTC)

    def test_missing_zone_is_reported(self):
        with pytest.raises(NormalizationError) as exc:
            dates.parse_timestamp("2026-10-01 10:30", ["%Y-%m-%d %H:%M"], None)
        assert exc.value.code is ReasonCode.TIMEZONE_MISSING

    def test_daylight_saving_gap_is_rejected(self):
        with pytest.raises(NormalizationError) as exc:
            dates.parse_timestamp("2026-03-08 02:30", ["%Y-%m-%d %H:%M"], "America/New_York")
        assert exc.value.code is ReasonCode.TIMESTAMP_NONEXISTENT

    def test_daylight_saving_overlap_is_rejected(self):
        with pytest.raises(NormalizationError) as exc:
            dates.parse_timestamp("2026-11-01 01:30", ["%Y-%m-%d %H:%M"], "America/New_York")
        assert exc.value.code is ReasonCode.TIMESTAMP_AMBIGUOUS


class TestUnits:
    def test_pounds_to_kilograms_is_exact_decimal(self):
        weight = units.CONCEPTS["body-weight"]
        quantity = units.normalize_quantity(weight, Decimal("154"), "[lb_av]")
        assert quantity.value == Decimal("69.85")
        assert str(quantity.value) == "69.85"
        assert quantity.conversion is not None
        assert quantity.conversion.factor == Decimal("0.45359237")

    def test_decimal_is_exact_where_float_drifts(self):
        assert Decimal("154") * Decimal("0.45359237") == Decimal("69.85322498")
        assert repr(154 * 0.45359237) == "69.85322498000001"  # why floats are not used

    def test_glucose_mg_per_dl_to_mmol_per_l(self):
        glucose = units.CONCEPTS["glucose"]
        quantity = units.normalize_quantity(glucose, Decimal("99"), "mg/dL")
        assert quantity.value == Decimal("5.5")
        assert quantity.unit == "mmol/L"
        # The canonical code is the substance-concentration LOINC code, matching the unit.
        assert glucose.loinc == "14749-6"

    def test_value_already_in_canonical_unit_keeps_source_precision(self):
        weight = units.CONCEPTS["body-weight"]
        quantity = units.normalize_quantity(weight, Decimal("70.10"), "kg")
        assert str(quantity.value) == "70.10"
        assert quantity.conversion is None

    def test_unknown_and_incompatible_units(self):
        with pytest.raises(NormalizationError) as unknown:
            units.canonical_unit("stone")
        assert unknown.value.code is ReasonCode.UNIT_UNKNOWN
        with pytest.raises(NormalizationError) as incompatible:
            units.normalize_quantity(units.CONCEPTS["glucose"], Decimal("5"), "kg")
        assert incompatible.value.code is ReasonCode.UNIT_INCOMPATIBLE

    @pytest.mark.parametrize("raw", ["high", "1,5", "NaN", True])
    def test_non_numbers_are_rejected(self, raw):
        with pytest.raises(NormalizationError) as exc:
            units.parse_decimal(raw)
        assert exc.value.code is ReasonCode.VALUE_NOT_NUMERIC


class TestIdentifiersAndCodes:
    def test_leading_zeros_survive(self):
        assert identifiers.normalize_identifier(" 00123 ").value == "00123"

    def test_numeric_identifier_is_flagged(self):
        result = identifiers.normalize_identifier(123)
        assert result.value == "123"
        assert result.was_numeric

    def test_empty_identifier_is_missing(self):
        with pytest.raises(NormalizationError) as exc:
            identifiers.normalize_identifier("  ")
        assert exc.value.code is ReasonCode.IDENTIFIER_MISSING

    def test_external_ids_compare_without_separators(self):
        assert identifiers.normalize_external_id("912-34-5678") == "912345678"
        assert identifiers.normalize_external_id("943 476 5919") == "9434765919"

    def test_emails_are_lower_cased_and_checked(self):
        assert identifiers.normalize_email(" Bob.Miller@Example.org ") == "bob.miller@example.org"
        assert identifiers.normalize_email("not-an-email") is None

    def test_code_table_lookup_is_case_insensitive(self):
        assert codes.map_code("male", {"Male": "male"}) == "male"
        with pytest.raises(NormalizationError) as exc:
            codes.map_code("X", {"M": "male"})
        assert exc.value.code is ReasonCode.CODE_UNMAPPED


class TestNames:
    def test_given_family_order(self):
        name = names.parse_full_name("ROBERT  MILLER", "given_family")
        assert (name.text, name.family, name.given) == ("Robert Miller", "Miller", ["Robert"])

    def test_family_comma_given(self):
        name = names.parse_full_name("O'BRIEN, mary ann", "family_given")
        assert (name.family, name.given) == ("O'Brien", ["Mary", "Ann"])

    def test_unsure_split_keeps_text_only(self):
        name = names.parse_full_name("Maria de la Cruz Lopez", "given_family")
        assert not name.certain
        assert name.text == "Maria de la Cruz Lopez"

    def test_unicode_is_composed_and_mixed_case_kept(self):
        decomposed = "Muñoz"
        assert names.name_part(decomposed) == "Muñoz"
        assert names.name_part("McDonald") == "McDonald"

    def test_match_key_ignores_accents_and_case(self):
        assert names.match_key("Muñoz") == names.match_key("MUNOZ") == "munoz"
