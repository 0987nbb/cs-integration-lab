# -*- coding: utf-8 -*-
"""Shared pytest fixtures.

Every test in this suite runs fully offline: outbound HTTP is intercepted by
``responses`` and Odoo is replaced by :class:`FakeOdooClient`, an in-memory
double that implements the same surface as
:class:`integration_service.odoo_client.OdooClient`.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional, Sequence

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Deterministic configuration: set before integration_service.config is imported
# so no developer's real .env can leak into a test run.
os.environ.setdefault("ODOO_URL", "https://odoo.test")
os.environ.setdefault("ODOO_DATABASE", "testdb")
os.environ.setdefault("ODOO_API_KEY", "test-api-key-0123456789")
os.environ["DRY_RUN"] = "false"
os.environ["HTTP_MAX_RETRIES"] = "2"
os.environ["HTTP_BACKOFF_FACTOR"] = "0.01"
os.environ["HTTP_BACKOFF_MAX"] = "0.05"
os.environ["SYNC_SAMPLE_LIMIT"] = "0"

from integration_service.config import HttpSettings, load_settings  # noqa: E402
from integration_service.connectors.base import ConnectorContext  # noqa: E402
from integration_service.errors import OdooError  # noqa: E402
from integration_service.http_client import ResilientHttpClient  # noqa: E402
from integration_service.sanitize import clear_secrets  # noqa: E402


class FakeOdooClient:
    """In-memory stand-in for :class:`OdooClient`.

    Records every mutation on :attr:`calls` so tests can assert on what would
    have been written, and lets a test force an error for any (model, method)
    pair via :meth:`fail_on` - which is how the access-denied and partial-failure
    paths are exercised.
    """

    def __init__(self, seed: Optional[Dict[str, List[Dict[str, Any]]]] = None,
                 dry_run: bool = False) -> None:
        self.store: Dict[str, List[Dict[str, Any]]] = {}
        self.calls: List[Dict[str, Any]] = []
        self.dry_run = dry_run
        self._next_id = 1000
        self._clock = 0
        self._failures: Dict[tuple, Exception] = {}
        for model, records in (seed or {}).items():
            for record in records:
                self._insert(model, dict(record))

    # -- test controls ------------------------------------------------------

    def fail_on(self, model: str, method: str, error: Exception) -> None:
        """Make ``model.method`` raise ``error`` on every subsequent call."""
        self._failures[(model, method)] = error

    def clear_failures(self) -> None:
        self._failures.clear()

    def records(self, model: str) -> List[Dict[str, Any]]:
        return self.store.setdefault(model, [])

    def calls_for(self, method: str, model: Optional[str] = None) -> List[Dict[str, Any]]:
        return [
            c for c in self.calls
            if c["method"] == method and (model is None or c["model"] == model)
        ]

    # -- internals ----------------------------------------------------------

    def _check(self, model: str, method: str) -> None:
        error = self._failures.get((model, method)) or self._failures.get((model, "*"))
        if error is not None:
            raise error

    def _insert(self, model: str, vals: Dict[str, Any]) -> int:
        record = dict(vals)
        record.setdefault("id", self._next_id)
        record.setdefault("write_date", self._stamp())
        self._next_id = max(self._next_id + 1, int(record["id"]) + 1)
        self.store.setdefault(model, []).append(record)
        return int(record["id"])

    def _stamp(self) -> str:
        """The ``write_date`` Odoo maintains on every record it touches.

        Faithful because it matters: a connector that compares a task's
        ``write_date`` against an upstream timestamp sees no ``write_date`` at
        all from a fake that omits it, and the comparison silently passes in a
        suite while failing against every real database. The clock runs forward
        from far in the future so a write always postdates the fixture
        timestamps a test writes by hand, exactly as a live import does.
        """
        self._clock += 1
        return f"2099-01-01 00:00:{self._clock:02d}"

    @staticmethod
    def _match(record: Dict[str, Any], condition: Sequence[Any]) -> bool:
        field, operator, expected = condition[0], condition[1], condition[2]
        actual = record.get(field)
        if isinstance(actual, (list, tuple)) and actual and field.endswith("_id"):
            actual = actual[0]  # normalise a many2one [id, name] pair
        if operator == "=":
            return actual == expected
        if operator == "!=":
            return actual != expected
        if operator == "in":
            return actual in (expected or [])
        if operator == "not in":
            return actual not in (expected or [])
        if operator == "like" or operator == "ilike":
            needle = str(expected).strip("%").lower()
            return needle in str(actual or "").lower()
        if operator == ">=":
            return actual is not None and actual >= expected
        if operator == "<=":
            return actual is not None and actual <= expected
        if operator == ">":
            return actual is not None and actual > expected
        if operator == "<":
            return actual is not None and actual < expected
        raise AssertionError(f"FakeOdooClient does not implement operator {operator!r}")

    def _evaluate(self, record: Dict[str, Any], domain: List[Any]) -> bool:
        """Evaluate an Odoo domain in prefix (polish) notation.

        ``'&'`` and ``'|'`` take the next two terms, ``'!'`` the next one, and any
        remaining top-level terms are ANDed - exactly Odoo's own semantics. The
        connectors rely on this: Open-Meteo selects contacts with
        ``["|", [lat, "!=", 0], [lon, "!=", 0]]``, which an AND-only evaluator
        would silently narrow.
        """
        if not domain:
            return True
        position = 0

        def parse() -> bool:
            nonlocal position
            token = domain[position]
            position += 1
            if token == "&":
                left, right = parse(), parse()
                return left and right
            if token == "|":
                left, right = parse(), parse()
                return left or right
            if token == "!":
                return not parse()
            return self._match(record, token)

        outcomes = []
        while position < len(domain):
            outcomes.append(parse())
        return all(outcomes)

    def _filter(self, model: str, domain: Optional[List[Any]]) -> List[Dict[str, Any]]:
        return [r for r in self.records(model) if self._evaluate(r, list(domain or []))]

    # -- OdooClient surface -------------------------------------------------

    def search_read(self, model: str, domain: Optional[List[Any]] = None,
                    fields: Optional[List[str]] = None, limit: Optional[int] = None,
                    offset: Optional[int] = None, order: Optional[str] = None,
                    context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        self._check(model, "search_read")
        self.calls.append({"method": "search_read", "model": model, "domain": domain})
        rows = self._filter(model, domain)
        rows = rows[(offset or 0):]
        if limit:
            rows = rows[:limit]
        if fields:
            wanted = set(fields) | {"id"}
            rows = [{k: v for k, v in r.items() if k in wanted} for r in rows]
        return [dict(r) for r in rows]

    def search_read_all(self, model: str, domain: Optional[List[Any]] = None,
                        fields: Optional[List[str]] = None, batch_size: int = 200,
                        order: str = "id asc", context: Optional[Dict[str, Any]] = None,
                        hard_limit: Optional[int] = None) -> List[Dict[str, Any]]:
        rows = self.search_read(model, domain, fields)
        return rows[:hard_limit] if hard_limit else rows

    def search_count(self, model: str, domain: Optional[List[Any]] = None,
                     context: Optional[Dict[str, Any]] = None) -> int:
        self._check(model, "search_count")
        return len(self._filter(model, domain))

    def read(self, model: str, ids: Sequence[int], fields: Optional[List[str]] = None,
             context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        self._check(model, "read")
        return [dict(r) for r in self.records(model) if r.get("id") in set(ids)]

    def fields_get(self, model: str, attributes: Optional[List[str]] = None) -> Dict[str, Any]:
        self._check(model, "fields_get")
        return {}

    def model_exists(self, model: str) -> bool:
        try:
            self._check(model, "search_count")
        except OdooError:
            return False
        return True

    def create(self, model: str, vals_list: Any) -> List[int]:
        self._check(model, "create")
        batch = [vals_list] if isinstance(vals_list, dict) else list(vals_list)
        self.calls.append({"method": "create", "model": model, "vals": batch})
        if self.dry_run:
            return []
        return [self._insert(model, vals) for vals in batch]

    def create_one(self, model: str, vals: Dict[str, Any]) -> Optional[int]:
        ids = self.create(model, [vals])
        return ids[0] if ids else None

    def write(self, model: str, ids: Sequence[int], vals: Dict[str, Any]) -> bool:
        self._check(model, "write")
        self.calls.append({"method": "write", "model": model, "ids": list(ids), "vals": dict(vals)})
        if self.dry_run:
            return True
        wanted = set(ids)
        for record in self.records(model):
            if record.get("id") in wanted:
                record.update(vals)
                record["write_date"] = self._stamp()
        return True

    def unlink(self, model: str, ids: Sequence[int]) -> bool:
        self._check(model, "unlink")
        self.calls.append({"method": "unlink", "model": model, "ids": list(ids)})
        wanted = set(ids)
        self.store[model] = [r for r in self.records(model) if r.get("id") not in wanted]
        return True

    def find_by_external_id(self, model: str, external_id: str,
                            fields: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        rows = self.search_read(model, [["x_external_id", "=", external_id]], limit=1)
        return rows[0] if rows else None

    def map_by_external_id(self, model: str, external_ids: Any,
                           fields: Optional[List[str]] = None,
                           chunk_size: int = 100) -> Dict[str, Dict[str, Any]]:
        self._check(model, "search_read")
        wanted = {e for e in external_ids if e}
        return {
            r["x_external_id"]: dict(r)
            for r in self.records(model)
            if r.get("x_external_id") in wanted
        }

    def find_or_create(self, model: str, domain: List[Any], vals: Dict[str, Any],
                       fields: Optional[List[str]] = None) -> Optional[int]:
        rows = self.search_read(model, domain, limit=1)
        if rows:
            return rows[0]["id"]
        return self.create_one(model, vals)

    def ref_id(self, value: Any) -> Optional[int]:
        if isinstance(value, (list, tuple)) and value:
            return int(value[0])
        if isinstance(value, int):
            return value
        return None

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _isolate_secrets():
    """Keep the redaction registry from leaking between tests."""
    clear_secrets()
    yield
    clear_secrets()


@pytest.fixture
def settings():
    """Fresh settings built from the deterministic test environment."""
    return load_settings()


@pytest.fixture
def http_settings():
    """Fast, deterministic retry budget: 2 retries, negligible backoff."""
    return HttpSettings(timeout=5, max_retries=2, backoff_factor=0.01, backoff_max=0.05)


@pytest.fixture
def sleep_calls():
    """Collects the backoff delays a client asked for, without sleeping."""
    return []


@pytest.fixture
def http_client(http_settings, sleep_calls):
    """A :class:`ResilientHttpClient` that never actually sleeps."""
    client = ResilientHttpClient(
        settings=http_settings,
        sleep=sleep_calls.append,
        jitter=False,
    )
    yield client
    client.close()


@pytest.fixture
def odoo():
    """A FakeOdooClient seeded with the fixtures the connectors expect."""
    return FakeOdooClient(seed={
        "res.company": [{"id": 1, "name": "Test Co", "currency_id": [160, "PKR"]}],
        "res.currency": [
            {"id": 160, "name": "PKR", "active": True},
            {"id": 126, "name": "EUR", "active": False},
            {"id": 144, "name": "GBP", "active": False},
            {"id": 31, "name": "TRY", "active": False},
            {"id": 1, "name": "USD", "active": False},
        ],
        "res.users": [
            {"id": 2, "login": "admin@test", "name": "Administrator",
             "share": False, "partner_id": [3, "Administrator"]},
            {"id": 5, "login": "second@test", "name": "Second User",
             "share": False, "partner_id": [7, "Second User"]},
        ],
        "helpdesk.team": [{"id": 1, "name": "Customer Care"}],
        # The live database names it "To-Do"; the connector accepts either spelling.
        "mail.activity.type": [{"id": 4, "name": "To-Do"}, {"id": 1, "name": "Email"}],
        "ir.model": [
            {"id": 90, "model": "res.partner", "name": "Contact"},
            {"id": 96, "model": "res.currency", "name": "Currency"},
            {"id": 102, "model": "res.users", "name": "User"},
        ],
        # Mirrors the live instance: res.currency does NOT carry mail.activity.mixin,
        # so an activity cannot be anchored on it. res.users and res.partner do.
        "ir.model.fields": [
            {"id": 1, "model": "res.users", "name": "activity_ids", "ttype": "one2many"},
            {"id": 2, "model": "res.partner", "name": "activity_ids", "ttype": "one2many"},
        ],
    })


@pytest.fixture
def ctx(odoo, settings, http_client):
    """A ConnectorContext wired to the fakes, with sync-log writing disabled."""
    return ConnectorContext(odoo=odoo, settings=settings, http=http_client, log_writer=None)
