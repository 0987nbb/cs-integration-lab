# -*- coding: utf-8 -*-
"""Final-pass tests for the four areas the trial sign-off covers.

These are deliberately behavioural. An earlier version of this file asserted
that a method returned a ``list`` and that a report dict had certain keys, which
passes just as happily against an implementation that does nothing - the fake
"Sync Now" server action that shipped in the trial would have satisfied every
one of them.

Area 3 in particular is a truth table, not a single case: the assignment asks
for an approval activity when the mapping is incomplete **or** a rate moves more
than 5%, so all four combinations are asserted, including the one where nothing
should happen at all.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
import responses

from integration_service.connectors.base import BaseConnector, ConnectorContext
from integration_service.connectors.frankfurter_connector import FrankfurterConnector
from integration_service.connectors.github_connector import GitHubConnector
from integration_service.connectors.jsonplaceholder_connector import JsonPlaceholderConnector
from integration_service.provisioning import (
    CONFIG_FIELD_SPECS,
    SYNC_LOG_FIELD_SPECS,
    ensure_fields,
    ensure_idempotency_fields,
    ensure_integration_config_records,
    ensure_model,
    ensure_partner_forecast_view,
    provision,
)
from integration_service.sync_request import (
    STATE_DONE,
    STATE_FAILED,
    STATE_REQUESTED,
    STATE_RUNNING,
    SYNC_NOW_ACTION_CODE,
    SyncRequestWorker,
)
from integration_service.sync_result import STATUS_FAILED, STATUS_SUCCESS, SyncResult

RATES_URL = "https://api.frankfurter.dev/v2/rates"
RATE_MODEL = "res.currency.rate"
ACTIVITY_MODEL = "mail.activity"
CONFIG_MODEL = "x_integration_config"

DATE = "2026-08-05"
USD_EUR = "0.86696"
USD_GBP = "0.74336"
USD_PKR = "278.58"
USD_TRY = "47.562"

EXPECTED_EUR = Decimal(USD_EUR) / Decimal(USD_PKR)
EXPECTED_GBP = Decimal(USD_GBP) / Decimal(USD_PKR)


def quote(code, rate, date=DATE, base="USD"):
    return {"date": date, "base": base, "quote": code, "rate": rate}


def payload(date=DATE):
    return [
        quote("EUR", float(USD_EUR), date),
        quote("GBP", float(USD_GBP), date),
        quote("PKR", float(USD_PKR), date),
        quote("TRY", float(USD_TRY), date),
    ]


def activities(odoo):
    return odoo.records(ACTIVITY_MODEL)


def seed_rate(odoo, currency_id, date, rate, company_id=1):
    odoo._insert(RATE_MODEL, {"currency_id": currency_id, "company_id": company_id,
                              "name": date, "rate": rate})


def unmap_currency(odoo, code):
    """Delete a currency so its quote has no Odoo record to land on.

    This is the assignment's "required relation cannot be resolved" case,
    reproduced exactly: the feed offers a rate for a currency Odoo does not have.
    """
    odoo.store["res.currency"] = [c for c in odoo.records("res.currency") if c["name"] != code]


# ===========================================================================
# AREA 3 - approval rules: incomplete mapping AND/OR currency move > 5%
# ===========================================================================

@responses.activate
def test_1_incomplete_mapping_true_and_small_move_raises_an_activity(connector, odoo):
    """TEST 1: mapping incomplete, rate move <= 5% -> activity, because of mapping."""
    unmap_currency(odoo, "EUR")                                   # incomplete mapping
    seed_rate(odoo, 144, "2026-08-04", float(EXPECTED_GBP / Decimal("1.01")))  # GBP +1%

    responses.add(responses.GET, RATES_URL, json=payload(), status=200)
    result = connector.run(write_log=False)

    raised = activities(odoo)
    assert len(raised) == 1
    assert "Unmapped Currency EUR" in raised[0]["summary"]
    assert "cannot be resolved" in raised[0]["note"]
    # The GBP move is under the threshold, so it was written rather than held.
    assert result.details["rates"]["GBP"]["action"] == "created"
    assert result.details["rates"]["GBP"]["pct_change"] == pytest.approx(1.0, abs=0.05)


@responses.activate
def test_2_complete_mapping_and_large_move_raises_an_activity(connector, odoo):
    """TEST 2: mapping complete, rate move > 5% -> activity, because of the rate."""
    seed_rate(odoo, 126, "2026-08-04", float(EXPECTED_EUR / Decimal("1.10")))  # EUR +10%

    responses.add(responses.GET, RATES_URL, json=payload(), status=200)
    result = connector.run(write_log=False)

    raised = activities(odoo)
    assert len(raised) == 1
    assert "EUR" in raised[0]["summary"] and "rate change" in raised[0]["summary"]
    assert result.details["rates"]["EUR"]["action"] == "pending_approval"
    # Withheld, not written.
    assert [r for r in odoo.records(RATE_MODEL)
            if r.get("currency_id") == 126 and r["name"] == DATE] == []


@responses.activate
def test_3_complete_mapping_and_small_move_raises_nothing(connector, odoo):
    """TEST 3: mapping complete, move <= 5% -> no approval activity at all."""
    seed_rate(odoo, 126, "2026-08-04", float(EXPECTED_EUR / Decimal("1.049")))  # EUR +4.9%
    seed_rate(odoo, 144, "2026-08-04", float(EXPECTED_GBP / Decimal("1.01")))   # GBP +1%

    responses.add(responses.GET, RATES_URL, json=payload(), status=200)
    result = connector.run(write_log=False)

    assert activities(odoo) == []
    assert result.details["rates"]["EUR"]["action"] == "created"
    assert result.details["rates"]["EUR"]["pct_change"] == pytest.approx(4.9, abs=0.05)
    assert result.status == STATUS_SUCCESS


@responses.activate
def test_4_both_conditions_true_lose_neither_reason(connector, odoo):
    """TEST 4: both conditions -> two activities, each naming its own reason."""
    unmap_currency(odoo, "GBP")                                               # incomplete
    seed_rate(odoo, 126, "2026-08-04", float(EXPECTED_EUR / Decimal("1.10")))  # EUR +10%

    responses.add(responses.GET, RATES_URL, json=payload(), status=200)
    result = connector.run(write_log=False)

    summaries = sorted(a["summary"] for a in activities(odoo))
    assert len(summaries) == 2, summaries
    assert any("Unmapped Currency GBP" in s for s in summaries)
    assert any("EUR" in s and "rate change" in s for s in summaries)
    # Neither reason is silently absorbed by the other.
    assert result.details["rates"]["EUR"]["action"] == "pending_approval"
    assert result.details["rates"]["GBP"]["action"] == "failed"


@responses.activate
def test_approval_activity_carries_the_context_a_reviewer_needs(connector, odoo):
    seed_rate(odoo, 126, "2026-08-04", float(EXPECTED_EUR / Decimal("1.10")))
    responses.add(responses.GET, RATES_URL, json=payload(), status=200)

    connector.run(write_log=False)

    note = activities(odoo)[0]["note"]
    for expected in ("EUR", "Prior rate", "Proposed rate", "Change", "Source"):
        assert expected in note, f"{expected!r} missing from the approval activity"


@responses.activate
def test_incomplete_mapping_is_not_counted_as_both_skipped_and_failed(connector, odoo):
    """One unmappable record is one number in the log, not two.

    The withhold path records a skip and the caller then raises, which the guard
    charges to `failed`. Counting both made a single bad currency read as two
    problems in the sync log.
    """
    unmap_currency(odoo, "EUR")
    responses.add(responses.GET, RATES_URL, json=payload(), status=200)

    result = connector.run(write_log=False)

    assert result.failed == 1
    # PKR (company currency) is the only legitimate skip here.
    assert result.skipped == 1
    assert any("withheld awaiting approval" in note for note in result.notes)


# ===========================================================================
# AREA 3 - incomplete mapping in the other connectors
# ===========================================================================

def test_github_issue_missing_a_title_is_held_even_though_it_has_a_number(ctx, odoo):
    """An `and` here used to let a titleless issue through as "#12 (no title)"."""
    connector = GitHubConnector(ctx)
    result = SyncResult("github")

    outcome, task_id = connector._sync_issue(
        {"id": 4711, "number": 12, "title": "  ", "state": "open"},
        "github:issue:4711", None, _upserter(connector, result), result,
    )

    assert outcome == "skipped"
    assert task_id is None
    raised = activities(odoo)
    assert len(raised) == 1
    assert "Incomplete Mapping" in raised[0]["summary"]
    assert "title" in raised[0]["note"]
    assert "github:issue:4711" in raised[0]["note"]


def test_jsonplaceholder_post_with_an_unresolvable_author_is_flagged_not_discarded(ctx, odoo):
    """A missing *link* is reviewed; the ticket itself is still worth keeping.

    Withholding here would throw away a real customer report to keep the
    bookkeeping tidy. partner_id is optional on a ticket, so the post is
    imported unlinked and the gap is raised as an activity instead.
    """
    connector = JsonPlaceholderConnector(ctx)
    result = SyncResult("jsonplaceholder")

    vals = connector._map_post({"id": 10, "title": "Real title", "body": "b", "userId": 99},
                               "helpdesk.ticket", None, "jsonplaceholder:post:10", result=result)

    assert vals["name"] == "Real title"          # imported, not discarded
    assert "partner_id" not in vals              # but not linked to a wrong contact
    raised = activities(odoo)
    assert len(raised) == 1
    assert "Unresolved Author" in raised[0]["summary"]
    assert "does not resolve to a res.partner" in raised[0]["note"]
    assert "jsonplaceholder:user:99" in raised[0]["note"]
    assert "was</strong> imported" in raised[0]["note"]


def test_jsonplaceholder_post_without_a_title_is_withheld(ctx, odoo):
    """A missing required value is disqualifying, unlike a missing link."""
    connector = JsonPlaceholderConnector(ctx)
    connector._partner_by_user_id[1] = 4242
    result = SyncResult("jsonplaceholder")

    with pytest.raises(Exception):
        connector._map_post({"id": 11, "title": "   ", "body": "b", "userId": 1},
                            "helpdesk.ticket", None, "jsonplaceholder:post:11", result=result)

    raised = activities(odoo)
    assert len(raised) == 1
    assert "Incomplete Mapping" in raised[0]["summary"]
    assert "'title' is empty" in raised[0]["note"]
    assert "not</strong> written" in raised[0]["note"]


def test_a_fully_mapped_post_raises_no_activity(ctx, odoo):
    connector = JsonPlaceholderConnector(ctx)
    connector._partner_by_user_id[99] = 4242
    result = SyncResult("jsonplaceholder")

    vals = connector._map_post({"id": 10, "title": "Real title", "body": "b", "userId": 99},
                               "helpdesk.ticket", None, "jsonplaceholder:post:10", result=result)

    assert activities(odoo) == []
    assert vals["name"] == "Real title"


def test_the_same_incomplete_mapping_does_not_file_a_second_activity(ctx, odoo):
    connector = GitHubConnector(ctx)
    issue = {"id": 4711, "number": 12, "title": "", "state": "open"}

    for _ in range(3):
        result = SyncResult("github")
        connector._sync_issue(issue, "github:issue:4711", None,
                              _upserter(connector, result), result)

    assert len(activities(odoo)) == 1


def _upserter(connector, result):
    from integration_service.idempotency import Upserter
    return Upserter(connector.odoo, "project.task", result, dry_run=False)


# ===========================================================================
# AREA 1 - the Sync Now request queue
# ===========================================================================

def test_the_sync_now_action_enqueues_and_never_claims_to_have_synced():
    """The shipped action wrote a success log and a "Sync Completed" toast.

    Odoo Online runs this under safe_eval - no import, no socket - so an action
    that reported a completed sync was reporting something it cannot do. Guard
    the properties that made the old one a lie.
    """
    assert "x_sync_state" in SYNC_NOW_ACTION_CODE
    assert "'requested'" in SYNC_NOW_ACTION_CODE
    # It must not fabricate a run record...
    assert "x_integration_sync_log" not in SYNC_NOW_ACTION_CODE
    assert "'x_status'" not in SYNC_NOW_ACTION_CODE
    # ...nor report success it did not observe.
    assert "Completed" not in SYNC_NOW_ACTION_CODE
    assert "SUCCESS" not in SYNC_NOW_ACTION_CODE
    # ...and cannot reach a provider from inside Odoo.
    assert "import" not in SYNC_NOW_ACTION_CODE


def test_worker_claims_a_request_runs_it_and_reports_the_real_outcome(odoo):
    odoo._insert(CONFIG_MODEL, {"id": 1, "x_name": "GitHub", "x_provider": "github",
                                "x_sync_state": STATE_REQUESTED, "x_active": True,
                                "x_sync_requested_by": "manager@test"})
    ran = []

    def runner(provider):
        ran.append(provider)
        result = SyncResult(provider)
        result.record_created()
        result.record_skipped()
        result.finish()
        return result

    reports = SyncRequestWorker(odoo, runner).drain()

    assert ran == ["github"]
    assert reports[0]["status"] == STATUS_SUCCESS
    assert reports[0]["created"] == 1
    row = odoo.records(CONFIG_MODEL)[0]
    assert row["x_sync_state"] == STATE_DONE
    assert "created=1" in row["x_sync_message"]
    assert "manager@test" in row["x_sync_message"]


def test_worker_ignores_rows_that_were_never_requested(odoo):
    odoo._insert(CONFIG_MODEL, {"id": 1, "x_name": "Idle", "x_provider": "github",
                                "x_sync_state": "idle", "x_active": True})
    odoo._insert(CONFIG_MODEL, {"id": 2, "x_name": "Running", "x_provider": "frankfurter",
                                "x_sync_state": STATE_RUNNING, "x_active": True})

    def runner(provider):  # pragma: no cover - must never be reached
        raise AssertionError(f"{provider} should not have run")

    assert SyncRequestWorker(odoo, runner).drain() == []


def test_a_failed_provider_run_leaves_the_request_marked_failed(odoo):
    odoo._insert(CONFIG_MODEL, {"id": 1, "x_name": "GitHub", "x_provider": "github",
                                "x_sync_state": STATE_REQUESTED, "x_active": True})

    def runner(provider):
        result = SyncResult(provider)
        result.mark_fatal("GitHub repository is not readable: HTTP 404")
        result.finish()
        return result

    reports = SyncRequestWorker(odoo, runner).drain()

    assert reports[0]["status"] == STATUS_FAILED
    row = odoo.records(CONFIG_MODEL)[0]
    assert row["x_sync_state"] == STATE_FAILED
    assert "not readable" in row["x_sync_message"]


def test_a_connector_that_raises_does_not_strand_the_request_as_running(odoo):
    """A crash must still release the row, or Sync Now is dead for that provider."""
    odoo._insert(CONFIG_MODEL, {"id": 1, "x_name": "GitHub", "x_provider": "github",
                                "x_sync_state": STATE_REQUESTED, "x_active": True})

    def runner(provider):
        raise RuntimeError("connector exploded")

    reports = SyncRequestWorker(odoo, runner).drain()

    assert reports[0]["status"] == "failed"
    row = odoo.records(CONFIG_MODEL)[0]
    assert row["x_sync_state"] == STATE_FAILED
    assert "connector exploded" in row["x_sync_message"]


def test_an_archived_integration_is_not_run(odoo):
    odoo._insert(CONFIG_MODEL, {"id": 1, "x_name": "Off", "x_provider": "github",
                                "x_sync_state": STATE_REQUESTED, "x_active": False})

    def runner(provider):  # pragma: no cover
        raise AssertionError("archived integration must not run")

    reports = SyncRequestWorker(odoo, runner).drain()
    assert reports[0]["status"] == "failed"
    assert odoo.records(CONFIG_MODEL)[0]["x_sync_state"] == STATE_FAILED


# ===========================================================================
# AREA 2 - fresh provisioning
# ===========================================================================

def test_provisioning_creates_both_integration_models_when_absent(ctx, odoo):
    """A fresh trial has neither model, and no addon can deliver them."""
    model_id, state = ensure_model(ctx.odoo, CONFIG_MODEL, "Integration Configuration")

    assert state == "created"
    assert any(m["model"] == CONFIG_MODEL and m["state"] == "manual"
               for m in odoo.records("ir.model"))


def test_provisioning_is_idempotent_for_models_and_fields(ctx, odoo):
    ensure_model(ctx.odoo, CONFIG_MODEL, "Integration Configuration")
    first = ensure_fields(ctx.odoo, CONFIG_MODEL, CONFIG_FIELD_SPECS)
    second = ensure_fields(ctx.odoo, CONFIG_MODEL, CONFIG_FIELD_SPECS)

    assert first["created"] and not first["failed"]
    assert second["created"] == []
    assert sorted(second["existing"]) == sorted(s["name"] for s in CONFIG_FIELD_SPECS)


def test_the_queue_columns_are_part_of_provisioning(ctx, odoo):
    """Sync Now cannot work on a fresh database without these."""
    names = {spec["name"] for spec in CONFIG_FIELD_SPECS}
    assert {"x_sync_state", "x_sync_requested_at",
            "x_sync_requested_by", "x_sync_message"} <= names


def test_selection_fields_are_provisioned_with_their_values(ctx, odoo):
    ensure_model(ctx.odoo, "x_integration_sync_log", "Integration Sync Log")
    ensure_fields(ctx.odoo, "x_integration_sync_log", SYNC_LOG_FIELD_SPECS)

    status = [f for f in odoo.records("ir.model.fields") if f.get("name") == "x_status"][0]
    values = [command[2]["value"] for command in status["selection_ids"]]
    assert values == ["success", "partial", "failed", "skipped"]


def test_config_rows_are_keyed_on_provider_not_on_an_editable_name(ctx, odoo):
    """Renaming an integration in the UI must not clone it on the next run."""
    ensure_model(ctx.odoo, CONFIG_MODEL, "Integration Configuration")
    ensure_integration_config_records(ctx.odoo)
    for row in odoo.records(CONFIG_MODEL):
        row["x_name"] = "renamed by a user"

    second = ensure_integration_config_records(ctx.odoo)

    assert second["created"] == []
    assert len(odoo.records(CONFIG_MODEL)) == 5


def test_fresh_provisioning_creates_the_idempotency_columns(ctx, odoo):
    for model_id, model in ((110, "project.task"), (111, "helpdesk.ticket"),
                            (112, "calendar.event")):
        odoo._insert("ir.model", {"id": model_id, "model": model, "name": model})

    report = ensure_idempotency_fields(ctx.odoo)

    for model in ("res.partner", "project.task", "helpdesk.ticket", "calendar.event"):
        assert sorted(report["created"][model]) == [
            "x_external_id", "x_external_updated_at", "x_source_hash"
        ], model


def test_the_forecast_contact_view_inherits_the_standard_partner_form(ctx, odoo):
    odoo._insert("ir.model.data", {"module": "base", "name": "view_partner_form", "res_id": 99})

    report = ensure_partner_forecast_view(ctx.odoo)

    assert report["status"] == "created"
    assert report["inherit_id"] == 99
    view = odoo.records("ir.ui.view")[0]
    assert view["model"] == "res.partner"
    # Every field the page renders must be one provisioning actually creates,
    # or the Contact form breaks for every user the moment it is opened.
    from integration_service.provisioning import PARTNER_FORECAST_FIELDS
    provisioned = {spec["name"] for spec in PARTNER_FORECAST_FIELDS}
    import re
    for name in re.findall(r'<field name="([^"]+)"', view["arch"]):
        assert name in provisioned, f"view references unprovisioned field {name}"


def test_provision_reports_every_step(ctx, odoo):
    report = provision(ctx.odoo, dry_run=True)
    for step in ("integration_models", "partner_forecast_fields", "idempotency_fields",
                 "partner_forecast_view", "integration_config_views",
                 "integration_config_records", "missing_idempotency_fields"):
        assert step in report, step


@pytest.fixture
def connector(ctx):
    return FrankfurterConnector(ctx)
