"""Oracle extracts used by training, generation and evaluation (all queries use bind variables)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import oracledb
import pandas as pd

from .. import db


def data_cutoff(conn: oracledb.Connection) -> date | None:
    v = db.scalar(conn, "SELECT MAX(transaction_datetime) FROM card_transactions")
    return None if v is None else v.date()


def _frame(conn: oracledb.Connection, sql: str, params: dict | None = None, arraysize: int = 50_000) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.arraysize = arraysize
        cur.prefetchrows = arraysize
        cur.execute(sql, params or {})
        cols = [d[0].lower() for d in cur.description]
        chunks = []
        while True:
            rows = cur.fetchmany()
            if not rows:
                break
            chunks.append(pd.DataFrame.from_records(rows, columns=cols))
    return pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame(columns=cols)


def load_transactions(conn: oracledb.Connection, start: datetime, end: datetime) -> pd.DataFrame:
    df = _frame(conn, """
        SELECT transaction_id, transaction_datetime, customer_id, card_id, merchant_id, amount_lkr,
               transaction_type, transaction_status, related_transaction_id
        FROM card_transactions
        WHERE transaction_datetime >= :s AND transaction_datetime < :e""", {"s": start, "e": end})
    for c in ("customer_id", "card_id", "merchant_id", "transaction_type", "transaction_status"):
        df[c] = df[c].astype("category")
    df["amount_lkr"] = df["amount_lkr"].astype("float64")
    df["transaction_datetime"] = pd.to_datetime(df["transaction_datetime"])
    return df


def load_customers(conn) -> pd.DataFrame:
    return _frame(conn, """SELECT customer_id, customer_segment, customer_home_city, customer_home_district,
                                  customer_home_province, customer_tenure_months
                           FROM customers ORDER BY customer_id""")


def load_merchants(conn) -> pd.DataFrame:
    df = _frame(conn, """SELECT merchant_id, merchant_brand, merchant_category, merchant_city, merchant_district,
                                merchant_province, merchant_type, merchant_offer_supported, is_active
                         FROM merchants""")
    df["merchant_offer_supported"] = df["merchant_offer_supported"].str.strip()
    return df


def load_cards(conn) -> pd.DataFrame:
    return _frame(conn, "SELECT card_id, customer_id, card_network, card_type, card_tier, card_status FROM cards")


def load_offers(conn, statuses: tuple[str, ...] = ("APPROVED",)) -> list[dict]:
    binds = {f"s{i}": s for i, s in enumerate(statuses)}
    return db.fetch_all(conn, f"""
        SELECT offer_id, offer_title, offer_description, merchant_brand, merchant_id, merchant_category, offer_type,
               offer_value, currency, minimum_spend, maximum_benefit, valid_from, valid_to, eligible_card_networks,
               eligible_card_types, eligible_card_tiers, eligible_locations, redemption_limit_per_customer,
               total_redemption_limit, campaign_budget, budget_consumed, terms_and_conditions, status, is_sample
        FROM offers WHERE status IN ({', '.join(':' + k for k in binds)})""", binds)


def load_rules(conn) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in db.fetch_all(conn, """SELECT offer_id, rule_type, operator, rule_value, is_active
                                   FROM offer_eligibility_rules WHERE is_active = 'Y'"""):
        out.setdefault(r["offer_id"], []).append(r)
    return out


def load_event_stats(conn, suppression_days: int, now: datetime):
    """Returns (customer redemptions {(cust, offer): n}, offer totals {offer: n}, dismissed {(cust, offer)})."""
    per_cust: dict[tuple[str, str], int] = {}
    totals: dict[str, int] = {}
    for r in db.fetch_all(conn, """SELECT customer_id, offer_id, COUNT(*) n FROM offer_events
                                   WHERE event_type = 'REDEMPTION' GROUP BY customer_id, offer_id""",
                          arraysize=10000):
        per_cust[(r["customer_id"], r["offer_id"])] = int(r["n"])
        totals[r["offer_id"]] = totals.get(r["offer_id"], 0) + int(r["n"])
    dismissed = {(r["customer_id"], r["offer_id"]) for r in db.fetch_all(conn, """
        SELECT DISTINCT customer_id, offer_id FROM offer_events
        WHERE event_type = 'DISMISSAL' AND occurred_at >= :since""",
        {"since": now - timedelta(days=suppression_days)}, arraysize=10000)}
    return per_cust, totals, dismissed


def load_labels(conn) -> pd.DataFrame:
    """Historical deliveries with outcome. Rows without delivery dates (GENERATED candidates) are excluded."""
    df = _frame(conn, """
        SELECT h.offer_id, h.customer_id, h.recommended_merchant_brand AS brand, h.recommended_category AS category,
               h.offer_start_date, h.offer_end_date, h.offer_utilized, h.offer_status
        FROM historical_offer_targeting h
        WHERE h.offer_received = 'YES' AND h.offer_start_date IS NOT NULL""")
    if not df.empty:
        df["offer_start_date"] = pd.to_datetime(df["offer_start_date"])
        df["offer_end_date"] = pd.to_datetime(df["offer_end_date"])
        df["label"] = (df["offer_utilized"].str.strip() == "YES").astype(int)
    return df
