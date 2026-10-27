# Recommendation approach and evaluation

## Why this approach (based on the actual data)

* **Items to recommend** are approved catalog offers, each tied to a merchant brand (or one outlet). Relevance is
  therefore learned at the **customer × brand** level and transferred to any offer on that brand, so offers that
  have never been shown can be ranked on day one.
* **Implicit feedback is rich**: 3M transactions, 50k customers, 91 brands, strong and stable preferences (the
  generator's validation shows the favourite category persists for ~65% of customers between halves of the period).
  Affinity, collaborative and content signals are all supported by the data.
* **Explicit outcome labels exist** but are limited: 87,328 historical deliveries, 26,146 redemptions in 6 waves, all
  *targeted by the source system's own rules* (not randomised). They justify a response ranker, but its scores are
  not presented as calibrated probabilities.
* **No live engagement history yet** (impressions/clicks/dismissals): the events API records them; dismissals
  already suppress offers for 30 days and redemptions count towards limits and budgets.

## Pipeline

1. **Net spend** (`app/ml/spend.py`): APPROVED only; refunds subtract; reversals cancel the original; amounts in LKR.
2. **Features** (`app/ml/features.py`), all computed *as of* a timestamp from earlier transactions only:
   RFM (30/90/365-day counts and spend, recency, average ticket, 30-vs-90-day trend), brand and category affinity
   (`0.5·txn share + 0.3·spend share + 0.2·exp(−recency/45)`), item-item collaborative score (cosine on the customer ×
   brand log-count matrix), content similarity (category, merchant type, price level), time-of-day/weekend pattern
   match, segment and home-district popularity, proximity (outlet in home district/province, online), brand momentum.
   Names, NICs, phone numbers and device ids are never inputs.
3. **Eligibility first** (`app/ml/eligibility.py`): status/validity (Asia/Colombo date), participating active outlet,
   an ACTIVE card matching networks/types/tiers and card rules, location, customer rules, per-customer and total
   redemption limits, campaign budget, recent dismissal.
4. **Relevance** (`app/ml/model.py`):
   * *Rules baseline* (transparent): brand affinity if the customer uses the brand, else ½ × category affinity, else
     segment popularity.
   * *Hybrid*: weighted blend of brand affinity 0.35, category 0.20, collaborative 0.15, content 0.10, time 0.05,
     segment popularity 0.10, district popularity 0.05; blended 60/40 with the rank-normalised **response ranker**
     (`HistGradientBoostingClassifier`) when the ranker beats the heuristic on the latest completed wave by ≥ 0.01 AUC.
   * *Cold start*: customers with no history use segment/district popularity and proximity; limited history (< 5
     transactions) blends 50/50. New brands without transactions are reached through category affinity and content.
5. **Final score** = 0.75 × relevance + 0.15 × benefit + 0.10 × proximity, where benefit = saving / (saving + 1000 LKR).
   The saving estimate assumes one purchase at the customer's average ticket for that brand (fallbacks: their typical
   purchase, then the brand's population median), raised to the minimum spend if below it, capped at the maximum
   benefit. The assumption text is returned with every offer.
6. **Variety**: greedy re-rank — one offer per brand, at most two per category, −0.10 per category repeat.
7. **Choice of card and outlet**: the eligible card used most at that brand (then category, then overall); the
   outlet the customer visits most for that brand, else one in the home district/province.
8. **Reserve depth 6** is stored; the API serves the first three that are still eligible at request time.

## Training, validation and promotion (daily 02:00 Asia/Colombo)

* **Data cutoff** = last date with transactions; history window = 12 calendar months back from the cutoff.
* **Response ranker**: one row per delivered historical offer with features as of the wave start. Waves whose
  redemption window was still open at the cutoff are excluded (censoring). Chronological split: train on earlier
  waves, validate on the latest completed wave, then refit on all completed waves.
* **Transaction hold-out**: features as of `cutoff − 30 days`; ground truth = participating brands each customer
  actually bought in the following 30 days; 20,000 sampled customers × 78 brands. The candidate's ranker in this
  check is trained only on waves that ended before the hold-out started (no leakage).
* **Promotion**: candidate NDCG@3 ≥ reference × (1 − 2%), where the reference is the active model (evaluated frozen)
  or, if none, the rules baseline. Otherwise the candidate is REJECTED and the active model keeps serving.
  The frozen active model may have seen labels overlapping the hold-out, which biases the comparison in its favour —
  the gate is conservative by construction.
* **Batch audit**: every published top-3 row is re-checked with the scalar eligibility function; anything below 100%
  compliance fails the run and nothing is published.
* Failures retry up to 3 times (5-minute delay); the previous model and batch keep serving throughout.

## Results on the supplied data (local Oracle run, data cutoff 2026-10-12)

| Method (hold-out 2026-09-13 → 2026-10-12) | Precision@3 | Recall@3 | NDCG@3 | Discovery recall@3 |
|---|---:|---:|---:|---:|
| Segment popularity | 0.315 | 0.181 | 0.340 | 0.181 |
| Rules baseline | 0.472 | 0.280 | 0.527 | 0.130 |
| Hybrid (heuristic only) | 0.488 | 0.294 | 0.547 | 0.189 |
| Hybrid + response ranker (candidate) | 0.500 | 0.297 | 0.559 | 0.115 |

Response ranker on the latest completed wave (2026-08-26, 14,758 deliveries, 4,144 redeemed): AUC 0.724 vs
0.712 heuristic and 0.701 rules baseline; average precision 0.486. Most important features (permutation AUC drop):
category affinity, brand transaction count, 90-day brand count.

Published batch: coverage 100% (all 50,000 customers received three offers), category diversity 0.90, catalog
coverage 97% of live offers, eligibility compliance 100% of 150,000 rows.

## Limitations — read before trusting the numbers

* **Purchases are not redemptions.** The hold-out measures whether we predict which participating brands a customer
  will buy from next; it does not measure whether an offer *causes* a purchase or a redemption. Recommending brands
  the customer already uses scores well here but has limited incremental value. Discovery recall (brands new to the
  customer) is reported separately and is lower for the ranker, which favours familiar brands.
* **Historical labels are biased** by the source system's targeting (only customers chosen by its rules received an
  offer, and only for one brand). The ranker learns redemption *among targeted customers*. Scores are ranking scores,
  not calibrated probabilities.
* **The offer catalog is synthetic.** Values, caps and eligibility were invented for testing (clearly labelled
  SAMPLE). Benefit-weighted ranking and savings estimates will only be meaningful with a real approved catalog.
* **Only 6 months of data**; seasonality over a year is not learnable and 365-day features equal the full history.
* **No real cold-start customers** exist in the data; cold-start behaviour is verified by unit tests only.
* To measure true uplift, run a randomised hold-out (e.g. 10% of eligible customers receive no offer or a random
  eligible offer) and evaluate redemptions recorded through `POST /api/v1/events`. Precision@3/Recall@3/NDCG@3 on
  real impressions → redemptions will then become available.
