# -*- coding: utf-8 -*-
"""Open-Meteo -> a rolling 7-day forecast stored on each geolocated contact.

``GET /v1/forecast`` is called once per contact, using the ``partner_latitude`` /
``partner_longitude`` that the JSONPlaceholder import wrote. The response is a
*columnar* series - ``daily.time``, ``daily.temperature_2m_max`` and
``daily.temperature_2m_min`` are three parallel lists - so the whole block is
validated before it is zipped: a missing column, a column that is not a list,
columns of unequal length or a null temperature would otherwise pair a date with
the wrong day's reading and store a silently wrong forecast. Each such failure is
raised inside the per-contact guard, so one unusable response costs one contact
rather than the run.

Two design points are worth stating explicitly.

**The three idempotency columns are not used here.** ``x_external_id``,
``x_source_hash`` and ``x_external_updated_at`` on ``res.partner`` are owned by
the JSONPlaceholder connector, which decides create/update/skip from them.
Writing a forecast hash into ``x_source_hash`` would make every JSONPlaceholder
run see a changed contact and rewrite it, and this connector would then see the
rewritten contact - an endless mutual update loop. The forecast therefore lives
in its own dedicated columns and idempotency is decided by comparing the newly
serialised payload against the stored one. :class:`Upserter` is deliberately not
involved.

**The payload is serialised canonically.** ``json.dumps(..., sort_keys=True,
separators=(",", ":"))`` makes the stored text byte-stable, which is what lets a
plain string comparison stand in for a hash. ``timezone=auto`` means the series
starts on the contact's *local* today, so the payload legitimately changes once a
day and the contact is rewritten once a day - not on every run.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from ..errors import InvalidPayloadError, OdooError, RecordSyncError
from ..sanitize import sanitize, truncate
from ..sync_result import SyncResult, to_odoo_datetime, utcnow
from .base import BaseConnector

PROVIDER = "open_meteo"

#: Query parameters proven against the live endpoint, sent verbatim on every
#: request. The daily variable list defines the response shape parsed below, and
#: ``timezone=auto`` dates each day in the contact's own timezone rather than UTC.
DAILY_PARAM = "temperature_2m_max,temperature_2m_min"
TIMEZONE_PARAM = "auto"

#: Columns the ``daily`` block must carry, in validation order.
TIME_KEY = "time"
MAX_KEY = "temperature_2m_max"
MIN_KEY = "temperature_2m_min"
DAILY_KEYS = (TIME_KEY, MAX_KEY, MIN_KEY)

#: Read once per contact: the coordinates to forecast for, plus the previously
#: stored payload that decides created / updated / skipped.
PARTNER_FIELDS = ["id", "name", "partner_latitude", "partner_longitude", "x_forecast_payload"]

#: Contacts worth forecasting: anything the JSONPlaceholder import geocoded.
#: Odoo stores an unset coordinate as 0.0, so 0/0 means "not geolocated".
GEOLOCATED_DOMAIN: List[Any] = [
    "|", ["partner_latitude", "!=", 0], ["partner_longitude", "!=", 0]
]


def _coordinate(value: Any) -> float:
    """Coerce an Odoo float, which arrives as ``False`` when it was never set."""
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


class OpenMeteoConnector(BaseConnector):
    """Stores a 7-day forecast and a display-ready next-day line on each contact."""

    provider = PROVIDER
    label = "Open-Meteo"

    # -- template method ----------------------------------------------------

    def sync(self, result: SyncResult) -> None:
        partners = self._select_partners(result)
        if not partners:
            # count=0 keeps ``processed`` at zero, which is what makes the run
            # report as "skipped" rather than as a success that did nothing.
            result.record_skipped(
                count=0,
                reason=(
                    "No contact carries coordinates, so there was nothing to forecast. "
                    "Run the JSONPlaceholder sync first: it imports the contacts together "
                    "with the latitude/longitude this connector selects on, or name "
                    "contacts explicitly in OPEN_METEO_PARTNER_IDS."
                ),
            )
            return

        breakdown: Dict[int, Dict[str, Any]] = {}
        result.details["partners"] = breakdown

        for partner in partners:
            partner_id = int(partner.get("id") or 0)
            with self.guard(result, external_id=f"res.partner#{partner_id}", label="forecast"):
                breakdown[partner_id] = self._sync_partner(result, partner)

    # -- contact selection ---------------------------------------------------

    def _select_partners(self, result: SyncResult) -> List[Dict[str, Any]]:
        """Resolve the contacts to forecast, an explicit id list winning.

        A read failure here is left to propagate: without contacts there is
        nothing for the run to do, so it is run-fatal rather than per-record.
        """
        configured = [int(pid) for pid in self.settings.open_meteo.partner_ids]
        if configured:
            partners = self.odoo.search_read_all(
                "res.partner",
                [["id", "in", configured]],
                fields=PARTNER_FIELDS,
                order="id asc",
            )
            found = {int(row.get("id") or 0) for row in partners}
            missing = [pid for pid in configured if pid not in found]
            if missing:
                result.add_note(
                    "OPEN_METEO_PARTNER_IDS names contacts that do not exist or are not "
                    f"readable with this API key: {missing}."
                )
        else:
            partners = self.odoo.search_read_all(
                "res.partner",
                GEOLOCATED_DOMAIN,
                fields=PARTNER_FIELDS,
                order="id asc",
            )
        return self.limited(partners, result, "partners")

    # -- one contact ---------------------------------------------------------

    def _sync_partner(self, result: SyncResult, partner: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch, compare and store one contact's forecast."""
        partner_id = int(partner.get("id") or 0)
        latitude = _coordinate(partner.get("partner_latitude"))
        longitude = _coordinate(partner.get("partner_longitude"))
        self._check_coordinates(partner_id, latitude, longitude)
        if latitude == 0.0 and longitude == 0.0:
            # Only reachable through OPEN_METEO_PARTNER_IDS; the geolocated domain
            # excludes 0/0. Open-Meteo answers for it, so name it rather than fail.
            result.add_note(
                f"res.partner#{partner_id} has no coordinates (0, 0); the forecast stored "
                "for it is the one for latitude 0 / longitude 0."
            )

        days = self._fetch_days(result, partner_id, latitude, longitude)
        payload = json.dumps(days, sort_keys=True, separators=(",", ":"))
        stored = partner.get("x_forecast_payload") or ""

        if stored == payload:
            result.record_skipped()
            return {"days": len(days), "action": "skipped"}

        self._write(result, partner_id, self._forecast_vals(result, partner_id, days, payload))
        # Counted only once the write survived, so a failed write stays a failure.
        if stored:
            result.record_updated()
            return {"days": len(days), "action": "updated"}
        result.record_created()
        return {"days": len(days), "action": "created"}

    @staticmethod
    def _check_coordinates(partner_id: int, latitude: float, longitude: float) -> None:
        """Reject an impossible coordinate locally rather than spending a request on it."""
        if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
            raise RecordSyncError(
                f"res.partner#{partner_id} has coordinates outside the valid range "
                f"(latitude {latitude}, longitude {longitude}); Open-Meteo would reject them.",
                f"res.partner#{partner_id}",
            )

    # -- upstream ------------------------------------------------------------

    def _fetch_days(
        self,
        result: SyncResult,
        partner_id: int,
        latitude: float,
        longitude: float,
    ) -> List[Dict[str, Any]]:
        """Return the validated, truncated ``[{date, temp_max, temp_min}, ...]`` series."""
        config = self.settings.open_meteo
        response = self.http.get(
            config.forecast_url,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "daily": DAILY_PARAM,
                "timezone": TIMEZONE_PARAM,
            },
        )
        if response.is_empty or not isinstance(response.data, dict):
            raise InvalidPayloadError(
                f"res.partner#{partner_id}: GET {sanitize(config.forecast_url)} did not return "
                f"a forecast object ({truncate(response.data, 120)})."
            )

        times, maxes, mins = self._validate_daily(response.data.get("daily"), partner_id)
        available = len(times)
        if available == 0:
            raise InvalidPayloadError(f"res.partner#{partner_id}: 'daily' carried no days.")

        # A limit of 0 is read the way SYNC_SAMPLE_LIMIT reads it: no cap.
        wanted = config.forecast_days
        kept = available if wanted <= 0 else min(wanted, available)
        if wanted > 0 and available < wanted:
            result.add_note(
                f"res.partner#{partner_id}: Open-Meteo returned {available} day(s) but "
                f"OPEN_METEO_FORECAST_DAYS is {wanted}; the shorter series was stored."
            )

        return [
            {
                "date": self._date(times[index], partner_id, index),
                "temp_max": self._temperature(maxes[index], MAX_KEY, partner_id, index),
                "temp_min": self._temperature(mins[index], MIN_KEY, partner_id, index),
            }
            for index in range(kept)
        ]

    @staticmethod
    def _validate_daily(daily: Any, partner_id: int) -> Tuple[List[Any], List[Any], List[Any]]:
        """Check that ``daily`` is three parallel lists before anything is zipped."""
        where = f"res.partner#{partner_id}"
        if not isinstance(daily, dict):
            raise InvalidPayloadError(
                f"{where}: 'daily' is {type(daily).__name__}, expected an object."
            )

        columns: List[List[Any]] = []
        for key in DAILY_KEYS:
            if key not in daily:
                raise InvalidPayloadError(f"{where}: 'daily' is missing '{key}'.")
            column = daily[key]
            if not isinstance(column, list):
                raise InvalidPayloadError(
                    f"{where}: 'daily.{key}' is {type(column).__name__}, expected a list."
                )
            columns.append(column)

        if len({len(column) for column in columns}) != 1:
            lengths = ", ".join(f"{key}={len(col)}" for key, col in zip(DAILY_KEYS, columns))
            raise InvalidPayloadError(
                f"{where}: 'daily' columns have mismatched lengths ({lengths}); the series "
                "cannot be zipped without pairing a date with the wrong reading."
            )
        return columns[0], columns[1], columns[2]

    @staticmethod
    def _date(value: Any, partner_id: int, index: int) -> str:
        text = "" if value is None else str(value).strip()
        if not text:
            raise InvalidPayloadError(
                f"res.partner#{partner_id}: 'daily.{TIME_KEY}[{index}]' is empty; "
                "the day cannot be dated."
            )
        return text

    @staticmethod
    def _temperature(value: Any, key: str, partner_id: int, index: int) -> float:
        if value is None:
            raise InvalidPayloadError(
                f"res.partner#{partner_id}: 'daily.{key}[{index}]' is null; a gap in the "
                "series would store a forecast that reads as complete but is not."
            )
        try:
            return float(value)
        except (TypeError, ValueError):
            raise InvalidPayloadError(
                f"res.partner#{partner_id}: 'daily.{key}[{index}]' is not a number "
                f"({truncate(value, 40)})."
            ) from None

    # -- Odoo side -----------------------------------------------------------

    def _forecast_vals(
        self,
        result: SyncResult,
        partner_id: int,
        days: List[Dict[str, Any]],
        payload: str,
    ) -> Dict[str, Any]:
        """Map the series onto this connector's own columns - and only those."""
        next_day = self._next_day(result, partner_id, days)
        return {
            "x_forecast_payload": payload,
            "x_forecast_updated_at": to_odoo_datetime(utcnow()),
            "x_forecast_next_date": next_day["date"],
            "x_forecast_next_temp_max": next_day["temp_max"],
            "x_forecast_next_temp_min": next_day["temp_min"],
            "x_forecast_next_summary": (
                f"{next_day['date']}: max {next_day['temp_max']:.1f} C "
                f"/ min {next_day['temp_min']:.1f} C"
            ),
        }

    @staticmethod
    def _next_day(
        result: SyncResult, partner_id: int, days: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """The day after the first returned one, i.e. index 1 while the series starts today."""
        if len(days) > 1:
            return days[1]
        result.add_note(
            f"res.partner#{partner_id}: Open-Meteo returned a single day, so the next-day "
            "fields show that day rather than tomorrow."
        )
        return days[0]

    def _write(self, result: SyncResult, partner_id: int, vals: Dict[str, Any]) -> None:
        """Apply the forecast, or describe it when the run may not write.

        The dry run is short-circuited here rather than left to ``OdooClient``,
        whose own dry-run branch logs but records nothing on the result.
        """
        if self.dry_run:
            result.add_mock_write("WRITE", f"odoo://res.partner/{partner_id}", vals)
            return
        try:
            self.odoo.write("res.partner", [partner_id], vals)
        except OdooError as exc:
            raise RecordSyncError(
                f"write on res.partner#{partner_id} failed: {sanitize(exc)}",
                f"res.partner#{partner_id}",
            ) from None


__all__ = ["OpenMeteoConnector"]
