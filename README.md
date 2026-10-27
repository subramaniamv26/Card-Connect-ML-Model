# Card Connect — personalised offer recommendations

Recommends the **best three eligible, approved offers** for each card customer from their last 12 months of
transactions. Next.js dashboard · FastAPI + python-oracledb · Oracle 19c · scheduled training worker · Docker.

```
            ┌──────────────┐  httpOnly cookie   ┌───────────────┐   bearer JWT   ┌──────────────────────┐
 browser ──▶│ web (Next.js)│ ── /api/proxy ───▶ │ api (FastAPI) │ ─────────────▶ │                      │
            └──────────────┘                    └───────┬───────┘                │  Oracle 19c (FreeSQL) │
                                                        │ queue rows             │  tcps://…:2484        │
                                                ┌───────▼───────┐  pooled conn   │                      │
                                                │    worker     │ ─────────────▶ │                      │
                                                │ 02:00 Colombo │                └──────────────────────┘
                                                └───────┬───────┘
                                     volumes: /models (artifacts), /data (uploads, exports)
```

* **api** – REST API (`/docs`), auth and roles, serving with request-time eligibility checks, uploads, audit.
* **worker** – daily training at 02:00 Asia/Colombo, Train Now queue, CSV imports, bulk exports. Lease locks in
  Oracle prevent overlapping runs across replicas.
* **web** – dashboard; the JWT lives in an httpOnly cookie and every call goes through a server-side proxy.

## Contents

| Path | What |
|---|---|
| `backend/app/ingest` | CSV validation (typed parsers, reference checks) and idempotent MERGE import |
| `backend/app/ml` | windows, net-spend rules, features, eligibility, models, generation, training/evaluation, publishing |
| `backend/app/api` | REST routers · `backend/app/services` serving · `backend/app/worker.py` scheduler |
| `backend/migrations` | Versioned Oracle 19c migrations `V001…V007` (applied by `python -m app.migrate`) |
| `backend/scripts` | `profile_data`, `import_csv`, `create_user`, `sample_csv` |
| `backend/tests` | 191 unit tests + 7 Oracle integration tests |
| `frontend/` | Next.js 15 dashboard |
| `data/sample/` | **Synthetic** sample offer catalog and eligibility rules (testing only) |
| `docs/` | data dictionary, data profile, data-quality report, source-to-target mapping, model, API, sample payloads |

## Status

**Verified** (on Windows 11 + Docker Desktop, against a local **Oracle Database 23ai Free** container; all SQL is
written for 19c and uses no 21c/23ai-only syntax):

* All 7 migrations apply; the full dataset imports with 0 rejections (3,000,000 transactions in 16.5 min locally);
  re-imports are idempotent (inserted / updated / unchanged counted); bad rows are rejected with codes and masked
  records; upload → worker → status works through the API and the dashboard.
* End-to-end training on all 3M transactions (≈ 2–2.5 min): ranker trained and validated, hold-out evaluation,
  promotion gate, generation for 50,000 customers, 100% eligibility re-check, atomic publish.
* API responses cross-checked against raw SQL for the 3-month aggregates; serving re-validation verified by
  suspending an offer (a reserve takes its place) and resuming it.
* Train Now and exports processed by the worker container; analyst exports masked, PII export denied (403).
* The cron trigger itself: with the schedule temporarily set a few minutes ahead (Asia/Colombo), the worker
  container enqueued a `SCHEDULED` run, loaded the previous model from the `models` volume, trained, promoted and
  published; a duplicate Train Now while a run is queued returns 409.
* `docker compose` stack builds and runs healthy (migrate → api, worker, web); login, proxy, redirects verified.
* `pytest`: 191 unit tests (validation, eligibility, aggregation, ranking, metrics, authorization matrix for all
  protected routes, masking) and 7 integration tests pass.

**Not yet verified — outstanding dependencies**

* **FreeSQL 19c itself.** The TLS endpoint was verified (`db.freesql.com:2484` presents a valid DigiCert certificate
  for `db.freesql.com`, TLS 1.3, so thin mode needs no wallet), but login, quota and privileges could not be checked
  without the password. See *Connecting to FreeSQL* below.
* **A real approved offer catalog.** The data contains none; the bundled catalog is synthetic (`[SAMPLE]`).
* `customer_contact_no` is absent from the supplied customers file (returned as `null`).
* No SMS/push adapter is bundled — `DELIVERY_REQUESTS` is the integration point.

**Assumptions** — USD→LKR at the dictionary's fixed rate 300; transaction timestamps are Asia/Colombo local time;
"last three months" = three calendar months up to and including the data cutoff; eligible locations refer to the
customer's home district/province; offers are LKR only.

## Quick start (Docker)

Prerequisites: Docker 24+ with Compose v2, network access to the Oracle database.

```bash
cp .env.example .env
```

Edit `.env`: set `ORACLE_PASSWORD` (current FreeSQL password) and a long random `JWT_SECRET`. Never commit `.env`.

```bash
docker compose --env-file .env build
```

```bash
docker compose --env-file .env up -d
```

`migrate` applies the schema once, then `api` (port 8000, docs at `/docs`), `worker` and `web` (port 3000) start.
Create the first administrator (the password is read from an environment variable, never the command line):

```bash
docker compose --env-file .env run --rm -e APP_USER_PASSWORD='choose-a-strong-password' api python -m scripts.create_user --username admin --role ADMIN
```

### Load the data

Large files are best loaded with the CLI importer (the dashboard upload accepts files up to 2 GB). Mount the CSV
folder and import in order:

```bash
docker compose --env-file .env run --rm -v /path/to/output:/import:ro api sh -c "for d in customers:customers cards:cards merchants:merchants transactions:credit_card_transactions_3m offer_targeting:customer_offer_candidates offer_outcomes:customer_offer_utilization; do python -m scripts.import_csv --dataset \${d%%:*} --file /import/\${d##*:}.csv --user bootstrap || exit 1; done"
```

Then load an offer catalog (here the synthetic sample) and approve offers in **Offers** (or via
`POST /api/v1/offers/{id}/actions/approve`):

```bash
docker compose --env-file .env run --rm -v "$(pwd)/data/sample:/import:ro" api sh -c "python -m scripts.import_csv --dataset offers --file /import/offer_catalog.sample.csv && python -m scripts.import_csv --dataset eligibility_rules --file /import/offer_eligibility_rules.sample.csv"
```

Finally press **Train now** on the Training page (or wait for 02:00 Asia/Colombo).

### Connecting to FreeSQL

* DSN `tcps://db.freesql.com:2484/19c_fy05s` (Easy Connect). python-oracledb **thin** mode does one-way TLS using the
  system CA store (`ca-certificates` is installed in the image); no wallet or Instant Client is needed.
* Check quota before a full load: `SELECT tablespace_name, max_bytes/1048576 mb FROM user_ts_quotas;`
  The full dataset needs about **1.1 GB** (transactions 418 MB + indexes). If the quota is smaller, load a
  consistent customer sample instead (every selected customer keeps all their history):

```bash
python -m scripts.sample_csv --src /path/to/output --dst /path/to/sample --customers 10000
```

* The schema user needs `CREATE TABLE` (incl. global temporary tables), `CREATE SEQUENCE` (identity columns) and quota.

## Local development

```bash
cd backend && python -m venv .venv && .venv/Scripts/python -m pip install -r requirements-dev.txt
```

```bash
cd backend && .venv/Scripts/python -m app.migrate && .venv/Scripts/python -m uvicorn app.main:app --port 8000
```

```bash
cd backend && .venv/Scripts/python -m app.worker
```

```bash
cd frontend && npm ci && npm run dev
```

`backend/.env` holds the same variables as `.env.example` (plus `DATA_DIR`, `MODEL_DIR` for local paths);
`frontend/.env.local` sets `BACKEND_URL=http://127.0.0.1:8000`.

### Tests

```bash
cd backend && .venv/Scripts/python -m pytest -q tests --ignore=tests/integration
```

Integration tests need a disposable, migrated schema with data loaded and one published batch:

```bash
cd backend && ORACLE_TEST_DSN=localhost:1521/FREEPDB1 ORACLE_TEST_USER=cc_test ORACLE_TEST_PASSWORD=*** .venv/Scripts/python -m pytest -q tests/integration
```

```bash
cd frontend && npx tsc --noEmit && npm run build
```

## Operation

* **Daily training** 02:00 Asia/Colombo (`TRAINING_CRON_HOUR/MINUTE`). A run computes the data cutoff (last
  transaction date), uses the rolling 12-month window, trains, evaluates against the active model (or the rules
  baseline), promotes or rejects, generates recommendations and publishes them atomically. A failed attempt is
  retried up to 3 times; the previous model and batch keep serving. History: **Training** page / `GET /training/runs`.
* **Train Now**: Training page (ADMIN) or `POST /api/v1/training/runs`. Only one run can be queued or running.
* **After catalog changes** (approve/revoke/suspend) serving reflects them immediately — revoked or expired offers
  are never returned and reserves fill the slot. To recompute rankings without retraining, start a
  `RECOMMENDATION_REFRESH` job.
* **Imports** are repeatable: re-uploading a file never duplicates rows; changed rows are updated.
* **Exports** expire after 72 hours; PII exports require ADMIN and are audited.
* **Model artifacts** are kept per version in the `models` volume with a SHA-256 recorded in `MODEL_REGISTRY`;
  an artifact is loaded only if its checksum matches. If the active artifact is missing, serving and the promotion
  gate fall back to the rules baseline and log an error.

## Model and evaluation

Hybrid recommender: brand/category affinity, item-item collaborative filtering, content similarity, time-pattern
match, popularity for cold start, plus a gradient-boosted response ranker trained on 72k historical deliveries →
redemptions (chronological wave split). On a 30-day transaction hold-out it reaches NDCG@3 0.559 vs 0.527 for the
rules baseline and 0.340 for popularity; ranker AUC 0.724 on the latest completed wave. Full method, numbers and the
**limitations of transaction-based evaluation** are in [docs/model.md](docs/model.md).

## Security and privacy

* Parameterised SQL everywhere (identifiers only from static code); request validation with Pydantic; uploads
  size-limited and stored under generated names; security headers on both services.
* bcrypt password hashes; HS256 JWT with expiry; login throttling; roles ADMIN / ANALYST / READ_ONLY enforced
  server-side on every route; audit log of logins, denials, PII views, catalog changes, exports and training.
* Names, NICs, phone numbers and device ids are never model features. NICs are never returned. Contact and device
  details only for ADMIN; names masked for READ_ONLY; rejected import rows and logs are masked.
* No PAN, CVV or PIN exists in the data model. Secrets come only from environment variables (`SecretStr`), never logs.
* Offers can only enter the catalog as PENDING_APPROVAL; optional four-eyes approval (`REQUIRE_FOUR_EYES_APPROVAL`).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ORA-01017` / `DPY-4011` on start | Wrong `ORACLE_USER`/`ORACLE_PASSWORD` (FreeSQL rotates passwords — copy the current one) |
| `DPY-6005` / certificate errors | Outbound 2484 blocked by a proxy/firewall, or the CA store is missing (`ca-certificates` in the image) |
| `ORA-01536: space quota exceeded` during import | Schema quota too small — load a `sample_csv` subset; earlier chunks stay committed and re-import is safe |
| `/health/ready` returns 503 | Database unreachable or migrations not applied (`docker compose logs migrate`) |
| Recommendations endpoint returns 503 | No batch published yet — import data, approve offers, run training |
| Every customer gets `NONE` | No approved offers are live today (check validity dates and approval status) |
| Training run `REJECTED` | Candidate failed the quality gate; the previous model keeps serving. See `promotion_decision` |
| Worker container unhealthy | No heartbeat for 120 s — `docker compose logs worker` |
| Login loop in the dashboard | `BACKEND_URL` not reachable from the web container, or `COOKIE_SECURE=true` without HTTPS |
