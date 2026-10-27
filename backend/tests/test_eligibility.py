from datetime import date

import pytest

from app.ml.eligibility import (Card, CustomerContext, OfferStats, customer_level_failures, evaluate,
                                offer_level_failures)

TODAY = date(2026, 10, 26)
VISA_C = Card("C1", "VISA", "CREDIT", "GOLD")
MC_D = Card("C2", "MASTERCARD", "DEBIT", "CLASSIC")
BLOCKED_AMEX = Card("C3", "AMEX", "CREDIT", "PLATINUM", "BLOCKED")


def offer(**kw):
    o = {"offer_id": "O1", "status": "APPROVED", "valid_from": date(2026, 10, 1), "valid_to": date(2027, 1, 31),
         "merchant_brand": "Keells Super", "merchant_id": None, "eligible_card_networks": "ALL",
         "eligible_card_types": "ALL", "eligible_card_tiers": "ALL", "eligible_locations": "ALL",
         "redemption_limit_per_customer": None, "total_redemption_limit": None, "campaign_budget": None,
         "budget_consumed": 0}
    o.update(kw)
    return o


def ctx(**kw):
    c = CustomerContext("CU1", "SALARIED", "Gampaha", "Western", 24, [VISA_C, MC_D, BLOCKED_AMEX], 60000.0,
                        {"Keells Super"})
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def check(o, c=None, rules=(), stats=None, brands=frozenset({"Keells Super"}), outlets=frozenset({"M1"})):
    return evaluate(o, list(rules), c or ctx(), TODAY, stats or OfferStats(), set(brands), set(outlets))


def test_eligible_offer_returns_matching_active_cards_only():
    r = check(offer())
    assert r.eligible and {c.card_id for c in r.cards} == {"C1", "C2"}


@pytest.mark.parametrize("kw,reason", [
    ({"status": "PENDING_APPROVAL"}, "offer status is PENDING_APPROVAL"),
    ({"status": "REVOKED"}, "offer status is REVOKED"),
    ({"valid_to": date(2026, 10, 25)}, "offer expired on 2026-10-25"),
    ({"valid_from": date(2026, 10, 27)}, "offer not valid until 2026-10-27"),
])
def test_status_and_validity(kw, reason):
    r = check(offer(**kw))
    assert not r.eligible and reason in r.failures


def test_validity_is_inclusive_of_both_ends():
    assert check(offer(valid_from=TODAY, valid_to=TODAY)).eligible


def test_card_network_type_tier_filters():
    assert [c.card_id for c in check(offer(eligible_card_networks="VISA")).cards] == ["C1"]
    assert [c.card_id for c in check(offer(eligible_card_types="DEBIT")).cards] == ["C2"]
    r = check(offer(eligible_card_networks="AMEX"))   # only AMEX card is BLOCKED
    assert not r.eligible and "no active card" in r.failures[0]
    assert not check(offer(eligible_card_tiers="PLATINUM;PREMIUM")).eligible


def test_locations():
    assert check(offer(eligible_locations="PROVINCE:Western")).eligible
    assert check(offer(eligible_locations="DISTRICT:Gampaha;DISTRICT:Colombo")).eligible
    assert check(offer(eligible_locations="ONLINE")).eligible
    assert not check(offer(eligible_locations="DISTRICT:Kandy")).eligible


def test_location_is_customer_level_not_offer_level():
    # Regression: a location-restricted offer must not be excluded globally before customers are checked.
    o = offer(eligible_locations="DISTRICT:Gampaha")
    assert offer_level_failures(o, TODAY, OfferStats(), {"Keells Super"}, set(), "Keells Super") == []
    fails, _ = customer_level_failures(o, [], ctx(home_district="Kandy", home_province="Central"), "Keells Super")
    assert fails == ["customer location is outside the offer's eligible locations"]


def test_participation():
    assert not check(offer(), brands=set()).eligible
    assert not check(offer(merchant_id="M9", merchant_brand=None), outlets={"M1"}).eligible
    assert check(offer(merchant_id="M1", merchant_brand=None), outlets={"M1"}).eligible


def test_limits_budget_and_dismissal():
    assert not check(offer(redemption_limit_per_customer=2), ctx(redemptions={"O1": 2})).eligible
    assert check(offer(redemption_limit_per_customer=2), ctx(redemptions={"O1": 1})).eligible
    assert not check(offer(total_redemption_limit=10), stats=OfferStats(10)).eligible
    assert not check(offer(campaign_budget=1000, budget_consumed=1000)).eligible
    assert not check(offer(), ctx(dismissed={"O1"})).eligible


def rule(t, op, v=None):
    return {"rule_type": t, "operator": op, "rule_value": v, "is_active": "Y"}


@pytest.mark.parametrize("r,expected", [
    (rule("CUSTOMER_SEGMENT", "IN", "SALARIED;AFFLUENT"), True),
    (rule("CUSTOMER_SEGMENT", "NOT_IN", "SALARIED"), False),
    (rule("HOME_DISTRICT", "IN", "Colombo"), False),
    (rule("HOME_PROVINCE", "IN", "Western"), True),
    (rule("MIN_TENURE_MONTHS", "GTE", "25"), False),
    (rule("MIN_TENURE_MONTHS", "GTE", "24"), True),
    (rule("MIN_SPEND_LAST_90D_LKR", "GTE", "50000"), True),
    (rule("MIN_SPEND_LAST_90D_LKR", "GTE", "70000"), False),
    (rule("NEW_TO_BRAND", "IS_TRUE"), False),
    (rule("EXISTING_BRAND_CUSTOMER", "IS_TRUE"), True),
])
def test_rules(r, expected):
    assert check(offer(), rules=[r]).eligible is expected


def test_card_rules_filter_cards_and_inactive_rules_ignored():
    r = check(offer(), rules=[rule("CARD_TYPE", "IN", "CREDIT")])
    assert [c.card_id for c in r.cards] == ["C1"]
    inactive = {**rule("HOME_DISTRICT", "IN", "Colombo"), "is_active": "N"}
    assert check(offer(), rules=[inactive]).eligible
