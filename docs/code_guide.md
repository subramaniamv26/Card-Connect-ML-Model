# Code guide

A tour of how the application works, in the order you will probably want to read it. File references are relative
to the project root.

## 1. The big picture

There are three things to understand:

1. **Data in** — CSV files are validated and loaded into Oracle (`backend/app/ingest/`).
2. **Model** — once a day the worker turns transactions into features, ranks the approved offers for every customer
   and stores the top candidates (`backend/app/ml/`).
3. **Data out** — the API serves the stored recommendations, re-checking eligibility on every request
   (`backend/app/services/recommendations.py`), and the dashboard displays them (`frontend/`).

The API never trains and never imports big files itself: it writes a row into a queue table
(`MODEL_TRAINING_RUNS`, `IMPORT_BATCHES`, `BULK_JOBS`) and the worker picks it up. That keeps the API fast.

## 2. Configuration and database access

| File | Read it for |
|---|---|
| `backend/app/config.py` | Every setting and its default. Values come from `backend/.env` |
| `backend/app/db.py` | The Oracle connection pool and four helpers used everywhere: `fetch_all`, `fetch_one`, `scalar`, `execute` |
| `backend/app/migrate.py` + `backend/migrations/V00x__*.sql` | How the schema is created and versioned |

Rule used throughout: **all SQL uses bind variables** (`:name`), never string formatting of user input.

Start with `V001`–`V004` to see the tables:

* master data — `CUSTOMERS`, `CUSTOMER_DEVICES`, `CARDS`, `MERCHANTS`
* facts — `CARD_TRANSACTIONS`, `HISTORICAL_OFFER_TARGETING`
* catalog — `OFFERS`, `OFFER_ELIGIBILITY_RULES`
* model output — `RECOMMENDATION_BATCHES`, `CUSTOMER_RECOMMENDATIONS`, `CUSTOMER_REC_STATUS`
* operations — `MODEL_TRAINING_RUNS`, `MODEL_REGISTRY`, `IMPORT_BATCHES`, `BULK_JOBS`, `AUDIT_LOG`, `APP_USERS`

Try in SQL Developer (CC_ML):

```sql
SELECT * FROM recommendation_batches ORDER BY batch_id DESC;
SELECT * FROM customer_recommendations WHERE batch_id = (SELECT MAX(batch_id) FROM recommendation_batches
  WHERE status = 'PUBLISHED') AND customer_id = 'CUSTOMER_000001' ORDER BY candidate_rank;
SELECT run_id, status, promotion_decision, duration_seconds FROM model_training_runs ORDER BY run_id DESC;
```

## 3. Importing data (`backend/app/ingest/`)

| File | Role |
|---|---|
| `fields.py` | Small parsers: text with byte-length limits, identifiers (kept as text, so leading zeros survive), numbers, dates, timestamps, lists |
| `datasets.py` | One `DatasetSpec` per CSV type: its columns, the per-row checks (`_validate_customer`, `_validate_txn`, …) and the target table |
| `importer.py` | `run_import()` — the engine |

How `run_import()` works:

1. Check the header has all required columns.
2. Load lookup data (existing customers, cards, merchants, …) into memory.
3. For every row: parse each field → run the dataset's checks → reject duplicates within the file.
   Bad rows go to `IMPORT_REJECTIONS` with an error code and a masked copy of the row.
4. Good rows are collected in chunks of 20,000, inserted into a temporary staging table (`STG_*`), then
   **MERGEd** into the real table. Unchanged rows are skipped and changed rows updated, so importing the same file
   twice never duplicates anything.
5. Counts (inserted / updated / unchanged / rejected) are written to `IMPORT_BATCHES`.

Good exercise: read `_validate_txn` in `datasets.py` and the matching tests in
`backend/tests/test_import_validation.py`.

## 4. The model (`backend/app/ml/`)

Read these files in this order:

| File | What it does |
|---|---|
| `windows.py` | Date windows. *Data cutoff* = last day with transactions; history = 12 months before it; "last 3 months" = 3 calendar months up to the cutoff |
| `spend.py` | `net_spend_frame()` — only APPROVED transactions count; refunds subtract; reversals cancel the original |
| `features.py` | `build_features()` turns transactions into customer × brand matrices: how often / how much / how recently each customer buys each brand, category affinity, "customers like you" (collaborative), brand similarity (content), time-of-day match, popularity, proximity |
| `eligibility.py` | `evaluate()` — is this offer allowed for this customer today? (approved, valid dates, matching active card, location, rules, limits, budget, not dismissed) |
| `model.py` | Scoring: `RecModel.relevance()` (how well the offer fits), `estimate_saving()`, `final_score()`, `select_diverse()` (no two offers for the same brand, max two per category), `explain()` (the readable reason) |
| `generate.py` | `generate()` — for every customer: eligible offers → score → pick up to 6 diverse candidates → choose card and outlet → 3-month spend with that outlet → reason |
| `training.py` | Trains the response model on past offer redemptions, and measures quality (Precision@3, Recall@3, NDCG@3) on the last 30 days of transactions |
| `store.py` | Saves model files and the registry; writes a batch and **publishes it in one transaction** so users never see half a batch |
| `pipeline.py` | `run_training()` — the daily job that calls all of the above in order |

`run_training()` step by step:

1. Find the data cutoff and the 12-month window.
2. Load transactions → net spend.
3. Train the response model on historical deliveries (`build_label_dataset`, `train_ranker`).
4. Evaluate the popularity, rules baseline, hybrid, active model and new candidate on the 30-day hold-out.
5. Promote the candidate only if it is at least as good (within 2%) as the current model. Otherwise keep the old one.
6. Generate recommendations for all customers with the chosen model.
7. Re-check every published offer for eligibility (must be 100%), then publish the batch.

Easiest way to watch it: run `dev\train-now.cmd` and read the log lines while following `pipeline.py`.

The model is explained in more detail in `docs/model.md`.

## 5. Serving recommendations

`GET /api/v1/customers/{id}/recommendations` → `backend/app/api/recommendations.py` → `serve()` in
`backend/app/services/recommendations.py`:

1. Find the current published batch (`APP_STATE.CURRENT_BATCH_ID`).
2. Load that customer's stored candidates (up to 6), their cards and the offers.
3. Re-run `eligibility.evaluate()` for each candidate **now**. An offer revoked after training is dropped and the
   next candidate moves up.
4. Build the response: customer, offers with card, merchant and 3-month spend. Personal data is masked by role in
   `_response()`.

## 6. The API (`backend/app/api/`)

`backend/app/main.py` creates the FastAPI app and includes one router per file:

| File | Endpoints |
|---|---|
| `auth.py` | login, current user, user management |
| `recommendations.py` | one customer, all customers (paged) |
| `customers.py` | dashboard overview, customer search, spending profile |
| `catalog.py` | merchants, offers (create/edit/approve/suspend/revoke), eligibility rules |
| `imports.py` | upload CSV, import history, rejected rows |
| `training.py` | training status, run history, Train now |
| `jobs.py` | bulk exports |
| `events.py` | impressions/clicks/redemptions/dismissals, engagement report |
| `delivery.py` | consent-checked delivery requests (no messages are actually sent) |

Security is in `backend/app/security.py`. Each endpoint declares `user = Depends(require("permission"))`, and the
`PERMISSIONS` table at the top of that file maps each permission to the minimum role. `backend/app/audit.py` writes
`AUDIT_LOG`. Use http://localhost:8000/docs to try endpoints: click **Authorize** after calling `/auth/login`.

## 7. The worker (`backend/app/worker.py`)

`main()` starts APScheduler with these jobs:

* `enqueue_scheduled_training` — at 02:00 Asia/Colombo, inserts a QUEUED training run.
* `process_training_queue` / `process_import_queue` / `process_job_queue` — every 15 s, pick the oldest queued item.
* `heartbeat` — every 30 s, so the Training page can show the worker is alive.

`backend/app/locks.py` makes sure two runs never overlap, even if two workers are started by accident.

## 8. The dashboard (`frontend/`)

| File | Role |
|---|---|
| `app/layout.tsx`, `components/Shell.tsx` | Page frame and navigation |
| `app/login/page.tsx` + `app/api/session/route.ts` | Sign in. The token is kept in an httpOnly cookie that browser JavaScript cannot read |
| `app/api/proxy/[...path]/route.ts` | Forwards `/api/proxy/...` to the FastAPI server and adds the token |
| `lib/api.ts` | `api()` and `useApi()` for calling the backend; formatting helpers |
| `app/<page>/page.tsx` | One file per screen: overview, customers, offers, merchants, imports, training, jobs, reports |
| `components/ui.tsx`, `components/OfferCard.tsx` | Shared UI pieces |

## 9. Tests

`dev\test.cmd` runs `backend/tests`:

* `test_import_validation.py` — CSV rules
* `test_eligibility.py` — every eligibility rule
* `test_aggregation_ranking.py` — net spend, 3-month boundaries, diversity, savings, metrics and generation on a
  tiny hand-made dataset
* `test_api_authorization.py` — every protected endpoint × every role

Changing a rule and seeing which test fails is a quick way to learn the code.

## 10. Debugging in VS Code

Open the project folder and use the launch configurations in `.vscode/launch.json` ("API", "Worker",
"Train once") to set breakpoints, for example inside `generate()` or `evaluate()`.
