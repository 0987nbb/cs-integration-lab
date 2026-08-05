# API usage

## Prerequisites

* Python 3.10+ (developed on 3.13)
* An Odoo 19 database and an API key for a user allowed to read/write the target models
* Network access to `api.github.com`, `jsonplaceholder.typicode.com`,
  `api.frankfurter.dev`, `api.open-meteo.com`, `date.nager.at`

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows
# source .venv/bin/activate       # macOS / Linux

pip install -r requirements.txt
cp .env.example .env               # then edit .env
```

Fill in the three required values; everything else has a working default:

```
ODOO_URL=https://ai-demo-company.odoo.com
ODOO_DATABASE=ai-demo-company
ODOO_API_KEY=<your key>
```

Generate the key in Odoo: avatar → **My Profile** → **Account Security** →
**New API Key**. `.env` is git-ignored; never commit it.

## Environment variables

### Odoo and transport

| Variable | Default | Meaning |
|---|---|---|
| `ODOO_URL` | — | **Required.** Instance base URL |
| `ODOO_DATABASE` | — | **Required.** Sent as `X-Odoo-Database` |
| `ODOO_API_KEY` | — | **Required.** Sent as `Authorization: Bearer` |
| `ODOO_TIMEOUT` | `30` | Seconds per Odoo request |
| `HTTP_TIMEOUT` | `30` | Seconds per outbound request |
| `HTTP_MAX_RETRIES` | `4` | Retries after the first attempt |
| `HTTP_BACKOFF_FACTOR` | `0.5` | Base of `factor * 2^(attempt-1)` |
| `HTTP_BACKOFF_MAX` | `30` | Backoff ceiling, seconds |
| `HTTP_USER_AGENT` | `cs-integration-lab/1.0` | Sent on every request |
| `SYNC_SAMPLE_LIMIT` | `10` | Max records per entity per run; `0` = no limit |
| `DRY_RUN` | `false` | When true nothing is written anywhere |
| `SYNC_LOG_FALLBACK_PATH` | `logs/sync_log.jsonl` | Used when Odoo logging is unavailable |

### Per connector

| Variable | Default | Meaning |
|---|---|---|
| `GITHUB_API_URL` | `https://api.github.com` | |
| `GITHUB_TOKEN` | *(empty)* | Empty ⇒ read-only; outbound writes become `[MOCK WRITE]` |
| `GITHUB_OWNER` / `GITHUB_REPO` | `octocat` / `Hello-World` | Target repository |
| `GITHUB_PROJECT_NAME` | `GitHub Issues` | Odoo project, created if missing |
| `GITHUB_SYNC_COMMENTS` | `true` | One extra request per issue |
| `GITHUB_PER_PAGE` | `100` | Capped to the sample limit when one is set |
| `JSONPLACEHOLDER_BASE_URL` | `https://api.jsonplaceholder.dev` | Host named in the brief |
| `JSONPLACEHOLDER_FALLBACK_BASE_URL` | `https://jsonplaceholder.typicode.com` | Used when the primary is unreachable |
| `JSONPLACEHOLDER_POST_MODEL` | `helpdesk.ticket` | Falls back to `project.task` if unavailable |
| `FRANKFURTER_BASE_URL` | `https://api.frankfurter.dev` | |
| `FRANKFURTER_BASE_CURRENCY` | `USD` | API base |
| `FRANKFURTER_QUOTES` | `EUR,GBP,TRY,PKR` | Company currency is added automatically |
| `FRANKFURTER_RATE_CHANGE_THRESHOLD` | `0.05` | Above this, approval is required |
| `FRANKFURTER_APPROVER_LOGIN` | *(empty)* | Defaults to the lowest-id internal user |
| `OPEN_METEO_BASE_URL` | `https://api.open-meteo.com` | |
| `OPEN_METEO_FORECAST_DAYS` | `7` | |
| `OPEN_METEO_PARTNER_IDS` | *(empty)* | Empty ⇒ auto-select geolocated contacts |
| `NAGER_BASE_URL` | `https://date.nager.at` | |
| `NAGER_COUNTRIES` | `US,DE` | ISO-3166-1 alpha-2. PK has no data (HTTP 204) |
| `NAGER_YEARS` | *(current year)* | Comma-separated |

### Scheduling

| Variable | Default | Meaning |
|---|---|---|
| `SCHEDULE_INTERVAL_MINUTES` | `60` | Global default |
| `SCHEDULE_GITHUB_MINUTES` | `15` | Per-provider override |
| `SCHEDULE_JSONPLACEHOLDER_MINUTES` | `360` | |
| `SCHEDULE_FRANKFURTER_MINUTES` | `720` | |
| `SCHEDULE_OPEN_METEO_MINUTES` | `180` | |
| `SCHEDULE_NAGER_DATE_MINUTES` | `1440` | |

## Commands

### Health check

```bash
python -m integration_service.cli --check
```

```json
{
  "odoo_url": "https://ai-demo-company.odoo.com",
  "database": "ai-demo-company",
  "dry_run": false,
  "sample_limit": 10,
  "github_write_enabled": false,
  "model_access": {
    "res.partner": "ok",
    "project.task": "ok",
    "helpdesk.ticket": "ok",
    "calendar.event": "ok",
    "res.currency": "ok",
    "res.currency.rate": "ok",
    "mail.activity": "ok",
    "x_integration_config": "no access rule (HTTP 403)",
    "x_integration_sync_log": "no access rule (HTTP 403)"
  }
}
```

### Provision custom fields (idempotent)

```bash
python -m integration_service.cli --provision
```

```json
{
  "partner_forecast_fields": {
    "created": ["x_forecast_updated_at", "x_forecast_payload", "x_forecast_next_date",
                "x_forecast_next_temp_max", "x_forecast_next_temp_min", "x_forecast_next_summary"],
    "existing": [], "failed": {}
  },
  "missing_idempotency_fields": {},
  "model_access": { "...": "..." }
}
```

Run it again and every field moves from `created` to `existing`.

### Prove Odoo JSON-2 CREATE / READ / UPDATE / DELETE

```bash
python -m integration_service.cli --proof
```

Creates a scratch `res.partner`, reads it back, updates it, verifies the stored
values changed, and deletes it. Add `--keep-proof-record` to leave it in place.
Real output:

```
[OK  ] 1. CREATE  POST https://ai-demo-company.odoo.com/json/2/res.partner/create
       request : {"vals_list": [{"name": "ZZ JSON-2 PROOF 2026-08-05 18:24:52", ...}]}
       response: [11]
[OK  ] 2. READ (after create)  POST .../res.partner/search_read
       note    : Verified x_source_hash match the value just written.
[OK  ] 3. UPDATE  POST .../res.partner/write
       request : {"ids": [11], "vals": {"email": "...", "x_source_hash": "updated"}}
       response: true
[OK  ] 4. READ (after update)  POST .../res.partner/search_read
       note    : Verified email, x_source_hash match the value just written.
[OK  ] 5. DELETE POST .../res.partner/unlink
RESULT: PASS - 5 step(s), verbs proven: CREATE, READ, UPDATE, DELETE
```

The `Authorization` header is never printed; the banner shows `Bearer [REDACTED]`.

### Sync Now

```bash
python -m integration_service.cli --provider all          # all five, in order
python -m integration_service.cli --provider github       # one connector
python -m integration_service.cli --provider all --dry-run
python -m integration_service.cli --provider nager_date --limit 0   # no cap
python -m integration_service.cli --provider all --json   # machine-readable
python -m integration_service.cli --provider github -v    # DEBUG logging
```

Providers run in a deliberate order: JSONPlaceholder imports the contacts (with
coordinates) that Open-Meteo then forecasts against.

```
==============================================================================
SYNC SUMMARY
==============================================================================
  github           success  created=10 updated=0 skipped=0 failed=0 (6.2s)
  jsonplaceholder  success  created=20 updated=0 skipped=0 failed=0 (42.1s)
  frankfurter      success  created=3 updated=0 skipped=1 failed=0 (5.0s)
  open_meteo       success  created=10 updated=0 skipped=0 failed=0 (6.7s)
  nager_date       success  created=19 updated=1 skipped=0 failed=0 (21.7s)
------------------------------------------------------------------------------
  TOTAL                     created=62 updated=1 skipped=1 failed=0
==============================================================================
```

`--no-sync-log` suppresses the run record.

### Scheduled sync

```bash
python -m integration_service.cli --show-schedule          # resolved plan, then exit
python -m integration_service.cli --schedule               # resident; Ctrl-C to stop
python -m integration_service.cli --schedule --once        # run what is due, exit
```

`--show-schedule`:

```json
[{"provider": "github", "enabled": true, "interval_minutes": 15,
  "next_run_at": "2026-08-05 18:23:26", "last_run_at": null,
  "last_status": null, "runs": 0}, ...]
```

**cron** (every 15 minutes, one-shot form):

```cron
*/15 * * * * cd /opt/cs-integration-lab && .venv/bin/python -m integration_service.cli --schedule --once >> logs/cron.log 2>&1
```

**Windows Task Scheduler**:

```powershell
schtasks /Create /SC MINUTE /MO 15 /TN "OdooIntegrationLab" ^
  /TR "\"D:\path\.venv\Scripts\python.exe\" -m integration_service.cli --schedule --once" ^
  /SD 01/01/2026
```

Run it from the repository root so `.env` and `logs/` resolve.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Every run succeeded (or was a legitimate no-op) |
| `1` | At least one run was **partial** |
| `2` | At least one run **failed**, or the JSON-2 proof failed |
| `3` | Startup failure (missing/invalid configuration) |

## What each connector sends

**GitHub**

```http
GET /repos/octocat/Hello-World/issues?state=all&per_page=10
Accept: application/vnd.github+json
X-GitHub-Api-Version: 2022-11-28
Authorization: Bearer <token>        # only when GITHUB_TOKEN is set
```

Response headers drive the reliability layer:
`Link: <...&page=2>; rel="next"`, `X-RateLimit-Limit: 60`, `X-RateLimit-Remaining`,
`X-RateLimit-Reset`.

**JSONPlaceholder**

```http
GET /users     ->  [{"id":1,"name":"Leanne Graham","email":"Sincere@april.biz",
                     "address":{"city":"Gwenborough","geo":{"lat":"-37.3159","lng":"81.1496"}}, ...}]
GET /posts     ->  [{"userId":1,"id":1,"title":"sunt aut facere ...","body":"..."}]
POST /posts    ->  {"id": 101, ...}      # fabricated id, nothing persisted
PATCH /posts/1 ->  {"id": 1, ...}
```

**Frankfurter**

```http
GET /v2/rates?base=USD&quotes=EUR,GBP,TRY,PKR
->  [{"date":"2026-08-05","base":"USD","quote":"EUR","rate":0.8663},
     {"date":"2026-08-05","base":"USD","quote":"PKR","rate":278.58}, ...]
```

**Open-Meteo**

```http
GET /v1/forecast?latitude=31.5204&longitude=74.3587&daily=temperature_2m_max,temperature_2m_min&timezone=auto
->  {"timezone":"Asia/Karachi",
     "daily":{"time":["2026-08-05", ... 7 dates],
              "temperature_2m_max":[34.6,33.7,...],
              "temperature_2m_min":[26.3,25.2,...]}}
```

**Nager.Date**

```http
GET /api/v4/Holidays/US/2026
->  [{"date":"2026-01-01","name":"New Year's Day","countryCode":"US",
      "nationalHoliday":true,"subdivisionCodes":null,"holidayTypes":["Public","Bank"]}]

GET /api/v4/Holidays/PK/2026   ->  204 No Content
```

## Enabling GitHub writes

1. GitHub → **Settings** → **Developer settings** → **Personal access tokens**.
   Fine-grained: *Issues: Read and write* on the target repository. Classic:
   `repo` (or `public_repo`).
2. Put it in `.env` as `GITHUB_TOKEN=...` and point `GITHUB_OWNER` / `GITHUB_REPO`
   at a repository you control.
3. `python -m integration_service.cli --check` should now report
   `"github_write_enabled": true`.

What changes: outbound POST/PATCH are actually sent instead of recorded as
`[MOCK WRITE]`, newly created issues have their id written back onto the task so
they are not created twice, and the rate limit rises from 60 to 5000 requests per
hour. No code changes.

## Tests

```bash
python -m pytest -q                                   # whole suite, offline
python -m pytest tests/test_frankfurter_connector.py -q
python -m pytest -q -k "timeout or rate_limit"
python -m pytest -q -v                                # per-test names
```

The suite never touches the network or Odoo: outbound HTTP is intercepted by
`responses` and Odoo is replaced by `FakeOdooClient` in `tests/conftest.py`, an
in-memory double that evaluates Odoo domains (including `|` / `!` prefix
operators) and can be told to fail any `(model, method)` pair.

## Postman

Import `postman/odoo_json2.postman_collection.json` and
`postman/odoo_integration_lab.postman_environment.json`, then fill in
`odoo_api_key` (and `github_token` if you have one) locally.

Folder **1. Odoo JSON-2 CRUD proof** runs in order: CREATE stores the new id into
`proof_partner_id`, and READ/UPDATE/DELETE reuse it. Every request carries test
assertions. Folder 1.6 reads `x_integration_sync_log` and accepts either 200 or
403 — 403 documents the missing access rule described in
[troubleshooting.md](troubleshooting.md).
