# -*- coding: utf-8 -*-
"""Tests for the JSONPlaceholder connector.

This is the connector whose primary host does not resolve, so the suite is
weighted towards failure handling rather than the happy path: host fallback, a
rate-limited collection, a permanently broken collection, and three separate
shapes of unusable payload. Each one asserts on the *counters and status* the
run ends with, because that is what ``x_integration_sync_log`` stores and what a
reviewer reads.

Everything runs offline. ``responses`` intercepts the HTTP layer and
``FakeOdooClient`` (see ``conftest.py``) stands in for Odoo, so no test can
reach a real host - the URLs below are deliberately fictional rather than the
real hostnames, so a missing interception fails loudly instead of hitting the
internet.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
import requests
import responses

from integration_service.config import JsonPlaceholderSettings
from integration_service.connectors.base import ConnectorContext
from integration_service.connectors.jsonplaceholder_connector import JsonPlaceholderConnector
from integration_service.errors import InvalidPayloadError, OdooError

PRIMARY = "https://primary.test"
FALLBACK = "https://fallback.test"

#: Id the remote echoes back for a POST it never actually stores.
ECHOED_POST_ID = 101


def _user(user_id: int, name: str, lat: Any, lng: Any) -> Dict[str, Any]:
    """One ``/users`` entry, with the geo coordinates left parameterisable.

    Coordinates arrive from this provider as *strings*, which is exactly why
    the connector parses them defensively; tests feed both good and bad ones.
    """
    return {
        "id": user_id,
        "name": name,
        "username": f"user{user_id}",
        "email": f"user{user_id}@example.test",
        "phone": "1-770-736-8031 x56442",
        "website": f"user{user_id}.example.test",
        "address": {
            "street": "Kulas Light",
            "suite": f"Apt. {user_id}",
            "city": "Gwenborough",
            "zipcode": "92998-3874",
            "geo": {"lat": lat, "lng": lng},
        },
        "company": {
            "name": "Romaguera-Crona",
            "catchPhrase": "Multi-layered client-server neural-net",
            "bs": "harness real-time e-markets",
        },
    }


USERS: List[Dict[str, Any]] = [
    _user(1, "Leanne Graham", "-37.3159", "81.1496"),
    _user(2, "Ervin Howell", "-43.9509", "-34.4618"),
]

POSTS: List[Dict[str, Any]] = [
    {"userId": 1, "id": 1, "title": "sunt aut facere", "body": "quia et suscipit\nreprehenderit"},
    {"userId": 2, "id": 2, "title": "qui est esse", "body": "est <b>rerum</b> tempore"},
]


@pytest.fixture
def jp_ctx(ctx: ConnectorContext) -> ConnectorContext:
    """Context with both hosts pinned to test-local URLs.

    The real defaults are overridden so a developer's ``.env`` can never steer a
    test at a live host, and so the fallback path has a distinct name to assert on.
    """
    ctx.settings.jsonplaceholder = JsonPlaceholderSettings(
        base_url=PRIMARY,
        fallback_base_url=FALLBACK,
        post_model="helpdesk.ticket",
    )
    return ctx


def _register_reads(
    base_url: str,
    users: Optional[List[Any]] = None,
    posts: Optional[List[Any]] = None,
) -> None:
    """Register the two collections a healthy host serves."""
    responses.add(responses.GET, f"{base_url}/users", json=USERS if users is None else users, status=200)
    responses.add(responses.GET, f"{base_url}/posts", json=POSTS if posts is None else posts, status=200)


def _register_writes(base_url: str, post_id: int = 1) -> None:
    """Register the simulated write endpoints: both echo, neither persists."""
    responses.add(responses.POST, f"{base_url}/posts", json={"id": ECHOED_POST_ID}, status=201)
    responses.add(responses.PATCH, f"{base_url}/posts/{post_id}", json={"id": post_id}, status=200)


def _by_external_id(odoo: Any, model: str, external_id: str) -> Dict[str, Any]:
    matches = [r for r in odoo.records(model) if r.get("x_external_id") == external_id]
    assert matches, f"no {model} record carries x_external_id {external_id!r}"
    return matches[0]


def _requests_to(base_url: str) -> List[Any]:
    return [call.request for call in responses.calls if call.request.url.startswith(base_url)]


# -- 1. success and idempotency ---------------------------------------------


@responses.activate
def test_users_and_posts_are_imported_as_partners_and_tickets(jp_ctx: ConnectorContext, odoo: Any) -> None:
    _register_reads(PRIMARY)
    _register_writes(PRIMARY)

    result = JsonPlaceholderConnector(jp_ctx).run()

    assert result.status == "success"
    assert result.failed == 0
    assert result.created == 4  # two partners plus two tickets
    assert result.details["users"]["created"] == 2
    assert result.details["posts"]["model"] == "helpdesk.ticket"

    partner = _by_external_id(odoo, "res.partner", "jsonplaceholder:user:1")
    assert partner["name"] == "Leanne Graham"
    assert partner["email"] == "user1@example.test"
    assert partner["street"] == "Kulas Light Apt. 1"
    assert partner["city"] == "Gwenborough"
    # The string coordinates are what Open-Meteo later forecasts against.
    assert partner["partner_latitude"] == pytest.approx(-37.3159)
    assert partner["partner_longitude"] == pytest.approx(81.1496)
    assert partner["x_source_hash"]

    other = _by_external_id(odoo, "res.partner", "jsonplaceholder:user:2")
    assert other["partner_latitude"] == pytest.approx(-43.9509)
    assert other["partner_longitude"] == pytest.approx(-34.4618)

    ticket = _by_external_id(odoo, "helpdesk.ticket", "jsonplaceholder:post:1")
    assert ticket["name"] == "sunt aut facere"
    assert ticket["kanban_state"] == "normal"
    assert ticket["team_id"] == 1
    assert ticket["partner_id"] == partner["id"]

    second = _by_external_id(odoo, "helpdesk.ticket", "jsonplaceholder:post:2")
    assert second["partner_id"] == other["id"]
    # The upstream body is escaped, never injected into the HTML field.
    assert "&lt;b&gt;rerum&lt;/b&gt;" in second["description"]


@responses.activate
def test_second_run_with_identical_data_skips_every_record(jp_ctx: ConnectorContext, odoo: Any) -> None:
    _register_reads(PRIMARY)
    _register_writes(PRIMARY)

    first = JsonPlaceholderConnector(jp_ctx).run()
    second = JsonPlaceholderConnector(jp_ctx).run()

    assert first.created == 4
    assert second.created == 0
    assert second.updated == 0
    assert second.skipped == 4
    assert second.status == "success"
    assert len(odoo.records("res.partner")) == 2
    assert len(odoo.records("helpdesk.ticket")) == 2
    # An unchanged source hash must not produce a write of any kind.
    assert not odoo.calls_for("write")


# -- 2. host fallback --------------------------------------------------------


@responses.activate
def test_primary_timeout_falls_back_to_the_secondary_host(jp_ctx: ConnectorContext, odoo: Any) -> None:
    responses.add(responses.GET, f"{PRIMARY}/users", body=requests.exceptions.ConnectTimeout("no route"))
    _register_reads(FALLBACK)
    _register_writes(FALLBACK)

    result = JsonPlaceholderConnector(jp_ctx).run()

    assert result.status == "success"
    assert result.created == 4
    assert len(odoo.records("res.partner")) == 2
    assert result.details["users"]["source"] == f"{FALLBACK}/users"

    named = [note for note in result.notes if PRIMARY in note and FALLBACK in note]
    assert named, f"no note names both hosts: {result.notes}"

    # The dead host gets a deliberately small budget so the run reaches the
    # fallback in seconds rather than minutes.
    assert len(_requests_to(PRIMARY)) == 2


@responses.activate
def test_both_hosts_timing_out_is_a_fatal_run(jp_ctx: ConnectorContext, odoo: Any) -> None:
    responses.add(responses.GET, f"{PRIMARY}/users", body=requests.exceptions.ConnectTimeout("no route"))
    responses.add(responses.GET, f"{FALLBACK}/users", body=requests.exceptions.ConnectTimeout("no route"))

    result = JsonPlaceholderConnector(jp_ctx).run()

    assert result.status == "failed"
    assert result.fatal is True
    assert result.created == 0
    assert not odoo.records("res.partner")
    assert PRIMARY in result.errors[0]
    assert FALLBACK in result.errors[0]
    assert "timed out" in result.errors[0]


# -- 3. rate limiting --------------------------------------------------------


@responses.activate
def test_rate_limited_posts_are_retried_after_the_advised_delay(
    jp_ctx: ConnectorContext, odoo: Any, sleep_calls: List[float]
) -> None:
    responses.add(responses.GET, f"{PRIMARY}/users", json=USERS, status=200)
    responses.add(
        responses.GET,
        f"{PRIMARY}/posts",
        json={"message": "slow down"},
        status=429,
        headers={"Retry-After": "2"},
    )
    responses.add(responses.GET, f"{PRIMARY}/posts", json=POSTS, status=200)
    _register_writes(PRIMARY)

    result = JsonPlaceholderConnector(jp_ctx).run()

    assert result.status == "success"
    assert result.created == 4
    assert len(odoo.records("helpdesk.ticket")) == 2
    # Retry-After wins over the exponential backoff, and the wait really happened.
    assert sleep_calls == [2.0]


# -- 4. persistent server error ---------------------------------------------


@responses.activate
def test_persistent_500_on_users_fails_the_run(jp_ctx: ConnectorContext, odoo: Any) -> None:
    responses.add(responses.GET, f"{PRIMARY}/users", body="upstream exploded", status=500)
    responses.add(responses.GET, f"{FALLBACK}/users", body="upstream exploded", status=500)

    result = JsonPlaceholderConnector(jp_ctx).run()

    assert result.status == "failed"
    assert result.fatal is True
    assert not odoo.records("res.partner")
    assert "HTTP 500" in result.errors[0]
    # Both hosts were retried rather than abandoned on the first 5xx.
    assert len(_requests_to(PRIMARY)) > 1
    assert len(_requests_to(FALLBACK)) > 1


# -- 5. unusable payloads ----------------------------------------------------


@responses.activate
def test_unparseable_geo_is_imported_as_zero_with_a_note(jp_ctx: ConnectorContext, odoo: Any) -> None:
    _register_reads(PRIMARY, users=[_user(1, "Leanne Graham", "not-a-number", "999.0")])
    _register_writes(PRIMARY)

    result = JsonPlaceholderConnector(jp_ctx).run()

    assert result.status == "success"
    assert result.failed == 0

    partner = _by_external_id(odoo, "res.partner", "jsonplaceholder:user:1")
    assert partner["name"] == "Leanne Graham"
    assert partner["partner_latitude"] == 0.0   # unparseable
    assert partner["partner_longitude"] == 0.0  # parseable but outside +/-180

    assert any("latitude" in n and "jsonplaceholder:user:1" in n for n in result.notes)
    assert any("longitude" in n and "jsonplaceholder:user:1" in n for n in result.notes)


@responses.activate
def test_users_object_instead_of_array_falls_through_to_the_fallback(
    jp_ctx: ConnectorContext, odoo: Any
) -> None:
    responses.add(responses.GET, f"{PRIMARY}/users", json={"users": USERS}, status=200)
    _register_reads(FALLBACK)
    _register_writes(FALLBACK)

    result = JsonPlaceholderConnector(jp_ctx).run()

    assert result.status == "success"
    assert len(odoo.records("res.partner")) == 2


@responses.activate
def test_users_object_on_both_hosts_fails_without_raising(jp_ctx: ConnectorContext, odoo: Any) -> None:
    responses.add(responses.GET, f"{PRIMARY}/users", json={"users": []}, status=200)
    responses.add(responses.GET, f"{FALLBACK}/users", json={"users": []}, status=200)

    result = JsonPlaceholderConnector(jp_ctx).run()

    assert result.status == "failed"
    assert result.fatal is True
    assert "JSON array" in result.errors[0]
    assert not odoo.records("res.partner")


@responses.activate
def test_non_json_users_body_ends_as_a_failed_run(jp_ctx: ConnectorContext, odoo: Any) -> None:
    responses.add(responses.GET, f"{PRIMARY}/users", body="<html>maintenance</html>", status=200)
    responses.add(responses.GET, f"{FALLBACK}/users", body="<html>maintenance</html>", status=200)

    result = JsonPlaceholderConnector(jp_ctx).run()

    assert result.status == "failed"
    assert result.fatal is True
    assert "non-JSON body" in result.errors[0]
    assert not odoo.records("res.partner")

    # The connector swallowed exactly this, rather than a generic exception.
    with pytest.raises(InvalidPayloadError):
        jp_ctx.http.get(f"{PRIMARY}/users")


# -- partial-failure continuation -------------------------------------------


@responses.activate
def test_a_user_without_an_id_fails_alone_and_the_run_continues(
    jp_ctx: ConnectorContext, odoo: Any
) -> None:
    _register_reads(PRIMARY, users=[{"name": "Nameless, idless"}, USERS[0]])
    _register_writes(PRIMARY)

    result = JsonPlaceholderConnector(jp_ctx).run()

    assert result.status == "partial"
    assert result.failed == 1
    assert result.created == 3  # one partner survived, plus both tickets
    assert len(odoo.records("res.partner")) == 1
    assert _by_external_id(odoo, "res.partner", "jsonplaceholder:user:1")


# -- outbound write demonstration -------------------------------------------


@responses.activate
def test_post_and_patch_are_sent_and_recorded_as_mock_writes(jp_ctx: ConnectorContext) -> None:
    _register_reads(PRIMARY)
    _register_writes(PRIMARY)

    result = JsonPlaceholderConnector(jp_ctx).run()

    assert result.mock_writes
    assert all(entry.startswith("[MOCK WRITE]") for entry in result.mock_writes)
    methods = [entry.split()[2] for entry in result.mock_writes]
    assert "POST" in methods
    assert "PATCH" in methods
    assert f"echoed_id': {ECHOED_POST_ID}" in result.mock_writes[methods.index("POST")]

    # A simulated write must never inflate the real counters.
    assert result.created == 4
    assert result.updated == 0

    sent = [r for r in _requests_to(PRIMARY) if r.method in ("POST", "PATCH")]
    assert [r.method for r in sent] == ["POST", "PATCH"]
    assert sent[0].url == f"{PRIMARY}/posts"
    assert sent[1].url == f"{PRIMARY}/posts/1"


@responses.activate
def test_dry_run_withholds_the_outbound_calls(jp_ctx: ConnectorContext, odoo: Any) -> None:
    jp_ctx.settings.dry_run = True
    _register_reads(PRIMARY)

    result = JsonPlaceholderConnector(jp_ctx).run()

    assert result.status == "success"
    assert not odoo.records("res.partner")
    assert not [r for r in _requests_to(PRIMARY) if r.method in ("POST", "PATCH")]
    assert all(entry.startswith("[MOCK WRITE]") for entry in result.mock_writes)
    assert any(" POST " in entry for entry in result.mock_writes)
    assert any(" PATCH " in entry for entry in result.mock_writes)


# -- post model resolution ---------------------------------------------------


@responses.activate
def test_posts_fall_back_to_project_task_when_helpdesk_is_denied(
    jp_ctx: ConnectorContext, odoo: Any
) -> None:
    odoo.fail_on(
        "helpdesk.ticket",
        "search_count",
        OdooError("Access denied on helpdesk.ticket", status_code=403),
    )
    _register_reads(PRIMARY)
    _register_writes(PRIMARY)

    result = JsonPlaceholderConnector(jp_ctx).run()

    assert result.status == "success"
    assert result.details["posts"]["model"] == "project.task"
    assert not odoo.records("helpdesk.ticket")

    task = _by_external_id(odoo, "project.task", "jsonplaceholder:post:1")
    assert task["state"] == "01_in_progress"  # required by project.task
    assert "partner_id" not in task
    assert "kanban_state" not in task
    assert any("project.task" in note for note in result.notes)
