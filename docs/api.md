# REST API

Base path `/api/v1`. Interactive OpenAPI documentation: `http://<api-host>:8000/docs` (schema at `/openapi.json`).
Authenticate with `POST /api/v1/auth/login` and send `Authorization: Bearer <token>`. Tokens expire after
`JWT_TTL_MINUTES` (default 60). Five failed logins within 5 minutes lock the username temporarily.
Real sample payloads (captured from the running service with the READ_ONLY role) are in `docs/samples/`.

## Roles

| Permission | READ_ONLY | ANALYST | ADMIN |
|---|:-:|:-:|:-:|
| Dashboards, recommendations, profiles, catalog, imports, training history | ✓ | ✓ | ✓ |
| Customer names unmasked, merchant contacts unmasked | | ✓ | ✓ |
| Record engagement events, create/download masked exports | | ✓ | ✓ |
| Contact numbers and device ids, PII exports (audited) | | | ✓ |
| Imports, offers/merchants/rules management, offer approval | | | ✓ |
| Train Now, regenerate recommendations, users, consent and delivery | | | ✓ |

Every denied request returns 403 and is written to `AUDIT_LOG`. PII views, exports, downloads, catalog changes,
logins and training triggers are audited too.

## Endpoints

| Method & path | Purpose |
|---|---|
| `POST /auth/login`, `GET /auth/me` | Sign in; current user and permissions |
| `GET /customers/{customer_id}/recommendations` | Top three eligible offers for one customer (re-validated at request time) |
| `GET /recommendations?page=&page_size=&status=&district=&province=&segment=&history_level=&offer_id=&customer_id_prefix=` | All customers, paginated and filterable (max 200 per page), served from the stored batch |
| `POST /recommendation-jobs` | Asynchronous bulk job: `RECOMMENDATION_EXPORT` (csv/jsonl, filters, `include_pii` ADMIN only) or `RECOMMENDATION_REFRESH` (regenerate with the active model, ADMIN) |
| `GET /recommendation-jobs`, `GET /recommendation-jobs/{id}` | Job status and progress (own jobs; ADMIN sees all) |
| `GET /recommendation-jobs/{id}/download` | Download a finished export (requester or ADMIN; expires after 72 h) |
| `GET /imports/datasets`, `GET /imports/templates/{dataset}` | Importable datasets, required columns, CSV header template |
| `POST /imports` (multipart: `dataset`, `file`) | Upload a CSV; validated and merged by the worker |
| `GET /imports`, `GET /imports/{id}`, `GET /imports/{id}/rejections` | Import history, counts, error summary, rejected rows (PII masked) |
| `GET/POST /merchants`, `PATCH /merchants/{id}`, `GET /merchant-brands` | Merchant management |
| `GET/POST /offers`, `GET/PUT /offers/{id}` | Offer catalog (PUT needs `version` for optimistic locking) |
| `POST /offers/{id}/actions/{submit|approve|reject|suspend|resume|revoke}` | Approval workflow (`reason` body required for reject/suspend/revoke) |
| `POST /offers/{id}/rules`, `PUT/DELETE /offers/{id}/rules/{rule_id}` | Eligibility rules |
| `GET /training/status`, `GET /training/runs`, `GET /training/runs/{id}`, `GET /models` | Training status, history, metrics, registry |
| `POST /training/runs` | Train Now (queued; 409 if a run is already queued or running) |
| `POST /events` | Impressions, clicks, redemptions, dismissals (idempotent by `idempotency_key`) |
| `GET /reports/engagement` | Engagement and redemption report |
| `GET /dashboard/overview`, `GET /customers`, `GET /customers/{id}/profile` | Dashboard data |
| `PUT /customers/{id}/consents/{SMS|PUSH|EMAIL}`, `POST /deliveries`, `GET /deliveries` | Consent-aware delivery integration point |
| `GET /health`, `GET /health/ready` | Liveness; readiness (database + schema version) |

## Recommendation response

```jsonc
{
  "customer": {
    "customer_id": "CUSTOMER_000001",
    "customer_name": "D***** K**********",          // masked for READ_ONLY
    "mobile_device_id": "DEV*******001",            // primary device; masked unless pii:read
    "customer_home_city": "Negombo", "customer_home_district": "Gampaha", "customer_home_province": "Western",
    "customer_contact_no": null,                    // not supplied in the source data
    "customer_contact_no_available": false,
    "history_level": "ESTABLISHED", "cards_held": 3, "active_cards": 3, "devices_registered": 1
    // ADMIN only: "devices": [{ "mobile_device_id", "device_type", "device_os", "is_primary" }]
  },
  "recommendation_status": "FULL",                 // FULL (3) | PARTIAL (1-2) | NONE
  "shortfall_reason": null,                        // explains why fewer than three offers were returned
  "offers": [{
    "rank": 1, "offer_id": "SMP-RES-001", "offer_title": "...", "offer_description": "...",
    "recommendation_score": 0.74315,               // ranking score in [0,1], not a probability
    "recommendation_reason": "You frequently shop at Pizza Hut (9 purchases in your history), there is an outlet in Gampaha, and this offer applies to your Amex credit card.",
    "reason_codes": ["FREQUENT_MERCHANT", "RECENT_MERCHANT", "NEARBY_OUTLET", "CARD_ELIGIBLE"],
    "offer_type": "PERCENT_DISCOUNT", "offer_value": 15.0, "minimum_spend": 3000.0, "maximum_benefit": 1500.0,
    "estimated_customer_saving": 838.71,
    "estimated_customer_saving_assumptions": "Assumes one purchase of LKR 5,591 (your average at Pizza Hut); benefit = 15% of the purchase. Not a guaranteed amount.",
    "currency": "LKR", "valid_from": "2026-10-01", "valid_to": "2027-01-31",
    "terms_and_conditions": "...", "is_sample_offer": true,
    "card": { "card_id": "CARD_00000001", "card_network": "AMEX", "card_type": "CREDIT" },
    "merchant": { "merchant_id": "MERCHANT_000303", "merchant_brand": "Pizza Hut", "merchant_location": "No. 435, Hospital Road, Negombo",
                  "merchant_city": "Negombo", "merchant_district": "Gampaha", "merchant_province": "Western",
                  "merchant_email": "p***@merchant-mail.example", "merchant_contact_no": "********596",
                  "merchant_type": "PHYSICAL", "merchant_category": "RESTAURANT" },
    "customer_merchant_spend": {
      "merchant_transaction_amount_last_3_months": 22448.6,
      "merchant_transaction_count_last_3_months": 3,
      "last_transaction_date_with_merchant": "2026-10-02",
      "average_transaction_amount_with_merchant": 6093.8,
      "aggregation_period_start": "2026-07-13", "aggregation_period_end": "2026-10-12", "currency": "LKR"
    }
  }],
  "generated_at": "2026-10-25T19:38:12Z", "data_as_of": "2026-10-12", "model_version": "hybrid-20261026-010612",
  "batch_id": 24, "aggregation_period_start": "2026-07-13", "aggregation_period_end": "2026-10-12",
  "served_at": "2026-10-26T01:20:00+05:30", "pii_masked": true
}
```

### Field definitions

* **Last three months** = the three calendar months up to and including the data cutoff:
  `[cutoff + 1 day − 3 months, cutoff + 1 day)`. Cutoff 2026-10-12 → 2026-07-13 00:00:00 to 2026-10-12 23:59:59.
  The exact boundaries are returned in every response.
* `merchant_transaction_amount_last_3_months` / `_count_` are per **customer × outlet** (`merchant_id`), APPROVED
  transactions only, in LKR. Refunds reduce the amount but not the count; a reversal removes the original
  transaction from both; declined/cancelled/reversed transactions are excluded; the amount is floored at 0.
  USD transactions are converted at the recorded per-transaction FX rate (fixed 300 LKR/USD for this dataset).
* `last_transaction_date_with_merchant` and `average_transaction_amount_with_merchant` use the full 12-month history
  window for that outlet (gross purchases).
* `estimated_customer_saving` is an illustration for one qualifying purchase, never a commitment; its assumptions are
  always returned alongside.
* Timestamps ending in `Z` are UTC; `served_at` carries an explicit +05:30 offset; dates are Asia/Colombo dates.

## Errors

`401` not authenticated · `403` insufficient role · `404` not found · `409` conflict (state transition, concurrent
edit, run already queued) · `410` export expired · `413` upload too large · `422` validation failed
(`{"detail": {"message": "...", "errors": [{"field", "code", "message"}]}}`) · `503` no published batch yet.

## Delivery integration point

Recommendation generation never sends SMS or push messages. `POST /deliveries` checks that the customer has
`GRANTED` consent for the channel (`PUT /customers/{id}/consents/{channel}`) and that the offer is currently among
their eligible recommendations, then records a `QUEUED` request in `DELIVERY_REQUESTS` for an external, authorised
channel adapter to process (no adapter is bundled). Otherwise the request is stored as `BLOCKED_NO_CONSENT` or
`BLOCKED_INELIGIBLE`.
