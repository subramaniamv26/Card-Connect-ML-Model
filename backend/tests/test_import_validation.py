from datetime import date, datetime
from decimal import Decimal

import pytest

from app.ingest.datasets import DATASETS, ReferenceData
from app.ingest.fields import Field, FieldError, parse_local_datetime
from app.ingest.importer import count_sql, merge_sql, validate_row


def ref() -> ReferenceData:
    r = ReferenceData()
    r.customers = {"CUSTOMER_000001": ("Dinesh Karunaratne", "971654398V"), "CUSTOMER_000002": ("Gayan P", "200222206807")}
    r.nic_owner = {"971654398V": "CUSTOMER_000001", "200222206807": "CUSTOMER_000002"}
    r.device_owner = {"DEVICE_000001": "CUSTOMER_000001"}
    r.cards = {"CARD_00000001": ("CUSTOMER_000001", "AMEX", "CREDIT", "CLASSIC")}
    r.merchants = {"MERCHANT_000001": ("Keells Super", "GROCERY", "Dehiwala", "YES"),
                   "MERCHANT_001100": ("Netflix", "ENTERTAINMENT", "INTERNATIONAL", "NO")}
    r.brand_categories = {"Keells Super": {"GROCERY"}, "Netflix": {"ENTERTAINMENT"}}
    r.districts, r.provinces = {"Colombo", "Gampaha"}, {"Western"}
    r.fx = {"LKR": [(date(2000, 1, 1), None, Decimal(1))], "USD": [(date(2000, 1, 1), None, Decimal(300))]}
    r.catalog_offers = {"SMP-1"}
    return r


def run(dataset: str, row: dict, reference: ReferenceData | None = None):
    return validate_row(DATASETS[dataset], row, set(row), reference or ref())


TXN = {"transaction_id": "TXN_000000001", "transaction_datetime": "2026-04-13 00:00:41", "customer_id": "CUSTOMER_000001",
       "card_id": "CARD_00000001", "merchant_id": "MERCHANT_000001", "transaction_type": "PURCHASE",
       "transaction_channel": "POS_CHIP", "transaction_amount": "1500.50", "transaction_currency": "LKR",
       "transaction_status": "APPROVED", "is_international": "0", "offer_id": "", "offer_utilized": "NO",
       "customer_name": "Dinesh Karunaratne", "nic_no": "971654398V", "card_network": "AMEX"}


def test_identifiers_keep_leading_zeros():
    assert Field("x", "id").parse("000123") == "000123"
    t, errs = run("transactions", TXN)
    assert errs is None and t[0] == "TXN_000000001"


def test_varchar_limits_are_bytes():
    with pytest.raises(FieldError) as e:
        Field("x", "str", max_bytes=4).parse("ශ්‍රී")  # 4 characters, > 4 bytes in UTF-8
    assert e.value.code == "TOO_LONG"


def test_timestamp_with_offset_is_converted_to_colombo():
    assert parse_local_datetime("2026-04-13T00:00:00Z") == datetime(2026, 4, 13, 5, 30)
    assert parse_local_datetime("2026-04-13 08:15:00") == datetime(2026, 4, 13, 8, 15)


def test_usd_amount_converted_to_lkr_with_recorded_rate():
    t, errs = run("transactions", {**TXN, "transaction_currency": "USD", "transaction_amount": "10.00",
                                   "is_international": "1"})
    assert errs is None
    assert t[9] == Decimal(300) and t[10] == Decimal("3000.00")


def test_unknown_currency_rejected():
    _, errs = run("transactions", {**TXN, "transaction_currency": "EUR"})
    assert errs[0][0] == "NO_FX_RATE"


@pytest.mark.parametrize("patch,code", [
    ({"transaction_amount": "-5"}, "NEGATIVE_AMOUNT"),
    ({"transaction_amount": "0"}, "ZERO_AMOUNT"),
    ({"transaction_amount": "1.234"}, "TOO_MANY_DECIMALS"),
    ({"transaction_amount": "abc"}, "NOT_A_NUMBER"),
    ({"transaction_datetime": "13/04/2026"}, "BAD_DATETIME"),
    ({"transaction_status": "PENDING"}, "BAD_VALUE"),
    ({"customer_id": "CUSTOMER_999999"}, "UNKNOWN_CUSTOMER"),
    ({"card_id": "CARD_X"}, "UNKNOWN_CARD"),
    ({"customer_id": "CUSTOMER_000002", "customer_name": "", "nic_no": ""}, "IDENTITY_MISMATCH"),
    ({"nic_no": "000000000V"}, "IDENTITY_MISMATCH"),
    ({"card_network": "VISA"}, "IDENTITY_MISMATCH"),
    ({"offer_id": "OFFER_1", "offer_utilized": "NO"}, "INCONSISTENT"),
    ({"transaction_type": "REVERSAL"}, "MISSING_REFERENCE"),
    ({"transaction_id": ""}, "REQUIRED"),
])
def test_transaction_rejections(patch, code):
    _, errs = run("transactions", {**TXN, **patch})
    assert errs is not None and errs[0][0] == code


def test_refund_negative_amount_is_normalised():
    t, errs = run("transactions", {**TXN, "transaction_type": "REFUND", "transaction_amount": "-200"})
    assert errs is None and t[7] == Decimal(200) and t[5] == "REFUND"


CUST = {"customer_id": "CUSTOMER_000003", "customer_name": "A B", "nic_no": "199726806651", "mobile_device_id": "DEVICE_000003",
        "device_type": "MOBILE_PHONE", "device_os": "IOS", "customer_segment": "SALARIED", "customer_home_city": "Kandy",
        "customer_home_district": "Kandy", "customer_home_province": "Central", "customer_tenure_months": "23",
        "number_of_cards": "2"}


def test_customer_identity_conflicts():
    assert run("customers", CUST)[1] is None
    assert run("customers", {**CUST, "nic_no": "971654398V"})[1][0][0] == "IDENTITY_CONFLICT"
    assert run("customers", {**CUST, "mobile_device_id": "DEVICE_000001"})[1][0][0] == "IDENTITY_CONFLICT"
    assert run("customers", {**CUST, "nic_no": "12345"})[1][0][0] == "BAD_FORMAT"


def test_customer_contact_optional_and_validated():
    t, errs = run("customers", CUST)
    assert errs is None and t[9] is None
    assert run("customers", {**CUST, "customer_contact_no": "not-a-phone"})[1][0][0] == "BAD_FORMAT"


def test_card_rules():
    base = {"card_id": "CARD_9", "customer_id": "CUSTOMER_000001", "card_network": "AMEX", "card_type": "DEBIT",
            "card_tier": "GOLD", "card_age_months": "3", "card_status": "ACTIVE"}
    assert run("cards", base)[1][0][0] == "BAD_VALUE"                       # AMEX must be credit
    assert run("cards", {**base, "card_type": "CREDIT", "customer_id": "CUSTOMER_000009"})[1][0][0] == "UNKNOWN_CUSTOMER"
    assert run("cards", {**base, "card_id": "CARD_00000001", "card_type": "CREDIT",
                         "customer_id": "CUSTOMER_000002"})[1][0][0] == "IDENTITY_CONFLICT"


OFFER = {"offer_id": "SMP-9", "offer_title": "t", "offer_description": "d", "merchant_brand": "Keells Super",
         "merchant_category": "GROCERY", "offer_type": "PERCENT_DISCOUNT", "offer_value": "10", "valid_from": "2026-10-01",
         "valid_to": "2027-01-31", "terms_and_conditions": "Terms apply to this offer."}


@pytest.mark.parametrize("patch,code", [
    ({"offer_value": "150"}, "OUT_OF_RANGE"),
    ({"merchant_brand": "Unknown Brand"}, "UNKNOWN_BRAND"),
    ({"merchant_category": "FUEL"}, "INCONSISTENT"),
    ({"merchant_brand": "", "merchant_id": "MERCHANT_001100", "merchant_category": "ENTERTAINMENT"},
     "MERCHANT_NOT_PARTICIPATING"),
    ({"valid_to": "2026-01-01"}, "BAD_DATE_RANGE"),
    ({"eligible_locations": "DISTRICT:Atlantis"}, "BAD_LOCATION"),
    ({"eligible_card_networks": "VISA;DINERS"}, "BAD_VALUE"),
    ({"currency": "USD"}, "UNSUPPORTED_CURRENCY"),
])
def test_offer_rejections(patch, code):
    _, errs = run("offers", {**OFFER, **patch})
    assert errs is not None and errs[0][0] == code


def test_offer_lists_normalised():
    t, errs = run("offers", {**OFFER, "eligible_card_networks": "visa, amex", "eligible_locations": "DISTRICT:Colombo|ONLINE"})
    assert errs is None
    cols = dict(zip(DATASETS["offers"].stage_columns, t))
    assert cols["eligible_card_networks"] == "VISA;AMEX"
    assert cols["eligible_locations"] == "DISTRICT:Colombo;ONLINE"
    assert "status" not in cols  # status is never taken from a file: imports always land as PENDING_APPROVAL


def test_rule_operator_validation():
    ok = {"offer_id": "SMP-1", "rule_type": "MIN_TENURE_MONTHS", "operator": "GTE", "rule_value": "6"}
    assert run("eligibility_rules", ok)[1] is None
    assert run("eligibility_rules", {**ok, "operator": "IN"})[1][0][0] == "BAD_OPERATOR"
    assert run("eligibility_rules", {**ok, "rule_value": "six"})[1][0][0] == "NOT_A_NUMBER"
    assert run("eligibility_rules", {**ok, "offer_id": "NOPE"})[1][0][0] == "UNKNOWN_OFFER"


def test_identical_rows_hash_identically_and_changes_are_detected():
    a, _ = run("customers", CUST)
    b, _ = run("customers", dict(CUST))
    c, _ = run("customers", {**CUST, "customer_home_city": "Peradeniya"})
    assert a[-1] == b[-1] != c[-1]


def test_merge_sql_is_idempotent_upsert_on_natural_key():
    sql = merge_sql(DATASETS["customers"])
    assert sql.startswith("MERGE INTO customers t USING (SELECT * FROM stg_customers) s ON (t.customer_id = s.customer_id)")
    assert "t.row_hash <> s.row_hash" in sql and "WHEN NOT MATCHED THEN INSERT" in sql
    txn = merge_sql(DATASETS["transactions"])
    assert "DECODE(t.amount_lkr, s.amount_lkr, 0, 1) = 1" in txn
    outcome = merge_sql(DATASETS["offer_outcomes"])
    assert "WHEN NOT MATCHED" not in outcome
    new, changed = count_sql(DATASETS["eligibility_rules"])
    assert "t.offer_id = s.offer_id AND t.rule_type = s.rule_type" in new
