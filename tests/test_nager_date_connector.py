# -*- coding: utf-8 -*-
"""Nager.Date connector tests.

Covers the five mandated scenarios (success, timeout, 429, 500, invalid payload)
plus the behaviours specific to this feed: HTTP 204 for an uncovered country is a
skip rather than a failure, re-importing a year must not clone events, a renamed
holiday updates in place, and one failing country must not cost the others.
"""
from __future__ import annotations

import json

import pytest
import requests
import responses

from integration_service.connectors.nager_date_connector import NagerDateConnector
from integration_service.sync_result import (
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
)

EVENT_MODEL = "calendar.event"


def url_for(country: str = "US", year: int = 2026) -> str:
    return f"https://date.nager.at/api/v4/Holidays/{country}/{year}"


def holiday(date: str, name: str, **overrides):
    """A holiday object in the exact shape the live v4 endpoint returns."""
    item = {
        "date": date,
        "name": name,
        "countryCode": "US",
        "nationalHoliday": True,
        "subdivisionCodes": None,
        "holidayTypes": ["Public", "Bank"],
    }
    item.update(overrides)
    return item


TWO_HOLIDAYS = [
    holiday("2026-01-01", "New Year's Day"),
    holiday("2026-01-19", "Martin Luther King, Jr. Day"),
]


@pytest.fixture
def connector(ctx):
    """A connector pinned to a single country/year so URLs are predictable."""
    ctx.settings.nager.countries = ["US"]
    ctx.settings.nager.years = [2026]
    return NagerDateConnector(ctx)


def events(odoo):
    return odoo.records(EVENT_MODEL)


# -- 1. success ---------------------------------------------------------------


@responses.activate
def test_success_imports_holidays_as_all_day_events(connector, odoo):
    responses.add(responses.GET, url_for(), json=TWO_HOLIDAYS, status=200)

    result = connector.run(write_log=False)

    assert result.status == STATUS_SUCCESS
    assert (result.created, result.updated, result.skipped, result.failed) == (2, 0, 0, 0)

    stored = sorted(events(odoo), key=lambda r: r["start"])
    assert len(stored) == 2

    first = stored[0]
    assert first["name"] == "New Year's Day (US)"
    assert first["start"] == "2026-01-01 00:00:00"
    assert first["stop"] == "2026-01-01 23:59:59"
    assert first["allday"] is True
    assert first["show_as"] == "free"
    assert first["x_external_id"] == "nager:holiday:US:2026-01-01:new-year-s-day"
    assert first["x_source_hash"]
    # The feed has no per-record timestamp, so this column stays unset.
    assert "x_external_updated_at" not in first

    assert "Public" in first["description"] and "Bank" in first["description"]
    assert result.details["US:2026"] == {"created": 2, "updated": 0, "skipped": 0, "failed": 0}


@responses.activate
def test_start_and_end_times_are_recorded(connector):
    responses.add(responses.GET, url_for(), json=TWO_HOLIDAYS, status=200)

    result = connector.run(write_log=False)

    assert result.started_at is not None
    assert result.ended_at is not None
    assert result.ended_at >= result.started_at
    assert result.duration_seconds >= 0


# -- 2. timeout ---------------------------------------------------------------


@responses.activate
def test_timeout_fails_the_run_after_exhausting_retries(connector, sleep_calls):
    responses.add(responses.GET, url_for(), body=requests.exceptions.ConnectTimeout())

    result = connector.run(write_log=False)

    assert result.status == STATUS_FAILED
    assert result.failed == 1
    # HTTP_MAX_RETRIES=2 in the test environment: three attempts, two backoffs.
    assert len(responses.calls) == 3
    assert len(sleep_calls) == 2
    assert any("timed out" in error.lower() for error in result.errors)


# -- 3. HTTP 429 --------------------------------------------------------------


@responses.activate
def test_rate_limit_is_retried_honouring_retry_after(connector, sleep_calls):
    responses.add(
        responses.GET, url_for(), json={"message": "slow down"}, status=429,
        headers={"Retry-After": "7"},
    )
    responses.add(responses.GET, url_for(), json=TWO_HOLIDAYS, status=200)

    result = connector.run(write_log=False)

    assert result.status == STATUS_SUCCESS
    assert result.created == 2
    assert sleep_calls == [7.0]


# -- 4. HTTP 500 --------------------------------------------------------------


@responses.activate
def test_server_error_fails_the_run(connector):
    responses.add(responses.GET, url_for(), body="upstream exploded", status=500)

    result = connector.run(write_log=False)

    assert result.status == STATUS_FAILED
    assert result.failed == 1
    assert len(responses.calls) == 3
    assert any("500" in error for error in result.errors)


# -- 5. invalid payload -------------------------------------------------------


@responses.activate
def test_object_instead_of_list_is_an_invalid_payload(connector, odoo):
    responses.add(responses.GET, url_for(), json={"error": "not a list"}, status=200)

    result = connector.run(write_log=False)

    assert result.status == STATUS_FAILED
    assert events(odoo) == []
    assert any("expected a list" in error for error in result.errors)


@responses.activate
def test_non_json_body_is_an_invalid_payload(connector):
    responses.add(responses.GET, url_for(), body="<html>maintenance</html>", status=200)

    result = connector.run(write_log=False)

    assert result.status == STATUS_FAILED
    assert result.failed == 1


@responses.activate
@pytest.mark.parametrize(
    "broken",
    [
        {"date": "2026-03-01", "countryCode": "US"},              # no name
        {"name": "Nameless Day", "countryCode": "US"},            # no date
        {"date": "not-a-date", "name": "Bad Date", "countryCode": "US"},
        "a bare string, not an object",
    ],
    ids=["missing-name", "missing-date", "unparseable-date", "not-an-object"],
)
def test_bad_row_fails_alone_and_good_rows_still_import(connector, odoo, broken):
    """Partial-failure continuation: one unusable row must not cost the others."""
    responses.add(
        responses.GET, url_for(),
        json=[broken, holiday("2026-07-04", "Independence Day")],
        status=200,
    )

    result = connector.run(write_log=False)

    assert result.status == STATUS_PARTIAL
    assert result.failed == 1
    assert result.created == 1
    stored = events(odoo)
    assert len(stored) == 1
    assert stored[0]["name"] == "Independence Day (US)"


# -- HTTP 204: the real PK behaviour -----------------------------------------


@responses.activate
def test_204_is_skipped_not_failed(ctx, odoo):
    """The provider has no dataset for PK and says so with 204 No Content."""
    ctx.settings.nager.countries = ["PK"]
    ctx.settings.nager.years = [2026]
    responses.add(responses.GET, url_for("PK"), body="", status=204)

    result = NagerDateConnector(ctx).run(write_log=False)

    assert result.status == STATUS_SKIPPED
    assert result.failed == 0
    assert result.created == 0
    # No record was examined, so no record counter moves; the note carries the reason.
    assert result.processed == 0
    assert events(odoo) == []
    assert any("PK" in note and "no holiday data" in note for note in result.notes)


@responses.activate
def test_204_for_one_country_does_not_hide_another_country_importing(ctx, odoo):
    ctx.settings.nager.countries = ["PK", "US"]
    ctx.settings.nager.years = [2026]
    responses.add(responses.GET, url_for("PK"), body="", status=204)
    responses.add(responses.GET, url_for(), json=TWO_HOLIDAYS, status=200)

    result = NagerDateConnector(ctx).run(write_log=False)

    assert result.status == STATUS_SUCCESS
    assert result.created == 2
    assert result.failed == 0
    assert any("PK" in note for note in result.notes)


@responses.activate
def test_no_country_configured_is_a_skipped_run(ctx):
    ctx.settings.nager.countries = []
    ctx.settings.nager.years = [2026]

    result = NagerDateConnector(ctx).run(write_log=False)

    assert result.status == STATUS_SKIPPED
    assert result.processed == 0
    assert any("NAGER_COUNTRIES" in note for note in result.notes)


# -- duplicate protection and update-in-place --------------------------------


@responses.activate
def test_second_run_skips_every_row_and_creates_no_duplicate(connector, odoo):
    responses.add(responses.GET, url_for(), json=TWO_HOLIDAYS, status=200)
    responses.add(responses.GET, url_for(), json=TWO_HOLIDAYS, status=200)

    first = connector.run(write_log=False)
    count_after_first = len(events(odoo))
    second = connector.run(write_log=False)

    assert first.created == 2
    assert second.created == 0
    assert second.skipped == 2
    assert second.status == STATUS_SUCCESS
    assert len(events(odoo)) == count_after_first == 2


@responses.activate
def test_renamed_holiday_updates_in_place(connector, odoo):
    """A changed name reaches the hash through the mapped values."""
    renamed = [
        holiday("2026-01-01", "New Year's Day", holidayTypes=["Public", "Bank", "Optional"]),
        TWO_HOLIDAYS[1],
    ]
    responses.add(responses.GET, url_for(), json=TWO_HOLIDAYS, status=200)
    responses.add(responses.GET, url_for(), json=renamed, status=200)

    connector.run(write_log=False)
    before = len(events(odoo))
    second = connector.run(write_log=False)

    assert second.updated == 1
    assert second.skipped == 1
    assert second.created == 0
    assert len(events(odoo)) == before  # updated in place, not cloned
    changed = [e for e in events(odoo) if e["name"].startswith("New Year")][0]
    assert "Optional" in changed["description"]


# -- per-country continuation -------------------------------------------------


@responses.activate
def test_one_failing_country_does_not_stop_the_next(ctx, odoo):
    ctx.settings.nager.countries = ["US", "DE"]
    ctx.settings.nager.years = [2026]
    responses.add(responses.GET, url_for("US"), body="boom", status=500)
    responses.add(responses.GET, url_for("DE"), json=[holiday("2026-01-01", "Neujahr", countryCode="DE")], status=200)

    result = NagerDateConnector(ctx).run(write_log=False)

    assert result.status == STATUS_PARTIAL
    assert result.created == 1
    assert result.failed == 1
    assert len(events(odoo)) == 1
    assert events(odoo)[0]["x_external_id"] == "nager:holiday:DE:2026-01-01:neujahr"


@responses.activate
def test_duplicate_country_entries_are_fetched_once(ctx):
    ctx.settings.nager.countries = ["US", "us"]
    ctx.settings.nager.years = [2026]
    responses.add(responses.GET, url_for(), json=TWO_HOLIDAYS, status=200)

    result = NagerDateConnector(ctx).run(write_log=False)

    assert len(responses.calls) == 1
    assert result.created == 2


# -- error details ------------------------------------------------------------


@responses.activate
def test_error_details_are_readable_and_carry_no_secret(connector, settings):
    responses.add(responses.GET, url_for(), body="boom", status=500)

    result = connector.run(write_log=False)
    details = result.error_details()

    assert "ERRORS:" in details
    assert settings.odoo.api_key not in details
    assert json.dumps(result.to_dict(), default=str)  # serialisable for the JSONL fallback


# -- same date, same name, different scope -----------------------------------

#: Both rows are returned by the live US 2026 feed for 2026-04-03.
GOOD_FRIDAY_PUBLIC = holiday(
    "2026-04-03", "Good Friday",
    nationalHoliday=False,
    subdivisionCodes=["US-CT", "US-DE", "US-HI"],
    holidayTypes=["Public"],
)
GOOD_FRIDAY_OPTIONAL = holiday(
    "2026-04-03", "Good Friday",
    nationalHoliday=False,
    subdivisionCodes=["US-TX"],
    holidayTypes=["Optional"],
)


@responses.activate
def test_two_holidays_sharing_a_date_and_name_get_distinct_records(connector, odoo):
    """Regression: country+date+name is not unique in this feed.

    US 2026 returns "Good Friday" twice on 2026-04-03 and "Columbus Day" twice on
    2026-10-12. Keyed on the triple alone the two rows shared one external id and
    overwrote each other on every run, so the sync never converged.
    """
    responses.add(responses.GET, url_for(),
                  json=[GOOD_FRIDAY_PUBLIC, GOOD_FRIDAY_OPTIONAL], status=200)

    result = connector.run(write_log=False)

    assert result.created == 2
    assert result.updated == 0
    stored = events(odoo)
    assert len(stored) == 2
    ids = {e["x_external_id"] for e in stored}
    assert len(ids) == 2, ids
    # The scope is visible in the name, so the two are tellable apart in a calendar.
    names = {e["name"] for e in stored}
    assert "Good Friday (US-TX)" in names
    assert "Good Friday (US-CT +2)" in names


@responses.activate
def test_colliding_rows_stay_stable_across_runs(connector, odoo):
    payload = [GOOD_FRIDAY_PUBLIC, GOOD_FRIDAY_OPTIONAL]
    responses.add(responses.GET, url_for(), json=payload, status=200)
    responses.add(responses.GET, url_for(), json=payload, status=200)

    connector.run(write_log=False)
    second = connector.run(write_log=False)

    assert second.skipped == 2
    assert second.created == 0
    assert second.updated == 0
    assert len(events(odoo)) == 2


@responses.activate
def test_national_holiday_name_still_uses_the_country_code(connector, odoo):
    responses.add(responses.GET, url_for(), json=[TWO_HOLIDAYS[0]], status=200)

    connector.run(write_log=False)

    assert events(odoo)[0]["name"] == "New Year's Day (US)"
