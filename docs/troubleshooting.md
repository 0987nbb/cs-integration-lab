# Troubleshooting

## Quick reference

| Symptom | Cause | Fix |
|---|---|---|
| `Startup failed: Missing required Odoo settings` | `.env` absent or incomplete | `cp .env.example .env`, set `ODOO_URL`, `ODOO_DATABASE`, `ODOO_API_KEY` |
| Sync log says `-> logs/sync_log.jsonl` | No access rule on `x_integration_sync_log` | [§1](#1-http-403-on-the-custom-models) |
| `You are not allowed to access ... No group currently allows this operation` | Same as above | [§1](#1-http-403-on-the-custom-models) |
| `the model 'ir.model.access' does not exist` (404) | Security models are not on the JSON-2 route | [§1](#1-http-403-on-the-custom-models) — not fixable from code |
| `call_kw Odoo error: user is not connected` | `/web/dataset/call_kw` needs a session cookie | [§2](#2-user-is-not-connected) |
| JSONPlaceholder run takes ~60 s before working | Primary host unreachable, fallback used | [§3](#3-apijsonplaceholderdev-times-out) |
| Nager.Date run reports `skipped`, nothing imported | Provider has no data for that country | [§4](#4-nagerdate-returns-204-no-content) |
| `API rate limit exceeded ... 60` | Unauthenticated GitHub | [§5](#5-github-rate-limit-exceeded) |
| `Only one currency rate per day allowed!` (422) | Concurrent run stored the rate | [§6](#6-only-one-currency-rate-per-day-allowed) |
| A currency rate did not update | Move exceeded 5%, awaiting approval | [§7](#7-a-rate-was-withheld-for-approval) |
| Open-Meteo reports `skipped`, no contacts | No contact has coordinates | [§8](#8-open-meteo-found-no-contacts) |
| `'res.currency' object has no attribute 'message_subscribe'` (500) | Activity anchored on a model without `mail.thread` | [§9](#9-mailactivity-cannot-attach-to-rescurrency) |
| `x_forecast_* is not a valid field` | Fields not provisioned | `python -m integration_service.cli --provision` |
| Tests fail collecting `cs_integration_lab/` | Odoo addon tests need an Odoo runtime | Already excluded by `pytest.ini`; run pytest from the repo root |

---

## 1. HTTP 403 on the custom models

**Symptom**

```
WARNING integration_service.sync_log: Cannot write to x_integration_sync_log
(no access rule grants this API key access to it). Falling back to logs/sync_log.jsonl.
```

`--check` shows:

```json
"x_integration_config": "no access rule (HTTP 403)",
"x_integration_sync_log": "no access rule (HTTP 403)"
```

**Cause.** `x_integration_config` and `x_integration_sync_log` are *manual*
models. Odoo grants no implicit access to a manual model — an `ir.model.access`
row must exist. None does on this database.

**Why it cannot be automated.** `ir.model.access` and `ir.rule` are not exposed
on the JSON-2 route. Requesting them returns:

```
Odoo API error (HTTP 404): the model 'ir.model.access' does not exist
```

`ir.model` and `ir.model.fields` *are* reachable (which is how custom fields are
provisioned), but the two security models are not. There is no API path to
creating the rule, so it is a one-time manual step.

**Fix — in the Odoo web UI**

1. Enable developer mode: **Settings** → scroll to the bottom → **Developer
   Tools** → **Activate the developer mode**.
2. Go to **Settings** → **Technical** → **Security** → **Access Rights**.
3. **New**, then for each of the two models:

   | Field | Value |
   |---|---|
   | Name | `x_integration_sync_log.full` |
   | Object | `Integration Sync Log (x_integration_sync_log)` |
   | Group | *(leave empty for all users, or pick e.g. Administration / Settings)* |
   | Read / Write / Create / Delete | all checked |

4. Repeat with Object = `Integration Configuration (x_integration_config)`.
5. Verify:

   ```bash
   python -m integration_service.cli --check
   ```

   Both entries should now read `"ok"`.

Nothing else changes: the next run writes its record into
`x_integration_sync_log` instead of the file, with no code change.

**Meanwhile.** The audit trail is not lost. `logs/sync_log.jsonl` holds one JSON
object per run with the same fields:

```bash
python -c "import json;[print(json.loads(l)['provider'], json.loads(l)['status'], json.loads(l)['created']) for l in open('logs/sync_log.jsonl')]"
```

---

## 2. `user is not connected`

**Symptom**

```
call_kw Odoo error in res.partner.search_read: user is not connected
```

**Cause.** `/web/dataset/call_kw` authenticates with a session cookie from
`/web/session/authenticate`. An API key in an `Authorization: Bearer` header is
not a session, so the route rejects it regardless of the key's validity.

**Fix.** Use the JSON-2 methods on `OdooClient` (`search_read`, `create`,
`write`, `unlink`, `read`, `search_count`, `fields_get`). `OdooClient.call_kw`
deliberately raises with this explanation rather than appearing to work.

---

## 3. `api.jsonplaceholder.dev` times out

**Symptom**

```
WARNING integration_service.http: Retrying GET https://api.jsonplaceholder.dev/users
in 0.41s (attempt 1/2): ... timed out after 10.0s
```

followed by a successful run and a note naming both hosts.

**Cause.** The host named in the assessment does not resolve from most networks.

**Fix.** None needed — the connector probes the primary once, falls back to
`JSONPLACEHOLDER_FALLBACK_BASE_URL`, and records the substitution on the run.

To skip the ~30 s probe entirely, point the primary at the working host:

```
JSONPLACEHOLDER_BASE_URL=https://jsonplaceholder.typicode.com
```

To fail loudly instead of falling back, set
`JSONPLACEHOLDER_FALLBACK_BASE_URL=` (empty).

---

## 4. Nager.Date returns 204 No Content

**Symptom**

```
nager_date  skipped  created=0 updated=0 skipped=0 failed=0
note: Nager.Date has no holiday data for PK in 2026 (HTTP 204, empty body)
```

**Cause.** Nager.Date does not cover every country. Pakistan is one it has no
dataset for, and it signals that with 204 rather than 404 or an empty array.

**This is correct behaviour, not a failure.** The counters stay at zero because
they count records and this country yielded none.

**Fix.** Use a covered country:

```
NAGER_COUNTRIES=US,DE,GB,FR
```

The full list is at `https://date.nager.at/api/v3/AvailableCountries`.

---

## 5. GitHub rate limit exceeded

**Symptom**

```
github sync aborted: GitHub repository octocat/Hello-World is not readable:
GET .../issues failed with HTTP 403: {"message":"API rate limit exceeded for 1.2.3.4.
(But here's the good news: Authenticated requests get a higher rate limit...)"}
```

**Cause.** Unauthenticated GitHub allows **60 requests per hour per IP**. A large
repository burns it quickly — `odoo/odoo` is 44 pages of issues on its own.

**Check what is left and when it resets**

```bash
python -c "import requests,time;r=requests.get('https://api.github.com/rate_limit').json()['rate'];print(r['remaining'],'left, resets in',round((r['reset']-time.time())/60,1),'min')"
```

**Fixes**

1. **Set a token** — raises the limit to 5000/hour and enables outbound writes:

   ```
   GITHUB_TOKEN=ghp_...
   ```

2. **Keep the sample limit low.** `SYNC_SAMPLE_LIMIT=10` bounds the *fetch*, not
   just the result: pagination stops once ten issues are in hand and `per_page`
   is capped to the limit, so one request suffices. A note records the stop:

   ```
   Stopped paginating after 10 issue(s): SYNC_SAMPLE_LIMIT is 10.
   ```

3. **Turn off comment fetching** — `GITHUB_SYNC_COMMENTS=false` saves one request
   per issue.

4. **Point at a smaller repository** via `GITHUB_OWNER` / `GITHUB_REPO`.

A 429 (rather than this 403) is handled automatically: `Retry-After`, or
`X-RateLimit-Reset` when absent, is honoured up to 120 seconds.

---

## 6. `Only one currency rate per day allowed!`

**Symptom** HTTP 422 from `res.currency.rate.create`.

**Cause.** A database constraint: one rate per currency per company per day.

**This is handled.** The connector searches for an existing row first
(currency + date + company) and updates it. If the constraint still fires,
another run stored the rate between the search and the create; the error is
downgraded to a **skip** with the note *"was stored concurrently by another run"*.

If you see it as a hard failure, the message did not match the expected pattern —
check `_is_duplicate_rate` in `frankfurter_connector.py`.

---

## 7. A rate was withheld for approval

**Symptom**

```
frankfurter  success  created=2 updated=0 skipped=2 failed=0
note: EUR moved 10.00% (threshold 5.00%) from 0.002827 to 0.003110 on 2026-08-05;
      the rate was not written and awaits approval.
```

**Cause.** Working as designed: a move larger than
`FRANKFURTER_RATE_CHANGE_THRESHOLD` (default 0.05) is not applied automatically.

**Where to find the approval task.** A `mail.activity` is created for the
approver. On this database `res.currency` cannot hold activities (§9), so it is
anchored on the approver's **user** record — it appears in their **Activities**
list in the systray. Find it via the API:

```bash
python -c "
import sys; sys.path.insert(0,'.')
from integration_service.odoo_client import OdooClient
c=OdooClient()
print(c.search_read('mail.activity',[['summary','like','rate change']],
      fields=['id','summary','res_model','res_id','user_id','date_deadline']))"
```

**To apply the rate** enter it manually in **Accounting → Configuration →
Currencies → <currency> → Rates**, then mark the activity done. The next run sees
the stored rate as the new baseline.

**To change the sensitivity** set `FRANKFURTER_RATE_CHANGE_THRESHOLD` (e.g. `0.10`
for 10%). Setting it very high effectively disables the gate.

The activity is created once per currency and date — re-running does not stack
duplicates.

---

## 8. Open-Meteo found no contacts

**Symptom**

```
open_meteo  skipped  created=0 updated=0 skipped=0 failed=0
note: No contact carries coordinates, so there was nothing to forecast.
      Run the JSONPlaceholder sync first...
```

**Cause.** The connector selects contacts with a non-zero
`partner_latitude` / `partner_longitude`. Those come from the JSONPlaceholder
import.

**Fixes**

```bash
python -m integration_service.cli --provider jsonplaceholder   # then re-run open_meteo
```

or name contacts explicitly:

```
OPEN_METEO_PARTNER_IDS=12,13,14
```

(contacts named here are forecast even without coordinates — 0/0 is a valid point
in the Atlantic, and the run notes it).

---

## 9. `mail.activity` cannot attach to `res.currency`

**Symptom**

```
HTTP 500: {"name":"builtins.AttributeError",
"message":"'res.currency' object has no attribute 'message_subscribe'"}
```

**Cause.** `mail.activity` requires its target model to inherit
`mail.activity.mixin`. `res.currency` does not. Worse, Odoo reports it as a
**500**, which the reliability layer treats as retryable — so a naive
implementation burns its whole retry budget on every withheld rate.

**This is handled.** The Frankfurter connector resolves the anchor by querying
`ir.model.fields` for an `activity_ids` field, preferring `res.currency`, then
`res.users`, then `res.partner`. On this database that selects `res.users`.

To confirm which models support activities:

```bash
python -c "
import sys; sys.path.insert(0,'.')
from integration_service.odoo_client import OdooClient
c=OdooClient()
print(sorted(r['model'] for r in c.search_read('ir.model.fields',
      [['name','=','activity_ids']], fields=['model'])))"
```

---

## 10. Reading a run's outcome

Every run produces a record whether or not Odoo logging works.

```bash
python -m integration_service.cli --provider all --json > run.json
```

Each entry carries `provider`, `status`, `start_time`, `end_time`,
`duration_seconds`, the four counters, `errors`, `notes`, `mock_writes` and a
per-entity `details` breakdown.

Interpreting `status`:

| Status | Meaning |
|---|---|
| `success` | Everything attempted succeeded (skips included) |
| `partial` | Some records synced, at least one failed — check `errors` |
| `failed` | The run aborted, or every attempted record failed |
| `skipped` | Nothing to examine (no country configured, no geolocated contact, HTTP 204) |

`failed=0` alongside `status: failed` means the run aborted before reaching any
individual record — the reason is the first entry in `errors`.

---

## 11. Secrets

`sanitize.py` redacts registered secrets and structural patterns. To verify:

```bash
python -c "
from integration_service.sanitize import register_secret, sanitize
register_secret('ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345')
print(sanitize('Authorization: Bearer ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 ?token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345'))"
```

Expected: no token characters in the output.

If a credential ever reaches a log, it was not registered and did not match a
pattern — add it via `register_secret` in `config.load_settings`.

`.env` is git-ignored. Confirm nothing is staged:

```bash
git check-ignore -v .env
git status --short
```
