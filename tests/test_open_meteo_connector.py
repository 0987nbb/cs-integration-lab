# -*- coding: utf-8 -*-
"""Open-Meteo connector tests.

Covers the five mandated scenarios plus the two properties that matter most for
this connector: the forecast must be idempotent (an unchanged series writes
nothing at all), and it must never touch the three idempotency columns that the
JSONPlaceholder connector owns on ``res.partner``.
"""
from __future__ import annotations

import json

import pytest
import requests
import responses

from integration_service.connectors.open_meteo_connector import OpenMeteoConnector
from integration_service.sync_result import (
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
)
from tests.conftest import FakeOdooClient

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
PARTNER_MODEL = "res.partner"

DATES = [
    "2026-08-05", "2026-08-06", "2026-08-07", "2026-08-08",
    "2026-08-09", "2026-08-10", "2026-08-11",
]
MAXES = [34.6, 33.7, 33.7, 33.8, 33.8, 33.7, 30.8]
MINS = [26.3, 25.2, 25.2, 25.5, 26.9, 26.6, 25.5]


def forecast_body(times=None, maxes=None, mins=None):
    """The exact response shape the live endpoint returns."""
    return {
        "latitude": 31.528997,
        "longitude": 74.38995,
        "timezone": "Asia/Karachi",
        "daily_units": {
            "time": "iso8601",
            "temperature_2m_max": "°C",
            "temperature_2m_min": "°C",
        },
        "daily": {
            "time": DATES if times is None else times,
            "temperature_2m_max": MAXES if maxes is None else maxes,
            "temperature_2m_min": MINS if mins is None else mins,
        },
    }


def partner(pid, name="Geolocated Contact", lat=31.5204, lon=74.3587, **extra):
    record = {
        "id": pid,
        "name": name,
        "partner_latitude": lat,
        "partner_longitude": lon,
        # Columns owned by the JSONPlaceholder connector; present so the tests can
        # prove this connector leaves them alone.
        "x_external_id": f"jsonplaceholder:user:{pid}",
        "x_source_hash": "owned-by-jsonplaceholder",
    }
    record.update(extra)
    return record


def make_odoo(*partners):
    return FakeOdooClient(seed={PARTNER_MODEL: list(partners)})


def connector_for(ctx, odoo):
    ctx.odoo = odoo
    return OpenMeteoConnector(ctx)


def partner_writes(odoo):
    return odoo.calls_for("write", PARTNER_MODEL)


# -- 1. success ---------------------------------------------------------------


@responses.activate
def test_success_stores_seven_days_and_next_day_fields(ctx):
    odoo = make_odoo(partner(101))
    responses.add(responses.GET, FORECAST_URL, json=forecast_body(), status=200)

    result = connector_for(ctx, odoo).run(write_log=False)

    assert result.status == STATUS_SUCCESS
    assert (result.created, result.updated, result.skipped, result.failed) == (1, 0, 0, 0)

    stored = odoo.records(PARTNER_MODEL)[0]
    days = json.loads(stored["x_forecast_payload"])
    assert len(days) == 7
    assert days[0] == {"date": "2026-08-05", "temp_max": 34.6, "temp_min": 26.3}

    # Next day is index 1: the series starts on the contact's local today.
    assert stored["x_forecast_next_date"] == "2026-08-06"
    assert stored["x_forecast_next_temp_max"] == 33.7
    assert stored["x_forecast_next_temp_min"] == 25.2
    assert stored["x_forecast_next_summary"] == "2026-08-06: max 33.7 C / min 25.2 C"
    assert stored["x_forecast_updated_at"]

    assert result.details["partners"][101] == {"days": 7, "action": "created"}


@responses.activate
def test_request_carries_the_required_query_parameters(ctx):
    odoo = make_odoo(partner(101, lat=52.52, lon=13.405))
    responses.add(responses.GET, FORECAST_URL, json=forecast_body(), status=200)

    connector_for(ctx, odoo).run(write_log=False)

    request = responses.calls[0].request
    assert "latitude=52.52" in request.url
    assert "longitude=13.405" in request.url
    assert "daily=temperature_2m_max%2Ctemperature_2m_min" in request.url
    assert "timezone=auto" in request.url


@responses.activate
def test_start_and_end_times_are_recorded(ctx):
    odoo = make_odoo(partner(101))
    responses.add(responses.GET, FORECAST_URL, json=forecast_body(), status=200)

    result = connector_for(ctx, odoo).run(write_log=False)

    assert result.started_at is not None and result.ended_at is not None
    assert result.ended_at >= result.started_at


# -- 2. timeout ---------------------------------------------------------------


@responses.activate
def test_timeout_fails_the_partner_and_the_run(ctx, sleep_calls):
    odoo = make_odoo(partner(101))
    responses.add(responses.GET, FORECAST_URL, body=requests.exceptions.ConnectTimeout())

    result = connector_for(ctx, odoo).run(write_log=False)

    assert result.status == STATUS_FAILED
    assert result.failed == 1
    assert len(responses.calls) == 3          # HTTP_MAX_RETRIES=2 -> 3 attempts
    assert len(sleep_calls) == 2
    assert partner_writes(odoo) == []


# -- 3. HTTP 429 --------------------------------------------------------------


@responses.activate
def test_rate_limit_is_retried_honouring_retry_after(ctx, sleep_calls):
    odoo = make_odoo(partner(101))
    responses.add(responses.GET, FORECAST_URL, json={"reason": "slow down"}, status=429,
                  headers={"Retry-After": "9"})
    responses.add(responses.GET, FORECAST_URL, json=forecast_body(), status=200)

    result = connector_for(ctx, odoo).run(write_log=False)

    assert result.status == STATUS_SUCCESS
    assert result.created == 1
    assert sleep_calls == [9.0]


# -- 4. HTTP 500 --------------------------------------------------------------


@responses.activate
def test_server_error_fails_the_run(ctx):
    odoo = make_odoo(partner(101))
    responses.add(responses.GET, FORECAST_URL, body="upstream exploded", status=500)

    result = connector_for(ctx, odoo).run(write_log=False)

    assert result.status == STATUS_FAILED
    assert result.failed == 1
    assert len(responses.calls) == 3
    assert partner_writes(odoo) == []


# -- 5. invalid payload -------------------------------------------------------


@responses.activate
@pytest.mark.parametrize(
    "body, marker",
    [
        ({"latitude": 1.0}, "'daily' is NoneType"),
        ({"daily": []}, "'daily' is list"),
        ({"daily": {"time": DATES, "temperature_2m_max": MAXES}}, "missing 'temperature_2m_min'"),
        ({"daily": {"time": DATES[:3], "temperature_2m_max": MAXES,
                    "temperature_2m_min": MINS}}, "mismatched lengths"),
        ({"daily": {"time": DATES, "temperature_2m_max": [None] + MAXES[1:],
                    "temperature_2m_min": MINS}}, "is null"),
        ({"daily": {"time": [], "temperature_2m_max": [], "temperature_2m_min": []}},
         "carried no days"),
    ],
    ids=["no-daily", "daily-not-object", "missing-column", "length-mismatch",
         "null-temperature", "empty-series"],
)
def test_malformed_daily_block_is_rejected(ctx, body, marker):
    odoo = make_odoo(partner(101))
    responses.add(responses.GET, FORECAST_URL, json=body, status=200)

    result = connector_for(ctx, odoo).run(write_log=False)

    assert result.status == STATUS_FAILED
    assert result.failed == 1
    assert partner_writes(odoo) == []
    assert any(marker in error for error in result.errors), result.errors


@responses.activate
def test_non_json_body_is_rejected(ctx):
    odoo = make_odoo(partner(101))
    responses.add(responses.GET, FORECAST_URL, body="<html>maintenance</html>", status=200)

    result = connector_for(ctx, odoo).run(write_log=False)

    assert result.status == STATUS_FAILED
    assert partner_writes(odoo) == []


@responses.activate
def test_bad_partner_does_not_stop_the_good_one(ctx):
    """Partial-failure continuation across contacts."""
    odoo = make_odoo(partner(101, name="Broken"), partner(202, name="Fine"))
    responses.add(responses.GET, FORECAST_URL, json={"daily": {"time": []}}, status=200)
    responses.add(responses.GET, FORECAST_URL, json=forecast_body(), status=200)

    result = connector_for(ctx, odoo).run(write_log=False)

    assert result.status == STATUS_PARTIAL
    assert result.failed == 1
    assert result.created == 1
    written = partner_writes(odoo)
    assert len(written) == 1
    assert written[0]["ids"] == [202]


# -- idempotency --------------------------------------------------------------


@responses.activate
def test_unchanged_forecast_is_skipped_without_any_write(ctx):
    odoo = make_odoo(partner(101))
    responses.add(responses.GET, FORECAST_URL, json=forecast_body(), status=200)
    responses.add(responses.GET, FORECAST_URL, json=forecast_body(), status=200)

    first = connector_for(ctx, odoo).run(write_log=False)
    writes_after_first = len(partner_writes(odoo))
    second = connector_for(ctx, odoo).run(write_log=False)

    assert first.created == 1
    assert second.skipped == 1
    assert second.created == 0 and second.updated == 0
    # A record was examined and needed nothing, which is a successful no-op run.
    # STATUS_SKIPPED is reserved for a run that had nothing to examine at all.
    assert second.status == STATUS_SUCCESS
    # The decisive assertion: a no-op run issues no write at all.
    assert len(partner_writes(odoo)) == writes_after_first == 1


@responses.activate
def test_changed_forecast_is_an_update(ctx):
    shifted = [m + 1.0 for m in MAXES]
    odoo = make_odoo(partner(101))
    responses.add(responses.GET, FORECAST_URL, json=forecast_body(), status=200)
    responses.add(responses.GET, FORECAST_URL, json=forecast_body(maxes=shifted), status=200)

    connector_for(ctx, odoo).run(write_log=False)
    second = connector_for(ctx, odoo).run(write_log=False)

    assert second.updated == 1
    assert second.created == 0
    assert odoo.records(PARTNER_MODEL)[0]["x_forecast_next_temp_max"] == 34.7


# -- ownership of the idempotency columns -------------------------------------


@responses.activate
def test_never_writes_the_columns_owned_by_jsonplaceholder(ctx):
    """Writing x_source_hash here would put the two connectors in an update loop."""
    odoo = make_odoo(partner(101))
    responses.add(responses.GET, FORECAST_URL, json=forecast_body(), status=200)

    connector_for(ctx, odoo).run(write_log=False)

    forbidden = {"x_external_id", "x_source_hash", "x_external_updated_at"}
    for call in partner_writes(odoo):
        assert forbidden.isdisjoint(call["vals"]), call["vals"]

    stored = odoo.records(PARTNER_MODEL)[0]
    assert stored["x_external_id"] == "jsonplaceholder:user:101"
    assert stored["x_source_hash"] == "owned-by-jsonplaceholder"


# -- contact selection --------------------------------------------------------


@responses.activate
def test_no_geolocated_contact_is_a_skipped_run(ctx):
    odoo = make_odoo(partner(101, lat=0.0, lon=0.0))

    result = connector_for(ctx, odoo).run(write_log=False)

    assert result.status == STATUS_SKIPPED
    assert result.processed == 0
    assert any("JSONPlaceholder" in note for note in result.notes)
    assert len(responses.calls) == 0


@responses.activate
def test_explicit_partner_ids_win_over_the_geolocated_domain(ctx):
    odoo = make_odoo(partner(101), partner(202))
    ctx.settings.open_meteo.partner_ids = [202]
    responses.add(responses.GET, FORECAST_URL, json=forecast_body(), status=200)

    result = connector_for(ctx, odoo).run(write_log=False)

    assert result.created == 1
    assert partner_writes(odoo)[0]["ids"] == [202]


@responses.activate
def test_impossible_coordinates_fail_without_spending_a_request(ctx):
    odoo = make_odoo(partner(101, lat=999.0, lon=0.0))

    result = connector_for(ctx, odoo).run(write_log=False)

    assert result.status == STATUS_FAILED
    assert result.failed == 1
    assert len(responses.calls) == 0
    assert any("outside the valid range" in error for error in result.errors)


@responses.activate
def test_short_series_is_stored_and_noted(ctx):
    odoo = make_odoo(partner(101))
    responses.add(responses.GET, FORECAST_URL,
                  json=forecast_body(times=DATES[:2], maxes=MAXES[:2], mins=MINS[:2]), status=200)

    result = connector_for(ctx, odoo).run(write_log=False)

    assert result.created == 1
    assert len(json.loads(odoo.records(PARTNER_MODEL)[0]["x_forecast_payload"])) == 2
    assert any("OPEN_METEO_FORECAST_DAYS" in note for note in result.notes)


@responses.activate
def test_single_day_series_falls_back_to_that_day(ctx):
    odoo = make_odoo(partner(101))
    responses.add(responses.GET, FORECAST_URL,
                  json=forecast_body(times=DATES[:1], maxes=MAXES[:1], mins=MINS[:1]), status=200)

    result = connector_for(ctx, odoo).run(write_log=False)

    assert result.created == 1
    assert odoo.records(PARTNER_MODEL)[0]["x_forecast_next_date"] == "2026-08-05"
    assert any("single day" in note for note in result.notes)


# -- dry run ------------------------------------------------------------------


@responses.activate
def test_dry_run_writes_nothing_and_marks_the_write(ctx):
    odoo = make_odoo(partner(101))
    ctx.settings.dry_run = True
    responses.add(responses.GET, FORECAST_URL, json=forecast_body(), status=200)

    result = connector_for(ctx, odoo).run(write_log=False)

    assert partner_writes(odoo) == []
    assert result.mock_writes
    assert all(entry.startswith("[MOCK WRITE]") for entry in result.mock_writes)
