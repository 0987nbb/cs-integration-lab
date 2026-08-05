# Odoo Integration Lab

External Python integration service that synchronises **five public APIs** into an
**Odoo 19 Enterprise Online** database through the **JSON-2 REST API** (Bearer token).

| | |
|---|---|
| Target | `https://ai-demo-company.odoo.com` |
| Database | `ai-demo-company` |
| Transport | `POST {ODOO_URL}/json/2/<model>/<method>` with `Authorization: Bearer` + `X-Odoo-Database` |
| Deployment | External service only. **No Odoo addon is installed on the target.** |

Odoo Online does not accept custom addons, so every custom field this service relies on is
provisioned through the API as a *manual* `ir.model.fields` record (`python -m integration_service.cli --provision`).

---

## Integrated APIs

| # | API | Endpoint used | Odoo target |
|---|-----|---------------|-------------|
| 1 | GitHub REST | `GET/POST https://api.github.com/repos/{owner}/{repo}/issues` | `project.task` (two-way) |
| 2 | JSONPlaceholder | `GET /users`, `GET/POST /posts`, `PATCH /posts/{id}` | `res.partner`, `helpdesk.ticket` |
| 3 | Frankfurter v2 | `GET https://api.frankfurter.dev/v2/rates?base=USD&quotes=EUR,GBP,TRY,PKR` | `res.currency.rate` |
| 4 | Open-Meteo | `GET https://api.open-meteo.com/v1/forecast?...&daily=temperature_2m_max,temperature_2m_min&timezone=auto` | `res.partner` forecast columns |
| 5 | Nager.Date | `GET https://date.nager.at/api/v4/Holidays/{CountryCode}/{Year}` | `calendar.event` |

---

## Quick start

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate on POSIX
pip install -r requirements.txt

cp .env.example .env            # then fill in ODOO_URL / ODOO_DATABASE / ODOO_API_KEY
```

```bash
# 1. Confirm connectivity and per-model access
python -m integration_service.cli --check

# 2. Create any missing custom field on res.partner (idempotent)
python -m integration_service.cli --provision

# 3. Prove Odoo JSON-2 CREATE / READ / UPDATE / DELETE
python -m integration_service.cli --proof

# 4. Rehearse without writing anything
python -m integration_service.cli --provider all --dry-run

# 5. Run for real  ("Sync Now")
python -m integration_service.cli --provider all
```

Run one connector at a time with `--provider github|jsonplaceholder|frankfurter|open_meteo|nager_date`.

### Scheduled sync

```bash
python -m integration_service.cli --show-schedule   # resolved plan, then exit
python -m integration_service.cli --schedule        # resident; Ctrl-C to stop
python -m integration_service.cli --schedule --once # run what is due, exit (for cron)
```

Odoo Online cannot run this code, so `ir.cron` is unavailable and the schedule lives in
the service. Intervals come from `SCHEDULE_INTERVAL_MINUTES` and per-provider
`SCHEDULE_<PROVIDER>_MINUTES`. Use `--schedule --once` from cron or Windows Task
Scheduler — see [docs/api_usage.md](docs/api_usage.md) for both entries.

**Exit codes:** `0` all runs succeeded · `1` at least one partial · `2` at least one failed ·
`3` the service could not start.

---

## Repository layout

```
integration_service/          External service (this is the deliverable)
  config.py                   Env-backed settings; the only place secrets are read
  sanitize.py                 Credential redaction applied to every log/error/sync-log line
  errors.py                   Exception hierarchy
  http_client.py              Timeouts, exponential backoff + jitter, 429/Retry-After, 5xx, pagination
  idempotency.py              x_external_id / x_source_hash upsert engine
  sync_result.py              created/updated/skipped/failed counters, timing, status
  sync_logger.py              Writes x_integration_sync_log, falls back to logs/sync_log.jsonl
  provisioning.py             Idempotent custom-field creation + access probing
  scheduler.py                Per-provider intervals; resident and one-shot modes
  json2_proof.py              Executable CREATE/READ/UPDATE/DELETE proof
  odoo_client/client.py       Odoo 19 JSON-2 API client
  connectors/
    base.py                   Connector template: run/sync/guard/limited
    github_connector.py
    jsonplaceholder_connector.py
    frankfurter_connector.py
    open_meteo_connector.py
    nager_date_connector.py
  cli.py                      Command-line entry point

tests/                        Offline pytest suite (all HTTP mocked via `responses`)
postman/                      JSON-2 CRUD collection + environment template
docs/                         architecture · data_mapping · api_usage · troubleshooting
cs_integration_lab/           Legacy Odoo addon from the foundation phase; NOT deployed
```

---

## Cross-cutting behaviour

Every connector inherits the same guarantees from `connectors/base.py` and `http_client.py`:

* **Idempotent upsert** — keyed on `x_external_id`; `x_source_hash` (SHA-256 of the mapped values)
  turns an unchanged record into a `skipped` no-op instead of a rewrite.
* **Sync logging** — one `x_integration_sync_log` record per run carrying provider, start/end time,
  created/updated/skipped/failed counts, sanitised error details and a
  `success` / `partial` / `failed` / `skipped` status.
* **Reliability** — per-request timeout; exponential backoff with full jitter; `Retry-After`
  honoured on HTTP 429 (and GitHub's `X-RateLimit-Reset` as a fallback); 5xx retried, 4xx terminal;
  `Link: rel="next"` and offset pagination.
* **Partial-failure continuation** — each record is processed inside `BaseConnector.guard`, so one
  bad record is counted `failed` and the run proceeds.
* **Security** — no hardcoded secrets; credentials come only from `.env`; `sanitize.py` redacts
  registered secrets plus `Authorization` headers, `?token=` parameters and `ghp_*` literals from
  everything that is logged, raised or persisted.

---

## Known environment constraints

These were verified against the live instance and are handled in code rather than assumed away.

| Constraint | Effect | Handling |
|---|---|---|
| `x_integration_config` / `x_integration_sync_log` return **HTTP 403** — no access rule exists, and `ir.model.access` is not exposed on the JSON-2 route (HTTP 404) | Sync logs cannot be written to Odoo until a rule is added in the UI | `SyncLogWriter` falls back to `logs/sync_log.jsonl`; no code change needed once the rule exists. Click-path in [docs/troubleshooting.md](docs/troubleshooting.md) |
| `/web/dataset/call_kw` rejects API-key auth (`user is not connected`) | Only `/json/2/` is usable | `OdooClient.call_kw` raises with that explanation; all traffic goes through `_request` |
| `api.jsonplaceholder.dev` does not resolve | The endpoint named in the brief is unreachable | Connector probes it first, then falls back to `jsonplaceholder.typicode.com` and records a note |
| Nager.Date has no data for some countries (e.g. `PK`) and answers **HTTP 204** | No holidays to import | Counted as `skipped` with a note — not a failure |
| Company currency is **PKR**, Frankfurter's base is **USD** | Raw API rates are not Odoo rates | Converted with `Decimal`: `odoo_rate(Q) = api_rate(USD→Q) / api_rate(USD→PKR)` |
| GitHub unauthenticated limit is 60 requests/hour | Large repos exhaust it | `Retry-After` / `X-RateLimit-Reset` respected; set `GITHUB_TOKEN` to raise the limit |
| No `GITHUB_TOKEN` configured | Outbound issue writes cannot be sent | Both write paths are fully implemented and emit `[MOCK WRITE]` entries; setting the token switches them to live with no code change |

---

## Tests

```bash
python -m pytest -q            # entire offline suite
python -m pytest tests/test_frankfurter_connector.py -q
```

The suite never touches the network or Odoo. For each of the five connectors it covers **success,
timeout, HTTP 429 rate limit, HTTP 500 server error and invalid payload**, plus per-connector
idempotency, duplicate-protection and partial-failure-continuation assertions.

---

## Verified against the live database

A limited-sample run against `ai-demo-company`, followed immediately by a second
run, demonstrates idempotency:

| Connector | First run | Second run |
|---|---|---|
| `jsonplaceholder` | created 20 | **skipped 20** |
| `frankfurter` | created 3, skipped 1 | **skipped 4** |
| `open_meteo` | created 10 | **skipped 10** |
| `nager_date` | created 36 | **skipped 36** |

Spot checks: `res.currency.rate` for EUR stored as `0.003109699189`, which is exactly
`USD→EUR 0.8663 / USD→PKR 278.58` to 12 decimal places; `res.partner` id 16 carries
`x_forecast_next_summary = "2026-08-06: max 18.0 C / min 17.2 C"` with all 7 days in
`x_forecast_payload`; holidays land as all-day `free` events with Odoo deriving
`start_date`/`stop_date`.

## Postman

`postman/odoo_json2.postman_collection.json` proves JSON-2 CREATE → READ → UPDATE →
READ → DELETE with assertions on every step, and issues the exact upstream request
each connector makes. Import
`postman/odoo_integration_lab.postman_environment.json` and fill in `odoo_api_key`
locally — the committed template holds no credential.

## Documentation

* [docs/architecture.md](docs/architecture.md) — components, connector lifecycle, reliability and security models
* [docs/data_mapping.md](docs/data_mapping.md) — per-API field mappings, external-id formats, upsert rules
* [docs/api_usage.md](docs/api_usage.md) — setup, every environment variable, every CLI invocation
* [docs/troubleshooting.md](docs/troubleshooting.md) — symptom → cause → fix, including the 403 access-rule fix
