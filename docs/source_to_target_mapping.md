# Source-to-target mapping

Source: the CSV files produced by the CardConnect synthetic data generator (`E:\Local_Repository\CardConnect_ML\output`),
described by `docs/data_dictionary.csv`. Target: Oracle 19c schema created by `backend/migrations/V001…V007`.

All identifiers are stored as `VARCHAR2` strings exactly as supplied (leading zeros are preserved). Every file is
imported with `python -m scripts.import_csv --dataset <name> --file <csv>` or the dashboard upload, which share one
validation and MERGE pipeline (`backend/app/ingest`).

Load order (later datasets reference earlier ones): `customers → cards → merchants → transactions → offer_targeting →
offer_outcomes → offers → eligibility_rules`.

## customers.csv → `CUSTOMERS` + `CUSTOMER_DEVICES`

| Source column | Target | Rule |
|---|---|---|
| customer_id | CUSTOMERS.customer_id (PK) | Identifier pattern `[A-Za-z0-9_-]`, ≤ 20 bytes |
| customer_name | CUSTOMERS.customer_name | Required. PII: never a model feature; masked for READ_ONLY users and in logs |
| nic_no | CUSTOMERS.nic_no (UNIQUE) | Old `9 digits + V/X` or new 12-digit format; a NIC already owned by another customer is rejected (`IDENTITY_CONFLICT`). Never returned by the API |
| mobile_device_id | CUSTOMER_DEVICES.mobile_device_id (PK) | Primary device. A new device for an existing customer is added and flagged primary; earlier devices are kept with `is_primary = 'N'` (no overwrite). A device owned by another customer is rejected |
| device_type, device_os | CUSTOMER_DEVICES | Domain-checked (`MOBILE_PHONE/TABLET`, `ANDROID/IOS`) |
| customer_segment | CUSTOMERS.customer_segment | Upper-case token; used only for cold-start popularity and eligibility rules |
| customer_home_city/district/province | CUSTOMERS | Required |
| customer_tenure_months, number_of_cards | CUSTOMERS | Non-negative integers |
| customer_contact_no | CUSTOMERS.customer_contact_no (nullable) | **Not present in the supplied file** although listed in the dictionary. Optional; when a file omits it, existing values are kept (`NVL`). API returns `null` with `customer_contact_no_available: false` |

## cards.csv → `CARDS`

| Source | Target | Rule |
|---|---|---|
| card_id | CARDS.card_id (PK) | Synthetic id, not a PAN. No PAN, CVV or PIN exists anywhere in the system |
| customer_id | CARDS.customer_id (FK) | Must exist; a card cannot move to another customer (`IDENTITY_CONFLICT`) |
| nic_no | — (validated only) | Must equal the customer's NIC; not stored again |
| card_network / card_type / card_tier / card_status | CARDS | Domain-checked; AMEX must be CREDIT |
| card_age_months | CARDS | Integer ≥ 0 |

## merchants.csv → `MERCHANTS`

| Source | Target | Rule |
|---|---|---|
| merchant_id | MERCHANTS.merchant_id (PK) | |
| merchant_brand, merchant_category | MERCHANTS | Category ∈ 12 known categories |
| merchant_location, city, district, province | MERCHANTS | `merchant_city = INTERNATIONAL` marks foreign online merchants |
| merchant_email, merchant_contact_no | MERCHANTS | Optional, format-checked; masked for READ_ONLY users |
| merchant_type | MERCHANTS | `PHYSICAL/ONLINE/SERVICE` |
| merchant_offer_supported | MERCHANTS.merchant_offer_supported | `YES/NO`; non-participating outlets are never recommended |
| — | MERCHANTS.is_active | Admin-managed (dashboard), default `Y` |

## credit_card_transactions_3m.csv → `CARD_TRANSACTIONS` (normalised)

| Source | Target | Rule |
|---|---|---|
| transaction_id | transaction_id (PK) | Re-imports MERGE on this key: identical rows count as unchanged, differing rows as updated |
| transaction_datetime | transaction_datetime (DATE) | `YYYY-MM-DD HH:MM:SS` local time (Asia/Colombo). Timestamps with an offset are converted to Asia/Colombo |
| customer_id, card_id, merchant_id | FK columns | Card must belong to the customer (composite FK) |
| transaction_type | transaction_type | `PURCHASE, BILL_PAYMENT, RECURRING_PAYMENT` plus `REFUND, REVERSAL` (supported though absent from the data) |
| transaction_channel | transaction_channel | Domain-checked |
| transaction_amount | transaction_amount NUMBER(14,2) | Original amount, ≥ 0. A negative amount is accepted only for `REFUND` (sign moves to the type) |
| transaction_currency | transaction_currency | Must have a rate in `FX_RATES`; unknown currencies are rejected (`NO_FX_RATE`) |
| — | fx_rate_to_lkr, amount_lkr | `amount_lkr = amount × rate` at import. USD = 300 LKR (fixed synthetic rate from the data dictionary, stored in `FX_RATES`) |
| transaction_status | transaction_status | `APPROVED, DECLINED, CANCELLED, REVERSED` |
| is_international | is_international `Y/N` | |
| related_transaction_id (optional) | related_transaction_id | Required for `REVERSAL` |
| offer_id / offer_utilized | redeemed_offer_id | Outcome label only — never a model feature. `offer_utilized` must agree with `offer_id` |
| customer_name, nic_no, mobile_device_id, card_network, card_type, card_tier, merchant_brand, merchant_category | — | **Validated against master data and discarded** (`IDENTITY_MISMATCH` if they differ) |
| bank_id, customer_segment, home_* , merchant_* address/contact, transaction_hour, time_of_day, day_of_week, is_weekend, month, is_online_transaction, is_domestic | — | Ignored (derivable or constant); time features are recomputed from `transaction_datetime` |

## customer_offer_candidates.csv → `HISTORICAL_OFFER_TARGETING`

Customer-targeted brand "offers" (`OFFER_xxxxxxxx`) created by the source system. They have **no financial terms**,
so they are **not** catalog offers and are never recommended. They are stored as targeting history and outcome labels.

| Source | Target | Note |
|---|---|---|
| offer_id | offer_id (PK) | |
| customer_id | customer_id (FK) | |
| recommended_category, recommended_merchant_brand | same | Brand must exist in MERCHANTS |
| offer_reason, preference_score, offer_priority | same | Metadata |
| offer_type | targeting_type | LOYALTY, CROSS_SELL, … (targeting rule, not a benefit type) |
| offer_status, offer_utilized | same | `REDEEMED ⇔ utilized = YES` enforced |

## customer_offer_utilization.csv → `HISTORICAL_OFFER_TARGETING` (outcome columns)

MERGE (update only) by `offer_id`; the targeting row must already exist and belong to the same customer.
`merchant_id, offer_start_date, offer_end_date, offer_received, offer_utilized, utilization_date,
utilization_transaction_id, utilization_amount` are stored. `offer_redeemed` must equal `offer_utilized`.

## Offer catalog CSV (`data/sample/offer_catalog.sample.csv` format) → `OFFERS`

Columns: `offer_id, offer_title, offer_description, merchant_brand, merchant_id, merchant_category, offer_type
(PERCENT_DISCOUNT|PERCENT_CASHBACK|FIXED_DISCOUNT|FIXED_CASHBACK), offer_value, currency (LKR only), minimum_spend,
maximum_benefit, valid_from, valid_to, eligible_card_networks, eligible_card_types, eligible_card_tiers,
eligible_locations (ALL | ONLINE | DISTRICT:<name> | PROVINCE:<name>, ';'-separated), redemption_limit_per_customer,
total_redemption_limit, campaign_budget, terms_and_conditions, is_sample`.
Imported or edited offers always land as `PENDING_APPROVAL`; a status is never taken from a file.

## Offer eligibility rules CSV → `OFFER_ELIGIBILITY_RULES`

`offer_id, rule_type, operator, rule_value, description, is_active`; one rule per (offer, rule_type).
Rule types: CARD_NETWORK, CARD_TYPE, CARD_TIER (IN/NOT_IN), CUSTOMER_SEGMENT, HOME_DISTRICT, HOME_PROVINCE
(IN/NOT_IN), MIN_TENURE_MONTHS, MIN_SPEND_LAST_90D_LKR (GTE), NEW_TO_BRAND, EXISTING_BRAND_CUSTOMER (IS_TRUE).

## Not imported

| File | Reason |
|---|---|
| customer_behavior_features.csv, customer_category_preferences.csv, customer_merchant_preferences.csv | Derived by the generator; the platform recomputes features from transactions for any rolling window (and these files embed outcome-derived columns such as `offers_utilized`) |
| customer_preference_profiles.csv | Latent GROUND_TRUTH of the generator — must never be a feature. Usable only for offline research evaluation |
| credit_card_transactions_{train,validation,test}.csv | Subsets of the 3M file; the platform performs its own chronological splits |

## Platform-generated tables

`RECOMMENDATION_BATCHES, CUSTOMER_RECOMMENDATIONS, CUSTOMER_REC_STATUS, MODEL_TRAINING_RUNS, MODEL_REGISTRY,
BULK_JOBS, OFFER_EVENTS, CUSTOMER_CHANNEL_CONSENTS, DELIVERY_REQUESTS, IMPORT_BATCHES, IMPORT_REJECTIONS, AUDIT_LOG,
APP_USERS, APP_STATE, JOB_LOCKS, FX_RATES, SCHEMA_MIGRATIONS` and session-private `STG_*` global temporary tables.
All `*_at` timestamps are stored in UTC.
