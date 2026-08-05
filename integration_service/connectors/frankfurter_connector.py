# -*- coding: utf-8 -*-
"""Frankfurter FX rates -> Odoo ``res.currency.rate``.

Three properties of this integration drive the whole design.

**The API base is not the company currency.** Frankfurter quotes *base -> quote*
(how many EUR one USD buys), while ``res.currency.rate.rate`` means *units of
that currency per one unit of the company currency*. On this instance the
company currency is PKR and the API base is USD, so every quote is rebased::

    odoo_rate(Q) = api_rate(base -> Q) / api_rate(base -> company_currency)

which collapses to ``api_rate(Q)`` when the company currency *is* the API base.
If the company currency comes back in neither position the conversion has no
defined answer, so the run is failed rather than silently storing USD-based
numbers under a PKR-based field.

**Money must not inherit binary float error.** Every rate is carried as
:class:`decimal.Decimal`, constructed from ``str(value)`` so the float that
``json`` produced is never used as an operand, and quantised to twelve decimals
with ``ROUND_HALF_UP``. ``float`` appears exactly once per rate: in the payload
handed to Odoo.

**A large move is a decision, not a data point.** A change of more than
``FRANKFURTER_RATE_CHANGE_THRESHOLD`` against the most recent prior rate is
usually a bad feed rather than a market event, so the write is withheld and an
Administrator ``mail.activity`` asks a human. The activity is keyed on its own
summary, so re-running the sync re-finds it instead of stacking duplicates.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from ..errors import (
    HttpError,
    IntegrationError,
    InvalidPayloadError,
    OdooError,
    RecordSyncError,
)
from ..idempotency import make_external_id
from ..sanitize import sanitize, truncate
from ..sync_result import SyncResult
from .base import BaseConnector, ConnectorContext

RATE_MODEL = "res.currency.rate"
CURRENCY_MODEL = "res.currency"
COMPANY_MODEL = "res.company"
ACTIVITY_MODEL = "mail.activity"
ACTIVITY_TYPE_MODEL = "mail.activity.type"
IR_MODEL = "ir.model"
USER_MODEL = "res.users"

#: Rates are stored to twelve decimals; the same quantum is the tolerance used
#: when deciding whether a stored rate already equals the incoming one.
RATE_PRECISION = Decimal("0.000000000001")
PERCENT_PRECISION = Decimal("0.01")

#: EUR/GBP/TRY ship inactive on this database, so every currency lookup has to
#: see archived records - otherwise the connector would report them as missing.
INACTIVE_CONTEXT = {"active_test": False}

#: Preferred activity type for the approval task; any type will do if absent.
#: The live database names it "To-Do"; other databases use "To Do".
PREFERRED_ACTIVITY_TYPES = ("To-Do", "To Do")

#: ``mail.activity`` can only attach to a model that inherits ``mail.activity.mixin``.
#: ``res.currency`` does NOT (verified: creating one there raises
#: "'res.currency' object has no attribute 'message_subscribe'" as HTTP 500), so the
#: approval reminder is anchored on the first candidate the database actually
#: supports. ``res.currency`` stays first so a database that does support it keeps
#: the most meaningful anchor; the approver's own user record is the fallback, which
#: still surfaces the activity in their Activities view.
ACTIVITY_ANCHORS = ("res.currency", "res.users", "res.partner")

#: Field present only on models carrying ``mail.activity.mixin``.
ACTIVITY_MIXIN_FIELD = "activity_ids"

#: The database enforces one rate per currency per day and reports the breach as
#: HTTP 422. Losing that race means another run already stored today's rate.
_DUPLICATE_RATE_RE = re.compile(r"only one currency rate per day", re.IGNORECASE)


@dataclass(frozen=True)
class _Quote:
    """One ``base -> quote`` observation, with its rate still unparsed.

    The raw value is kept verbatim so it is converted to :class:`Decimal` inside
    the per-currency guard, where a malformed number fails one currency instead
    of the run.
    """

    code: str
    date: str
    raw_rate: Any


def _to_decimal(value: Any, label: str) -> Decimal:
    """Parse a rate as an exact decimal, never routing it through ``float``."""
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise InvalidPayloadError(f"{label} is not a number: {truncate(value, 80)}") from None
    if not parsed.is_finite():
        raise InvalidPayloadError(f"{label} is not a finite number: {truncate(value, 80)}")
    return parsed


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(RATE_PRECISION, rounding=ROUND_HALF_UP)


def _percent(ratio: Decimal) -> Decimal:
    return (ratio * Decimal(100)).quantize(PERCENT_PRECISION, rounding=ROUND_HALF_UP)


def _valid_date(value: Any, label: str) -> str:
    """Validate an ISO date, which is what ``res.currency.rate.name`` stores."""
    text = str(value or "").strip()
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise InvalidPayloadError(f"{label} is not an ISO date: {truncate(value, 40)}") from None
    return text


def _is_duplicate_rate(exc: OdooError) -> bool:
    text = str(exc)
    if _DUPLICATE_RATE_RE.search(text):
        return True
    return exc.status_code == 422 and "currency rate" in text.lower()


class FrankfurterConnector(BaseConnector):
    """Imports Frankfurter FX rates, rebased onto the Odoo company currency."""

    provider = "frankfurter"
    label = "Frankfurter"

    def __init__(self, ctx: ConnectorContext) -> None:
        super().__init__(ctx)
        #: Singleton Odoo lookups (ir.model row, activity type, approver), each
        #: resolved at most once per run even when several rates need approval.
        self._ids: Dict[str, Optional[int]] = {}

    # -- template method ----------------------------------------------------

    def sync(self, result: SyncResult) -> None:
        cfg = self.settings.frankfurter
        company_id, company_currency_id, company_code = self._resolve_company()

        quotes = self._fetch_quotes(result, company_code)
        divisor = self._company_divisor(quotes, company_code)
        result.details["conversion"] = {
            "api_base": cfg.base_currency,
            "company_id": company_id,
            "company_currency": company_code,
            "company_currency_id": company_currency_id,
            "divisor": str(divisor),
        }
        result.add_note(
            f"Rates rebased for Odoo: units per 1 {company_code} = "
            f"api_rate({cfg.base_currency}->X) / api_rate({cfg.base_currency}->{company_code})."
        )

        targets = []
        for quote in quotes:
            if quote.code == company_code:
                # The company currency is 1 against itself by definition; Odoo
                # neither needs nor accepts a rate row asserting that.
                result.record_skipped(
                    reason=f"{company_code} is the company currency; its rate is 1 by definition."
                )
                continue
            targets.append(quote)

        selected = self.limited(targets, result, "currency rates")
        currencies = self._load_currencies([q.code for q in selected])
        breakdown: Dict[str, Any] = {}
        result.details["rates"] = breakdown

        for quote in selected:
            external_id = make_external_id(self.provider, "rate", quote.code, quote.date)
            with self.guard(result, external_id=external_id, label="currency rate"):
                self._sync_quote(quote, divisor, company_id, currencies, result, breakdown)

    # -- upstream -----------------------------------------------------------

    def _fetch_quotes(self, result: SyncResult, company_code: str) -> List[_Quote]:
        """Read the configured quotes plus whatever the rebasing needs."""
        cfg = self.settings.frankfurter
        # The company currency is requested even when it is not configured as a
        # quote: without it the rebasing below has no divisor.
        requested: List[str] = []
        for code in [*cfg.quotes, company_code]:
            code = str(code or "").strip().upper()
            if code and code not in requested:
                requested.append(code)

        params = {"base": cfg.base_currency, "quotes": ",".join(requested)}
        try:
            response = self.http.get(cfg.rates_url, params=params)
        except HttpError as exc:
            # Without the feed there is nothing to sync at all.
            raise IntegrationError(f"Frankfurter rates are unavailable: {sanitize(exc)}") from None

        if response.is_empty:
            raise IntegrationError(
                f"Frankfurter returned an empty body for {sanitize(cfg.rates_url)}."
            )
        quotes = self._parse_quotes(response.data, cfg.base_currency)
        if not quotes:
            raise IntegrationError(
                f"Frankfurter returned no rates for base {cfg.base_currency} "
                f"and quotes {', '.join(requested)}."
            )
        result.details["fetched"] = {"requested": requested, "returned": [q.code for q in quotes]}
        return quotes

    @staticmethod
    def _parse_quotes(payload: Any, base: str) -> List[_Quote]:
        """Normalise both documented response shapes into a list of quotes.

        v2 answers a list of ``{date, base, quote, rate}`` objects and is treated
        as canonical; the v1 ``{"date": ..., "rates": {code: rate}}`` envelope is
        still accepted so a pinned older host keeps working.
        """
        items: List[Mapping[str, Any]]
        if isinstance(payload, list):
            items = [row for row in payload if isinstance(row, Mapping)]
        elif isinstance(payload, Mapping) and isinstance(payload.get("rates"), Mapping):
            envelope_date = payload.get("date")
            items = [
                {"quote": code, "rate": value, "date": envelope_date, "base": payload.get("base")}
                for code, value in payload["rates"].items()
            ]
        elif isinstance(payload, Mapping) and "quote" in payload:
            items = [payload]
        else:
            raise InvalidPayloadError(
                f"Frankfurter returned {type(payload).__name__}, expected a list of quotes "
                f"or a rates envelope: {truncate(payload, 200)}"
            )

        quotes: List[_Quote] = []
        seen: set = set()
        for row in items:
            code = str(row.get("quote") or row.get("currency") or "").strip().upper()
            if not code or code in seen:
                continue
            seen.add(code)
            row_base = str(row.get("base") or base).strip().upper()
            if row_base != base.strip().upper():
                raise InvalidPayloadError(
                    f"Frankfurter answered with base {row_base}, but {base} was requested."
                )
            quotes.append(
                _Quote(code=code, date=str(row.get("date") or ""), raw_rate=row.get("rate"))
            )
        return quotes

    def _company_divisor(self, quotes: List[_Quote], company_code: str) -> Decimal:
        """Rate of the company currency against the API base, as a divisor."""
        base = self.settings.frankfurter.base_currency.strip().upper()
        if company_code == base:
            return Decimal(1)

        entry = next((q for q in quotes if q.code == company_code), None)
        if entry is None:
            raise IntegrationError(
                f"Frankfurter did not return the company currency {company_code}, and the API base "
                f"is {base}, so quotes cannot be rebased onto {company_code}. Add {company_code} to "
                f"FRANKFURTER_QUOTES, or set FRANKFURTER_BASE_CURRENCY={company_code}."
            )
        divisor = _to_decimal(entry.raw_rate, f"Frankfurter rate for {company_code}")
        if divisor <= 0:
            raise IntegrationError(
                f"Frankfurter reported a non-positive rate ({divisor}) for the company currency "
                f"{company_code}; quotes cannot be rebased against it."
            )
        return divisor

    # -- Odoo context -------------------------------------------------------

    def _resolve_company(self) -> Tuple[int, int, str]:
        """Return ``(company_id, currency_id, currency_code)`` for the main company."""
        rows = self.odoo.search_read(
            COMPANY_MODEL, [], fields=["id", "name", "currency_id"], limit=1, order="id asc"
        )
        if not rows:
            raise IntegrationError(
                "No res.company is readable with this API key; the company currency that every "
                "rate is expressed against cannot be determined."
            )
        company = rows[0]
        currency_id = self.odoo.ref_id(company.get("currency_id"))
        if not currency_id:
            raise IntegrationError(
                f"Company {company.get('id')} has no currency_id; rates have nothing to rebase onto."
            )

        currencies = self.odoo.search_read(
            CURRENCY_MODEL, [["id", "=", currency_id]], fields=["id", "name"],
            limit=1, context=INACTIVE_CONTEXT,
        )
        code = str(currencies[0]["name"]).strip().upper() if currencies else ""
        if not code:
            raise IntegrationError(
                f"Currency {currency_id} of company {company.get('id')} has no ISO code."
            )
        return int(company["id"]), int(currency_id), code

    def _load_currencies(self, codes: List[str]) -> Dict[str, Dict[str, Any]]:
        """Look up the target currencies, archived ones included."""
        if not codes:
            return {}
        rows = self.odoo.search_read(
            CURRENCY_MODEL, [["name", "in", sorted(set(codes))]],
            fields=["id", "name", "active"], context=INACTIVE_CONTEXT,
        )
        return {str(row["name"]).strip().upper(): row for row in rows}

    # -- per-currency -------------------------------------------------------

    def _sync_quote(
        self,
        quote: _Quote,
        divisor: Decimal,
        company_id: int,
        currencies: Mapping[str, Dict[str, Any]],
        result: SyncResult,
        breakdown: Dict[str, Any],
    ) -> None:
        """Store one rebased rate, or withhold it pending approval."""
        cfg = self.settings.frankfurter
        entry: Dict[str, Any] = {"date": "", "prior": None, "new": None,
                                 "pct_change": None, "action": "failed"}
        breakdown[quote.code] = entry

        rate_date = _valid_date(quote.date, f"Frankfurter date for {quote.code}")
        entry["date"] = rate_date
        api_rate = _to_decimal(quote.raw_rate, f"Frankfurter rate for {quote.code}")
        if api_rate <= 0:
            raise RecordSyncError(
                f"Frankfurter reported a non-positive rate ({api_rate}) for {quote.code}."
            )
        new_rate = _quantize(api_rate / divisor)
        entry["new"] = str(new_rate)

        row = currencies.get(quote.code)
        if row is None:
            raise RecordSyncError(
                f"No {CURRENCY_MODEL} named {quote.code} exists in Odoo; its rate cannot be stored.",
                external_id=quote.code,
            )
        currency_id = int(row["id"])
        if not row.get("active"):
            self._activate_currency(currency_id, quote.code, result)
            entry["activated"] = True

        prior = self._prior_rate(currency_id, company_id, rate_date, quote.code)
        if prior is not None:
            entry["prior"] = str(prior)
            change = abs(new_rate - prior) / prior
            entry["pct_change"] = float(_percent(change))
            if change > Decimal(str(cfg.rate_change_threshold)):
                self._withhold_for_approval(
                    quote, currency_id, prior, new_rate, change, rate_date, result, entry
                )
                return

        self._store_rate(quote, currency_id, company_id, rate_date, new_rate, result, entry)

    def _activate_currency(self, currency_id: int, code: str, result: SyncResult) -> None:
        """Un-archive a currency so a rate may be attached to it."""
        self._write_record(result, CURRENCY_MODEL, [currency_id], {"active": True})
        result.add_note(f"Activated currency {code} (id {currency_id}) so its rate could be stored.")

    def _prior_rate(
        self, currency_id: int, company_id: int, rate_date: str, code: str
    ) -> Optional[Decimal]:
        """Most recent rate strictly before ``rate_date``, or ``None``."""
        rows = self.odoo.search_read(
            RATE_MODEL,
            [
                ["currency_id", "=", currency_id],
                ["name", "<", rate_date],
                ["company_id", "=", company_id],
            ],
            fields=["id", "name", "rate"],
            limit=1,
            order="name desc",
            context=INACTIVE_CONTEXT,
        )
        if not rows:
            return None
        prior = _to_decimal(rows[0].get("rate"), f"Stored {code} rate {rows[0].get('name')}")
        if prior <= 0:
            # A zero or negative stored rate makes the ratio undefined; treat it
            # as no baseline rather than dividing by it.
            return None
        return prior

    def _store_rate(
        self,
        quote: _Quote,
        currency_id: int,
        company_id: int,
        rate_date: str,
        new_rate: Decimal,
        result: SyncResult,
        entry: Dict[str, Any],
    ) -> None:
        """Create, update or leave alone the rate row for currency+date."""
        domain = [
            ["currency_id", "=", currency_id],
            ["name", "=", rate_date],
            ["company_id", "=", company_id],
        ]
        try:
            existing = self.odoo.search_read(
                RATE_MODEL, domain, fields=["id", "rate"], limit=1, context=INACTIVE_CONTEXT
            )
            if not existing:
                vals = {
                    "currency_id": currency_id,
                    "company_id": company_id,
                    "name": rate_date,
                    # The only place a rate becomes a float, and the last.
                    "rate": float(new_rate),
                }
                record_id = self._create_record(result, RATE_MODEL, vals)
                entry["action"] = "created"
                entry["rate_id"] = record_id
                result.record_created()
                return

            stored = _to_decimal(existing[0].get("rate"), f"Stored {quote.code} rate {rate_date}")
            entry["rate_id"] = existing[0].get("id")
            if abs(stored - new_rate) <= RATE_PRECISION:
                entry["action"] = "unchanged"
                result.record_skipped(
                    reason=f"{quote.code} rate for {rate_date} already stored as {new_rate}."
                )
                return

            self._write_record(result, RATE_MODEL, [existing[0]["id"]], {"rate": float(new_rate)})
            entry["action"] = "updated"
            result.record_updated()
        except OdooError as exc:
            if not _is_duplicate_rate(exc):
                raise
            # Another run stored this day's rate between our search and create.
            entry["action"] = "duplicate"
            result.record_skipped(
                reason=f"{quote.code} rate for {rate_date} was stored concurrently by another run."
            )

    # -- approval -----------------------------------------------------------

    def _withhold_for_approval(
        self,
        quote: _Quote,
        currency_id: int,
        prior: Decimal,
        new_rate: Decimal,
        change: Decimal,
        rate_date: str,
        result: SyncResult,
        entry: Dict[str, Any],
    ) -> None:
        """Skip the write and ask a human, exactly once per currency and date."""
        cfg = self.settings.frankfurter
        pct = _percent(change)
        threshold_pct = _percent(Decimal(str(cfg.rate_change_threshold)))
        summary = sanitize(
            f"Approve {quote.code} rate change of {pct}% on {rate_date}"
        )
        entry["action"] = "pending_approval"

        activity_id = self._ensure_activity(
            quote, currency_id, prior, new_rate, pct, threshold_pct, rate_date, summary, result
        )
        entry["activity_id"] = activity_id
        result.record_skipped(
            reason=(
                f"{quote.code} moved {pct}% (threshold {threshold_pct}%) from {prior} to {new_rate} "
                f"on {rate_date}; the rate was not written and awaits approval."
            )
        )

    def _ensure_activity(
        self,
        quote: _Quote,
        currency_id: int,
        prior: Decimal,
        new_rate: Decimal,
        pct: Decimal,
        threshold_pct: Decimal,
        rate_date: str,
        summary: str,
        result: SyncResult,
    ) -> Optional[int]:
        """Find or create the approval activity. Never turns a skip into a failure."""
        try:
            user_id = self._approver_id(result)
            anchor = self._activity_anchor(currency_id, user_id)
            if anchor is None:
                result.add_note(
                    f"Approval activity for {quote.code} on {rate_date} could not be anchored: no "
                    f"candidate model among {', '.join(ACTIVITY_ANCHORS)} supports activities. "
                    "The rate write was withheld anyway."
                )
                return None
            anchor_model, anchor_id = anchor

            existing = self.odoo.search_read(
                ACTIVITY_MODEL,
                [
                    ["res_model", "=", anchor_model],
                    ["res_id", "=", anchor_id],
                    ["summary", "=", summary],
                ],
                fields=["id"],
                limit=1,
            )
            if existing:
                result.add_note(f"Approval activity for {quote.code} on {rate_date} already exists.")
                return existing[0]["id"]

            res_model_id = self._model_id(anchor_model)
            activity_type_id = self._activity_type_id()
            missing = [
                name
                for name, value in (
                    (f"ir.model row for {anchor_model}", res_model_id),
                    ("mail.activity.type", activity_type_id),
                    ("approver user", user_id),
                )
                if not value
            ]
            if missing:
                result.add_note(
                    f"Approval activity for {quote.code} on {rate_date} could not be created "
                    f"(unresolved: {', '.join(missing)}); the rate write was withheld anyway."
                )
                return None

            vals = {
                "res_model_id": res_model_id,
                "res_model": anchor_model,
                "res_id": anchor_id,
                "activity_type_id": activity_type_id,
                "user_id": user_id,
                "date_deadline": rate_date,
                "summary": summary,
                "note": self._activity_note(quote, prior, new_rate, pct, threshold_pct, rate_date),
            }
            return self._create_record(result, ACTIVITY_MODEL, vals)
        except OdooError as exc:
            # The withheld write is the point; a missing reminder must not
            # reclassify this record as a failure.
            result.add_note(
                f"Approval activity for {quote.code} on {rate_date} could not be created: "
                f"{sanitize(exc)}. The rate write was withheld anyway."
            )
            return None

    def _activity_note(
        self,
        quote: _Quote,
        prior: Decimal,
        new_rate: Decimal,
        pct: Decimal,
        threshold_pct: Decimal,
        rate_date: str,
    ) -> str:
        cfg = self.settings.frankfurter
        rows = [
            ("Currency", quote.code),
            ("Rate date", rate_date),
            ("Prior rate", str(prior)),
            ("Proposed rate", str(new_rate)),
            ("Change", f"{pct}% (threshold {threshold_pct}%)"),
            ("Source", f"Frankfurter {cfg.rates_url} (base {cfg.base_currency})"),
        ]
        body = "".join(
            f"<li><strong>{html.escape(key)}:</strong> {html.escape(value)}</li>"
            for key, value in rows
        )
        return sanitize(
            "<p>An automated currency rate update was withheld because it exceeds the configured "
            "change threshold. Approve it by entering the rate manually, or investigate the feed.</p>"
            f"<ul>{body}</ul>"
        )

    # -- singleton lookups --------------------------------------------------

    def _cached_id(self, key: str, resolve: Callable[[], Optional[int]]) -> Optional[int]:
        if key not in self._ids:
            self._ids[key] = resolve()
        return self._ids[key]

    def _model_id(self, model: str) -> Optional[int]:
        def resolve() -> Optional[int]:
            rows = self.odoo.search_read(
                IR_MODEL, [["model", "=", model]], fields=["id"], limit=1
            )
            return rows[0]["id"] if rows else None

        return self._cached_id(f"ir.model:{model}", resolve)

    def _activity_type_id(self) -> Optional[int]:
        def resolve() -> Optional[int]:
            rows = self.odoo.search_read(
                ACTIVITY_TYPE_MODEL, [["name", "in", list(PREFERRED_ACTIVITY_TYPES)]],
                fields=["id"], limit=1,
            )
            if not rows:
                rows = self.odoo.search_read(
                    ACTIVITY_TYPE_MODEL, [], fields=["id"], limit=1, order="id asc"
                )
            return rows[0]["id"] if rows else None

        return self._cached_id("activity_type", resolve)

    def _supports_activities(self, model: str) -> bool:
        """True when ``model`` carries ``mail.activity.mixin``.

        Checked through ``ir.model.fields`` rather than by attempting a create,
        because an unsupported anchor fails as HTTP 500 and would burn the whole
        retry budget on every withheld rate.
        """
        def resolve() -> Optional[int]:
            rows = self.odoo.search_read(
                "ir.model.fields",
                [["model", "=", model], ["name", "=", ACTIVITY_MIXIN_FIELD]],
                fields=["id"], limit=1,
            )
            return rows[0]["id"] if rows else None

        return bool(self._cached_id(f"activity_mixin:{model}", resolve))

    def _activity_anchor(
        self, currency_id: int, user_id: Optional[int]
    ) -> Optional[Tuple[str, int]]:
        """Pick the first anchor model this database can attach an activity to."""
        candidates = {
            "res.currency": currency_id,
            "res.users": user_id,
            "res.partner": self._approver_partner_id(user_id),
        }
        for model in ACTIVITY_ANCHORS:
            record_id = candidates.get(model)
            if record_id and self._supports_activities(model):
                return model, record_id
        return None

    def _approver_partner_id(self, user_id: Optional[int]) -> Optional[int]:
        if not user_id:
            return None

        def resolve() -> Optional[int]:
            rows = self.odoo.search_read(
                USER_MODEL, [["id", "=", user_id]], fields=["partner_id"], limit=1,
                context=INACTIVE_CONTEXT,
            )
            return self.odoo.ref_id(rows[0].get("partner_id")) if rows else None

        return self._cached_id(f"approver_partner:{user_id}", resolve)

    def _approver_id(self, result: SyncResult) -> Optional[int]:
        """The configured approver, else the lowest-id internal user."""
        def resolve() -> Optional[int]:
            login = self.settings.frankfurter.approver_login
            if login:
                rows = self.odoo.search_read(
                    USER_MODEL, [["login", "=", login]], fields=["id"],
                    limit=1, context=INACTIVE_CONTEXT,
                )
                if rows:
                    return rows[0]["id"]
                result.add_note(
                    f"FRANKFURTER_APPROVER_LOGIN {login!r} matches no user; "
                    f"falling back to the first internal user."
                )
            rows = self.odoo.search_read(
                USER_MODEL, [["share", "=", False]], fields=["id"], limit=1, order="id asc"
            )
            return rows[0]["id"] if rows else None

        return self._cached_id("approver", resolve)

    # -- writes -------------------------------------------------------------

    def _create_record(self, result: SyncResult, model: str, vals: Dict[str, Any]) -> Optional[int]:
        """Create, or describe the create when the run is a dry run."""
        if self.dry_run:
            result.add_mock_write("CREATE", f"odoo://{model}", vals)
            return None
        return self.odoo.create_one(model, vals)

    def _write_record(
        self, result: SyncResult, model: str, ids: List[int], vals: Dict[str, Any]
    ) -> None:
        if self.dry_run:
            result.add_mock_write("WRITE", f"odoo://{model}/{ids}", vals)
            return
        self.odoo.write(model, ids, vals)


__all__ = ["FrankfurterConnector"]
