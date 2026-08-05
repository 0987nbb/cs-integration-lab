# -*- coding: utf-8 -*-
"""Frankfurter connector tests.

Covers the five mandated scenarios plus the three requirements specific to FX:
decimal-safe conversion onto the company currency (PKR, not the API base USD),
one rate per currency per day, and the rule that a move larger than 5% is
withheld and raised as a ``mail.activity`` for approval instead of being written.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
import requests
import responses

from integration_service.connectors.frankfurter_connector import FrankfurterConnector
from integration_service.errors import OdooError
from integration_service.sync_result import (
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_SUCCESS,
)

RATES_URL = "https://api.frankfurter.dev/v2/rates"
RATE_MODEL = "res.currency.rate"
ACTIVITY_MODEL = "mail.activity"
CURRENCY_MODEL = "res.currency"

DATE = "2026-08-05"

# The exact numbers the live endpoint returned during development.
USD_EUR = "0.86696"
USD_GBP = "0.74336"
USD_PKR = "278.58"
USD_TRY = "47.562"

#: Company currency is PKR, so an Odoo rate is EUR-per-PKR, not EUR-per-USD.
EXPECTED_EUR = Decimal(USD_EUR) / Decimal(USD_PKR)
EXPECTED_GBP = Decimal(USD_GBP) / Decimal(USD_PKR)


def quote(code, rate, date=DATE, base="USD"):
    return {"date": date, "base": base, "quote": code, "rate": rate}


def live_payload(date=DATE):
    return [
        quote("EUR", float(USD_EUR), date),
        quote("GBP", float(USD_GBP), date),
        quote("PKR", float(USD_PKR), date),
        quote("TRY", float(USD_TRY), date),
    ]


@pytest.fixture
def connector(ctx):
    return FrankfurterConnector(ctx)


def rates(odoo):
    return odoo.records(RATE_MODEL)


def rate_for(odoo, currency_id):
    return next((r for r in rates(odoo) if r.get("currency_id") == currency_id), None)


def activities(odoo):
    return odoo.records(ACTIVITY_MODEL)


def seed_rate(odoo, currency_id, date, rate, company_id=1):
    odoo.store.setdefault(RATE_MODEL, []).append({
        "id": 9000 + len(odoo.records(RATE_MODEL)),
        "currency_id": currency_id,
        "company_id": company_id,
        "name": date,
        "rate": rate,
    })


# -- 1. success ---------------------------------------------------------------


@responses.activate
def test_success_converts_onto_the_company_currency(connector, odoo):
    responses.add(responses.GET, RATES_URL, json=live_payload(), status=200)

    result = connector.run(write_log=False)

    assert result.status == STATUS_SUCCESS
    # EUR, GBP, TRY are written; PKR is the company currency and is skipped.
    assert result.created == 3
    assert result.skipped == 1
    assert result.failed == 0

    eur = rate_for(odoo, 126)
    assert eur is not None
    assert eur["name"] == DATE
    assert eur["company_id"] == 1
    # The decisive assertion: the stored value is USD->EUR divided by USD->PKR.
    assert abs(Decimal(str(eur["rate"])) - EXPECTED_EUR) < Decimal("1e-12")

    gbp = rate_for(odoo, 144)
    assert abs(Decimal(str(gbp["rate"])) - EXPECTED_GBP) < Decimal("1e-12")

    assert result.details["conversion"]["company_currency"] == "PKR"
    assert result.details["conversion"]["divisor"] == USD_PKR
    assert result.details["rates"]["EUR"]["action"] == "created"


@responses.activate
def test_company_currency_is_skipped_with_a_reason(connector, odoo):
    responses.add(responses.GET, RATES_URL, json=live_payload(), status=200)

    result = connector.run(write_log=False)

    assert rate_for(odoo, 160) is None  # no PKR row
    assert any("company currency" in note for note in result.notes)


@responses.activate
def test_inactive_currencies_are_activated_before_their_rate_is_stored(connector, odoo):
    responses.add(responses.GET, RATES_URL, json=live_payload(), status=200)

    connector.run(write_log=False)

    activated = {
        call["ids"][0]
        for call in odoo.calls_for("write", CURRENCY_MODEL)
        if call["vals"].get("active") is True
    }
    assert {126, 144, 31} <= activated
    for currency in odoo.records(CURRENCY_MODEL):
        if currency["id"] in (126, 144, 31):
            assert currency["active"] is True


@responses.activate
def test_request_uses_the_required_query_parameters(connector):
    responses.add(responses.GET, RATES_URL, json=live_payload(), status=200)

    connector.run(write_log=False)

    url = responses.calls[0].request.url
    assert "base=USD" in url
    assert "EUR" in url and "GBP" in url and "TRY" in url and "PKR" in url


@responses.activate
def test_start_and_end_times_are_recorded(connector):
    responses.add(responses.GET, RATES_URL, json=live_payload(), status=200)

    result = connector.run(write_log=False)

    assert result.started_at is not None and result.ended_at is not None
    assert result.ended_at >= result.started_at


# -- 2. timeout ---------------------------------------------------------------


@responses.activate
def test_timeout_fails_the_run(connector, odoo, sleep_calls):
    responses.add(responses.GET, RATES_URL, body=requests.exceptions.ConnectTimeout())

    result = connector.run(write_log=False)

    assert result.status == STATUS_FAILED
    assert rates(odoo) == []
    assert len(responses.calls) == 3
    assert len(sleep_calls) == 2


# -- 3. HTTP 429 --------------------------------------------------------------


@responses.activate
def test_rate_limit_is_retried_honouring_retry_after(connector, sleep_calls):
    responses.add(responses.GET, RATES_URL, json={"message": "slow down"}, status=429,
                  headers={"Retry-After": "11"})
    responses.add(responses.GET, RATES_URL, json=live_payload(), status=200)

    result = connector.run(write_log=False)

    assert result.status == STATUS_SUCCESS
    assert result.created == 3
    assert sleep_calls == [11.0]


# -- 4. HTTP 500 --------------------------------------------------------------


@responses.activate
def test_server_error_fails_the_run(connector, odoo):
    responses.add(responses.GET, RATES_URL, body="upstream exploded", status=500)

    result = connector.run(write_log=False)

    assert result.status == STATUS_FAILED
    assert rates(odoo) == []
    assert len(responses.calls) == 3


# -- 5. invalid payload -------------------------------------------------------


@responses.activate
def test_non_json_body_fails_the_run(connector):
    responses.add(responses.GET, RATES_URL, body="<html>maintenance</html>", status=200)

    result = connector.run(write_log=False)

    assert result.status == STATUS_FAILED


@responses.activate
def test_unusable_json_shape_fails_the_run(connector):
    responses.add(responses.GET, RATES_URL, json={"unexpected": "envelope"}, status=200)

    result = connector.run(write_log=False)

    assert result.status == STATUS_FAILED
    assert any("expected a list of quotes" in error for error in result.errors)


@responses.activate
def test_wrong_base_is_rejected(connector):
    responses.add(responses.GET, RATES_URL,
                  json=[quote("EUR", 0.86, base="CHF")], status=200)

    result = connector.run(write_log=False)

    assert result.status == STATUS_FAILED
    assert any("base" in error.lower() for error in result.errors)


@responses.activate
@pytest.mark.parametrize("bad_rate", [None, "not-a-number", 0, -1.5],
                         ids=["null", "text", "zero", "negative"])
def test_one_unusable_rate_fails_alone(connector, odoo, bad_rate):
    """Partial-failure continuation: the other currencies still sync."""
    payload = [
        quote("EUR", bad_rate),
        quote("GBP", float(USD_GBP)),
        quote("PKR", float(USD_PKR)),
    ]
    responses.add(responses.GET, RATES_URL, json=payload, status=200)

    result = connector.run(write_log=False)

    assert result.status == STATUS_PARTIAL
    assert result.failed == 1
    assert result.created == 1               # GBP still stored
    assert rate_for(odoo, 126) is None       # EUR not stored
    assert rate_for(odoo, 144) is not None


@responses.activate
def test_bad_date_fails_that_currency_alone(connector, odoo):
    payload = [
        quote("EUR", float(USD_EUR), date="05/08/2026"),
        quote("GBP", float(USD_GBP)),
        quote("PKR", float(USD_PKR)),
    ]
    responses.add(responses.GET, RATES_URL, json=payload, status=200)

    result = connector.run(write_log=False)

    assert result.status == STATUS_PARTIAL
    assert result.failed == 1
    assert result.created == 1


@responses.activate
def test_missing_company_currency_is_run_fatal(connector):
    """Without USD->PKR there is no divisor, so nothing can be rebased."""
    responses.add(responses.GET, RATES_URL,
                  json=[quote("EUR", float(USD_EUR)), quote("GBP", float(USD_GBP))], status=200)

    result = connector.run(write_log=False)

    assert result.status == STATUS_FAILED
    assert any("PKR" in error for error in result.errors)


# -- idempotency and duplicate protection ------------------------------------


@responses.activate
def test_second_run_with_unchanged_rates_skips_everything(connector, odoo):
    responses.add(responses.GET, RATES_URL, json=live_payload(), status=200)
    responses.add(responses.GET, RATES_URL, json=live_payload(), status=200)

    first = connector.run(write_log=False)
    count_after_first = len(rates(odoo))
    second = connector.run(write_log=False)

    assert first.created == 3
    assert second.created == 0
    assert second.updated == 0
    assert second.skipped == 4               # 3 unchanged rates + the PKR company skip
    assert len(rates(odoo)) == count_after_first == 3


@responses.activate
def test_changed_rate_updates_the_same_row(connector, odoo):
    """One row per currency per day: a new value updates, never duplicates."""
    nudged = [
        quote("EUR", float(Decimal(USD_EUR) * Decimal("1.01"))),   # +1%, under threshold
        quote("GBP", float(USD_GBP)),
        quote("PKR", float(USD_PKR)),
    ]
    responses.add(responses.GET, RATES_URL, json=live_payload(), status=200)
    responses.add(responses.GET, RATES_URL, json=nudged, status=200)

    connector.run(write_log=False)
    before = len(rates(odoo))
    second = connector.run(write_log=False)

    assert second.updated == 1
    assert second.created == 0
    assert len(rates(odoo)) == before


@responses.activate
def test_existing_rate_for_the_same_day_is_not_duplicated(connector, odoo):
    seed_rate(odoo, 126, DATE, float(EXPECTED_EUR))
    responses.add(responses.GET, RATES_URL, json=live_payload(), status=200)

    result = connector.run(write_log=False)

    eur_rows = [r for r in rates(odoo) if r.get("currency_id") == 126]
    assert len(eur_rows) == 1
    assert result.details["rates"]["EUR"]["action"] == "unchanged"


@responses.activate
def test_concurrent_duplicate_is_downgraded_to_a_skip(connector, odoo):
    """The DB constraint means another run won the race, not that we failed."""
    odoo.fail_on(RATE_MODEL, "create", OdooError(
        "Odoo res.currency.rate.create failed (HTTP 422): "
        "The operation cannot be completed: Only one currency rate per day allowed!",
        status_code=422, model=RATE_MODEL, method="create",
    ))
    responses.add(responses.GET, RATES_URL, json=live_payload(), status=200)

    result = connector.run(write_log=False)

    assert result.failed == 0
    assert result.skipped == 4               # 3 duplicate races + the PKR company skip
    assert result.status == STATUS_SUCCESS
    assert result.details["rates"]["EUR"]["action"] == "duplicate"


# -- the >5% approval rule ----------------------------------------------------


@responses.activate
def test_large_move_is_withheld_and_raises_an_approval_activity(connector, odoo):
    # Prior EUR rate 10% below the incoming one.
    prior = EXPECTED_EUR / Decimal("1.10")
    seed_rate(odoo, 126, "2026-08-04", float(prior))
    responses.add(responses.GET, RATES_URL, json=live_payload(), status=200)

    result = connector.run(write_log=False)

    # No EUR rate for today was written.
    assert [r for r in rates(odoo) if r.get("currency_id") == 126 and r["name"] == DATE] == []
    assert result.details["rates"]["EUR"]["action"] == "pending_approval"
    assert result.details["rates"]["EUR"]["pct_change"] == pytest.approx(10.0, abs=0.05)
    # Withheld, not failed.
    assert result.failed == 0
    assert result.created == 2               # GBP and TRY still written

    created = activities(odoo)
    assert len(created) == 1
    activity = created[0]
    assert activity["user_id"] == 2                     # lowest-id internal user
    assert activity["activity_type_id"] == 4            # "To-Do"
    assert activity["date_deadline"] == DATE
    assert "EUR" in activity["summary"]
    assert "10" in activity["summary"]
    assert str(prior)[:8] in activity["note"] or "Prior rate" in activity["note"]


@responses.activate
def test_approval_activity_is_not_anchored_on_res_currency(connector, odoo):
    """res.currency has no mail.activity.mixin, so anchoring there raises HTTP 500."""
    seed_rate(odoo, 126, "2026-08-04", float(EXPECTED_EUR / Decimal("1.10")))
    responses.add(responses.GET, RATES_URL, json=live_payload(), status=200)

    connector.run(write_log=False)

    activity = activities(odoo)[0]
    assert activity["res_model"] != "res.currency"
    assert activity["res_model"] == "res.users"
    assert activity["res_id"] == 2
    assert activity["res_model_id"] == 102


@responses.activate
def test_approval_activity_is_created_once_across_runs(connector, odoo):
    seed_rate(odoo, 126, "2026-08-04", float(EXPECTED_EUR / Decimal("1.10")))
    responses.add(responses.GET, RATES_URL, json=live_payload(), status=200)
    responses.add(responses.GET, RATES_URL, json=live_payload(), status=200)

    connector.run(write_log=False)
    assert len(activities(odoo)) == 1
    second = connector.run(write_log=False)

    assert len(activities(odoo)) == 1
    assert any("already exists" in note for note in second.notes)


@responses.activate
def test_move_below_the_threshold_is_written_automatically(connector, odoo):
    """4.9% is under the 5% threshold and must not need approval."""
    prior = EXPECTED_EUR / Decimal("1.049")
    seed_rate(odoo, 126, "2026-08-04", float(prior))
    responses.add(responses.GET, RATES_URL, json=live_payload(), status=200)

    result = connector.run(write_log=False)

    assert activities(odoo) == []
    assert result.details["rates"]["EUR"]["action"] == "created"
    stored = [r for r in rates(odoo) if r.get("currency_id") == 126 and r["name"] == DATE]
    assert len(stored) == 1


@responses.activate
def test_first_ever_rate_has_no_baseline_and_is_written(connector, odoo):
    responses.add(responses.GET, RATES_URL, json=live_payload(), status=200)

    result = connector.run(write_log=False)

    assert activities(odoo) == []
    assert result.created == 3
    assert result.details["rates"]["EUR"]["prior"] is None


@responses.activate
def test_configured_approver_login_is_used(ctx, odoo):
    ctx.settings.frankfurter.approver_login = "second@test"
    seed_rate(odoo, 126, "2026-08-04", float(EXPECTED_EUR / Decimal("1.10")))
    responses.add(responses.GET, RATES_URL, json=live_payload(), status=200)

    FrankfurterConnector(ctx).run(write_log=False)

    assert activities(odoo)[0]["user_id"] == 5


@responses.activate
def test_failure_to_create_the_activity_still_withholds_the_rate(connector, odoo):
    """The withheld write is the point; a missing reminder must not undo it."""
    seed_rate(odoo, 126, "2026-08-04", float(EXPECTED_EUR / Decimal("1.10")))
    odoo.fail_on(ACTIVITY_MODEL, "create", OdooError("boom", status_code=500))
    responses.add(responses.GET, RATES_URL, json=live_payload(), status=200)

    result = connector.run(write_log=False)

    assert [r for r in rates(odoo) if r.get("currency_id") == 126 and r["name"] == DATE] == []
    assert result.failed == 0
    assert any("could not be created" in note for note in result.notes)


# -- decimal safety -----------------------------------------------------------


@responses.activate
def test_conversion_never_routes_through_binary_float(connector, odoo):
    """A rate chosen to expose float error must still convert exactly."""
    responses.add(responses.GET, RATES_URL, json=[
        quote("EUR", "0.1"),
        quote("PKR", "0.3"),
    ], status=200)

    connector.run(write_log=False)

    # Decimal("0.1")/Decimal("0.3") quantised to 12dp; the float path would give
    # 0.33333333333333337 and fail this comparison.
    expected = (Decimal("0.1") / Decimal("0.3")).quantize(Decimal("0.000000000001"))
    assert Decimal(str(rate_for(odoo, 126)["rate"])) == expected


@responses.activate
def test_v1_envelope_shape_is_still_accepted(connector, odoo):
    responses.add(responses.GET, RATES_URL, json={
        "date": DATE, "base": "USD",
        "rates": {"EUR": float(USD_EUR), "PKR": float(USD_PKR)},
    }, status=200)

    result = connector.run(write_log=False)

    assert result.created == 1
    assert abs(Decimal(str(rate_for(odoo, 126)["rate"])) - EXPECTED_EUR) < Decimal("1e-12")


# -- security -----------------------------------------------------------------


@responses.activate
def test_error_details_carry_no_credential(connector, settings):
    responses.add(responses.GET, RATES_URL, body="boom", status=500)

    result = connector.run(write_log=False)

    assert settings.odoo.api_key not in result.error_details()
