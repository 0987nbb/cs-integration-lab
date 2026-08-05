# -*- coding: utf-8 -*-
"""Two-way sync between GitHub Issues and Odoo ``project.task``.

Inbound is the authoritative direction: an issue is mapped onto a task keyed by
``x_external_id`` so a re-run of an unchanged repository rewrites nothing.

Outbound exists because a task is useful in Odoo only if an edit made there can
reach the repository. It is deliberately gated twice:

* ``GitHubSettings.can_write`` - no token means no write is sent, but the full
  write path still executes and is recorded as ``[MOCK WRITE]``, so the
  behaviour is reviewable without handing this service repository credentials;
* an issue whose inbound outcome was *not* ``skipped`` is never pushed back -
  the task holds what this very run read, and echoing it would overwrite GitHub
  with its own data. Among the skipped ones, only a task that actually says
  something different from its issue is pushed, so a repository nobody has
  touched produces no writes at all.

The issues endpoint also serves pull requests. A PR carries a ``pull_request``
key and is counted as skipped: it is not a defect, it is data this connector
deliberately does not own.
"""
from __future__ import annotations

import html
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..errors import ClientError, IntegrationError
from ..idempotency import SKIPPED, Upserter, make_external_id, parse_iso_datetime
from ..sanitize import sanitize
from ..sync_result import SyncResult, to_odoo_datetime
from .base import BaseConnector

PROJECT_MODEL = "project.project"
TASK_MODEL = "project.task"
TAG_MODEL = "project.tags"

#: GitHub issue state -> ``project.task.state``. The right-hand values are the
#: only ones the live selection accepts.
TASK_STATE_BY_ISSUE_STATE = {"open": "01_in_progress", "closed": "1_done"}
DEFAULT_TASK_STATE = "01_in_progress"
#: Task states that mean "closed" when the state travels back to GitHub.
CLOSED_TASK_STATES = frozenset({"1_done", "1_canceled"})

#: Read once per run so the outbound pass can compare the task against the issue.
TASK_SNAPSHOT_FIELDS = ["name", "description", "state"]

#: The fields an outbound PATCH carries, and therefore the only ones whose
#: divergence is worth a write.
OUTBOUND_FIELDS = ("title", "body", "state")

_NUMBER_PREFIX_RE = re.compile(r"^\s*#\d+\s+")
_PRE_BLOCK_RE = re.compile(r"<pre>(.*?)</pre>", re.DOTALL | re.IGNORECASE)
_BREAK_RE = re.compile(r"(?i)<br\s*/?>|</p>|</div>|</li>")
_TAG_RE = re.compile(r"<[^>]+>")
_TRAILING_WS_RE = re.compile(r"[ \t]+$", re.MULTILINE)


class GitHubConnector(BaseConnector):
    """Sync GitHub issues into ``project.task`` and push task edits back."""

    provider = "github"
    label = "GitHub"

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        # One find_or_create per distinct label name per run, not per issue.
        self._tag_cache: Dict[str, Optional[int]] = {}

    # -- template method ----------------------------------------------------

    def sync(self, result: SyncResult) -> None:
        cfg = self.settings.github
        issues, pull_requests = self._fetch_issues(result)
        if pull_requests:
            result.record_skipped(
                pull_requests,
                f"Skipped {pull_requests} pull request(s) returned by the issues endpoint.",
            )

        project_id = self._ensure_project(result)
        selected = self.limited(issues, result, "issues")

        upserter = Upserter(self.odoo, TASK_MODEL, result, dry_run=self.dry_run)
        external_ids = [self._external_id(i) for i in selected]
        upserter.preload([e for e in external_ids if e], fields=TASK_SNAPSHOT_FIELDS)

        outcomes: Dict[str, Dict[str, Any]] = {}
        for issue, external_id in zip(selected, external_ids):
            if not external_id:
                result.record_failed("GitHub returned an issue without an id.")
                continue
            with self.guard(result, external_id=external_id, label="issue"):
                outcome, task_id = self._sync_issue(issue, external_id, project_id, upserter, result)
                outcomes[external_id] = {"issue": issue, "outcome": outcome, "task_id": task_id}

        result.details["issues"] = {
            "fetched": len(issues),
            "processed": len(selected),
            "pull_requests_skipped": pull_requests,
        }

        allowed = cfg.can_write and not self.dry_run
        pushed = self._push_task_updates(result, upserter, outcomes, allowed)
        created = self._push_new_tasks(result, project_id, allowed)
        result.details["outbound"] = {"updates": pushed, "creates": created, "sent": allowed}
        if not allowed and (pushed or created):
            result.add_note(
                "Outbound writes were recorded as [MOCK WRITE]: no GITHUB_TOKEN configured "
                "or DRY_RUN is set."
            )

    # -- inbound ------------------------------------------------------------

    def _fetch_issues(self, result: SyncResult) -> Tuple[List[Dict[str, Any]], int]:
        """Return ``(issues, pull_request_count)`` for the configured repository.

        Pagination stops as soon as ``SYNC_SAMPLE_LIMIT`` issues are in hand.
        Walking every page first and trimming afterwards is what exhausted the
        unauthenticated 60-requests/hour budget on a large repository: odoo/odoo
        alone is 44 pages, and only the first was ever going to be used.
        """
        cfg = self.settings.github
        limit = self.ctx.sample_limit
        page_size = min(cfg.per_page, limit) if limit and limit > 0 else cfg.per_page
        issues: List[Dict[str, Any]] = []
        pull_requests = 0
        try:
            pages = self.http.paginate(
                cfg.issues_url,
                params={"state": "all", "per_page": page_size},
                headers=self._auth_headers(),
            )
            for page in pages:
                if page.is_empty:
                    continue
                if not isinstance(page.data, list):
                    raise IntegrationError(
                        f"GitHub issues endpoint returned {type(page.data).__name__}, expected a list."
                    )
                for item in page.data:
                    if not isinstance(item, dict):
                        continue
                    if "pull_request" in item:
                        pull_requests += 1
                        continue
                    issues.append(item)
                if limit and limit > 0 and len(issues) >= limit:
                    result.add_note(
                        f"Stopped paginating after {len(issues)} issue(s): SYNC_SAMPLE_LIMIT is "
                        f"{limit}. Raise it to walk the whole repository."
                    )
                    # Closing the generator releases the underlying response and
                    # stops paginate() from requesting the next page.
                    pages.close()
                    break
        except ClientError as exc:
            # 404 (unknown owner/repo) or 401/403 (rejected token) leaves the run
            # with nothing to sync at all, so it is fatal rather than per-record.
            raise IntegrationError(
                f"GitHub repository {cfg.owner}/{cfg.repo} is not readable: {sanitize(exc)}"
            ) from None
        return issues, pull_requests

    def _sync_issue(
        self,
        issue: Mapping[str, Any],
        external_id: str,
        project_id: Optional[int],
        upserter: Upserter,
        result: SyncResult,
    ) -> Tuple[str, Optional[int]]:
        comments = self._fetch_comments(issue, result)
        labels = self._label_names(issue)
        vals: Dict[str, Any] = {
            "name": self._task_name(issue),
            "description": self._render_description(issue, comments),
            "state": TASK_STATE_BY_ISSUE_STATE.get(str(issue.get("state") or ""), DEFAULT_TASK_STATE),
            "tag_ids": [[6, 0, self._tag_ids(labels)]],
        }
        # Comments live in the description, but they are also hashed explicitly so
        # the intent survives a future change to how the description is rendered.
        extra = {"comments": [self._comment_digest(c) for c in comments]} if comments else None
        return upserter.upsert(
            external_id,
            vals,
            external_updated_at=parse_iso_datetime(issue.get("updated_at")),
            extra_hash_input=extra,
            create_only_vals={"project_id": project_id} if project_id else None,
        )

    def _fetch_comments(self, issue: Mapping[str, Any], result: SyncResult) -> List[Dict[str, Any]]:
        """Fetch an issue's comments; an unreadable thread is not a record failure."""
        cfg = self.settings.github
        url = issue.get("comments_url")
        if not cfg.sync_comments or not url or not int(issue.get("comments") or 0):
            return []
        comments: List[Dict[str, Any]] = []
        try:
            for page in self.http.paginate(
                str(url), params={"per_page": cfg.per_page}, headers=self._auth_headers()
            ):
                if page.is_empty or not isinstance(page.data, list):
                    continue
                comments.extend(c for c in page.data if isinstance(c, dict))
        except IntegrationError as exc:
            result.add_note(
                f"Comments unavailable for issue #{issue.get('number')}, "
                f"syncing it without them: {sanitize(exc)}"
            )
            return []
        return comments

    def _ensure_project(self, result: SyncResult) -> Optional[int]:
        """Resolve the target project once per run, creating it when absent."""
        name = self.settings.github.project_name
        project_id = self.odoo.find_or_create(PROJECT_MODEL, [["name", "=", name]], {"name": name})
        if not project_id:
            # Expected under DRY_RUN, where no create is actually performed.
            result.add_note(f"Project {name!r} unresolved; tasks are synced without a project.")
        return project_id

    def _tag_ids(self, names: Sequence[str]) -> List[int]:
        ids: List[int] = []
        for name in names:
            if name not in self._tag_cache:
                self._tag_cache[name] = self.odoo.find_or_create(
                    TAG_MODEL, [["name", "=", name]], {"name": name}
                )
            tag_id = self._tag_cache[name]
            if tag_id:
                ids.append(tag_id)
        # Sorted so an unchanged label set always hashes to the same value.
        return sorted(set(ids))

    # -- mapping ------------------------------------------------------------

    @staticmethod
    def _external_id(issue: Mapping[str, Any]) -> Optional[str]:
        issue_id = issue.get("id")
        if issue_id is None:
            return None
        return make_external_id("github", "issue", issue_id)

    @staticmethod
    def _label_names(issue: Mapping[str, Any]) -> List[str]:
        names: List[str] = []
        for label in issue.get("labels") or []:
            name = label.get("name") if isinstance(label, dict) else label
            if name:
                names.append(str(name))
        return names

    @staticmethod
    def _task_name(issue: Mapping[str, Any]) -> str:
        title = str(issue.get("title") or "").strip() or "(no title)"
        return f"#{issue.get('number')} {title}"

    def _render_description(
        self, issue: Mapping[str, Any], comments: Sequence[Mapping[str, Any]]
    ) -> str:
        """Build the task's HTML description. Everything upstream is escaped."""
        body = str(issue.get("body") or "").strip()
        author = self._login(issue)
        blocks = [
            "<p>Imported from GitHub issue "
            f'<a href="{html.escape(str(issue.get("html_url") or ""))}">'
            f"#{html.escape(str(issue.get('number')))}</a> opened by {html.escape(author)}.</p>"
        ]
        # <pre> keeps the Markdown body readable and is the block the outbound
        # pass reads back, so the round trip stays lossless.
        blocks.append(f"<pre>{html.escape(body)}</pre>" if body else "<pre></pre>")

        labels = self._label_names(issue)
        blocks.append(
            "<p><strong>Labels:</strong> "
            f"{html.escape(', '.join(labels)) if labels else 'none'}</p>"
        )

        if comments:
            blocks.append(f"<p><strong>Comments ({len(comments)})</strong></p>")
            for comment in comments:
                blocks.append(
                    f"<p><strong>{html.escape(self._login(comment))}</strong> "
                    f"<em>{html.escape(str(comment.get('created_at') or ''))}</em></p>"
                    f"<pre>{html.escape(str(comment.get('body') or '').strip())}</pre>"
                )
        return "".join(blocks)

    @staticmethod
    def _login(payload: Mapping[str, Any]) -> str:
        user = payload.get("user")
        if isinstance(user, dict) and user.get("login"):
            return str(user["login"])
        return "unknown"

    @staticmethod
    def _comment_digest(comment: Mapping[str, Any]) -> str:
        return "|".join(
            [
                str((comment.get("user") or {}).get("login") or ""),
                str(comment.get("created_at") or ""),
                str(comment.get("body") or "").strip(),
            ]
        )

    # -- outbound -----------------------------------------------------------

    def _push_task_updates(
        self,
        result: SyncResult,
        upserter: Upserter,
        outcomes: Mapping[str, Dict[str, Any]],
        allowed: bool,
    ) -> int:
        """PATCH issues whose Odoo task diverges from the issue it was imported from."""
        cfg = self.settings.github
        candidates: List[Tuple[str, Dict[str, Any], Dict[str, Any]]] = []
        for external_id, entry in outcomes.items():
            if entry["outcome"] != SKIPPED:
                # Created or updated in this very run: the task holds what we
                # just read, so pushing it back would echo GitHub at itself.
                continue
            record = upserter.find(external_id)
            if not record:
                continue
            payload = self._outbound_payload(record)
            if self._diverges(payload, entry["issue"]):
                candidates.append((external_id, entry, payload))

        pushed = 0
        for external_id, entry, payload in self.limited(candidates, result, "outbound issue updates"):
            number = entry["issue"].get("number")
            if not number:
                result.add_note(f"{external_id} has no issue number; outbound update skipped.")
                continue
            url = f"{cfg.issues_url}/{number}"
            with self.guard(result, external_id=external_id, label="outbound issue update"):
                self.mock_or_call(
                    result, allowed, "PATCH", url, payload,
                    send=lambda: self.http.patch(url, json=payload, headers=self._auth_headers()),
                )
                # details["outbound"] reports what was identified; the run
                # counters only move for a write that actually reached GitHub.
                pushed += 1
                if allowed:
                    result.record_updated()
                else:
                    result.record_skipped(
                        reason=f"Outbound update of issue #{number} withheld; recorded as [MOCK WRITE]."
                    )
        return pushed

    def _push_new_tasks(self, result: SyncResult, project_id: Optional[int], allowed: bool) -> int:
        """POST tasks that exist only in Odoo, then adopt the issue they became."""
        cfg = self.settings.github
        if not project_id:
            return 0
        # An unset char field reads back as False; '' is included because a task
        # edited by hand can leave an empty string behind.
        domain = [["project_id", "=", project_id], ["x_external_id", "in", [False, ""]]]
        try:
            tasks = self.odoo.search_read_all(
                TASK_MODEL, domain, fields=["id", "name", "description"], batch_size=100
            )
        except IntegrationError as exc:
            result.add_note(f"Could not list Odoo-only tasks for export: {sanitize(exc)}")
            return 0

        created = 0
        for task in self.limited(tasks, result, "outbound issue creates"):
            title = self._outbound_title(task)
            if not title:
                result.add_note(f"Task {task.get('id')} has no title; outbound create skipped.")
                continue
            payload = {"title": title, "body": self._outbound_body(task)}
            with self.guard(result, label="outbound issue create"):
                response = self.mock_or_call(
                    result, allowed, "POST", cfg.issues_url, payload,
                    send=lambda: self.http.post(
                        cfg.issues_url, json=payload, headers=self._auth_headers()
                    ),
                )
                created += 1
                if allowed:
                    self._adopt_created_issue(task, response)
                    result.record_created()
                else:
                    result.record_skipped(
                        reason=(
                            f"Outbound create for Odoo task {task.get('id')} withheld; "
                            "recorded as [MOCK WRITE]."
                        )
                    )
        return created

    def _adopt_created_issue(self, task: Mapping[str, Any], response: Any) -> None:
        """Stamp the new issue's identity onto the task so it is created once.

        ``x_source_hash`` is left alone on purpose: the next inbound run computes
        it from the issue itself, which is the only authoritative source for it.
        """
        issue = getattr(response, "data", None)
        if not isinstance(issue, dict) or issue.get("id") is None:
            return
        vals: Dict[str, Any] = {
            "x_external_id": make_external_id("github", "issue", issue["id"]),
            "name": self._task_name(issue),
        }
        updated_at = to_odoo_datetime(parse_iso_datetime(issue.get("updated_at")))
        if updated_at:
            vals["x_external_updated_at"] = updated_at
        self.odoo.write(TASK_MODEL, [task["id"]], vals)

    def _outbound_payload(self, record: Mapping[str, Any]) -> Dict[str, Any]:
        """The PATCH body that would make the issue match this task."""
        return {
            "title": self._outbound_title(record),
            "body": self._outbound_body(record),
            "state": "closed" if record.get("state") in CLOSED_TASK_STATES else "open",
        }

    @classmethod
    def _diverges(cls, payload: Mapping[str, Any], issue: Mapping[str, Any]) -> bool:
        """True when the task no longer says what its issue says.

        Only ever asked of an issue whose inbound outcome was *skipped*, which
        means the upstream data still hashes to what we last imported. Anything
        the task says differently was therefore said in Odoo, and is ours to push.

        Comparing content rather than timestamps is deliberate. ``write_date``
        cannot answer this question: our own import sets it, and it is always
        later than the issue's ``updated_at`` that it was compared against, so
        every skipped issue looked locally edited and every re-run of an
        untouched repository PATCHed the whole repository back at itself.
        """
        upstream = {
            "title": str(issue.get("title") or ""),
            "body": str(issue.get("body") or ""),
            "state": str(issue.get("state") or "open"),
        }
        return any(
            cls._comparable(payload.get(field)) != cls._comparable(upstream[field])
            for field in OUTBOUND_FIELDS
        )

    @staticmethod
    def _comparable(value: Any) -> str:
        """Normalise text for comparison only; what is sent stays verbatim.

        GitHub stores bodies with CRLF and Odoo's HTML field does not, so the
        two differ byte-wise while saying exactly the same thing.
        """
        text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
        return _TRAILING_WS_RE.sub("", text).strip()

    @staticmethod
    def _outbound_title(record: Mapping[str, Any]) -> str:
        """Strip the ``#123 `` prefix the inbound mapping added to the task name."""
        return _NUMBER_PREFIX_RE.sub("", str(record.get("name") or "")).strip()

    @staticmethod
    def _outbound_body(record: Mapping[str, Any]) -> str:
        """Recover Markdown from the task's HTML description.

        The inbound mapping puts the issue body in the first ``<pre>`` block, so
        reading that block back returns exactly what GitHub sent. A description
        written by hand in Odoo has no such block and is flattened instead.
        """
        description = str(record.get("description") or "")
        if not description.strip():
            return ""
        match = _PRE_BLOCK_RE.search(description)
        source = match.group(1) if match else description
        text = _BREAK_RE.sub("\n", source)
        return html.unescape(_TAG_RE.sub("", text)).strip()

    # -- auth ---------------------------------------------------------------

    def _auth_headers(self) -> Dict[str, str]:
        """Request headers for the GitHub REST API. Never log the return value."""
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = self.settings.github.token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers


__all__ = ["GitHubConnector"]
