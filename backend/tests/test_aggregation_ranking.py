"""Net spend, windows, customer-merchant aggregates, ranking, diversity, savings, metrics and generation."""
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from app.ml.features import build_features
from app.ml.generate import customer_merchant_aggregates, generate
from app.ml.model import RecModel, baseline_model, estimate_saving, select_diverse
from app.ml.spend import net_spend_frame
from app.ml.training import ranking_metrics
from app.ml.windows import add_months, compute_windows

# ------------------------------------------------------------------------------------------------
# windows
# ------------------------------------------------------------------------------------------------


def test_aggregation_period_is_three_calendar_months_to_cutoff():
    w = compute_windows(date(2026, 10, 12))
    assert (w.aggregation_start, w.aggregation_end, w.window_end) == (date(2026, 7, 13), date(2026, 10, 12),
                                                                      date(2026, 10, 13))
    assert w.window_start == date(2025, 10, 13)


def test_add_months_clamps_day():
    assert add_months(date(2026, 5, 31), -3) == date(2026, 2, 28)
    assert add_months(date(2024, 3, 31), -1) == date(2024, 2, 29)
    assert add_months(date(2026, 1, 15), -12) == date(2025, 1, 15)


# ------------------------------------------------------------------------------------------------
# net spend
# ------------------------------------------------------------------------------------------------
def txn(i, cust, merch, dt, amt, ttype="PURCHASE", status="APPROVED", related=None, card="K1"):
    return {"transaction_id": f"T{i}", "transaction_datetime": pd.Timestamp(dt), "customer_id": cust, "card_id": card,
            "merchant_id": merch, "amount_lkr": amt, "transaction_type": ttype, "transaction_status": status,
            "related_transaction_id": related}


def test_net_spend_rules():
    df = pd.DataFrame([
        txn(1, "C1", "M1", "2026-09-01", 1000),
        txn(2, "C1", "M1", "2026-09-02", 500, status="DECLINED"),
        txn(3, "C1", "M1", "2026-09-03", 200, ttype="REFUND"),
        txn(4, "C1", "M1", "2026-09-04", 700),
        txn(5, "C1", "M1", "2026-09-05", 700, ttype="REVERSAL", related="T4"),
        txn(6, "C1", "M1", "2026-09-06", 300, status="CANCELLED"),
    ])
    s = net_spend_frame(df)
    assert set(s["transaction_id"]) == {"T1", "T3"}
    assert s["signed_amount"].sum() == 800 and s["txn_count"].sum() == 1


def test_customer_merchant_aggregates_respect_period_boundaries():
    w = compute_windows(date(2026, 10, 12))
    df = pd.DataFrame([
        txn(1, "C1", "M1", "2026-07-12 23:59:59", 100),   # day before aggregation start: outside 3m
        txn(2, "C1", "M1", "2026-07-13 00:00:00", 200),   # first second of the period
        txn(3, "C1", "M1", "2026-10-12 23:59:59", 300),   # last second of the cutoff day
        txn(4, "C1", "M1", "2026-10-13 00:00:00", 999),   # after cutoff: excluded everywhere
        txn(5, "C1", "M1", "2026-09-01", 50, ttype="REFUND"),
        txn(6, "C1", "M2", "2026-08-01", 40),
    ])
    agg = customer_merchant_aggregates(net_spend_frame(df), w)
    c3, a3, last, avg, cnt = agg[("C1", "M1")]
    assert (c3, a3) == (2, 450.0)                  # 200 + 300 - 50 refund
    assert last == date(2026, 10, 12) and cnt == 3 and avg == pytest.approx(200.0)
    assert agg[("C1", "M2")][:2] == (1, 40.0)


# ------------------------------------------------------------------------------------------------
# ranking helpers
# ------------------------------------------------------------------------------------------------
def test_diversity_one_per_brand_max_two_per_category():
    scores = [0.9, 0.89, 0.88, 0.87, 0.5]
    brands = ["A", "A", "B", "C", "D"]
    cats = ["GROCERY", "GROCERY", "GROCERY", "GROCERY", "FUEL"]
    chosen = select_diverse(scores, brands, cats, depth=3)
    assert [brands[i] for i in chosen] == ["A", "B", "D"]


def test_category_penalty_prefers_variety_when_scores_are_close():
    chosen = select_diverse([0.80, 0.79, 0.75], ["A", "B", "C"], ["X", "X", "Y"], depth=2)
    assert [i for i in chosen] == [0, 2]


@pytest.mark.parametrize("args,expected", [
    (("PERCENT_DISCOUNT", 10, 5000, 1500, 8000), 800.0),
    (("PERCENT_DISCOUNT", 10, 5000, 500, 8000), 500.0),           # capped
    (("PERCENT_CASHBACK", 10, 5000, None, 2000), 500.0),          # below minimum spend -> min spend assumed
    (("FIXED_DISCOUNT", 750, 7500, 750, 9000), 750.0),
])
def test_estimated_saving(args, expected):
    saving, note = estimate_saving(*args, "your average")
    assert saving == expected and "Not a guaranteed amount" in note


def test_ranking_metrics_hand_computed():
    top = np.array([[0, 1, 2], [3, 4, 5], [6, 7, 8]])
    truth = [{0, 2}, {9}, set()]
    m = ranking_metrics(top, truth)
    assert m["evaluated_customers"] == 2
    assert m["precision_at_3"] == pytest.approx((2 / 3 + 0) / 2, abs=1e-4)
    assert m["recall_at_3"] == pytest.approx(0.5, abs=1e-4)
    ndcg1 = (1 + 1 / np.log2(4)) / (1 + 1 / np.log2(3))
    assert m["ndcg_at_3"] == pytest.approx(ndcg1 / 2, abs=1e-4)


# ------------------------------------------------------------------------------------------------
# end-to-end generation on a tiny dataset
# ------------------------------------------------------------------------------------------------
@pytest.fixture
def world():
    merchants = pd.DataFrame([
        ("M1", "Keells Super", "GROCERY", "Colombo", "Colombo", "Western", "PHYSICAL", "YES", "Y"),
        ("M2", "Cargills", "GROCERY", "Kandy", "Kandy", "Central", "PHYSICAL", "YES", "Y"),
        ("M3", "Pizza Hut", "RESTAURANT", "Colombo", "Colombo", "Western", "PHYSICAL", "YES", "Y"),
        ("M4", "Lanka IOC", "FUEL", "Colombo", "Colombo", "Western", "PHYSICAL", "YES", "Y"),
        ("M5", "Daraz", "E-COMMERCE", "Colombo", "Colombo", "Western", "ONLINE", "YES", "Y"),
        ("M6", "Sathosa", "GROCERY", "Colombo", "Colombo", "Western", "PHYSICAL", "NO", "Y"),
        ("M7", "New Brand", "RETAIL", "Colombo", "Colombo", "Western", "PHYSICAL", "YES", "Y"),
    ], columns=["merchant_id", "merchant_brand", "merchant_category", "merchant_city", "merchant_district",
                "merchant_province", "merchant_type", "merchant_offer_supported", "is_active"])
    customers = pd.DataFrame([
        ("C1", "SALARIED", "Colombo", "Colombo", "Western", 24),
        ("C2", "SALARIED", "Colombo", "Colombo", "Western", 12),
        ("C3", "STUDENT", "Kandy", "Kandy", "Central", 3),    # no history -> cold start
    ], columns=["customer_id", "customer_segment", "customer_home_city", "customer_home_district",
                "customer_home_province", "customer_tenure_months"])
    cards = pd.DataFrame([("K1", "C1", "VISA", "CREDIT", "GOLD", "ACTIVE"), ("K2", "C1", "MASTERCARD", "DEBIT", "CLASSIC", "ACTIVE"),
                          ("K3", "C2", "MASTERCARD", "DEBIT", "CLASSIC", "ACTIVE"), ("K4", "C3", "VISA", "DEBIT", "CLASSIC", "ACTIVE")],
                         columns=["card_id", "customer_id", "card_network", "card_type", "card_tier", "card_status"])
    rows, i = [], 0
    for d in pd.date_range("2026-07-01", "2026-10-10", freq="5D"):
        for cust, merch, amt, card in (("C1", "M1", 6000, "K2"), ("C1", "M3", 3500, "K1"), ("C2", "M1", 4000, "K3"),
                                       ("C2", "M4", 5000, "K3")):
            i += 1
            rows.append(txn(i, cust, merch, d + pd.Timedelta(hours=10), amt, card=card))
    spend = net_spend_frame(pd.DataFrame(rows))
    return merchants, customers, cards, spend


def make_offer(oid, brand, cat, **kw):
    o = {"offer_id": oid, "offer_title": oid, "offer_description": oid, "merchant_brand": brand, "merchant_id": None,
         "merchant_category": cat, "offer_type": "PERCENT_DISCOUNT", "offer_value": 10, "currency": "LKR",
         "minimum_spend": 1000, "maximum_benefit": 2000, "valid_from": date(2026, 10, 1), "valid_to": date(2027, 1, 31),
         "eligible_card_networks": "ALL", "eligible_card_types": "ALL", "eligible_card_tiers": "ALL",
         "eligible_locations": "ALL", "redemption_limit_per_customer": None, "total_redemption_limit": None,
         "campaign_budget": None, "budget_consumed": 0, "terms_and_conditions": "t", "status": "APPROVED",
         "is_sample": "Y"}
    o.update(kw)
    return o


def run_generate(world, offers, model=None, rules=None):
    merchants, customers, cards, spend = world
    w = compute_windows(date(2026, 10, 12))
    fs = build_features(spend, customers, merchants, as_of=w.window_end_dt, window_start=w.window_start_dt)
    return generate(fs, spend, w, customers, cards, merchants, offers, rules or {}, {}, {}, set(),
                    model or RecModel(version="t", algorithm="t"), date(2026, 10, 26))


def test_generation_ranks_affinity_and_never_returns_ineligible(world):
    offers = [make_offer("KEELLS", "Keells Super", "GROCERY"), make_offer("PIZZA", "Pizza Hut", "RESTAURANT"),
              make_offer("IOC", "Lanka IOC", "FUEL"), make_offer("DARAZ", "Daraz", "E-COMMERCE"),
              make_offer("EXPIRED", "Cargills", "GROCERY", valid_to=date(2026, 9, 30)),
              make_offer("AMEXONLY", "Keells Super", "GROCERY", eligible_card_networks="AMEX", offer_id="AMEXONLY"),
              make_offer("NOTPART", "Sathosa", "GROCERY")]
    res = run_generate(world, offers)
    recs = res.recommendations
    assert not set(recs["offer_id"]) & {"EXPIRED", "AMEXONLY", "NOTPART"}
    c1 = recs[recs.customer_id == "C1"].sort_values("candidate_rank")
    assert list(c1["offer_id"][:2]) == ["KEELLS", "PIZZA"] or list(c1["offer_id"][:2]) == ["PIZZA", "KEELLS"]
    assert c1["recommendation_reason"].iloc[0].startswith("You frequently shop at")
    # card choice follows usage: Keells is paid with the debit card K2, Pizza Hut with K1
    assert dict(zip(c1.offer_id, c1.card_id))["KEELLS"] == "K2"
    assert dict(zip(c1.offer_id, c1.card_id))["PIZZA"] == "K1"
    # brands distinct per customer
    assert recs.groupby("customer_id")["offer_id"].apply(lambda s: s.is_unique).all()


def test_cold_start_customer_gets_popularity_fallback(world):
    res = run_generate(world, [make_offer("KEELLS", "Keells Super", "GROCERY"), make_offer("DARAZ", "Daraz", "E-COMMERCE")])
    st = res.statuses.set_index("customer_id")
    assert st.at["C3", "history_level"] == "NONE"
    c3 = res.recommendations[res.recommendations.customer_id == "C3"]
    assert len(c3) and all("popular" in r for r in c3["recommendation_reason"])


def test_new_merchant_without_history_can_be_recommended(world):
    res = run_generate(world, [make_offer("NEW", "New Brand", "RETAIL")])
    assert "NEW" in set(res.recommendations["offer_id"])


def test_shortfall_reason_when_fewer_than_three(world):
    offers = [make_offer("KEELLS", "Keells Super", "GROCERY"),
              make_offer("CREDITONLY", "Pizza Hut", "RESTAURANT", eligible_card_types="CREDIT")]
    res = run_generate(world, offers)
    st = res.statuses.set_index("customer_id")
    assert st.at["C2", "status"] == "PARTIAL"
    assert "card network/type/tier (1)" in st.at["C2", "shortfall_reason"]
    assert "only 2 approved offers are currently live" in st.at["C2", "shortfall_reason"]


def test_no_live_offers_gives_none_status(world):
    res = run_generate(world, [make_offer("FUTURE", "Daraz", "E-COMMERCE", valid_from=date(2027, 1, 1))])
    assert set(res.statuses["status"]) == {"NONE"}
    assert res.statuses["shortfall_reason"].iloc[0] == "No approved offers are currently live in the catalog."


def test_location_restricted_offer_reaches_matching_customers_only(world):
    res = run_generate(world, [make_offer("KANDY", "Cargills", "GROCERY", eligible_locations="DISTRICT:Kandy")])
    assert set(res.recommendations["customer_id"]) == {"C3"}


def test_rules_baseline_model_runs(world):
    res = run_generate(world, [make_offer("KEELLS", "Keells Super", "GROCERY")], model=baseline_model())
    assert len(res.recommendations) == 3


def test_saving_vector_matches_stored_scalar(world):
    res = run_generate(world, [make_offer("KEELLS", "Keells Super", "GROCERY", offer_value=10, maximum_benefit=450)])
    r = res.recommendations.set_index("customer_id")
    assert r.at["C1", "estimated_customer_saving"] == 450.0          # 10% of 6000 capped at 450
    assert "capped at LKR 450" in r.at["C1", "saving_assumption"]
