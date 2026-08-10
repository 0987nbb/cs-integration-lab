# -*- coding: utf-8 -*-
"""Behavioural tests for :class:`GitHubConnector`.

GitHub is the only two-way integration in the service, so both directions are
pinned here: an issue must land in ``project.task`` exactly once no matter how
often the sync runs, and a task edited in Odoo must travel back - as a real
PATCH when a token exists and as a ``[MOCK WRITE]`` when it does not.

The five failure modes every connector has to survive are exercised against the
same connector: connect timeout, HTTP 429, persistent HTTP 5xx, a non-JSON body
and a well-formed body of the wrong shape. None of them may raise out of
:meth:`BaseConnector.run`; each must come back as a :class:`SyncResult` whose
status and error text tell an operator what happened.

Every test is offline: ``responses`` intercepts HTTP and ``FakeOdooClient``
stands in for Odoo, so the retry budget is asserted on the injected
``sleep_calls`` list rather than on wall-clock time.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

import pytest
import requests
import responses
from responses import matchers

from integration_service.config import GitHubSettings
from integration_service.connectors.github_connector import GitHubConnector
from integration_service.sanitize import REDACTED, register_secret
from integration_service.sync_result import STATUS_FAILED, STATUS_PARTIAL, STATUS_SUCCESS

API_URL = "https://api.github.test"
OWNER = "acme"
REPO = "widgets"
ISSUES_URL = f"{API_URL}/repos/{OWNER}/{REPO}/issues"
PROJECT_NAME = "GitHub Issues"
PROJECT_ID = 500

#: The query the connector sends on the first page of the issues listing.
FIRST_PAGE_QUERY = {"state": "all", "per_page": "100"}

#: Shaped like a real personal access token so the structural redaction rule in
#: sanitize.py is exercised alongside the registered-secret rule.
TOKEN = "ghp_0123456789abcdefghij0123456789abcdef"


def make_issue(
    number: int,
    title: str,
    *,
    issue_id: Optional[int] = None,
    state: str = "open",
    body: str = "",
    updated_at: str = "2026-01-01T00:00:00Z",
    labels: Sequence[str] = (),
    comments: int = 0,
) -> Dict[str, Any]:
    """Build the subset of a GitHub issue payload the connector reads."""
    return {
        "id": 1000 + number if issue_id is None else issue_id,
        "number": number,
        "title": title,
        "state": state,
        "body": body,
        "updated_at": updated_at,
        "html_url": f"https://github.test/{OWNER}/{REPO}/issues/{number}",
        "user": {"login": "octocat"},
        "labels": [{"name": name} for name in labels],
        "comments": comments,
        "comments_url": f"{ISSUES_URL}/{number}/comments",
    }


def make_comment(login: str, body: str, created_at: str = "2026-01-02T00:00:00Z") -> Dict[str, Any]:
    return {"user": {"login": login}, "body": body, "created_at": created_at}


def tasks(odoo) -> List[Dict[str, Any]]:
    return odoo.records("project.task")


def task_by_external_id(odoo, external_id: str) -> Dict[str, Any]:
    matches = [t for t in tasks(odoo) if t.get("x_external_id") == external_id]
    assert len(matches) == 1, f"expected exactly one task for {external_id}, got {len(matches)}"
    return matches[0]


@pytest.fixture
def github_settings(ctx) -> GitHubSettings:
    """Pin the GitHub configuration.

    ``load_settings`` reads the developer's ``.env``; overriding the block here
    keeps a real GITHUB_TOKEN or repository out of the test run. Comments are
    off by default so only the tests that care pay for the extra request.
    """
    ctx.settings.github = GitHubSettings(
        api_url=API_URL,
        token="",
        owner=OWNER,
        repo=REPO,
        project_name=PROJECT_NAME,
        sync_comments=False,
        per_page=100,
    )
    return ctx.settings.github


@pytest.fixture
def project(odoo) -> int:
    """Pre-create the target project so its id is known to the assertions."""
    odoo.records("project.project").append({"id": PROJECT_ID, "name": PROJECT_NAME})
    return PROJECT_ID


@pytest.fixture
def new_connector(ctx, github_settings, project):
    """Build a fresh connector per run: in production each run is a new process."""
    return lambda: GitHubConnector(ctx)


# -- 1. success, idempotency, update -----------------------------------------


@responses.activate
def test_two_issues_become_two_tasks(new_connector, odoo):
    responses.add(
        responses.GET,
        ISSUES_URL,
        json=[
            make_issue(1, "Login button does nothing", labels=["bug"]),
            make_issue(2, "Document the API", state="closed"),
        ],
        status=200,
    )

    result = new_connector().run()

    assert result.status == STATUS_SUCCESS
    assert (result.created, result.updated, result.skipped, result.failed) == (2, 0, 0, 0)
    assert result.details["issues"] == {"fetched": 2, "processed": 2, "pull_requests_skipped": 0}

    first = task_by_external_id(odoo, "github:issue:1001")
    assert first["name"] == "#1 Login button does nothing"
    assert first["state"] == "01_in_progress"
    assert first["project_id"] == PROJECT_ID
    assert first["x_external_updated_at"] == "2026-01-01 00:00:00"
    assert first["x_source_hash"]

    second = task_by_external_id(odoo, "github:issue:1002")
    assert second["name"] == "#2 Document the API"
    assert second["state"] == "1_done"


def test_rerun_is_idempotent_then_notices_a_changed_title(new_connector, odoo):
    unchanged = make_issue(1, "Login button does nothing")
    payload = [unchanged, make_issue(2, "Document the API")]

    with responses.RequestsMock() as first_run:
        first_run.add(responses.GET, ISSUES_URL, json=payload, status=200)
        created = new_connector().run()
    assert (created.created, created.skipped, created.updated) == (2, 0, 0)

    # Identical upstream payload: the stored source hash matches, so nothing is
    # rewritten. This is the property that makes a cron-driven re-run cheap.
    with responses.RequestsMock() as second_run:
        second_run.add(responses.GET, ISSUES_URL, json=payload, status=200)
        rerun = new_connector().run()
    assert rerun.status == STATUS_SUCCESS
    assert (rerun.created, rerun.skipped, rerun.updated, rerun.failed) == (0, 2, 0, 0)
    assert len(tasks(odoo)) == 2

    with responses.RequestsMock() as third_run:
        third_run.add(
            responses.GET,
            ISSUES_URL,
            json=[unchanged, make_issue(2, "Document the REST API")],
            status=200,
        )
        edited = new_connector().run()

    assert edited.status == STATUS_SUCCESS
    assert (edited.created, edited.skipped, edited.updated, edited.failed) == (0, 1, 1, 0)
    assert len(tasks(odoo)) == 2
    assert task_by_external_id(odoo, "github:issue:1002")["name"] == "#2 Document the REST API"


# -- 2. timeout ---------------------------------------------------------------


@responses.activate
def test_connect_timeout_fails_the_run_after_spending_the_retry_budget(
    new_connector, http_client, sleep_calls
):
    responses.add(responses.GET, ISSUES_URL, body=requests.exceptions.ConnectTimeout())

    result = new_connector().run()

    assert result.status == STATUS_FAILED
    assert result.fatal is True
    assert "timed out" in result.error_details()
    # Backoff actually happened: one sleep between each pair of attempts.
    assert len(responses.calls) == http_client.settings.max_retries + 1
    assert len(sleep_calls) == http_client.settings.max_retries
    assert sleep_calls == [pytest.approx(0.01), pytest.approx(0.02)]


# -- 3. rate limiting ---------------------------------------------------------


@responses.activate
def test_rate_limited_run_waits_for_retry_after_then_succeeds(new_connector, sleep_calls, odoo):
    responses.add(
        responses.GET,
        ISSUES_URL,
        body="rate limit exceeded",
        status=429,
        headers={"Retry-After": "7"},
    )
    responses.add(
        responses.GET, ISSUES_URL, json=[make_issue(1, "Login button does nothing")], status=200
    )

    result = new_connector().run()

    assert result.status == STATUS_SUCCESS
    assert result.created == 1
    assert len(responses.calls) == 2
    # The server-advised delay wins over exponential backoff (which would be 0.01s).
    assert sleep_calls == [pytest.approx(7.0)]
    assert task_by_external_id(odoo, "github:issue:1001")["name"] == "#1 Login button does nothing"


# -- 4. server error ----------------------------------------------------------


@responses.activate
def test_persistent_server_error_fails_after_max_retries_plus_one_attempts(
    new_connector, http_client, sleep_calls
):
    responses.add(responses.GET, ISSUES_URL, body="upstream exploded", status=500)

    result = new_connector().run()

    assert result.status == STATUS_FAILED
    assert result.fatal is True
    assert "HTTP 500" in result.error_details()
    assert len(responses.calls) == http_client.settings.max_retries + 1
    assert len(sleep_calls) == http_client.settings.max_retries


# -- 5. invalid payload -------------------------------------------------------


@responses.activate
def test_non_json_body_is_recorded_not_raised(new_connector, odoo):
    responses.add(
        responses.GET,
        ISSUES_URL,
        body="<html><body>502 Bad Gateway</body></html>",
        status=200,
        content_type="text/html",
    )

    result = new_connector().run()

    assert result.status in (STATUS_FAILED, STATUS_PARTIAL)
    assert "non-JSON body" in result.error_details()
    assert tasks(odoo) == []
    # A non-JSON 200 is a server bug, not a transient failure: no retry.
    assert len(responses.calls) == 1


@responses.activate
def test_json_object_where_a_list_is_expected_is_recorded_not_raised(new_connector, odoo):
    responses.add(
        responses.GET,
        ISSUES_URL,
        json={"message": "Not Found", "documentation_url": "https://docs.github.test"},
        status=200,
    )

    result = new_connector().run()

    assert result.status in (STATUS_FAILED, STATUS_PARTIAL)
    assert "expected a list" in result.error_details()
    assert tasks(odoo) == []


# -- partial-failure continuation ---------------------------------------------


@responses.activate
def test_an_unusable_issue_is_counted_failed_and_the_rest_still_sync(new_connector, odoo):
    malformed = make_issue(3, "No id at all")
    del malformed["id"]
    responses.add(
        responses.GET,
        ISSUES_URL,
        json=[malformed, make_issue(1, "Login button does nothing")],
        status=200,
    )

    result = new_connector().run()

    assert result.status == STATUS_PARTIAL
    assert (result.created, result.failed) == (1, 1)
    assert len(tasks(odoo)) == 1
    assert "without an id" in result.error_details()


# -- GitHub specifics ---------------------------------------------------------


@responses.activate
def test_pull_requests_are_skipped_not_imported(new_connector, odoo):
    pull_request = make_issue(7, "Fix the login button")
    pull_request["pull_request"] = {"url": f"{API_URL}/repos/{OWNER}/{REPO}/pulls/7"}
    responses.add(
        responses.GET,
        ISSUES_URL,
        json=[make_issue(1, "Login button does nothing"), pull_request],
        status=200,
    )

    result = new_connector().run()

    assert result.details["issues"] == {"fetched": 1, "processed": 1, "pull_requests_skipped": 1}
    assert result.created == 1
    assert result.skipped == 1
    assert [t["x_external_id"] for t in tasks(odoo)] == ["github:issue:1001"]
    assert any("pull request" in note for note in result.notes)


@responses.activate
def test_pagination_follows_the_link_rel_next_header(new_connector, odoo):
    responses.add(
        responses.GET,
        ISSUES_URL,
        json=[make_issue(1, "Login button does nothing")],
        status=200,
        headers={"Link": f'<{ISSUES_URL}?page=2>; rel="next", <{ISSUES_URL}?page=2>; rel="last"'},
        match=[matchers.query_param_matcher(FIRST_PAGE_QUERY)],
    )
    responses.add(
        responses.GET,
        ISSUES_URL,
        json=[make_issue(2, "Document the API")],
        status=200,
        match=[matchers.query_param_matcher({"page": "2"})],
    )

    result = new_connector().run()

    assert result.status == STATUS_SUCCESS
    assert result.created == 2
    assert len(responses.calls) == 2
    assert "page=2" in responses.calls[1].request.url
    assert sorted(t["x_external_id"] for t in tasks(odoo)) == [
        "github:issue:1001",
        "github:issue:1002",
    ]


@responses.activate
def test_comments_are_rendered_into_the_description(new_connector, ctx, odoo):
    ctx.settings.github.sync_comments = True
    responses.add(
        responses.GET,
        ISSUES_URL,
        json=[make_issue(1, "Login button does nothing", comments=1)],
        status=200,
    )
    responses.add(
        responses.GET,
        f"{ISSUES_URL}/1/comments",
        json=[make_comment("maintainer", "Reproduced on Firefox.")],
        status=200,
    )

    result = new_connector().run()

    assert result.created == 1
    description = task_by_external_id(odoo, "github:issue:1001")["description"]
    assert "Comments (1)" in description
    assert "maintainer" in description
    assert "Reproduced on Firefox." in description


def test_a_new_comment_alone_flips_the_source_hash(new_connector, ctx, odoo):
    ctx.settings.github.sync_comments = True
    issue = make_issue(1, "Login button does nothing", comments=1)

    with responses.RequestsMock() as first_run:
        first_run.add(responses.GET, ISSUES_URL, json=[issue], status=200)
        first_run.add(
            responses.GET,
            f"{ISSUES_URL}/1/comments",
            json=[make_comment("maintainer", "Reproduced on Firefox.")],
            status=200,
        )
        assert new_connector().run().created == 1

    # The issue itself is untouched - only the thread grew. A comment reaches
    # both the description and the hash material, so the task is rewritten.
    with responses.RequestsMock() as second_run:
        second_run.add(
            responses.GET,
            ISSUES_URL,
            json=[make_issue(1, "Login button does nothing", comments=2)],
            status=200,
        )
        second_run.add(
            responses.GET,
            f"{ISSUES_URL}/1/comments",
            json=[
                make_comment("maintainer", "Reproduced on Firefox."),
                make_comment("octocat", "Fixed in 1.2.", created_at="2026-01-03T00:00:00Z"),
            ],
            status=200,
        )
        rerun = new_connector().run()

    assert (rerun.updated, rerun.created, rerun.skipped) == (1, 0, 0)
    assert "Fixed in 1.2." in task_by_external_id(odoo, "github:issue:1001")["description"]


@responses.activate
def test_an_unreadable_comment_thread_does_not_fail_the_issue(new_connector, ctx, odoo):
    ctx.settings.github.sync_comments = True
    responses.add(
        responses.GET,
        ISSUES_URL,
        json=[make_issue(1, "Login button does nothing", comments=1)],
        status=200,
    )
    responses.add(responses.GET, f"{ISSUES_URL}/1/comments", body="nope", status=500)

    result = new_connector().run()

    assert result.created == 1
    assert result.failed == 0
    assert any("Comments unavailable" in note for note in result.notes)


# -- outbound -----------------------------------------------------------------


def test_outbound_writes_are_mocked_when_no_token_is_configured(new_connector, odoo):
    body = "Clicking Login does nothing.\n<b>Reproduced</b> on Firefox."
    issue = make_issue(1, "Login button does nothing", body=body)
    odoo.records("project.task").append(
        {
            "id": 700,
            "name": "Ship the release notes",
            "project_id": PROJECT_ID,
            "description": "<p>Written by hand in Odoo</p>",
            "x_external_id": False,
        }
    )

    with responses.RequestsMock() as first_run:
        first_run.add(responses.GET, ISSUES_URL, json=[issue], status=200)
        created = new_connector().run()

    # The Odoo-only task is exported on the first run; the imported issue is not
    # pushed back, because its write_date is this run's own import.
    assert [w for w in created.mock_writes if w.startswith("[MOCK WRITE] POST")]
    assert not [w for w in created.mock_writes if "PATCH" in w]

    imported = task_by_external_id(odoo, "github:issue:1001")
    imported["name"] = "#1 Login button is unresponsive"
    imported["write_date"] = "2026-06-01 00:00:00"  # a human edited it after the import

    with responses.RequestsMock() as second_run:
        second_run.add(responses.GET, ISSUES_URL, json=[issue], status=200)
        result = new_connector().run()
        methods = [c.request.method for c in second_run.calls]

    assert all(entry.startswith("[MOCK WRITE]") for entry in result.mock_writes)
    patches = [w for w in result.mock_writes if w.startswith(f"[MOCK WRITE] PATCH {ISSUES_URL}/1")]
    posts = [w for w in result.mock_writes if w.startswith(f"[MOCK WRITE] POST {ISSUES_URL}")]
    assert len(patches) == 1
    assert len(posts) == 1
    # The local edit is what travels back, with the "#1 " display prefix removed.
    assert "'title': 'Login button is unresponsive'" in patches[0]
    # The issue body survives the HTML round trip through the description.
    assert "Clicking Login does nothing." in patches[0]
    assert result.details["outbound"] == {"updates": 1, "comments": 0, "creates": 1, "sent": False}
    assert any("[MOCK WRITE]" in note for note in result.notes)
    # Nothing left the process except the issue listing.
    assert methods == ["GET"]


def test_outbound_patch_is_sent_when_a_token_is_configured(new_connector, ctx, odoo):
    ctx.settings.github.token = TOKEN
    register_secret(TOKEN)
    # Multi-line Markdown containing HTML: it is escaped on the way into the
    # description and must come back byte-identical on the way out.
    body = "Clicking Login does nothing.\n\nSteps:\n1. Open <b>/login</b>\n2. Click it"
    issue = make_issue(1, "Login button does nothing", body=body)

    with responses.RequestsMock() as first_run:
        first_run.add(responses.GET, ISSUES_URL, json=[issue], status=200)
        assert new_connector().run().created == 1

    imported = task_by_external_id(odoo, "github:issue:1001")
    imported["name"] = "#1 Login button is unresponsive"
    imported["write_date"] = "2026-06-01 00:00:00"

    with responses.RequestsMock() as second_run:
        second_run.add(responses.GET, ISSUES_URL, json=[issue], status=200)
        second_run.add(responses.PATCH, f"{ISSUES_URL}/1", json={"number": 1}, status=200)
        result = new_connector().run()
        sent = [c.request for c in second_run.calls if c.request.method == "PATCH"]

    assert result.mock_writes == []
    assert result.details["outbound"] == {"updates": 1, "comments": 0, "creates": 0, "sent": True}
    assert result.updated == 1
    assert len(sent) == 1
    pushed = json.loads(sent[0].body)
    assert pushed["title"] == "Login button is unresponsive"
    assert pushed["body"] == body
    assert pushed["state"] == "open"
    assert sent[0].headers["Authorization"] == f"Bearer {TOKEN}"


def test_rerunning_an_untouched_repository_sends_nothing_back(new_connector, ctx, odoo):
    """The re-run of a repository nobody edited must be read-only.

    Regression: the outbound gate used to ask whether the task's ``write_date``
    was newer than the issue's ``updated_at``. It always is - the import itself
    sets ``write_date``, seconds after the timestamp GitHub sent - so every
    skipped issue read as locally edited and a second run PATCHed the entire
    repository back at itself with a round trip of its own data.
    """
    ctx.settings.github.token = TOKEN
    register_secret(TOKEN)
    payload = [
        make_issue(1, "Login button does nothing", body="Clicking Login does nothing."),
        make_issue(2, "Document the API", state="closed", labels=["documentation"]),
    ]

    with responses.RequestsMock() as first_run:
        first_run.add(responses.GET, ISSUES_URL, json=payload, status=200)
        assert new_connector().run().created == 2

    # Nothing changed on either side between the runs.
    with responses.RequestsMock() as second_run:
        second_run.add(responses.GET, ISSUES_URL, json=payload, status=200)
        rerun = new_connector().run()
        methods = [c.request.method for c in second_run.calls]

    assert (rerun.created, rerun.updated, rerun.skipped, rerun.failed) == (0, 0, 2, 0)
    assert rerun.details["outbound"] == {"updates": 0, "comments": 0, "creates": 0, "sent": True}
    assert rerun.mock_writes == []
    # The listing, and nothing else: no PATCH was even attempted.
    assert methods == ["GET"]


def test_a_task_closed_in_odoo_closes_its_issue(new_connector, ctx, odoo):
    """State is the one field a human is most likely to change on the Odoo side."""
    ctx.settings.github.token = TOKEN
    register_secret(TOKEN)
    issue = make_issue(1, "Login button does nothing", body="Clicking Login does nothing.")

    with responses.RequestsMock() as first_run:
        first_run.add(responses.GET, ISSUES_URL, json=[issue], status=200)
        assert new_connector().run().created == 1

    task_by_external_id(odoo, "github:issue:1001")["state"] = "1_done"

    with responses.RequestsMock() as second_run:
        second_run.add(responses.GET, ISSUES_URL, json=[issue], status=200)
        second_run.add(responses.PATCH, f"{ISSUES_URL}/1", json={"number": 1}, status=200)
        result = new_connector().run()
        sent = [c.request for c in second_run.calls if c.request.method == "PATCH"]

    assert result.details["outbound"] == {"updates": 1, "comments": 0, "creates": 0, "sent": True}
    assert len(sent) == 1
    pushed = json.loads(sent[0].body)
    assert pushed["state"] == "closed"
    # Title and body were not touched in Odoo, so they travel back unchanged.
    assert pushed["title"] == "Login button does nothing"
    assert pushed["body"] == "Clicking Login does nothing."


def test_a_body_differing_only_in_line_endings_is_not_a_change(new_connector, ctx, odoo):
    """GitHub stores CRLF and Odoo's HTML field does not: same text, different bytes."""
    ctx.settings.github.token = TOKEN
    register_secret(TOKEN)
    issue = make_issue(1, "Login button", body="Line one\r\nLine two\r\n")

    with responses.RequestsMock() as first_run:
        first_run.add(responses.GET, ISSUES_URL, json=[issue], status=200)
        assert new_connector().run().created == 1

    # Odoo's HTML field is free to hand the CRLF back as LF; the text is the
    # same and must not read as an edit made in Odoo.
    imported = task_by_external_id(odoo, "github:issue:1001")
    imported["description"] = imported["description"].replace("\r\n", "\n")

    with responses.RequestsMock() as second_run:
        second_run.add(responses.GET, ISSUES_URL, json=[issue], status=200)
        rerun = new_connector().run()
        methods = [c.request.method for c in second_run.calls]

    assert rerun.details["outbound"]["updates"] == 0
    assert methods == ["GET"]


def test_an_issue_changed_upstream_is_not_echoed_back_in_the_same_run(new_connector, ctx, odoo):
    """Inbound wins for the run that imported the change.

    The task below carries a ``write_date`` newer than the upstream timestamp,
    which on its own reads as "a human edited this in Odoo". It did not: the
    edit is arriving from GitHub right now, and pushing it back would overwrite
    the issue with a copy of itself. DRY_RUN is what makes the trap visible -
    it is the one mode where the pre-import snapshot survives the upsert.
    """
    ctx.settings.dry_run = True
    odoo.records("project.task").append(
        {
            "id": 800,
            "name": "#1 Login button does nothing",
            "description": "<pre>the body as imported last week</pre>",
            "state": "01_in_progress",
            "project_id": PROJECT_ID,
            "x_external_id": "github:issue:1001",
            "x_source_hash": "hash-of-the-previous-import",
            "x_external_updated_at": "2026-01-01 00:00:00",
            "write_date": "2026-06-01 00:00:00",
        }
    )

    with responses.RequestsMock() as rsps:
        rsps.add(
            responses.GET,
            ISSUES_URL,
            json=[make_issue(1, "Login button does nothing", body="rewritten upstream")],
            status=200,
        )
        result = new_connector().run()

    assert result.updated == 1
    assert result.details["outbound"] == {"updates": 0, "comments": 0, "creates": 0, "sent": False}
    assert not [w for w in result.mock_writes if "PATCH" in w]
    assert [w for w in result.mock_writes if w.startswith("[MOCK WRITE] WRITE odoo://project.task")]
    # DRY_RUN describes the write instead of performing it.
    assert odoo.calls_for("write", "project.task") == []
    assert odoo.records("project.task")[0]["x_source_hash"] == "hash-of-the-previous-import"


# -- secret handling ----------------------------------------------------------


@responses.activate
def test_the_token_never_reaches_the_recorded_error_text(new_connector, ctx):
    ctx.settings.github.token = TOKEN
    register_secret(TOKEN)
    # An upstream that echoes the credential back inside its own error body is
    # the worst case: the connector must not relay it into the sync log.
    responses.add(
        responses.GET,
        ISSUES_URL,
        json={"message": f"Bad credentials for Bearer {TOKEN}"},
        status=500,
    )

    result = new_connector().run()

    assert result.status == STATUS_FAILED
    assert responses.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in result.error_details()
    assert REDACTED in result.error_details()
    assert TOKEN not in json.dumps(result.to_dict(), default=str)


# -- pagination is bounded by the sample limit --------------------------------


@responses.activate
def test_pagination_stops_once_the_sample_limit_is_reached(ctx, new_connector, odoo):
    """Regression: a live run against odoo/odoo walked 44 pages before trimming.

    Fetching every page and only then applying SYNC_SAMPLE_LIMIT exhausted the
    unauthenticated 60-requests/hour budget, so the run failed with HTTP 403
    having imported nothing. The limit must bound the fetch, not filter it.
    """
    ctx.settings.sample_limit = 2
    page_one = [make_issue(1, "First"), make_issue(2, "Second")]
    responses.add(
        responses.GET, ISSUES_URL, json=page_one, status=200,
        headers={"Link": f'<{ISSUES_URL}?page=2>; rel="next"'},
    )
    responses.add(responses.GET, ISSUES_URL, json=[make_issue(3, "Third")], status=200)

    result = new_connector().run(write_log=False)

    # Exactly one listing request: the second page was never asked for.
    listing_calls = [c for c in responses.calls if "/issues" in c.request.url and "comments" not in c.request.url]
    assert len(listing_calls) == 1
    assert "per_page=2" in listing_calls[0].request.url
    assert result.created == 2
    assert any("Stopped paginating" in note for note in result.notes)


# -- mock writes must not be counted as work done -----------------------------


@responses.activate
def test_withheld_outbound_writes_count_as_skipped_not_created(ctx, new_connector, odoo):
    """Without a token nothing reaches GitHub, so nothing may be counted created.

    details["outbound"] still reports what was identified - intent and effect are
    deliberately separate - but created/updated only move for a write that was
    actually sent.
    """
    issue = make_issue(1, "Login button is unresponsive", body="Clicking Login does nothing.")
    responses.add(responses.GET, ISSUES_URL, json=[issue], status=200)

    # A task that exists only in Odoo, so the outbound create path has work to do.
    odoo.store.setdefault("project.task", []).append({
        "id": 777, "name": "Only in Odoo", "description": "<pre>local</pre>",
        "project_id": PROJECT_ID, "x_external_id": False,
    })

    result = new_connector().run(write_log=False)

    assert ctx.settings.github.can_write is False
    # One issue imported inbound; the outbound create was withheld.
    assert result.created == 1
    assert result.details["outbound"]["creates"] == 1
    assert result.details["outbound"]["sent"] is False
    assert result.skipped >= 1
    assert any("withheld" in note for note in result.notes)
    assert all(entry.startswith("[MOCK WRITE]") for entry in result.mock_writes)
    # The Odoo-only task was not stamped with an external id, so it stays a
    # candidate for the next run once a token is configured.
    only_local = [t for t in odoo.records("project.task") if t["id"] == 777][0]
    assert not only_local["x_external_id"]
