# -*- coding: utf-8 -*-
"""JSONPlaceholder -> Odoo contacts and tickets.

Two collections are imported, in this order:

``GET /users``
    mapped onto ``res.partner``. The geo coordinates carried by each user are
    the input the Open-Meteo connector later forecasts against, so they are
    parsed defensively rather than trusted: they arrive as *strings*, and a
    value that will not parse degrades to ``0.0`` with a note instead of
    failing the contact.
``GET /posts``
    mapped onto ``helpdesk.ticket`` (or ``project.task`` when helpdesk is not
    available), linked back to the partner created for ``post.userId``.

Two peculiarities of this provider drive the rest of the design:

**The host in the assessment brief does not resolve.** ``api.jsonplaceholder.dev``
black-holes connections from this network while ``jsonplaceholder.typicode.com``
serves an identical schema, so :meth:`JsonPlaceholderConnector._resolve_base_url`
probes the primary once with a reduced budget, falls back, names both hosts in a
note, and pins the winner for the whole run.

**Writes are simulated server-side.** ``POST /posts`` echoes a fake id and
``PATCH /posts/<id>`` echoes a merged record; neither is persisted. The verbs are
still exercised for real (outside a dry run) to prove the write path works, but
because nothing survives on the remote every one of them is also recorded as a
``[MOCK WRITE]`` and kept out of the created/updated counters.
"""
from __future__ import annotations

import re
from html import escape as html_escape
from html import unescape as html_unescape
from typing import Any, Dict, List, Optional, Tuple

from ..errors import (
    HttpError,
    IntegrationError,
    InvalidPayloadError,
    OdooError,
    RecordSyncError,
)
from ..idempotency import Upserter, make_external_id
from ..sanitize import sanitize, truncate
from ..sync_result import SyncResult
from .base import BaseConnector, ConnectorContext

PROVIDER = "jsonplaceholder"

#: Model posts are mapped onto when the configured one is unavailable.
FALLBACK_POST_MODEL = "project.task"

#: The primary host is probed with a deliberately small budget: it fails by
#: timing out, and the normal retry budget would spend minutes on a host we
#: already have a working alternative for. The last candidate gets the full
#: budget, because for it a retry is the only remaining chance.
PROBE_MAX_RETRIES = 1
PROBE_TIMEOUT_SECONDS = 10.0

_TAG_RE = re.compile(r"<[^>]+>")


def _text(value: Any) -> str:
    """Trimmed string, with ``None``/``False`` collapsing to an empty string."""
    if value is None or value is False:
        return ""
    return str(value).strip()


def _html_paragraph(value: Any) -> str:
    """Escape plain text into a one-paragraph HTML fragment."""
    clean = html_escape(_text(value))
    if not clean:
        return ""
    return "<p>" + clean.replace("\n", "<br/>") + "</p>"


def _html_to_text(value: Any) -> str:
    """Flatten an Odoo HTML field back to plain text for an outbound payload."""
    raw = _text(value)
    if not raw:
        return ""
    return html_unescape(_TAG_RE.sub(" ", raw.replace("<br/>", "\n").replace("<br>", "\n"))).strip()


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _counters(result: SyncResult) -> Dict[str, int]:
    return {
        "created": result.created,
        "updated": result.updated,
        "skipped": result.skipped,
        "failed": result.failed,
    }


def _delta(before: Dict[str, int], after: Dict[str, int]) -> Dict[str, int]:
    """Per-entity counts, derived from the shared counters the Upserter moved."""
    return {key: after[key] - before[key] for key in before}


class JsonPlaceholderConnector(BaseConnector):
    """Imports JSONPlaceholder users and posts, and demonstrates POST/PATCH."""

    provider = PROVIDER
    label = "JSONPlaceholder"

    def __init__(self, ctx: ConnectorContext) -> None:
        super().__init__(ctx)
        self._base_url: Optional[str] = None
        self._users: Optional[List[Any]] = None
        #: upstream ``user.id`` -> Odoo ``res.partner`` id, built by the users
        #: pass so the posts pass never queries per post.
        self._partner_by_user_id: Dict[int, int] = {}
        #: Every ``user.id`` this run saw on /users. A dry run creates no
        #: partner, so without this every post would look authorless and raise
        #: a review that a real run would never have raised.
        self._seen_user_ids: set = set()

    # -- template method ----------------------------------------------------

    def sync(self, result: SyncResult) -> None:
        base_url = self._resolve_base_url(result)
        self._sync_users(result, base_url)
        model, sample_post = self._sync_posts(result, base_url)
        self._demonstrate_writes(result, base_url, model, sample_post)

    # -- host resolution ----------------------------------------------------

    def _resolve_base_url(self, result: SyncResult) -> str:
        """Pin the host to whichever of the two candidates answers ``/users``.

        The probe response is kept so the users pass does not pay for the same
        request twice. Both hosts failing is run-fatal: there is nothing to sync.
        """
        if self._base_url is not None:
            return self._base_url

        config = self.settings.jsonplaceholder
        candidates = [config.base_url]
        if config.fallback_base_url and config.fallback_base_url != config.base_url:
            candidates.append(config.fallback_base_url)

        failures: List[str] = []
        for index, base_url in enumerate(candidates):
            is_last = index == len(candidates) - 1
            options: Dict[str, Any] = {}
            if not is_last:
                options = {
                    "max_retries": PROBE_MAX_RETRIES,
                    "timeout": min(float(self.settings.http.timeout), PROBE_TIMEOUT_SECONDS),
                }
            try:
                # TimeoutError_ (the observed failure for the primary host) is an
                # HttpError, so this one clause covers connect timeouts too.
                response = self.http.get(f"{base_url}/users", **options)
            except (HttpError, InvalidPayloadError) as exc:
                failures.append(f"{base_url} ({sanitize(exc)})")
                continue
            if not isinstance(response.data, list):
                failures.append(f"{base_url} (expected a JSON array of users at /users)")
                continue

            if index > 0:
                result.add_note(
                    f"Primary host {candidates[0]} did not answer GET /users, so the whole run "
                    f"used the fallback host {base_url}; both serve the same schema."
                )
            self._base_url = base_url
            self._users = response.data
            return base_url

        raise IntegrationError(
            "No JSONPlaceholder host answered GET /users: " + "; ".join(failures)
        )

    # -- users --------------------------------------------------------------

    def _sync_users(self, result: SyncResult, base_url: str) -> None:
        raw_users = self._users if self._users is not None else []
        users = self.limited(raw_users, result, "users")
        before = _counters(result)

        upserter = Upserter(self.odoo, "res.partner", result, dry_run=self.dry_run)
        upserter.preload([eid for eid in (self._user_external_id(u) for u in users) if eid])

        for user in users:
            external_id = self._user_external_id(user)
            with self.guard(result, external_id=external_id, label="user"):
                if external_id is None:
                    raise RecordSyncError(f"Skipped a /users entry without an id: {truncate(user, 120)}")
                self._seen_user_ids.add(_as_int(user.get("id"), 0))
                vals = self._map_user(user, result, external_id)
                _outcome, partner_id = upserter.upsert(external_id, vals)
                if partner_id:
                    self._partner_by_user_id[_as_int(user.get("id"), 0)] = partner_id

        result.details["users"] = {
            "source": f"{base_url}/users",
            "fetched": len(raw_users),
            "processed": len(users),
            "partners_linked": len(self._partner_by_user_id),
            **_delta(before, _counters(result)),
        }

    @staticmethod
    def _user_external_id(user: Any) -> Optional[str]:
        if not isinstance(user, dict) or user.get("id") is None:
            return None
        return make_external_id(PROVIDER, "user", user["id"])

    def _map_user(self, user: Dict[str, Any], result: SyncResult, external_id: str) -> Dict[str, Any]:
        address = user.get("address") or {}
        geo = address.get("geo") or {}

        # street and suite are two halves of one Odoo street line.
        street = " ".join(part for part in (_text(address.get("street")), _text(address.get("suite"))) if part)

        return {
            "name": _text(user.get("name")) or _text(user.get("username")) or external_id,
            "email": _text(user.get("email")),
            "phone": _text(user.get("phone")),
            "website": _text(user.get("website")),
            "street": street,
            "city": _text(address.get("city")),
            "zip": _text(address.get("zipcode")),
            "partner_latitude": self._coordinate(geo.get("lat"), 90.0, result, external_id, "latitude"),
            "partner_longitude": self._coordinate(geo.get("lng"), 180.0, result, external_id, "longitude"),
            "comment": self._user_comment(user),
        }

    def _coordinate(
        self,
        value: Any,
        bound: float,
        result: SyncResult,
        external_id: str,
        axis: str,
    ) -> float:
        """Parse a coordinate string, degrading to ``0.0`` rather than failing.

        Open-Meteo consumes these, so an unusable value is worth a note - but it
        is not worth losing the whole contact over.
        """
        try:
            parsed = float(_text(value))
        except ValueError:
            result.add_note(
                f"{external_id}: {axis} {truncate(value, 40)!r} is not a number; stored 0.0."
            )
            return 0.0
        if not -bound <= parsed <= bound:
            result.add_note(
                f"{external_id}: {axis} {parsed} is outside +/-{bound:g}; stored 0.0."
            )
            return 0.0
        return parsed

    @staticmethod
    def _user_comment(user: Dict[str, Any]) -> str:
        """Carry the fields Odoo has no column for into the notes field."""
        company = user.get("company") or {}
        rows = (
            ("Username", user.get("username")),
            ("Company", company.get("name")),
            ("Catch phrase", company.get("catchPhrase")),
            ("Business", company.get("bs")),
        )
        items = [
            f"<li><strong>{html_escape(label)}:</strong> {html_escape(_text(value))}</li>"
            for label, value in rows
            if _text(value)
        ]
        return "<ul>" + "".join(items) + "</ul>" if items else ""

    # -- posts --------------------------------------------------------------

    def _sync_posts(
        self, result: SyncResult, base_url: str
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """Import ``/posts``; return the model used and one sample post."""
        url = f"{base_url}/posts"
        try:
            response = self.http.get(url)
        except (HttpError, InvalidPayloadError) as exc:
            result.record_failed(f"posts: GET {sanitize(url)} failed: {sanitize(exc)}")
            result.details["posts"] = {"source": url, "fetched": 0}
            return None, None

        if not isinstance(response.data, list):
            result.record_failed(f"posts: GET {sanitize(url)} did not return a JSON array.")
            result.details["posts"] = {"source": url, "fetched": 0}
            return None, None

        raw_posts: List[Any] = response.data
        sample_post = next((p for p in raw_posts if isinstance(p, dict)), None)
        model = self._resolve_post_model(result)
        if model is None:
            result.details["posts"] = {"source": url, "fetched": len(raw_posts), "processed": 0}
            return None, sample_post

        posts = self.limited(raw_posts, result, "posts")
        before = _counters(result)
        team_id = self._first_team_id(model, result)

        upserter = Upserter(self.odoo, model, result, dry_run=self.dry_run)
        upserter.preload([eid for eid in (self._post_external_id(p) for p in posts) if eid])

        for post in posts:
            external_id = self._post_external_id(post)
            with self.guard(result, external_id=external_id, label="post"):
                if external_id is None:
                    raise RecordSyncError(f"Skipped a /posts entry without an id: {truncate(post, 120)}")
                upserter.upsert(external_id, self._map_post(post, model, team_id, external_id, result=result))

        result.details["posts"] = {
            "source": url,
            "model": model,
            "fetched": len(raw_posts),
            "processed": len(posts),
            **_delta(before, _counters(result)),
        }
        return model, sample_post

    def _resolve_post_model(self, result: SyncResult) -> Optional[str]:
        """Prefer the configured model, degrade to ``project.task``, else give up."""
        configured = _text(self.settings.jsonplaceholder.post_model) or "helpdesk.ticket"
        if self.odoo.model_exists(configured):
            return configured
        if configured != FALLBACK_POST_MODEL and self.odoo.model_exists(FALLBACK_POST_MODEL):
            result.add_note(
                f"{configured} is not installed or not readable with this API key; "
                f"posts were mapped onto {FALLBACK_POST_MODEL} instead."
            )
            return FALLBACK_POST_MODEL
        result.add_note(
            f"Neither {configured} nor {FALLBACK_POST_MODEL} is readable with this API key; "
            "no post was imported."
        )
        return None

    def _first_team_id(self, model: str, result: SyncResult) -> Optional[int]:
        if model != "helpdesk.ticket":
            return None
        try:
            rows = self.odoo.search_read("helpdesk.team", [], fields=["id"], limit=1, order="id asc")
        except OdooError as exc:
            result.add_note(f"helpdesk.team is not readable ({sanitize(exc)}); tickets carry no team.")
            return None
        if not rows:
            result.add_note("No helpdesk.team exists; tickets were created without a team.")
            return None
        return self.odoo.ref_id(rows[0].get("id"))

    @staticmethod
    def _post_external_id(post: Any) -> Optional[str]:
        if not isinstance(post, dict) or post.get("id") is None:
            return None
        return make_external_id(PROVIDER, "post", post["id"])

    def _map_post(
        self,
        post: Dict[str, Any],
        model: str,
        team_id: Optional[int],
        external_id: str,
        result: Optional[SyncResult] = None,
    ) -> Dict[str, Any]:
        user_id = _as_int(post.get("userId"), 0)
        partner_id = self._partner_by_user_id.get(user_id) if user_id else None
        if user_id and not partner_id and self.odoo:
            user_ext_id = make_external_id(PROVIDER, "user", user_id)
            try:
                rows = self.odoo.search_read("res.partner", [["x_external_id", "=", user_ext_id]], fields=["id"], limit=1)
                if rows:
                    partner_id = int(rows[0]["id"])
                    self._partner_by_user_id[user_id] = partner_id
            except Exception:
                pass

        # Two different kinds of "incomplete", which deserve two different
        # outcomes. The previous version keyed on `incomplete_mapping` /
        # `require_partner_approval` - fields JSONPlaceholder never sends, so
        # only a test could set them and neither real case was ever detected.
        #
        # A missing title is disqualifying: the ticket's name would fall back to
        # the external id, which is not a subject anyone can triage.
        if not _text(post.get("title")) and result is not None:
            self.withhold_for_approval(
                external_id=external_id,
                summary=f"Approval Required: Incomplete Mapping for Post #{post.get('id')}",
                note=f"Post #{post.get('id')} cannot be mapped onto a {model} record.",
                reason="required source field 'title' is empty",
                required_action="Correct the post upstream, then re-run the sync.",
                result=result,
                count_skipped=False,
            )
            raise RecordSyncError(
                f"Post #{post.get('id')} withheld for approval: required field 'title' is empty.",
                external_id=external_id,
            )

        # An unresolvable author is a missing *link*, not a missing value. The
        # ticket is still worth having, so it is imported unlinked and the gap is
        # raised for review instead of thrown away. In a dry run nothing was
        # created, so an author this run would have created is not a real gap.
        if user_id and not partner_id and result is not None:
            would_have_been_created = self.dry_run and user_id in self._seen_user_ids
            if not would_have_been_created:
                self.flag_for_approval(
                    external_id=external_id,
                    summary=f"Approval Required: Unresolved Author for Post #{post.get('id')}",
                    note=f"Post #{post.get('id')} was imported without a customer link.",
                    reason=(
                        f"userId {user_id} does not resolve to a res.partner "
                        f"(expected x_external_id {make_external_id(PROVIDER, 'user', user_id)})"
                    ),
                    required_action=(
                        "Run the JSONPlaceholder user sync so the author exists as a contact, "
                        "then re-run this sync to attach it."
                    ),
                    result=result,
                )

        vals: Dict[str, Any] = {
            "name": _text(post.get("title")) or external_id,
            "description": _html_paragraph(post.get("body")),
        }

        if model != "helpdesk.ticket":
            # project.task requires a state from its own selection.
            vals["state"] = "01_in_progress"
            return vals

        vals["kanban_state"] = "normal"
        if team_id:
            vals["team_id"] = team_id
        if partner_id:
            vals["partner_id"] = partner_id
        return vals

    # -- outbound write demonstration ---------------------------------------

    def _demonstrate_writes(
        self,
        result: SyncResult,
        base_url: str,
        model: Optional[str],
        sample_post: Optional[Dict[str, Any]],
    ) -> None:
        """Exercise POST and PATCH against the remote, recorded as mock writes.

        :meth:`BaseConnector.mock_or_call` is deliberately not used: it records a
        mock write *instead of* sending. Here the request is genuinely sent (the
        point is proving the verbs work), and it is *still* a mock write because
        the remote persists nothing - so both happen, always.
        """
        with self.guard(result, label="write demonstration"):
            if not sample_post:
                result.add_note("No upstream post was available; the POST/PATCH demonstration was skipped.")
                return

            result.add_note(
                "JSONPlaceholder simulates writes: POST /posts echoes a fake id and "
                "PATCH /posts/<id> echoes a merged record, and neither is persisted. "
                "Both calls are therefore recorded as [MOCK WRITE] and excluded from the counters."
            )

            title, body, user_id, origin = self._write_source(result, model, sample_post)

            create_url = f"{base_url}/posts"
            create_payload = {"title": title, "body": body[:240], "userId": user_id}
            echoed_id = self._echo(result, "POST", create_url, create_payload)
            result.add_mock_write(
                "POST", create_url, {"from": origin, "sent": create_payload, "echoed_id": echoed_id}
            )

            post_id = _as_int(sample_post.get("id"), 0)
            if not post_id:
                result.add_note("The sample post carried no id; the PATCH demonstration was skipped.")
                return
            update_url = f"{base_url}/posts/{post_id}"
            update_payload = {"title": f"{title} [updated from Odoo]"}
            echoed_id = self._echo(result, "PATCH", update_url, update_payload)
            result.add_mock_write(
                "PATCH", update_url, {"from": origin, "sent": update_payload, "echoed_id": echoed_id}
            )

    def _write_source(
        self,
        result: SyncResult,
        model: Optional[str],
        sample_post: Dict[str, Any],
    ) -> Tuple[str, str, int, str]:
        """Build the outbound payload from a record that really exists in Odoo.

        Falls back to the upstream post when nothing was written (a dry run, or
        the posts pass could not resolve a model), so the demonstration still runs.
        """
        record: Optional[Dict[str, Any]] = None
        if model:
            try:
                rows = self.odoo.search_read(
                    model,
                    [["x_external_id", "like", f"{PROVIDER}:post:"]],
                    fields=["id", "name", "description"],
                    limit=1,
                    order="id desc",
                )
                record = rows[0] if rows else None
            except OdooError as exc:
                result.add_note(f"Could not read a {model} record back for the write demonstration: {sanitize(exc)}")

        user_id = _as_int(sample_post.get("userId"), 1)
        if record:
            return (
                _text(record.get("name")),
                _html_to_text(record.get("description")),
                user_id,
                f"{model}#{record.get('id')}",
            )

        result.add_note(
            "No synced Odoo record was readable, so the outbound demonstration used the "
            "upstream post payload instead."
        )
        return (_text(sample_post.get("title")), _text(sample_post.get("body")), user_id, "upstream post")

    def _echo(self, result: SyncResult, method: str, url: str, payload: Dict[str, Any]) -> Optional[Any]:
        """Send the request unless this is a dry run; return the echoed id.

        A transport failure here is a note, not a record failure: the import
        itself already succeeded and nothing downstream depends on the echo.
        """
        if self.dry_run:
            return None
        try:
            response = self.http.request(method, url, json=payload)
        except (HttpError, InvalidPayloadError) as exc:
            result.add_note(f"{method} {sanitize(url)} could not be sent: {sanitize(exc)}")
            return None
        return response.data.get("id") if isinstance(response.data, dict) else None


__all__ = ["JsonPlaceholderConnector"]
