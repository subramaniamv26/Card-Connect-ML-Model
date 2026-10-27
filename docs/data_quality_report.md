# Data quality report (Stage 1 inspection)

Produced by `backend/scripts/profile_data.py` over **every row** of the supplied files (full results in
`docs/data_profile.json`). Re-run with:

```bash
python -m scripts.profile_data --data-dir <csv dir> --dictionary ../docs/data_dictionary.csv --out ../docs
```

## Volumes

| File | Rows | Duplicate keys | Orphans |
|---|---:|---:|---:|
| customers.csv | 50,000 | 0 (ids, NICs, devices all unique) | — |
| cards.csv | 88,489 | 0 | 0 |
| merchants.csv | 1,175 (91 brands, 12 categories, 25 districts) | 0 | — |
| credit_card_transactions_3m.csv | 3,000,000 | 0 transaction ids, 0 (card, timestamp) pairs | 0 customers / cards / merchants |
| customer_offer_candidates.csv | 231,213 | 0 | 0 |
| customer_offer_utilization.csv | 87,328 | 0 | 0 |

Consistency: 0 transactions whose card belongs to another customer; 0 mismatches between the customer, card and
merchant attributes repeated on each transaction and the master files; 0 unparseable amounts or timestamps;
0 non-positive amounts; `transaction_hour` matches the timestamp on every row; `offer_utilized ⇔ offer_id` on every row;
every utilisation transaction id exists. The full 3M-row import into Oracle rejected **0** rows.

## Findings that affect the design

| Severity | Finding | Handling |
|---|---|---|
| HIGH | Transactions cover **182 days** (2026-04-13 → 2026-10-12), not 12 months | The pipeline uses a rolling 12-month window ending at the data cutoff; with this data it contains all 6 months. 365-day features equal "all history". Seasonality over a year cannot be learned. |
| HIGH | **No approved offer catalog.** `OFFER_xxxxxxxx` rows are per-customer targeting records without value, minimum spend, cap, validity, card eligibility or T&Cs | Catalog import + admin screen built. A clearly labelled **synthetic sample catalog** (`data/sample/offer_catalog.sample.csv`, 38 offers, `is_sample=YES`, titles prefixed `[SAMPLE]`) is provided for testing; it imports as PENDING_APPROVAL and must be approved by an admin. Replace it with a real catalog before any real use. |
| MEDIUM | `customer_contact_no` is in the data dictionary but **absent from customers.csv** | Column is nullable; the API returns `null` and `customer_contact_no_available: false`. Supply it in a later customers import (existing values are never wiped by files that omit it). |
| MEDIUM | No refunds, reversals or cancellations exist (only APPROVED 2,955,036 / DECLINED 44,964) | Supported anyway (types REFUND/REVERSAL, statuses CANCELLED/REVERSED, `related_transaction_id`) and unit-tested. |
| INFO | 163,886 transactions (5.5%) are in USD (all international) | Converted at the data dictionary's fixed synthetic rate 300 LKR/USD, recorded per row (`fx_rate_to_lkr`). |
| INFO | Card status is a snapshot; it is not historical | Only ACTIVE cards are eligible for offers today; history is used regardless of current card status. |
| INFO | Historical offer waves: 6 × 21-day waves; the 6th (2026-09-25 → 2026-10-15) was still open at the data cutoff | The open wave is excluded from labels entirely (censoring). |
| INFO | All 50,000 customers have ≥ 3 transactions | No real cold-start customers; cold-start logic is covered by unit tests only. |

Historical redemption rates (useful labels): LOYALTY 43.5%, FREQUENCY_GROWTH 39.6%, VALUE_GROWTH 32.3%,
CATEGORY_GROWTH 20.7%, REACTIVATION 10.9%, CROSS_SELL 7.3%.
