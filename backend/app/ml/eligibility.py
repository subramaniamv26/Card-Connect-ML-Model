"""Offer eligibility. Applied before ranking (bulk generation) and again at serving time.

An offer is eligible for a customer only if ALL of the following hold:
1. offer.status = APPROVED and valid_from <= today <= valid_to (today = Asia/Colombo date).
2. The offer's brand/outlet has at least one active outlet that participates in bank offers.
3. The customer holds at least one ACTIVE card matching eligible_card_networks / _types / _tiers
   (and any CARD_* eligibility rules).
4. Location: eligible_locations is ALL, or contains ONLINE, or contains the customer's home
   DISTRICT:<name> or PROVINCE:<name>.
5. Every active eligibility rule passes (segment, home district/province, tenure, 90-day spend,
   new-to-brand, existing brand customer).
6. Limits: customer redemptions < redemption_limit_per_customer; total redemptions < total_redemption_limit;
   budget_consumed < campaign_budget.
7. The customer has not dismissed the offer within the suppression period.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from functools import lru_cache
from typing import Any, Iterable


@lru_cache(maxsize=4096)
def parse_list(value: str | None) -> frozenset[str] | None:
    """None means 'ALL'."""
    if value is None:
        return None
    parts = frozenset(p.strip() for p in value.replace(",", ";").replace("|", ";").split(";") if p.strip())
    return None if not parts or {p.upper() for p in parts} == {"ALL"} else parts


@dataclass(frozen=True)
class Card:
    card_id: str
    card_network: str
    card_type: str
    card_tier: str
    card_status: str = "ACTIVE"


@dataclass
class CustomerContext:
    customer_id: str
    segment: str | None
    home_district: str | None
    home_province: str | None
    tenure_months: int | None
    cards: list[Card]
    spend_90_lkr: float = 0.0
    used_brands: set[str] = field(default_factory=set)
    redemptions: dict[str, int] = field(default_factory=dict)     # offer_id -> count for this customer
    dismissed: set[str] = field(default_factory=set)              # offer ids dismissed within suppression


@dataclass
class OfferStats:
    total_redemptions: int = 0


@dataclass
class EligibilityResult:
    eligible: bool
    cards: list[Card]
    failures: list[str]


def offer_is_live(offer: dict[str, Any], today: date) -> str | None:
    """Return None if live, otherwise a reason."""
    if offer["status"] != "APPROVED":
        return f"offer status is {offer['status']}"
    vf, vt = _as_date(offer["valid_from"]), _as_date(offer["valid_to"])
    if today < vf:
        return f"offer not valid until {vf.isoformat()}"
    if today > vt:
        return f"offer expired on {vt.isoformat()}"
    return None


def card_matches(offer: dict[str, Any], card: Card) -> bool:
    nets, types, tiers = (parse_list(offer.get("eligible_card_networks")), parse_list(offer.get("eligible_card_types")),
                          parse_list(offer.get("eligible_card_tiers")))
    return (card.card_status == "ACTIVE"
            and (nets is None or card.card_network in nets)
            and (types is None or card.card_type in types)
            and (tiers is None or card.card_tier in tiers))


def location_matches(offer: dict[str, Any], district: str | None, province: str | None) -> bool:
    locs = parse_list(offer.get("eligible_locations"))
    if locs is None or "ONLINE" in locs:
        return True
    return (district is not None and f"DISTRICT:{district}" in locs) or \
           (province is not None and f"PROVINCE:{province}" in locs)


def _rule_card_filter(rule: dict[str, Any], card: Card) -> bool:
    values = parse_list(rule.get("rule_value")) or set()
    attr = {"CARD_NETWORK": card.card_network, "CARD_TYPE": card.card_type, "CARD_TIER": card.card_tier}[rule["rule_type"]]
    return (attr in values) if rule["operator"] == "IN" else (attr not in values)


def rule_passes(rule: dict[str, Any], ctx: CustomerContext, brand: str | None) -> bool:
    """Customer-level rules (card rules are applied per card in evaluate())."""
    rt, op, raw = rule["rule_type"], rule["operator"], rule.get("rule_value")
    if rt in ("CUSTOMER_SEGMENT", "HOME_DISTRICT", "HOME_PROVINCE"):
        attr = {"CUSTOMER_SEGMENT": ctx.segment, "HOME_DISTRICT": ctx.home_district,
                "HOME_PROVINCE": ctx.home_province}[rt]
        values = parse_list(raw) or set()
        if rt == "CUSTOMER_SEGMENT":
            values = {v.upper() for v in values}
        return (attr in values) if op == "IN" else (attr not in values)
    if rt == "MIN_TENURE_MONTHS":
        return ctx.tenure_months is not None and Decimal(ctx.tenure_months) >= Decimal(raw)
    if rt == "MIN_SPEND_LAST_90D_LKR":
        return Decimal(str(ctx.spend_90_lkr)) >= Decimal(raw)
    if rt == "NEW_TO_BRAND":
        return brand is not None and brand not in ctx.used_brands
    if rt == "EXISTING_BRAND_CUSTOMER":
        return brand is not None and brand in ctx.used_brands
    raise ValueError(f"Unsupported rule type {rt}")


CARD_RULES = {"CARD_NETWORK", "CARD_TYPE", "CARD_TIER"}


def offer_level_failures(offer: dict[str, Any], today: date, stats: OfferStats, participating_brands: set[str],
                         participating_outlets: set[str], brand: str | None) -> list[str]:
    """Checks that do not depend on the customer (status, validity, participation, global limits)."""
    failures: list[str] = []
    live = offer_is_live(offer, today)
    if live:
        failures.append(live)
    if offer.get("merchant_id"):
        if offer["merchant_id"] not in participating_outlets:
            failures.append("merchant outlet is not active or not participating in offers")
    elif brand not in participating_brands:
        failures.append("merchant brand has no active participating outlet")
    total = offer.get("total_redemption_limit")
    if total is not None and stats.total_redemptions >= total:
        failures.append("offer redemption limit reached")
    budget = offer.get("campaign_budget")
    if budget is not None and float(offer.get("budget_consumed") or 0) >= float(budget):
        failures.append("campaign budget exhausted")
    return failures


def customer_level_failures(offer: dict[str, Any], rules: Iterable[dict[str, Any]], ctx: CustomerContext,
                            brand: str | None) -> tuple[list[str], list[Card]]:
    failures: list[str] = []
    active_rules = [r for r in rules if r.get("is_active", "Y") == "Y"]
    card_rules = [r for r in active_rules if r["rule_type"] in CARD_RULES]
    cards = [c for c in ctx.cards if card_matches(offer, c) and all(_rule_card_filter(r, c) for r in card_rules)]
    if not cards:
        failures.append("no active card matches the offer's card network/type/tier")
    if not location_matches(offer, ctx.home_district, ctx.home_province):
        failures.append("customer location is outside the offer's eligible locations")
    for r in active_rules:
        if r["rule_type"] not in CARD_RULES and not rule_passes(r, ctx, brand):
            failures.append(f"eligibility rule {r['rule_type']} not met")
    per_cust = offer.get("redemption_limit_per_customer")
    if per_cust is not None and ctx.redemptions.get(offer["offer_id"], 0) >= per_cust:
        failures.append("customer redemption limit reached")
    if offer["offer_id"] in ctx.dismissed:
        failures.append("customer dismissed this offer recently")
    return failures, cards


def evaluate(offer: dict[str, Any], rules: Iterable[dict[str, Any]], ctx: CustomerContext, today: date,
             stats: OfferStats, participating_brands: set[str], participating_outlets: set[str],
             offer_brand: str | None = None) -> EligibilityResult:
    brand = offer_brand or offer.get("merchant_brand")
    failures = offer_level_failures(offer, today, stats, participating_brands, participating_outlets, brand)
    cust_failures, cards = customer_level_failures(offer, rules, ctx, brand)
    failures += cust_failures
    return EligibilityResult(not failures, cards, failures)


def _as_date(v: Any) -> date:
    return v.date() if hasattr(v, "date") and callable(v.date) else v
