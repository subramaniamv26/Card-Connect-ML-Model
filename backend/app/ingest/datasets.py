"""Declarative specifications for every importable dataset.

A DatasetSpec lists the CSV fields (with parsers), the staging table, natural key, and how the staged
rows are merged into the target. `validate_row` performs cross-field and reference checks using
ReferenceData preloaded from Oracle, and returns the tuple inserted into the staging table.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable

from .fields import Field, FieldError

CATEGORIES = frozenset({"GROCERY", "RESTAURANT", "FUEL", "RETAIL", "ELECTRONICS", "TRAVEL", "HOTEL",
                        "ENTERTAINMENT", "HEALTHCARE", "UTILITIES", "EDUCATION", "E-COMMERCE"})
NETWORKS = frozenset({"VISA", "MASTERCARD", "AMEX"})
CARD_TYPES = frozenset({"CREDIT", "DEBIT"})
CARD_TIERS = frozenset({"CLASSIC", "GOLD", "PLATINUM", "SIGNATURE", "PREMIUM"})
CARD_STATUSES = frozenset({"ACTIVE", "INACTIVE", "BLOCKED"})
TXN_TYPES = frozenset({"PURCHASE", "BILL_PAYMENT", "RECURRING_PAYMENT", "REFUND", "REVERSAL"})
TXN_STATUSES = frozenset({"APPROVED", "DECLINED", "CANCELLED", "REVERSED"})
CHANNELS = frozenset({"POS_CHIP", "CONTACTLESS", "ONLINE", "MOBILE_APP", "RECURRING_AUTO_DEBIT"})
TARGETING_TYPES = frozenset({"LOYALTY", "CROSS_SELL", "CATEGORY_GROWTH", "REACTIVATION", "FREQUENCY_GROWTH",
                             "VALUE_GROWTH"})
OFFER_TYPES = frozenset({"PERCENT_DISCOUNT", "PERCENT_CASHBACK", "FIXED_DISCOUNT", "FIXED_CASHBACK"})
RULE_TYPES = frozenset({"CARD_NETWORK", "CARD_TYPE", "CARD_TIER", "CUSTOMER_SEGMENT", "HOME_DISTRICT",
                        "HOME_PROVINCE", "MIN_TENURE_MONTHS", "MIN_SPEND_LAST_90D_LKR", "NEW_TO_BRAND",
                        "EXISTING_BRAND_CUSTOMER"})
RULE_OPERATORS = {"CARD_NETWORK": {"IN", "NOT_IN"}, "CARD_TYPE": {"IN", "NOT_IN"}, "CARD_TIER": {"IN", "NOT_IN"},
                  "CUSTOMER_SEGMENT": {"IN", "NOT_IN"}, "HOME_DISTRICT": {"IN", "NOT_IN"},
                  "HOME_PROVINCE": {"IN", "NOT_IN"}, "MIN_TENURE_MONTHS": {"GTE"},
                  "MIN_SPEND_LAST_90D_LKR": {"GTE"}, "NEW_TO_BRAND": {"IS_TRUE"},
                  "EXISTING_BRAND_CUSTOMER": {"IS_TRUE"}}

NIC_RE = re.compile(r"^(\d{9}[VvXx]|\d{12})$")
PHONE_RE = re.compile(r"^\+?[0-9][0-9\- ]{6,19}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TOKEN_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,29}$")


class RowError(Exception):
    def __init__(self, code: str, message: str, column: str | None = None):
        super().__init__(message)
        self.code, self.message, self.column = code, message, column


@dataclass
class ReferenceData:
    """Lookups loaded from Oracle before validation (only those a dataset needs)."""
    customers: dict[str, tuple[str, str]] = field(default_factory=dict)      # id -> (name, nic)
    nic_owner: dict[str, str] = field(default_factory=dict)                  # nic -> customer_id
    device_owner: dict[str, str] = field(default_factory=dict)               # device -> customer_id
    cards: dict[str, tuple[str, str, str, str]] = field(default_factory=dict)  # id -> (cust, net, type, tier)
    merchants: dict[str, tuple[str, str, str, str]] = field(default_factory=dict)  # id -> (brand, cat, city, offer)
    brand_categories: dict[str, set[str]] = field(default_factory=dict)
    districts: set[str] = field(default_factory=set)
    provinces: set[str] = field(default_factory=set)
    fx: dict[str, list[tuple[date, date | None, Decimal]]] = field(default_factory=dict)
    targeting_offers: set[str] = field(default_factory=set)
    targeting_customer: dict[str, str] = field(default_factory=dict)
    catalog_offers: set[str] = field(default_factory=set)

    def fx_rate(self, currency: str, on: date) -> Decimal:
        for start, end, rate in self.fx.get(currency, []):
            if start <= on and (end is None or on <= end):
                return rate
        raise RowError("NO_FX_RATE", f"No LKR conversion rate for {currency} on {on}", "transaction_currency")


def row_hash(values: list[Any]) -> str:
    h = hashlib.md5(usedforsecurity=False)
    for v in values:
        h.update(("" if v is None else str(v)).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


Validator = Callable[[dict[str, Any], dict[str, str], ReferenceData], tuple]


@dataclass
class DatasetSpec:
    name: str
    label: str
    fields: list[Field]
    stage_table: str
    stage_columns: list[str]
    target_table: str
    key_columns: list[str]
    validator: Validator
    change_mode: str = "hash"                     # hash | columns
    update_columns: list[str] | None = None       # default: all non-key stage columns except helpers
    insert_columns: list[str] | None = None
    merge_insert: bool = True
    update_extra: str = ""                        # appended to UPDATE SET (fixed SQL, no user input)
    insert_extra_cols: str = ""
    insert_extra_vals: str = ""
    needs: frozenset[str] = frozenset()
    file_hint: str = ""
    post_merge: list[str] = field(default_factory=list)

    @property
    def required_columns(self) -> list[str]:
        return [f.name for f in self.fields if f.required]

    @property
    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]


def _f(name, kind="str", **kw) -> Field:
    return Field(name, kind, **kw)


def _ident(name, max_bytes=20, required=True) -> Field:
    return Field(name, "id", required=required, max_bytes=max_bytes)


# --------------------------------------------------------------------------------------------------
# customers.csv -> CUSTOMERS + CUSTOMER_DEVICES
# --------------------------------------------------------------------------------------------------
CUSTOMER_FIELDS = [
    _ident("customer_id"),
    _f("customer_name", max_bytes=100),
    _f("nic_no", max_bytes=12, pattern=NIC_RE, upper=True),
    _ident("mobile_device_id"),
    _f("device_type", "enum", choices=frozenset({"MOBILE_PHONE", "TABLET"})),
    _f("device_os", "enum", choices=frozenset({"ANDROID", "IOS"})),
    _f("customer_segment", max_bytes=30, pattern=TOKEN_RE, upper=True),
    _f("customer_home_city", max_bytes=60),
    _f("customer_home_district", max_bytes=40),
    _f("customer_home_province", max_bytes=40),
    _f("customer_tenure_months", "int", min_value=Decimal(0), max_value=Decimal(9999)),
    _f("number_of_cards", "int", min_value=Decimal(0), max_value=Decimal(99)),
    _f("customer_contact_no", required=False, max_bytes=20, pattern=PHONE_RE),
]


def _validate_customer(v: dict, raw: dict, ref: ReferenceData) -> tuple:
    cid, nic, dev = v["customer_id"], v["nic_no"], v["mobile_device_id"]
    owner = ref.nic_owner.get(nic)
    if owner is not None and owner != cid:
        raise RowError("IDENTITY_CONFLICT", "nic_no already belongs to another customer", "nic_no")
    dev_owner = ref.device_owner.get(dev)
    if dev_owner is not None and dev_owner != cid:
        raise RowError("IDENTITY_CONFLICT", "mobile_device_id already belongs to another customer",
                       "mobile_device_id")
    cust_cols = [cid, v["customer_name"], nic, v["customer_segment"], v["customer_home_city"],
                 v["customer_home_district"], v["customer_home_province"], v["customer_tenure_months"],
                 v["number_of_cards"], v["customer_contact_no"]]
    return (*cust_cols, dev, v["device_type"], v["device_os"], row_hash(cust_cols))


CUSTOMERS = DatasetSpec(
    name="customers", label="Customers", fields=CUSTOMER_FIELDS, stage_table="stg_customers",
    stage_columns=["customer_id", "customer_name", "nic_no", "customer_segment", "customer_home_city",
                   "customer_home_district", "customer_home_province", "customer_tenure_months",
                   "number_of_cards", "customer_contact_no", "mobile_device_id", "device_type", "device_os",
                   "row_hash"],
    target_table="customers", key_columns=["customer_id"], validator=_validate_customer,
    update_columns=["customer_name", "nic_no", "customer_segment", "customer_home_city", "customer_home_district",
                    "customer_home_province", "customer_tenure_months", "number_of_cards", "row_hash"],
    insert_columns=["customer_id", "customer_name", "nic_no", "customer_segment", "customer_home_city",
                    "customer_home_district", "customer_home_province", "customer_tenure_months",
                    "number_of_cards", "customer_contact_no", "row_hash"],
    # Contact numbers are only overwritten when the file actually supplies one.
    update_extra=", t.customer_contact_no = NVL(s.customer_contact_no, t.customer_contact_no)",
    needs=frozenset({"customers", "devices"}), file_hint="customers.csv",
    post_merge=[
        """MERGE INTO customer_devices d
           USING (SELECT mobile_device_id, customer_id, device_type, device_os FROM stg_customers) s
           ON (d.mobile_device_id = s.mobile_device_id)
           WHEN MATCHED THEN UPDATE SET d.device_type = s.device_type, d.device_os = s.device_os,
                d.is_primary = 'Y', d.last_seen_at = SYS_EXTRACT_UTC(SYSTIMESTAMP), d.import_id = :import_id
           WHEN NOT MATCHED THEN INSERT (mobile_device_id, customer_id, device_type, device_os, is_primary, import_id)
                VALUES (s.mobile_device_id, s.customer_id, s.device_type, s.device_os, 'Y', :import_id)""",
        # Keep earlier devices for history but mark them non-primary.
        """UPDATE customer_devices d SET d.is_primary = 'N'
           WHERE d.is_primary = 'Y'
             AND d.customer_id IN (SELECT customer_id FROM stg_customers)
             AND d.mobile_device_id NOT IN (SELECT mobile_device_id FROM stg_customers)""",
    ],
)

# --------------------------------------------------------------------------------------------------
# cards.csv -> CARDS
# --------------------------------------------------------------------------------------------------
CARD_FIELDS = [
    _ident("card_id"), _ident("customer_id"),
    _f("nic_no", required=False, max_bytes=12, pattern=NIC_RE, upper=True),
    _f("card_network", "enum", choices=NETWORKS), _f("card_type", "enum", choices=CARD_TYPES),
    _f("card_tier", "enum", choices=CARD_TIERS),
    _f("card_age_months", "int", min_value=Decimal(0), max_value=Decimal(9999)),
    _f("card_status", "enum", choices=CARD_STATUSES),
]


def _validate_card(v: dict, raw: dict, ref: ReferenceData) -> tuple:
    cust = ref.customers.get(v["customer_id"])
    if cust is None:
        raise RowError("UNKNOWN_CUSTOMER", "customer_id not found (import customers first)", "customer_id")
    if v["nic_no"] is not None and v["nic_no"] != cust[1]:
        raise RowError("IDENTITY_MISMATCH", "nic_no does not match the customer's NIC", "nic_no")
    existing = ref.cards.get(v["card_id"])
    if existing is not None and existing[0] != v["customer_id"]:
        raise RowError("IDENTITY_CONFLICT", "card_id already belongs to another customer", "card_id")
    if v["card_network"] == "AMEX" and v["card_type"] != "CREDIT":
        raise RowError("BAD_VALUE", "AMEX cards must be CREDIT", "card_type")
    cols = [v["card_id"], v["customer_id"], v["card_network"], v["card_type"], v["card_tier"],
            v["card_age_months"], v["card_status"]]
    return (*cols, row_hash(cols))


CARDS = DatasetSpec(
    name="cards", label="Cards", fields=CARD_FIELDS, stage_table="stg_cards",
    stage_columns=["card_id", "customer_id", "card_network", "card_type", "card_tier", "card_age_months",
                   "card_status", "row_hash"],
    target_table="cards", key_columns=["card_id"], validator=_validate_card,
    needs=frozenset({"customers", "cards"}), file_hint="cards.csv",
)

# --------------------------------------------------------------------------------------------------
# merchants.csv -> MERCHANTS
# --------------------------------------------------------------------------------------------------
MERCHANT_FIELDS = [
    _ident("merchant_id"), _f("merchant_brand", max_bytes=100),
    _f("merchant_category", "enum", choices=CATEGORIES),
    _f("merchant_location", max_bytes=200), _f("merchant_city", max_bytes=60),
    _f("merchant_district", max_bytes=40), _f("merchant_province", max_bytes=40),
    _f("merchant_email", required=False, max_bytes=120, pattern=EMAIL_RE),
    _f("merchant_contact_no", required=False, max_bytes=20, pattern=PHONE_RE),
    _f("merchant_type", "enum", choices=frozenset({"PHYSICAL", "ONLINE", "SERVICE"})),
    _f("merchant_offer_supported", "yesno"),
]


def _validate_merchant(v: dict, raw: dict, ref: ReferenceData) -> tuple:
    cols = [v[c] for c in MERCHANTS_COLS]
    return (*cols, row_hash(cols))


MERCHANTS_COLS = ["merchant_id", "merchant_brand", "merchant_category", "merchant_location", "merchant_city",
                  "merchant_district", "merchant_province", "merchant_email", "merchant_contact_no",
                  "merchant_type", "merchant_offer_supported"]
MERCHANTS = DatasetSpec(
    name="merchants", label="Merchants", fields=MERCHANT_FIELDS, stage_table="stg_merchants",
    stage_columns=MERCHANTS_COLS + ["row_hash"], target_table="merchants", key_columns=["merchant_id"],
    validator=_validate_merchant, file_hint="merchants.csv",
)

# --------------------------------------------------------------------------------------------------
# credit_card_transactions_*.csv -> CARD_TRANSACTIONS
# --------------------------------------------------------------------------------------------------
TXN_FIELDS = [
    _ident("transaction_id"), _f("transaction_datetime", "datetime"),
    _ident("customer_id"), _ident("card_id"), _ident("merchant_id"),
    _f("transaction_type", "enum", choices=TXN_TYPES), _f("transaction_channel", "enum", choices=CHANNELS),
    _f("transaction_amount", "decimal", scale=2, precision=14),
    _f("transaction_currency", "str", max_bytes=3, pattern=re.compile(r"^[A-Z]{3}$"), upper=True),
    _f("transaction_status", "enum", choices=TXN_STATUSES),
    _f("is_international", "flag01", required=False),
    _ident("related_transaction_id", required=False),
    _ident("offer_id", required=False),
    _f("offer_utilized", "yesno", required=False),
    # Denormalised attributes: validated against master data when present, never stored.
    _f("customer_name", required=False), _f("nic_no", required=False, upper=True),
    _f("mobile_device_id", required=False), _f("card_network", required=False, upper=True),
    _f("card_type", required=False, upper=True), _f("card_tier", required=False, upper=True),
    _f("merchant_brand", required=False), _f("merchant_category", required=False, upper=True),
]


def _validate_txn(v: dict, raw: dict, ref: ReferenceData) -> tuple:
    cust = ref.customers.get(v["customer_id"])
    if cust is None:
        raise RowError("UNKNOWN_CUSTOMER", "customer_id not found", "customer_id")
    card = ref.cards.get(v["card_id"])
    if card is None:
        raise RowError("UNKNOWN_CARD", "card_id not found", "card_id")
    if card[0] != v["customer_id"]:
        raise RowError("IDENTITY_MISMATCH", "card does not belong to customer_id", "card_id")
    merch = ref.merchants.get(v["merchant_id"])
    if merch is None:
        raise RowError("UNKNOWN_MERCHANT", "merchant_id not found", "merchant_id")
    checks = [("customer_name", cust[0]), ("nic_no", cust[1]), ("card_network", card[1]), ("card_type", card[2]),
              ("card_tier", card[3]), ("merchant_brand", merch[0]), ("merchant_category", merch[1])]
    for col, expected in checks:
        if v.get(col) is not None and v[col] != expected:
            raise RowError("IDENTITY_MISMATCH", f"{col} differs from master data", col)
    dev = v.get("mobile_device_id")
    if dev is not None and ref.device_owner.get(dev) not in (None, v["customer_id"]):
        raise RowError("IDENTITY_MISMATCH", "mobile_device_id belongs to another customer", "mobile_device_id")

    amount: Decimal = v["transaction_amount"]
    ttype = v["transaction_type"]
    if amount < 0:
        if ttype != "REFUND":
            raise RowError("NEGATIVE_AMOUNT", "negative amounts are only accepted for REFUND", "transaction_amount")
        amount = -amount  # direction is carried by transaction_type
    if amount == 0 and ttype in ("PURCHASE", "BILL_PAYMENT", "RECURRING_PAYMENT"):
        raise RowError("ZERO_AMOUNT", "purchase amount must be greater than zero", "transaction_amount")
    if ttype == "REVERSAL" and v["related_transaction_id"] is None:
        raise RowError("MISSING_REFERENCE", "REVERSAL requires related_transaction_id", "related_transaction_id")
    if v["offer_utilized"] == "YES" and v["offer_id"] is None:
        raise RowError("INCONSISTENT", "offer_utilized=YES without offer_id", "offer_id")
    if v["offer_utilized"] == "NO" and v["offer_id"] is not None:
        raise RowError("INCONSISTENT", "offer_id present but offer_utilized=NO", "offer_utilized")

    dt: datetime = v["transaction_datetime"]
    rate = ref.fx_rate(v["transaction_currency"], dt.date())
    amount_lkr = (amount * rate).quantize(Decimal("0.01"))
    intl = v["is_international"] or ("Y" if merch[2] == "INTERNATIONAL" else "N")
    return (v["transaction_id"], dt, v["customer_id"], v["card_id"], v["merchant_id"], ttype,
            v["transaction_channel"], amount, v["transaction_currency"], rate, amount_lkr,
            v["transaction_status"], intl, v["related_transaction_id"], v["offer_id"])


TRANSACTIONS = DatasetSpec(
    name="transactions", label="Card transactions", fields=TXN_FIELDS, stage_table="stg_transactions",
    stage_columns=["transaction_id", "transaction_datetime", "customer_id", "card_id", "merchant_id",
                   "transaction_type", "transaction_channel", "transaction_amount", "transaction_currency",
                   "fx_rate_to_lkr", "amount_lkr", "transaction_status", "is_international",
                   "related_transaction_id", "redeemed_offer_id"],
    target_table="card_transactions", key_columns=["transaction_id"], validator=_validate_txn,
    change_mode="columns", needs=frozenset({"customers", "devices", "cards", "merchants", "fx"}),
    file_hint="credit_card_transactions_3m.csv",
)

# --------------------------------------------------------------------------------------------------
# customer_offer_candidates.csv -> HISTORICAL_OFFER_TARGETING (targeting part)
# --------------------------------------------------------------------------------------------------
TARGETING_FIELDS = [
    _ident("offer_id"), _ident("customer_id"),
    _f("recommended_category", "enum", choices=CATEGORIES), _f("recommended_merchant_brand", max_bytes=100),
    _f("offer_reason", required=False, max_bytes=400),
    _f("preference_score", "decimal", required=False, scale=4, min_value=Decimal(0), max_value=Decimal(1)),
    _f("offer_priority", "enum", required=False, choices=frozenset({"HIGH", "MEDIUM", "LOW"})),
    _f("offer_type", "enum", choices=TARGETING_TYPES),
    _f("offer_status", "enum", choices=frozenset({"GENERATED", "SENT", "EXPIRED", "REDEEMED"})),
    _f("offer_utilized", "yesno"),
]


def _validate_targeting(v: dict, raw: dict, ref: ReferenceData) -> tuple:
    if v["customer_id"] not in ref.customers:
        raise RowError("UNKNOWN_CUSTOMER", "customer_id not found", "customer_id")
    if v["recommended_merchant_brand"] not in ref.brand_categories:
        raise RowError("UNKNOWN_BRAND", "recommended_merchant_brand not in merchant master",
                       "recommended_merchant_brand")
    if (v["offer_status"] == "REDEEMED") != (v["offer_utilized"] == "YES"):
        raise RowError("INCONSISTENT", "offer_status REDEEMED must match offer_utilized YES", "offer_status")
    cols = [v["offer_id"], v["customer_id"], v["recommended_category"], v["recommended_merchant_brand"],
            v["offer_reason"], v["preference_score"], v["offer_priority"], v["offer_type"], v["offer_status"],
            v["offer_utilized"]]
    return (*cols, row_hash(cols))


OFFER_TARGETING = DatasetSpec(
    name="offer_targeting", label="Historical offer targeting", fields=TARGETING_FIELDS,
    stage_table="stg_offer_targeting",
    stage_columns=["offer_id", "customer_id", "recommended_category", "recommended_merchant_brand", "offer_reason",
                   "preference_score", "offer_priority", "targeting_type", "offer_status", "offer_utilized",
                   "row_hash"],
    target_table="historical_offer_targeting", key_columns=["offer_id"], validator=_validate_targeting,
    needs=frozenset({"customers", "merchants"}), file_hint="customer_offer_candidates.csv",
)

# --------------------------------------------------------------------------------------------------
# customer_offer_utilization.csv -> HISTORICAL_OFFER_TARGETING (outcome part)
# --------------------------------------------------------------------------------------------------
OUTCOME_FIELDS = [
    _ident("offer_id"), _ident("customer_id"), _ident("merchant_id"),
    _f("merchant_brand", required=False), _f("merchant_category", required=False, upper=True),
    _f("offer_type", "enum", required=False, choices=TARGETING_TYPES),
    _f("offer_start_date", "date"), _f("offer_end_date", "date"),
    _f("offer_received", "yesno"), _f("offer_redeemed", "yesno", required=False), _f("offer_utilized", "yesno"),
    _f("utilization_date", "datetime", required=False), _ident("utilization_transaction_id", required=False),
    _f("utilization_amount", "decimal", required=False, scale=2, precision=14, min_value=Decimal(0)),
]


def _validate_outcome(v: dict, raw: dict, ref: ReferenceData) -> tuple:
    oid = v["offer_id"]
    if oid not in ref.targeting_offers:
        raise RowError("UNKNOWN_OFFER", "offer_id not found (import offer targeting first)", "offer_id")
    if ref.targeting_customer.get(oid) != v["customer_id"]:
        raise RowError("IDENTITY_MISMATCH", "customer_id differs from the targeting record", "customer_id")
    merch = ref.merchants.get(v["merchant_id"])
    if merch is None:
        raise RowError("UNKNOWN_MERCHANT", "merchant_id not found", "merchant_id")
    if v["offer_end_date"] < v["offer_start_date"]:
        raise RowError("BAD_DATE_RANGE", "offer_end_date before offer_start_date", "offer_end_date")
    used = v["offer_utilized"] == "YES"
    if v["offer_redeemed"] is not None and v["offer_redeemed"] != v["offer_utilized"]:
        raise RowError("INCONSISTENT", "offer_redeemed differs from offer_utilized", "offer_redeemed")
    if used and (v["utilization_transaction_id"] is None or v["utilization_date"] is None):
        raise RowError("INCONSISTENT", "utilised offer lacks utilization transaction/date", "utilization_transaction_id")
    if not used and v["utilization_transaction_id"] is not None:
        raise RowError("INCONSISTENT", "unutilised offer has a utilization transaction", "utilization_transaction_id")
    return (oid, v["merchant_id"], v["offer_start_date"], v["offer_end_date"], v["offer_received"],
            v["offer_utilized"], v["utilization_date"], v["utilization_transaction_id"], v["utilization_amount"])


OFFER_OUTCOMES = DatasetSpec(
    name="offer_outcomes", label="Historical offer outcomes", fields=OUTCOME_FIELDS, stage_table="stg_offer_outcomes",
    stage_columns=["offer_id", "merchant_id", "offer_start_date", "offer_end_date", "offer_received", "offer_utilized",
                   "utilization_date", "utilization_transaction_id", "utilization_amount"],
    target_table="historical_offer_targeting", key_columns=["offer_id"], validator=_validate_outcome,
    change_mode="columns", merge_insert=False, needs=frozenset({"targeting", "merchants"}),
    file_hint="customer_offer_utilization.csv",
)

# --------------------------------------------------------------------------------------------------
# Offer catalog CSV -> OFFERS (always lands as PENDING_APPROVAL)
# --------------------------------------------------------------------------------------------------
LOCATION_TOKEN_RE = re.compile(r"^(DISTRICT|PROVINCE):.+$|^ONLINE$")
OFFER_FIELDS = [
    _f("offer_id", "id", max_bytes=30), _f("offer_title", max_bytes=150), _f("offer_description", max_bytes=1000),
    _f("merchant_brand", required=False, max_bytes=100), _ident("merchant_id", required=False),
    _f("merchant_category", "enum", choices=CATEGORIES), _f("offer_type", "enum", choices=OFFER_TYPES),
    _f("offer_value", "decimal", scale=2, precision=12, min_value=Decimal("0.01")),
    _f("currency", "str", required=False, max_bytes=3, upper=True, pattern=re.compile(r"^[A-Z]{3}$")),
    _f("minimum_spend", "decimal", required=False, scale=2, precision=14, min_value=Decimal(0)),
    _f("maximum_benefit", "decimal", required=False, scale=2, precision=14, min_value=Decimal("0.01")),
    _f("valid_from", "date"), _f("valid_to", "date"),
    _f("eligible_card_networks", "list", required=False, max_bytes=100, list_choices=NETWORKS),
    _f("eligible_card_types", "list", required=False, max_bytes=30, list_choices=CARD_TYPES),
    _f("eligible_card_tiers", "list", required=False, max_bytes=100, list_choices=CARD_TIERS),
    _f("eligible_locations", required=False, max_bytes=1000),
    _f("redemption_limit_per_customer", "int", required=False, min_value=Decimal(1), max_value=Decimal(999999)),
    _f("total_redemption_limit", "int", required=False, min_value=Decimal(1), max_value=Decimal(9999999999)),
    _f("campaign_budget", "decimal", required=False, scale=2, precision=16, min_value=Decimal("0.01")),
    _f("terms_and_conditions", max_bytes=32000),
    _f("is_sample", "yesno", required=False),
]


def normalise_locations(raw: str | None, ref: ReferenceData) -> str:
    """'ALL' or ';'-joined tokens DISTRICT:<name> / PROVINCE:<name> / ONLINE with names checked."""
    if raw is None or raw.strip().upper() in ("", "ALL"):
        return "ALL"
    out = []
    for tok in [t.strip() for t in re.split(r"[;|]", raw) if t.strip()]:
        kind, _, name = tok.partition(":")
        kind = kind.strip().upper()
        name = name.strip()
        if kind == "ONLINE" and not name:
            out.append("ONLINE")
        elif kind == "DISTRICT" and name in ref.districts:
            out.append(f"DISTRICT:{name}")
        elif kind == "PROVINCE" and name in ref.provinces:
            out.append(f"PROVINCE:{name}")
        else:
            raise RowError("BAD_LOCATION", f"eligible_locations entry '{tok[:40]}' is not a known "
                           "DISTRICT:<name>, PROVINCE:<name> or ONLINE", "eligible_locations")
    return ";".join(dict.fromkeys(out))


def _validate_offer(v: dict, raw: dict, ref: ReferenceData) -> tuple:
    brand, mid, cat = v["merchant_brand"], v["merchant_id"], v["merchant_category"]
    if brand is None and mid is None:
        raise RowError("REQUIRED", "merchant_brand or merchant_id is required", "merchant_brand")
    if mid is not None:
        m = ref.merchants.get(mid)
        if m is None:
            raise RowError("UNKNOWN_MERCHANT", "merchant_id not found", "merchant_id")
        if brand is not None and m[0] != brand:
            raise RowError("INCONSISTENT", "merchant_id does not belong to merchant_brand", "merchant_id")
        brand = brand or m[0]
        if m[3] != "YES":
            raise RowError("MERCHANT_NOT_PARTICIPATING", "merchant does not support bank offers", "merchant_id")
    if brand not in ref.brand_categories:
        raise RowError("UNKNOWN_BRAND", "merchant_brand not in merchant master", "merchant_brand")
    if cat not in ref.brand_categories[brand]:
        raise RowError("INCONSISTENT", f"brand is not in category {cat}", "merchant_category")
    if v["offer_type"].startswith("PERCENT") and v["offer_value"] > 100:
        raise RowError("OUT_OF_RANGE", "percentage offer_value must be <= 100", "offer_value")
    if v["valid_to"] < v["valid_from"]:
        raise RowError("BAD_DATE_RANGE", "valid_to before valid_from", "valid_to")
    currency = v["currency"] or "LKR"
    if currency != "LKR":
        # Savings are estimated from LKR spend; multi-currency offers would need a documented conversion policy.
        raise RowError("UNSUPPORTED_CURRENCY", "only LKR offers are supported", "currency")
    locations = normalise_locations(v["eligible_locations"], ref)
    cols = [v["offer_id"], v["offer_title"], v["offer_description"], v["merchant_brand"], mid, cat, v["offer_type"],
            v["offer_value"], currency, v["minimum_spend"] or Decimal(0), v["maximum_benefit"], v["valid_from"],
            v["valid_to"], v["eligible_card_networks"] or "ALL", v["eligible_card_types"] or "ALL",
            v["eligible_card_tiers"] or "ALL", locations, v["redemption_limit_per_customer"],
            v["total_redemption_limit"], v["campaign_budget"], v["terms_and_conditions"],
            "Y" if v["is_sample"] == "YES" else "N"]
    return (*cols, row_hash(cols))


OFFER_COLS = ["offer_id", "offer_title", "offer_description", "merchant_brand", "merchant_id", "merchant_category",
              "offer_type", "offer_value", "currency", "minimum_spend", "maximum_benefit", "valid_from", "valid_to",
              "eligible_card_networks", "eligible_card_types", "eligible_card_tiers", "eligible_locations",
              "redemption_limit_per_customer", "total_redemption_limit", "campaign_budget", "terms_and_conditions",
              "is_sample"]
OFFERS = DatasetSpec(
    name="offers", label="Offer catalog", fields=OFFER_FIELDS, stage_table="stg_offers",
    stage_columns=OFFER_COLS + ["row_hash"], target_table="offers", key_columns=["offer_id"],
    validator=_validate_offer, needs=frozenset({"merchants", "geo"}),
    # Any change to an offer's terms sends it back for approval (a revoked offer stays revoked).
    update_extra=(", t.status = CASE WHEN t.status = 'REVOKED' THEN t.status ELSE 'PENDING_APPROVAL' END,"
                  " t.approved_by = NULL, t.approved_at = NULL, t.version = t.version + 1,"
                  " t.updated_by = :username"),
    insert_extra_cols=", status, created_by, updated_by",
    insert_extra_vals=", 'PENDING_APPROVAL', :username, :username",
    file_hint="offer_catalog.csv",
)

# --------------------------------------------------------------------------------------------------
# Eligibility rules CSV -> OFFER_ELIGIBILITY_RULES (one rule per offer and rule_type)
# --------------------------------------------------------------------------------------------------
RULE_FIELDS = [
    _f("offer_id", "id", max_bytes=30), _f("rule_type", "enum", choices=RULE_TYPES),
    _f("operator", "enum", choices=frozenset({"IN", "NOT_IN", "GTE", "LTE", "IS_TRUE"})),
    _f("rule_value", required=False, max_bytes=400), _f("description", required=False, max_bytes=400),
    _f("is_active", "yesno", required=False),
]


def validate_rule_value(rule_type: str, operator: str, value: str | None) -> str | None:
    if operator not in RULE_OPERATORS[rule_type]:
        raise RowError("BAD_OPERATOR", f"{rule_type} supports {sorted(RULE_OPERATORS[rule_type])}", "operator")
    if operator == "IS_TRUE":
        return None
    if value is None or not value.strip():
        raise RowError("REQUIRED", "rule_value is required", "rule_value")
    if operator in ("GTE", "LTE"):
        try:
            Decimal(value)
        except Exception:
            raise RowError("NOT_A_NUMBER", "rule_value must be numeric", "rule_value") from None
        return value.strip()
    items = [p.strip() for p in re.split(r"[;,|]", value) if p.strip()]
    allowed = {"CARD_NETWORK": NETWORKS, "CARD_TYPE": CARD_TYPES, "CARD_TIER": CARD_TIERS}.get(rule_type)
    if allowed is not None:
        items = [i.upper() for i in items]
        bad = [i for i in items if i not in allowed]
        if bad:
            raise RowError("BAD_VALUE", f"invalid values {bad[:5]}", "rule_value")
    if rule_type == "CUSTOMER_SEGMENT":
        items = [i.upper() for i in items]
    return ";".join(items)


def _validate_rule(v: dict, raw: dict, ref: ReferenceData) -> tuple:
    if v["offer_id"] not in ref.catalog_offers:
        raise RowError("UNKNOWN_OFFER", "offer_id not in offer catalog", "offer_id")
    value = validate_rule_value(v["rule_type"], v["operator"], v["rule_value"])
    cols = [v["offer_id"], v["rule_type"], v["operator"], value, v["description"],
            "N" if v["is_active"] == "NO" else "Y"]
    return (*cols, row_hash(cols))


ELIGIBILITY_RULES = DatasetSpec(
    name="eligibility_rules", label="Offer eligibility rules", fields=RULE_FIELDS, stage_table="stg_eligibility_rules",
    stage_columns=["offer_id", "rule_type", "operator", "rule_value", "description", "is_active", "row_hash"],
    target_table="offer_eligibility_rules", key_columns=["offer_id", "rule_type"], validator=_validate_rule,
    needs=frozenset({"catalog"}), insert_extra_cols=", created_by", insert_extra_vals=", :username",
    file_hint="offer_eligibility_rules.csv",
)

DATASETS: dict[str, DatasetSpec] = {s.name: s for s in (CUSTOMERS, CARDS, MERCHANTS, TRANSACTIONS, OFFER_TARGETING,
                                                         OFFER_OUTCOMES, OFFERS, ELIGIBILITY_RULES)}
