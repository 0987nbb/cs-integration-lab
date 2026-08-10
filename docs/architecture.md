# Architecture

## 1. Shape of the system

An external Python service reads five public APIs and writes into an Odoo 19
Enterprise **Online** database over the JSON-2 REST API. Nothing runs inside Odoo:
Odoo Online does not accept a filesystem addon, so there is no server-side code,
no `ir.cron`, and no ORM access. Every read and write is an HTTP call.

```mermaid
flowchart LR
    subgraph ext["External APIs"]
        GH["GitHub REST<br/>/repos/{owner}/{repo}/issues"]
        JP["JSONPlaceholder<br/>/users, /posts"]
        FR["Frankfurter v2<br/>/v2/rates"]
        OM["Open-Meteo<br/>/v1/forecast"]
        NG["Nager.Date<br/>/api/v4/Holidays/{CC}/{Year}"]
    end

    subgraph svc["integration_service (Python)"]
        HTTP["ResilientHttpClient<br/>timeout · backoff · 429/5xx · pagination"]
        CONN["5 connectors<br/>BaseConnector.run()"]
        IDEM["Upserter<br/>x_external_id · x_source_hash"]
        RES["SyncResult<br/>counters · timing · status"]
        LOG["SyncLogWriter"]
        SCH["Scheduler"]
        ODC["OdooClient (JSON-2)"]
    end

    subgraph odoo["Odoo 19 Online - ai-demo-company"]
        MODELS["res.partner · project.task<br/>helpdesk.ticket · calendar.event<br/>res.currency.rate · mail.activity"]
        SLOG["x_integration_sync_log"]
    end

    FILE["logs/sync_log.jsonl<br/>(fallback sink)"]

    GH & JP & FR & OM & NG --> HTTP --> CONN --> IDEM --> ODC --> MODELS
    CONN --> RES --> LOG --> SLOG
    LOG -. "on HTTP 403" .-> FILE
    SCH --> CONN
```

**Transport.** `POST {ODOO_URL}/json/2/<model>/<method>` with
`Authorization: Bearer <api key>` and `X-Odoo-Database`. The body is the method's
keyword arguments (`{"domain": [...], "fields": [...]}`, `{"vals_list": [...]}`,
`{"ids": [...], "vals": {...}}`).

The older `/web/dataset/call_kw` JSON-RPC route is **not usable**: it authenticates
with a session cookie and answers `user is not connected` to an API key.
`OdooClient.call_kw` therefore raises with that explanation rather than appearing
to work.

## 2. Module layout and ownership

| Module | Owns |
|---|---|
| `config.py` | Reading the environment into typed settings. The only place a credential enters the process. |
| `sanitize.py` | Redaction. Every log line, exception message and stored error passes through it. |
| `errors.py` | The exception hierarchy. Every message is already sanitised when raised. |
| `http_client.py` | Timeouts, retry/backoff, 429/5xx classification, `Link` pagination. |
| `odoo_client/client.py` | JSON-2 verbs and the read/write helpers built on them. |
| `idempotency.py` | `x_external_id` construction, source hashing, and the create/update/skip decision. |
| `sync_result.py` | Counters, start/end time, and the rule that turns them into a status. |
| `sync_logger.py` | Persisting a run record, with the file fallback. |
| `scheduler.py` | Per-provider intervals, resident and one-shot execution. |
| `provisioning.py` | Idempotent custom-field creation and access probing. |
| `json2_proof.py` | Executable CREATE/READ/UPDATE/DELETE proof. |
| `connectors/base.py` | The run template, per-record isolation, sample capping. |
| `connectors/*_connector.py` | Mapping one API onto Odoo. Nothing else. |

A connector implements exactly one method. Everything above is inherited, which
is why all five behave identically under failure.

## 3. Connector lifecycle

```
BaseConnector.run()
  ├─ SyncResult(provider)                     start time captured
  ├─ sync(result)                             the only method a connector writes
  │    ├─ fetch          via ResilientHttpClient  (retries, pagination)
  │    ├─ limited(...)   apply SYNC_SAMPLE_LIMIT, recording what was dropped
  │    ├─ preload(...)   one read per 100 external ids
  │    └─ for each record:
  │         with guard(result, external_id):   <-- partial-failure boundary
  │             Upserter.upsert(...)           -> created | updated | skipped
  ├─ except IntegrationError  -> result.mark_fatal(...)      status = failed
  ├─ finally: result.finish()                 end time captured
  └─ SyncLogWriter.write(result)              Odoo, else logs/sync_log.jsonl
```

`guard` is the mechanism behind *partial failure continuation*: it catches
`RecordSyncError`, `HttpError`, any `IntegrationError` and any unexpected
exception, charges it to `result.failed` against that record's external id, and
returns control to the loop. A connector bug degrades one record, not the run.

## 4. Reliability model

All of it lives in `ResilientHttpClient`, so no connector reimplements any of it.

| Concern | Behaviour | Default |
|---|---|---|
| Timeout | Per request, connect + read | `HTTP_TIMEOUT=20` |
| Retries | `max_retries + 1` attempts | `HTTP_MAX_RETRIES=3` |
| Backoff | `backoff_factor * 2^(attempt-1)`, capped, then **full jitter** (`uniform(d/2, d)`) | `0.5`, cap `20` |
| HTTP 429 | Retried; `Retry-After` (delta-seconds **or** HTTP-date) overrides the backoff; GitHub's `X-RateLimit-Reset` is used when `Retry-After` is absent; clamped to 120 s | — |
| HTTP 5xx / 408 / 425 | Retried as transient | — |
| HTTP 4xx | **Never** retried; raised as `ClientError` | — |
| Non-JSON body | `InvalidPayloadError` | — |
| HTTP 204 | `HttpResponse.is_empty`, not an error | — |
| Pagination | `Link: rel="next"` walked automatically, with cycle detection and a `max_pages` bound | 100 pages |

Full jitter (rather than a fixed delay) matters because five connectors can run
back to back against the same host; identical retry timings would synchronise
into a burst.

Two things learned from live runs shaped this:

* **The sample limit must bound the fetch, not filter it.** An early run against
  `odoo/odoo` walked 44 pages before trimming to 10 issues and exhausted GitHub's
  60-requests/hour unauthenticated budget. `_fetch_issues` now closes the page
  generator as soon as it has enough, and requests `per_page = min(per_page, limit)`.
* **An unsupported activity anchor burns the whole retry budget.** Odoo answers
  HTTP 500 (a retryable class) when a `mail.activity` is attached to a model
  without `mail.thread`. The Frankfurter connector therefore checks
  `ir.model.fields` for `activity_ids` instead of discovering it by failing.

## 5. Idempotency model

Three columns, present on `res.partner`, `project.task`, `helpdesk.ticket` and
`calendar.event`:

| Column | Meaning |
|---|---|
| `x_external_id` | `"<provider>:<entity>:<id>"` — the stable upstream key |
| `x_source_hash` | SHA-256 over the *mapped Odoo values*, canonical JSON, sorted keys |
| `x_external_updated_at` | Upstream `updated_at`, used to decide direction in two-way sync |

The decision, in `Upserter.upsert`:

```
no record with this x_external_id      -> create   (+ create_only_vals)
record exists, stored hash == new hash -> skip     (no write at all)
record exists, hash differs            -> write    (update in place)
```

The hash deliberately **excludes** `x_source_hash`, `x_external_id` and
`x_external_updated_at`: including a field that the write itself sets would make
every hash differ from the previous run and every record update forever.

Two consequences worth stating:

* **Anything not in the mapped values is invisible to the hash.** Nager.Date
  renders holiday classification into the description precisely so a re-typed
  holiday is detected. GitHub folds comment bodies in through
  `extra_hash_input` because comments live in the description but are fetched
  separately.
* **A record's three columns have exactly one owner.** `res.partner` is owned by
  the JSONPlaceholder connector. Open-Meteo writes the same contacts but must
  never touch those columns — if it wrote a forecast hash into `x_source_hash`,
  JSONPlaceholder would see a changed contact and rewrite it, Open-Meteo would
  see the rewrite, and the two would update each other indefinitely. Open-Meteo
  therefore stores into dedicated `x_forecast_*` columns and decides idempotency
  by comparing the canonically serialised payload against the stored one. A test
  asserts the forbidden columns never appear in its write payloads.

`make_external_id` keeps ids, dates and country codes verbatim and slugifies
free text. Callers that key on a human-readable name pass `slugify(name)`
explicitly, because otherwise a single-word name (`Neujahr`) would be preserved
cased while a multi-word one (`New Year's Day`) became a slug — and an upstream
re-casing would then clone the record instead of matching it.

## 6. Run accounting and status

| Counter | Means |
|---|---|
| `created` | A new Odoo record was written for an unseen external id |
| `updated` | An existing record's source hash changed and was rewritten |
| `skipped` | Already matched (no-op), or deliberately withheld, or nothing to import |
| `failed` | This record could not be synced; the run continued |

```
fatal, or every attempted record failed  -> failed
some succeeded and at least one failed   -> partial
nothing was examined at all              -> skipped
everything attempted succeeded           -> success
```

`skipped` is reserved for "there was nothing to examine" — an unconfigured
country list, no geolocated contact, or Nager.Date answering 204. A run that
examined records and found them all unchanged is a **successful no-op**, which is
why a second identical run reports `success` with a non-zero skip count. For the
same reason the 204 branch records `count=0`: the counters count *records*, and a
country with no data yielded none.

## 7. Security model

* No secret appears in any tracked file. `.env` is git-ignored; `.env.example`
  and the Postman environment ship with empty credential values.
* Secrets enter the process only through `config.py`, which registers each one
  with `sanitize.register_secret` at load time.
* `sanitize()` redacts registered literals (longest first) **and** structural
  patterns that catch a credential nobody registered: `Authorization:` headers,
  `?token=` / `?api_key=` query parameters, `"api_key": "..."` JSON fragments,
  `ghp_*` / `github_pat_*` literals, and `https://user:pass@host` URLs.
* `sanitize_headers` replaces sensitive header values wholesale rather than
  pattern-matching them, because such a value is a credential in its entirety.
* `SyncResult` only accepts text that has already been sanitised, so
  `x_error_details` and `logs/sync_log.jsonl` are safe by construction.
* GitHub's `Authorization` header is built per request and never logged; the
  connector's own log lines carry the URL only.

## 8. Sync logging and its fallback

One `x_integration_sync_log` record per run: provider, start/end time, the four
counters, status, and sanitised error details, linked to `x_integration_config`
when that row is readable.

`SyncLogWriter` still degrades to `logs/sync_log.jsonl` if the write fails, and
says so on the run, so a log that did not reach Odoo never looks like one that
did. It is a fallback, not the normal path: provisioning installs the access rule
the model needs (§10).

> **Superseded.** Earlier revisions of this document said both custom models
> answer HTTP 403 and that the rule "cannot be created from code" because
> `ir.model.access` is not routed. The second half was a misreading of a 404.
> Odoo 19 merged `ir.model.access` and `ir.rule` into a single **`ir.access`**
> model — the old names 404 because they no longer exist, not because the route
> withholds them. `ir.access` *is* reachable over JSON-2, so the rules are
> provisioned like everything else.

## 9. Sync Now: the request queue

Odoo Online evaluates server-action code under `safe_eval`: no `import`, no
socket, no `requests`. Odoo therefore cannot call GitHub or Frankfurter itself,
and a button that reported "Sync Completed" from inside Odoo would be reporting
something it is structurally incapable of doing.

So the button enqueues, and says that is what it did:

```
        Odoo UI                         integration_service
  ┌──────────────────┐
  │ Sync Now (886)   │  x_sync_state = requested
  │ ir.actions.server│ ─────────────────────────────▶ ┌─────────────────────┐
  └──────────────────┘                                 │ SyncRequestWorker   │
                                                       │  --drain-requests   │
        idle ◀── done/failed ◀── real counters ────────│  --serve-requests   │
                                 real x_integration_   └─────────┬───────────┘
                                 sync_log record                 │
                                                          real provider API
```

`_claim` is a compare-and-set: the worker writes `running`, re-reads the row, and
only proceeds if the state it reads back is its own, so two workers racing for
one request cannot both run it. A connector that raises still releases the row —
otherwise one crash would leave Sync Now permanently dead for that provider.

## 9a. Scheduling

* **Resident** — `--schedule` keeps the process alive, running each provider on
  its own `SCHEDULE_<PROVIDER>_MINUTES` interval, sleeping in 5-second slices so
  SIGINT/SIGTERM is honoured promptly.
* **One-shot** — `--schedule --once` runs only what is currently due and exits;
  this is the form cron or Windows Task Scheduler should invoke.
* **On demand** — `--provider <name>` runs immediately, ignoring the schedule.

Due times advance by whole intervals rather than `now + interval`, so a slow run
or a suspended machine does not drift the cadence.

## 10. Schema provisioning

`--provision` brings a database — including an empty one — to the expected state,
and every step is individually idempotent, so a second run writes nothing.

| Step | Creates |
|---|---|
| `ensure_integration_models` | `x_integration_config`, `x_integration_sync_log` and every column, selection values included |
| `ensure_partner_forecast_fields` | the six `x_forecast_*` columns on `res.partner` |
| `ensure_idempotency_fields` | `x_external_id` / `x_source_hash` / `x_external_updated_at` on the four integrated models |
| `ensure_partner_forecast_view` | the Contact form page, inheriting `base.view_partner_form` |
| `ensure_integration_config_views` | the **Sync Now** server action |
| `ensure_integration_config_records` | one config row per provider, keyed on `x_provider` |
| `ensure_menus` | the CS Integration Lab menu and its two actions |
| `ensure_security` | the Integration Manager group, its `ir.access` rules, and the group restriction on every menu/action |

Manual *models* are created here, which an earlier revision claimed was
impossible. The original conclusion — "a manual model has no access rule and the
rule cannot be created, so the model is unreadable" — was correct in its first
half and wrong in its second; with `ir.access` writable the model is perfectly
usable. The 7-day forecast still lives in `res.partner` columns rather than a
child model, but now because a column is the simpler mapping, not because a model
was unreachable.

`ensure_security` runs its rules through a named server action
(`CS Integration Lab: Provision Security`) rather than direct writes, because it
needs `env.user` — the account this service authenticates as is added to the
group *before* any broader grant is withdrawn, so the integration cannot lock
itself out. The body is a constant; no caller-supplied code is executed.

## 11. Known constraints and trade-offs

| Constraint | Consequence | Response |
|---|---|---|
| `safe_eval` forbids `import`, sockets, `try`/`except` and dunders | Odoo cannot call a provider, and provisioning code cannot catch its own errors | Sync Now enqueues; the worker runs the provider; a provisioning failure surfaces as Odoo's own traceback and rolls back |
| Inbound is authoritative on conflict | An Odoo-side edit made while the same issue also changed on GitHub is overwritten on the next run | Documented; the Odoo change must be re-applied. Only same-record, same-window collisions are affected |
| `api.jsonplaceholder.dev` does not resolve | The endpoint named in the brief is unreachable | Probed first, then `jsonplaceholder.typicode.com`, with a note on the run |
| Nager.Date has no data for some countries (PK) | HTTP 204 | Counted as skipped with a note, never failed |
| Company currency is PKR, API base is USD | Raw API rates are not Odoo rates | Rebased with `Decimal`; see [data_mapping.md](data_mapping.md) |
| `res.currency` lacks `mail.thread` | An approval activity cannot attach to it | Anchor resolved from `ir.model.fields`; falls back to the approver's user record |
| GitHub unauthenticated limit is 60/hour | Large repositories exhaust it | Fetch bounded by the sample limit; `Retry-After` honoured; a token raises the limit |
| No `GITHUB_TOKEN` configured | Outbound issue writes cannot be sent | Both write paths implemented; emit `[MOCK WRITE]`; setting the token switches them live with no code change |
| JSONPlaceholder does not persist writes | POST/PATCH are inherently simulated | Sent for real, then recorded as `[MOCK WRITE]` with the echoed id |
