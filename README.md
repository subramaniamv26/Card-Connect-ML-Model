# Card Connect — personalised offer recommendations (local development)

Recommends the **best three eligible, approved offers** for each card customer from their transaction history.
Everything runs directly on your PC: Python/FastAPI backend, a Python worker, and a Next.js dashboard, all using the
Oracle database you know in SQL Developer as **CC_ML** (`cc_app@localhost:1531/FREEPDB1`).

> New here? Read [docs/code_guide.md](docs/code_guide.md) — a walkthrough of how the code fits together.

## The three processes

```
  Browser ──▶ Dashboard (Next.js)  http://localhost:3000      dev\start-web.cmd
                   │  forwards every call, adding the login token
                   ▼
              API (FastAPI)        http://localhost:8000/docs dev\start-api.cmd
                   │  reads/writes                 ▲
                   ▼                               │ "queue" rows (Train now, uploads, exports)
              Oracle CC_ML  ◀──────────────  Worker (Python)       dev\start-worker.cmd
              localhost:1531/FREEPDB1        daily training 02:00 Asia/Colombo
```

| Process | What it does |
|---|---|
| **API** (`backend/app/main.py`) | Login and roles, recommendations, customers, offers, merchants, imports, training status |
| **Worker** (`backend/app/worker.py`) | Trains the model daily at 02:00 Colombo time, runs "Train now", CSV imports and exports |
| **Dashboard** (`frontend/`) | The web UI. Talks only to the API |

## First-time setup

Prerequisites: Python 3.12 (`py -3.12`), Node.js 20+, and the CC_ML database reachable.

1. Create `backend\.env` from `backend\.env.example` and fill in the `cc_app` password and a `JWT_SECRET`.
   (On this PC it already exists.)
2. Install everything and apply the database schema:

```bat
dev\setup.cmd
```

3. Create a login for the dashboard (you will be prompted for the password; minimum 10 characters):

```bat
dev\create-user.cmd admin ADMIN
```

Roles: `ADMIN` (everything), `ANALYST` (read + exports + events), `READ_ONLY` (read, personal data masked).

## Daily use

Open three terminals:

```bat
dev\start-api.cmd
```

```bat
dev\start-worker.cmd
```

```bat
dev\start-web.cmd
```

Then open http://localhost:3000 and sign in. API documentation: http://localhost:8000/docs.

Other helpers:

| Command | Purpose |
|---|---|
| `dev\train-now.cmd` | Run one full training + recommendation run in the terminal (~2–3 min; no worker needed) |
| `dev\import.cmd <dataset> <file.csv>` | Import a CSV (same validation as the Imports page) |
| `dev\test.cmd` | Run the 191 unit tests (no database needed) |

## Where the data comes from

The database already contains the synthetic dataset from `E:\Local_Repository\CardConnect_ML\output` (50,000
customers, 88,489 cards, 1,175 merchant outlets, 3,000,000 transactions, offer history) and the **synthetic sample
offer catalog** `data\sample\offer_catalog.sample.csv` (38 offers marked `[SAMPLE]`, created only for testing because
the source data has no real catalog). To load data into an empty schema, import in this order:

```bat
dev\import.cmd customers E:\Local_Repository\CardConnect_ML\output\customers.csv
```

…then `cards`, `merchants`, `transactions` (`credit_card_transactions_3m.csv`, ~15 min), `offer_targeting`
(`customer_offer_candidates.csv`), `offer_outcomes` (`customer_offer_utilization.csv`), `offers`
(`data\sample\offer_catalog.sample.csv`) and `eligibility_rules` (`data\sample\offer_eligibility_rules.sample.csv`).
Approve offers on the **Offers** page, then run `dev\train-now.cmd`. Re-importing a file never creates duplicates.

## Project layout

| Path | What |
|---|---|
| `backend/app/api/` | REST endpoints, one file per area |
| `backend/app/ingest/` | CSV validation and import |
| `backend/app/ml/` | features, eligibility, model, training, recommendation generation |
| `backend/app/services/recommendations.py` | serving recommendations (re-checks eligibility on every request) |
| `backend/app/worker.py` | scheduler and job queue |
| `backend/migrations/` | database schema, `V001…V007` (applied by `python -m app.migrate`) |
| `backend/scripts/` | command-line tools (import, create user, train once, data profiling, sampling) |
| `backend/tests/` | unit tests; `tests/integration` needs a test database |
| `frontend/app/` | dashboard pages; `frontend/components/` shared UI; `frontend/lib/api.ts` API calls |
| `dev/` | the `.cmd` scripts above |
| `docs/` | code guide, data quality report, source-to-target mapping, model and evaluation, API reference |
| `local_data/` | uploads, exports, trained models, logs (git-ignored, created automatically) |

## Current status

* Verified locally: full data import (0 rejections), training on all 3M transactions, recommendations for all
  50,000 customers with 100% eligibility re-check, dashboard and API, 191 unit + 7 integration tests.
* Not done yet: connection to **FreeSQL** (needs the password), a **real offer catalog**, and Docker packaging —
  the Docker files were moved to `E:\Local_Repository\Card-Connect-ML-Model-docker-backup` until you ask for them.
* Known data limits: only 6 months of transactions; `customer_contact_no` is missing from the source; no refunds
  or reversals in the data. Details: [docs/data_quality_report.md](docs/data_quality_report.md).

## Troubleshooting

| Symptom | Fix |
|---|---|
| API logs `ORA-12541` / `DPY-6005` (cannot connect) | The database is not running. CC_ML is served by the Oracle container `cc-oracle-test`: start Docker Desktop, then `docker start cc-oracle-test` |
| `ORA-01017` invalid username/password | Check `ORACLE_PASSWORD` in `backend\.env` matches the CC_ML connection |
| Dashboard shows "API is unreachable" | `dev\start-api.cmd` is not running, or `frontend\.env.local` has the wrong `BACKEND_URL` |
| Login keeps returning to the sign-in page | Token expired (60 min) — sign in again |
| Recommendations return 503 | No batch published yet — approve offers and run `dev\train-now.cmd` |
| Everyone gets `NONE` | No approved offer is valid today (check dates/status on the Offers page) |
| "Train now" stays QUEUED | The worker is not running — start `dev\start-worker.cmd` |
| Worker logs "Active model … could not be loaded" | The model file is missing (e.g. trained elsewhere); it falls back to the rules baseline. Run `dev\train-now.cmd` |
