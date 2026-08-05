# Data mapping

Every table below was read off the connector source and confirmed against records
written into the live `ai-demo-company` database.

## Conventions

* **External id** — `x_external_id`, the idempotency key. Format
  `<provider>:<entity>:<id>`; ids, dates and ISO codes are kept verbatim, free
  text is slugified.
* **Hashed** — what goes into `x_source_hash`. Only the *mapped Odoo values* are
  hashed, minus the three bookkeeping columns. Anything not listed is invisible
  to change detection.
* **Odoo datetimes** are naive UTC, `YYYY-MM-DD HH:MM:SS`. Odoo dates are `YYYY-MM-DD`.

## Odoo required fields (verified against the live instance)

| Model | Required | Notes |
|---|---|---|
| `project.task` | `name`, `state` | `state` ∈ `01_in_progress`, `02_changes_requested`, `03_approved`, `1_done`, `1_canceled`, `04_waiting_normal` |
| `helpdesk.ticket` | `name`, `kanban_state` | `kanban_state` ∈ `normal`, `done`, `blocked` |
| `calendar.event` | `name`, `show_as`, `start`, `stop` | `show_as` ∈ `free`, `busy`; `start`/`stop` are datetimes even when `allday` is set |
| `res.currency.rate` | `name` (a date), `currency_id` | DB constraint: one rate per currency per day (HTTP 422) |
| `mail.activity` | `date_deadline` | Anchor model must carry `mail.activity.mixin` |
| `res.partner` | — | |

---

## 1. GitHub REST → `project.task`

**Endpoints**

| Verb | URL | Use |
|---|---|---|
| GET | `https://api.github.com/repos/{owner}/{repo}/issues?state=all&per_page=N` | Inbound listing, `Link: rel="next"` pagination |
| GET | `{issue.comments_url}` | Comment bodies, when `GITHUB_SYNC_COMMENTS=true` |
| POST | `https://api.github.com/repos/{owner}/{repo}/issues` | Outbound create |
| PATCH | `https://api.github.com/repos/{owner}/{repo}/issues/{number}` | Outbound update |

**External id** `github:issue:<issue.id>` — e.g. `github:issue:5072873882`.
Keyed on the immutable `id`, not `number`, so a transferred issue still matches.

**Inbound mapping**

| GitHub | `project.task` | Notes |
|---|---|---|
| `number`, `title` | `name` | `"#{number} {title}"` |
| `body`, comments | `description` | HTML-escaped; comments rendered as a "Comments (N)" block |
| `state` | `state` | `open → 01_in_progress`, `closed → 1_done` |
| `labels[].name` | `tag_ids` | `project.tags` found or created; written as `[[6, 0, ids]]` |
| `updated_at` | `x_external_updated_at` | Parsed from ISO-8601 `Z` |
| — | `project_id` | **create-only**, so a later manual move in Odoo is not undone |
| `id` | `x_external_id` | |

**Hashed** `name`, `description`, `state`, `tag_ids`, plus comment bodies folded
in via `extra_hash_input` — a new comment alone therefore triggers an update.

**Rules**

* An item carrying a `pull_request` key is a PR and is **skipped**, not imported.
* A comments fetch that fails does not fail the issue: it is noted and the issue
  syncs without them.
* Pagination stops once `SYNC_SAMPLE_LIMIT` issues are collected.
* Outbound is only sent when `GITHUB_TOKEN` is set and `DRY_RUN` is false;
  otherwise the payload is recorded as `[MOCK WRITE]`.
* **Withheld writes are counted as `skipped`, never as `created`/`updated`.**
  Nothing reached GitHub, so counting them would report work that never happened.
  `details["outbound"]` still reports `{"updates": n, "creates": n, "sent": false}`
  — intent and effect are deliberately separate. A withheld create also leaves the
  task's `x_external_id` unset, so it remains a candidate once a token is added.
* An issue whose inbound outcome was **not** `skipped` is never pushed back — its
  `write_date` is our own import, and echoing it would overwrite GitHub with what
  we just read.

---

## 2. JSONPlaceholder → `res.partner` and `helpdesk.ticket`

**Endpoints**

| Verb | URL | Use |
|---|---|---|
| GET | `{base}/users` | → `res.partner` |
| GET | `{base}/posts` | → `helpdesk.ticket` |
| POST | `{base}/posts` | Outbound demonstration (mock) |
| PATCH | `{base}/posts/{id}` | Outbound demonstration (mock) |

`{base}` is `JSONPLACEHOLDER_BASE_URL` (`https://api.jsonplaceholder.dev`, the host
named in the brief). It does not resolve from this network, so the connector
probes it once and falls back to `JSONPLACEHOLDER_FALLBACK_BASE_URL`
(`https://jsonplaceholder.typicode.com`), recording a note naming both hosts.

**Users → `res.partner`** — external id `jsonplaceholder:user:<id>`

| JSONPlaceholder | `res.partner` |
|---|---|
| `name` | `name` |
| `email` | `email` |
| `phone` | `phone` |
| `website` | `website` |
| `address.street` + `address.suite` | `street` |
| `address.city` | `city` |
| `address.zipcode` | `zip` |
| `address.geo.lat` | `partner_latitude` (string → float) |
| `address.geo.lng` | `partner_longitude` (string → float) |
| `username`, `company.*` | `comment` (HTML) |

A non-numeric coordinate defaults to `0.0` with a note rather than failing the
record. These coordinates are what the Open-Meteo connector later selects on.

**Posts → `helpdesk.ticket`** — external id `jsonplaceholder:post:<id>`

| JSONPlaceholder | `helpdesk.ticket` |
|---|---|
| `title` | `name` |
| `body` | `description` (HTML-escaped) |
| `userId` | `partner_id` — resolved through `jsonplaceholder:user:{userId}` |
| — | `team_id` = first `helpdesk.team` (live: `Customer Care`, id 1) |
| — | `kanban_state` = `normal` |

If `helpdesk.ticket` is unavailable the connector falls back to `project.task`
(`state = 01_in_progress`) and records a note. On this database Helpdesk **is**
installed, so tickets are used.

**Mock writes.** JSONPlaceholder echoes a fabricated id and persists nothing, so
POST and PATCH are genuinely simulated. They are sent for real (proving the verbs
work end to end) and then recorded as `[MOCK WRITE]` with the echoed id. They do
not move the created/updated counters and cannot fail the run. Live example:

```
[MOCK WRITE] POST https://jsonplaceholder.typicode.com/posts payload={'from': 'helpdesk.ticket#11', ..., 'echoed_id': 101}
[MOCK WRITE] PATCH https://jsonplaceholder.typicode.com/posts/1 payload={..., 'echoed_id': 1}
```

---

## 3. Frankfurter v2 → `res.currency.rate`

**Endpoint**
`GET https://api.frankfurter.dev/v2/rates?base=USD&quotes=EUR,GBP,TRY,PKR`

Returns a **list**, one object per quote:

```json
[{"date": "2026-08-05", "base": "USD", "quote": "EUR", "rate": 0.8663}, ...]
```

The older v1 envelope (`{"date": ..., "rates": {...}}`) is still accepted.

**External id** `frankfurter:rate:<CODE>:<YYYY-MM-DD>` (used for error attribution;
`res.currency.rate` has no custom columns — currency + date + company is the key).

### The conversion

Odoo's `res.currency.rate.rate` means **units of that currency per 1 unit of the
company currency**. The company currency here is **PKR**, but the API base is
**USD**, so the raw API rate is not the Odoo rate:

```
odoo_rate(Q) = api_rate(USD → Q) / api_rate(USD → PKR)
```

Worked from the live run of 2026-08-05 (`USD→EUR 0.8663`, `USD→PKR 278.58`):

```
odoo_rate(EUR) = 0.8663 / 278.58 = 0.003109699189
```

which is exactly what is stored in `res.currency.rate` id 3. All arithmetic uses
`decimal.Decimal` built from `str(value)`, quantised to 12 decimal places with
`ROUND_HALF_UP`; the value becomes a `float` only at the moment of writing.

If the company currency equals the API base the divisor is 1. If it is neither
the base nor among the returned quotes, the run is fatal with a message naming
the missing currency — there is no divisor.

**Mapping**

| Source | `res.currency.rate` |
|---|---|
| `date` | `name` |
| resolved `res.currency` id | `currency_id` |
| main company | `company_id` |
| converted value | `rate` (float, 12 dp) |

**Rules**

* PKR itself is **skipped** — a currency's rate against itself is 1 by definition.
* Inactive currencies (EUR, GBP, TRY ship archived) are looked up with
  `active_test: False` and activated before their rate is written.
* **Duplicate protection:** search on `currency_id + name + company_id` first —
  absent → create, equal within 1e-12 → skip, different → update. If the DB
  constraint fires anyway (HTTP 422, *"Only one currency rate per day allowed!"*),
  it is downgraded to a skip: another run won the race.
* **The >5% rule.** The most recent rate strictly before this date is loaded. If
  `abs(new − prior) / prior > FRANKFURTER_RATE_CHANGE_THRESHOLD` (default 0.05),
  **no rate is written**. A `mail.activity` is created instead:

  | Field | Value |
  |---|---|
  | `res_model` / `res_id` | first anchor supporting activities — `res.users` on this database |
  | `activity_type_id` | `To-Do` (live id 4), else the lowest-id type |
  | `user_id` | `FRANKFURTER_APPROVER_LOGIN`, else the lowest-id internal user |
  | `date_deadline` | the rate date |
  | `summary` | `Approve EUR rate change of 10.00% on 2026-08-05` |
  | `note` | prior rate, proposed rate, percent change, threshold, source URL |

  The outcome is **skipped**, not failed. Creating the same activity twice is
  prevented by searching on anchor + summary first.

  `res.currency` does **not** carry `mail.activity.mixin` on this instance —
  attaching there returns HTTP 500 (`'res.currency' object has no attribute
  'message_subscribe'`). The anchor is therefore chosen by checking
  `ir.model.fields` for `activity_ids`, preferring `res.currency`, then
  `res.users`, then `res.partner`.

---

## 4. Open-Meteo → `res.partner` forecast columns

**Endpoint**

```
GET https://api.open-meteo.com/v1/forecast
    ?latitude={lat}&longitude={lon}
    &daily=temperature_2m_max,temperature_2m_min
    &timezone=auto
```

Response is **columnar** — three parallel lists:

```json
{"daily": {"time": ["2026-08-05", ...], "temperature_2m_max": [34.6, ...], "temperature_2m_min": [26.3, ...]}}
```

**Contact selection.** `OPEN_METEO_PARTNER_IDS` when set; otherwise contacts with
coordinates, domain `["|", ["partner_latitude","!=",0], ["partner_longitude","!=",0]]`
— i.e. exactly what the JSONPlaceholder import geocoded. With none, the run is
`skipped` with a note pointing at that connector.

**Mapping** (all six columns provisioned by `--provision`)

| Source | `res.partner` |
|---|---|
| all 7 days | `x_forecast_payload` — `[{"date","temp_max","temp_min"}, ...]` as canonical JSON |
| run time | `x_forecast_updated_at` |
| `daily.time[1]` | `x_forecast_next_date` |
| `daily.temperature_2m_max[1]` | `x_forecast_next_temp_max` |
| `daily.temperature_2m_min[1]` | `x_forecast_next_temp_min` |
| derived | `x_forecast_next_summary` — `"2026-08-06: max 18.0 C / min 17.2 C"` |

Index 1 is the **next** day because `timezone=auto` starts the series on the
contact's local today. A single-day series falls back to index 0 with a note.

Live example (`res.partner` id 16, Chelsey Dietrich):

```
x_forecast_next_summary = "2026-08-06: max 18.0 C / min 17.2 C"
x_forecast_payload      = [{"date":"2026-08-05","temp_max":17.8,"temp_min":17.3},
                           {"date":"2026-08-06","temp_max":18.0,"temp_min":17.2}, ...]
```

**Idempotency without a hash.** This connector deliberately does **not** use
`Upserter` and never writes `x_external_id`, `x_source_hash` or
`x_external_updated_at` — those belong to JSONPlaceholder, and sharing them would
put the two connectors in a mutual update loop. Instead the newly serialised
payload is compared byte-for-byte against the stored one: identical → skipped
with **no write issued at all**; different → updated. Canonical serialisation
(`sort_keys=True, separators=(",", ":")`) is what makes that comparison sound.

**Validation.** `daily` must be an object holding three lists of equal length
with no null temperature; anything else raises inside the per-contact guard, so
one bad response costs one contact. Coordinates outside ±90/±180 fail locally
without spending a request.

---

## 5. Nager.Date → `calendar.event`

**Endpoint** `GET https://date.nager.at/api/v4/Holidays/{CountryCode}/{Year}`

```json
[{"date": "2026-01-01", "name": "New Year's Day", "countryCode": "US",
  "nationalHoliday": true, "subdivisionCodes": null, "holidayTypes": ["Public", "Bank"]}]
```

v4 has no `localName` field. A country the provider does not cover (PK) answers
**HTTP 204 No Content**.

**External id**
`nager:holiday:<COUNTRY>:<YYYY-MM-DD>:<slugified-name>` — e.g.
`nager:holiday:US:2026-01-01:new-year-s-day`. This is both the duplicate
protection and the mapping back to the upstream entry. The name is slugified
explicitly so the key is case-insensitive.

**Mapping**

| Source | `calendar.event` |
|---|---|
| `name`, `countryCode` | `name` — `"New Year's Day (US)"` |
| `date` | `start` = `"{date} 00:00:00"`, `stop` = `"{date} 23:59:59"` |
| — | `allday` = `True` |
| — | `show_as` = `free` (an imported holiday should not mark everyone busy) |
| `holidayTypes`, `nationalHoliday`, `subdivisionCodes` | `description` (HTML list) |

Odoo derives `start_date` / `stop_date` from the datetimes when `allday` is set —
verified live.

**Hashed** `name`, `start`, `stop`, `allday`, `show_as`, `description`. The feed
carries no per-record timestamp, so `x_external_updated_at` is left unset and the
hash is the only change detector — which is why the classification fields are
rendered into the description rather than dropped.

**Rules**

* HTTP 204 → `record_skipped(count=0)` with a note naming the country. Not a
  failure, not an invalid payload. `count=0` because the counters count records
  and this country yielded none.
* A row missing `date` or `name`, or with an unparseable date, is one failed
  record; the rest of the country still imports.
* A body that is neither empty nor a list is an invalid payload for that country;
  other countries continue.
* Duplicate country codes are de-duplicated so a year is fetched once.
