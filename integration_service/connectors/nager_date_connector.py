# -*- coding: utf-8 -*-
"""Nager.Date public holidays -> Odoo ``calendar.event``.

``GET /api/v4/Holidays/{CountryCode}/{Year}`` is called once per configured
(country, year) pair. Three properties of that feed shape this connector.

**There is no per-record timestamp.** A holiday object carries a date, a name and
its classification - nothing that says when the entry itself last changed. So
``x_external_updated_at`` is left unset and ``x_source_hash`` carries the whole
change decision: a holiday that was renamed or re-typed upstream hashes
differently and is updated in place, while an unchanged year re-runs as pure
skips. That is also why the description is built from the classification fields
rather than dropped - anything omitted from the mapped values is invisible to the
hash and would silently fail to trigger an update.

**A country with no data answers 204, not an error.** Nager.Date has no dataset
for several countries (PK is one), and says so with ``204 No Content``. That is a
legitimate "nothing to import" outcome, so it is counted as *skipped* with a note
naming the country - never as a failure, and never as an invalid payload.

**One bad country must not cost the others.** Each pair is fetched inside its own
guard, so a 404 from a mistyped country code or a malformed body costs that
country alone; inside a country, each holiday is guarded again, so a row missing
its date is one failed record and the remaining holidays still import.

The external id is ``nager:holiday:<COUNTRY>:<YYYY-MM-DD>:<slugified-name>``,
which is both the duplicate protection (re-importing a year matches the existing
events instead of cloning them) and the mapping back to the upstream entry.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date as date_type, datetime
from html import escape as html_escape
from typing import Any, Dict, List, Optional, Tuple

from ..errors import InvalidPayloadError, RecordSyncError
from ..idempotency import Upserter, make_external_id, slugify
from ..sanitize import truncate
from ..sync_result import SyncResult
from .base import BaseConnector

PROVIDER = "nager_date"
EVENT_MODEL = "calendar.event"

#: First segment of ``x_external_id``. Deliberately shorter than the sync-log
#: provider value: the external id is the stable public key of the upstream
#: record and renaming the log selection must not orphan already-imported events.
EXTERNAL_ID_PROVIDER = "nager"

#: A holiday is a whole calendar day. ``calendar.event.start``/``stop`` are
#: datetime columns even when ``allday`` is set, so the day is spanned end to end.
DAY_START = "00:00:00"
DAY_END = "23:59:59"

#: ``show_as=free`` keeps an imported holiday from marking everybody busy.
SHOW_AS = "free"

_DATE_FORMAT = "%Y-%m-%d"


def _text(value: Any) -> str:
    """Trimmed string, with ``None``/``False`` collapsing to an empty string."""
    if value is None or value is False:
        return ""
    return str(value).strip()


def _as_list(value: Any) -> List[str]:
    """Normalise a scalar / list / null field into a list of non-empty strings."""
    if value is None or value is False:
        return []
    if isinstance(value, (list, tuple)):
        return [part for part in (_text(item) for item in value) if part]
    text = _text(value)
    return [text] if text else []


def _joined(value: Any) -> str:
    """Render a scalar or a list of scalars as one comma-separated string."""
    if isinstance(value, (list, tuple)):
        return ", ".join(part for part in (_text(item) for item in value) if part)
    return _text(value)


def _scope_digest(item: Dict[str, Any]) -> str:
    """Key fragment separating two same-day, same-name holidays, or ``""``.

    Country + date + name is *not* unique in this feed. US 2026 returns "Good
    Friday" twice on 2026-04-03 (Public across ten states, and Optional in US-TX)
    and "Columbus Day" twice on 2026-10-12 (Public in 33 states, and a nationwide
    Bank holiday). Keyed on the triple alone the two rows share an external id and
    overwrite each other on every run, so the sync never converges.

    Only ``subdivisionCodes`` is folded in, for two reasons: it already separates
    both real collisions, and it is the field that genuinely denotes a different
    observance. ``holidayTypes`` is deliberately excluded - a holiday that gains a
    type upstream is the same holiday, and including it would orphan the existing
    event and create a new one instead of updating in place.

    A nationwide holiday has no subdivisions and gets no suffix, which keeps the
    common case readable and its key unchanged.
    """
    subdivisions = sorted(_as_list(item.get("subdivisionCodes")))
    if not subdivisions:
        return ""
    canonical = json.dumps(subdivisions, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:10]


def _scope_label(item: Dict[str, Any], country_code: str) -> str:
    """Human-readable scope for the event name, so duplicates are tellable apart."""
    subdivisions = sorted(_as_list(item.get("subdivisionCodes")))
    if not subdivisions:
        return country_code
    if len(subdivisions) == 1:
        return subdivisions[0]
    return f"{subdivisions[0]} +{len(subdivisions) - 1}"


def _parse_day(value: str) -> Optional[date_type]:
    """Parse a ``YYYY-MM-DD`` calendar date, or ``None`` when it is not one."""
    try:
        return datetime.strptime(value, _DATE_FORMAT).date()
    except ValueError:
        return None


class NagerDateConnector(BaseConnector):
    """Imports public holidays as all-day, free calendar events."""

    provider = PROVIDER
    label = "Nager.Date"

    # -- template method ----------------------------------------------------

    def sync(self, result: SyncResult) -> None:
        pairs = self._pairs(result)
        if not pairs:
            # count=0 keeps ``processed`` at zero, which is what makes the run
            # report as "skipped" rather than as a success that did nothing.
            result.record_skipped(
                count=0,
                reason=(
                    "No country/year pair is configured, so there were no holidays to "
                    "import. Set NAGER_COUNTRIES (e.g. US,DE) and NAGER_YEARS."
                ),
            )
            return

        # One upserter for the whole run: its cache is shared across countries,
        # so a holiday already seen this run costs no second read.
        upserter = Upserter(self.odoo, EVENT_MODEL, result, dry_run=self.dry_run)

        for country_code, year in pairs:
            key = f"{country_code}:{year}"
            bucket: Dict[str, int] = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}
            result.details[key] = bucket
            failed_before = result.failed
            with self.guard(result, external_id=key, label="holiday calendar"):
                self._sync_calendar(result, upserter, country_code, year, bucket)
            # Per-holiday failures and a failure of the country itself both land
            # on result.failed, so diffing it keeps one source of truth per pair.
            bucket["failed"] = result.failed - failed_before

    # -- configuration -------------------------------------------------------

    def _pairs(self, result: SyncResult) -> List[Tuple[str, int]]:
        """Expand the configured countries and years into deduplicated pairs.

        A country repeated in ``NAGER_COUNTRIES`` would otherwise be fetched
        twice and inflate the skip count on its second, identical pass.
        """
        config = self.settings.nager
        pairs: List[Tuple[str, int]] = []
        for raw_country in config.countries:
            country_code = _text(raw_country).upper()
            if not country_code:
                continue
            for raw_year in config.years:
                try:
                    year = int(raw_year)
                except (TypeError, ValueError):
                    result.add_note(
                        f"Ignoring NAGER_YEARS entry {truncate(raw_year, 20)}: not a year."
                    )
                    continue
                pair = (country_code, year)
                if pair not in pairs:
                    pairs.append(pair)
        return pairs

    # -- one country/year ----------------------------------------------------

    def _sync_calendar(
        self,
        result: SyncResult,
        upserter: Upserter,
        country_code: str,
        year: int,
        bucket: Dict[str, int],
    ) -> None:
        """Fetch one country's year and upsert every holiday in it."""
        url = self.settings.nager.holidays_url(country_code, year)
        response = self.http.get(url)

        if response.is_empty:
            # count=0 because the counters count records, and this country yielded
            # none. Counting the country itself as a skipped record would make a run
            # that imported nothing look like a run that examined something, and
            # would report "success" instead of "skipped".
            result.record_skipped(
                count=0,
                reason=(
                    f"Nager.Date has no holiday data for {country_code} in {year} "
                    f"(HTTP {response.status_code}, empty body); nothing to import for "
                    "that country."
                ),
            )
            return

        if not isinstance(response.data, list):
            raise InvalidPayloadError(
                f"Nager.Date returned {type(response.data).__name__} for {country_code}/{year}, "
                f"expected a list of holidays ({truncate(response.data, 120)})."
            )

        items = self.limited(response.data, result, f"holidays for {country_code}/{year}")
        prepared = [(item, self._external_id(country_code, item)) for item in items]
        # One read per 100 ids for the whole batch, so a re-run of an unchanged
        # year costs reads rather than a lookup per holiday.
        upserter.preload([eid for _, eid in prepared if eid])

        for item, external_id in prepared:
            with self.guard(result, external_id=external_id, label="holiday"):
                outcome, _record_id = self._sync_holiday(upserter, country_code, item, external_id, result)
                bucket[outcome] += 1

    # -- one holiday ---------------------------------------------------------

    def _sync_holiday(
        self,
        upserter: Upserter,
        country_code: str,
        item: Any,
        external_id: Optional[str],
        result: SyncResult,
    ) -> Tuple[str, Optional[int]]:
        """Map and upsert one holiday, raising :class:`RecordSyncError` on bad input."""
        name, day = self._validated(item, external_id, result)
        vals = {
            "name": f"{name} ({_scope_label(item, country_code)})",
            "start": f"{day} {DAY_START}",
            "stop": f"{day} {DAY_END}",
            "allday": True,
            "show_as": SHOW_AS,
            "description": self._description(item),
        }
        # external_updated_at stays None: the feed carries no modification time,
        # so the hash over these values is the only change detector available.
        return upserter.upsert(external_id, vals)

    # -- validation ----------------------------------------------------------

    def _validated(
        self,
        item: Any,
        external_id: Optional[str],
        result: Optional[SyncResult] = None,
    ) -> Tuple[str, str]:
        """Return ``(name, YYYY-MM-DD)``, or raise for a row that cannot be mapped.

        Every rejection here is a per-record failure: a holiday without a usable
        date or name has no identity and no start, and importing it would produce
        an event that cannot be matched again on the next run.
        """
        if not isinstance(item, dict):
            raise RecordSyncError(
                f"Nager.Date returned {type(item).__name__} where a holiday object was "
                f"expected ({truncate(item, 120)}).",
                external_id,
            )

        name = _text(item.get("name"))
        raw_date = _text(item.get("date"))
        missing = [field for field, value in (("date", raw_date), ("name", name)) if not value]
        if missing:
            if result is not None and external_id:
                self.withhold_for_approval(
                    external_id=external_id,
                    summary=f"Approval Required: Incomplete Holiday Mapping for {external_id}",
                    note=f"Holiday object is missing required field(s): {', '.join(missing)}.",
                    result=result,
                    count_skipped=False,
                )
            raise RecordSyncError(
                f"Holiday is missing required field(s) {', '.join(missing)}: "
                f"{truncate(item, 120)}.",
                external_id,
            )

        day = _parse_day(raw_date)
        if day is None:
            raise RecordSyncError(
                f"Holiday date {truncate(raw_date, 40)} is not a YYYY-MM-DD date, so the "
                "calendar event could not be dated.",
                external_id,
            )
        return name, day.isoformat()

    @staticmethod
    def _external_id(country_code: str, item: Any) -> Optional[str]:
        """Key of one holiday, or ``None`` for a row too broken to key.

        Returning ``None`` instead of raising keeps the preload pass free of
        control flow: the record loop re-checks the same two fields inside its
        guard, where the failure is charged to that one holiday.
        """
        if not isinstance(item, dict):
            return None
        raw_date = _text(item.get("date"))
        name = _text(item.get("name"))
        if not raw_date or not name:
            return None
        # Slugified explicitly rather than left to make_external_id, which keeps a
        # single word like "Neujahr" verbatim while turning "New Year's Day" into a
        # slug. That inconsistency would make the key case-sensitive, so an upstream
        # re-casing of a one-word holiday would clone the event instead of matching it.
        # The trailing digest separates rows the (country, date, name) triple
        # cannot; it is empty, and omitted, for a nationwide holiday.
        parts = [country_code, raw_date, slugify(name)]
        scope = _scope_digest(item)
        if scope:
            parts.append(scope)
        return make_external_id(EXTERNAL_ID_PROVIDER, "holiday", *parts)

    @staticmethod
    def _description(item: Dict[str, Any]) -> str:
        """Render the classification fields as an HTML fragment.

        These values are part of the hash, which is what makes a holiday that
        gains a subdivision or a type upstream register as an update.
        """
        rows: List[Tuple[str, str]] = []

        types = _joined(item.get("holidayTypes"))
        if types:
            rows.append(("Holiday types", types))
        if "nationalHoliday" in item:
            rows.append(("National holiday", "Yes" if item.get("nationalHoliday") else "No"))
        # null for a holiday observed nationwide, which is the common case.
        subdivisions = _joined(item.get("subdivisionCodes"))
        if subdivisions:
            rows.append(("Subdivisions", subdivisions))

        if not rows:
            return ""
        cells = "".join(
            f"<li>{html_escape(label)}: {html_escape(value)}</li>" for label, value in rows
        )
        return f"<ul>{cells}</ul>"


__all__ = ["NagerDateConnector"]
