"""Recommendation models.

* RulesBaseline - transparent: brand affinity if the customer already uses the brand, otherwise
  0.5 x category affinity, otherwise segment popularity (cold start).
* HybridModel - weighted blend of brand affinity, category affinity, collaborative and content signals,
  time-pattern match and popularity; when a response ranker has been trained and accepted, its score is
  blended in (it is trained on historical offer deliveries -> redemptions; see training.py).

Scores are ranking scores in [0, 1]. They are NOT calibrated redemption probabilities: the historical
labels come from a targeted (non-random) campaign, so the ranker's output is only used for ordering.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .features import PAIR_FEATURES, FeatureStore

HYBRID_WEIGHTS = {"brand_aff": 0.35, "cat_aff": 0.20, "collab": 0.15, "content": 0.10, "time_match": 0.05,
                  "seg_pop": 0.10, "dist_pop": 0.05}
RANKER_BLEND = 0.6        # share of the relevance score taken from the response ranker when available
COLD_START_WEIGHTS = {"seg_pop": 0.5, "dist_pop": 0.3, "proximity": 0.2}


@dataclass
class RecModel:
    version: str
    algorithm: str
    ranker: Any | None = None                  # sklearn estimator with predict_proba
    ranker_features: list[str] = field(default_factory=lambda: list(PAIR_FEATURES))
    weights: dict[str, float] = field(default_factory=lambda: dict(HYBRID_WEIGHTS))
    ranker_blend: float = RANKER_BLEND
    metrics: dict[str, Any] = field(default_factory=dict)
    kind: str = "hybrid"                       # hybrid | baseline

    def relevance(self, X: pd.DataFrame, history_level: np.ndarray) -> np.ndarray:
        if self.kind == "baseline":
            return baseline_relevance(X)
        heuristic = sum(w * X[k].to_numpy() for k, w in self.weights.items())
        heuristic = heuristic / sum(self.weights.values())
        rel = heuristic
        if self.ranker is not None:
            proba = self.ranker.predict_proba(X[self.ranker_features])[:, 1]
            rel = self.ranker_blend * rank_normalise(proba) + (1 - self.ranker_blend) * heuristic
        cold = sum(w * X[k].to_numpy() for k, w in COLD_START_WEIGHTS.items())
        none = history_level == "NONE"
        rel = np.where(none, cold, rel)
        limited = history_level == "LIMITED"
        rel = np.where(limited, 0.5 * rel + 0.5 * cold, rel)
        return np.clip(rel, 0.0, 1.0)


def baseline_relevance(X: pd.DataFrame) -> np.ndarray:
    used = X["brand_used"].to_numpy() > 0
    return np.where(used, 0.5 + 0.5 * X["brand_aff"].to_numpy(),
                    np.where(X["cat_aff"].to_numpy() > 0, 0.5 * X["cat_aff"].to_numpy(), 0.25 * X["seg_pop"].to_numpy()))


def rank_normalise(x: np.ndarray) -> np.ndarray:
    """Map scores to (0, 1] by global rank, so blending does not depend on the ranker's raw scale."""
    if len(x) == 0:
        return x
    order = x.argsort(kind="stable")
    ranks = np.empty(len(x), np.float64)
    ranks[order] = np.arange(1, len(x) + 1)
    return ranks / len(x)


def baseline_model() -> RecModel:
    return RecModel(version="baseline-rules-v1", algorithm="rules: brand affinity > category affinity > popularity",
                    kind="baseline")


# --------------------------------------------------------------------------------------------------
# Customer benefit
# --------------------------------------------------------------------------------------------------
def estimate_saving(offer_type: str, value: float, min_spend: float, max_benefit: float | None,
                    typical_ticket: float, ticket_source: str) -> tuple[float, str]:
    """Estimated saving (LKR) on ONE qualifying purchase, and the assumptions behind it."""
    ticket = typical_ticket
    basis = ticket
    note = f"one purchase of LKR {basis:,.0f} ({ticket_source})"
    if ticket < min_spend:
        basis = min_spend
        note = f"one purchase at the minimum spend of LKR {min_spend:,.0f} (typical LKR {ticket:,.0f} is below it)"
    if offer_type in ("PERCENT_DISCOUNT", "PERCENT_CASHBACK"):
        saving = basis * value / 100.0
        rule = f"{value:g}% of the purchase"
    else:
        saving = value
        rule = f"fixed LKR {value:,.0f}"
    if max_benefit is not None and saving > max_benefit:
        saving = max_benefit
        rule += f", capped at LKR {max_benefit:,.0f}"
    return round(saving, 2), f"Assumes {note}; benefit = {rule}. Not a guaranteed amount."


def benefit_score(saving: np.ndarray, scale: float = 1000.0) -> np.ndarray:
    """Smoothly map LKR saving to [0, 1): 1000 LKR -> 0.5."""
    return saving / (saving + scale)


FINAL_WEIGHTS = {"relevance": 0.75, "benefit": 0.15, "proximity": 0.10}


def final_score(relevance: np.ndarray, benefit: np.ndarray, proximity: np.ndarray) -> np.ndarray:
    return (FINAL_WEIGHTS["relevance"] * relevance + FINAL_WEIGHTS["benefit"] * benefit
            + FINAL_WEIGHTS["proximity"] * proximity)


# --------------------------------------------------------------------------------------------------
# Diversity-aware selection
# --------------------------------------------------------------------------------------------------
def select_diverse(scores: list[float], brands: list[str], categories: list[str], depth: int,
                   max_per_category: int = 2, category_penalty: float = 0.10) -> list[int]:
    """Greedy diversity-aware re-rank of one customer's candidates. Returns chosen positions in order.

    At most one offer per brand, at most `max_per_category` offers per category, and each repeat of a
    category costs `category_penalty`, so near-identical promotions do not fill every slot."""
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    chosen: list[int] = []
    used_brands: set[str] = set()
    cat_count: dict[str, int] = {}
    while len(chosen) < depth:
        best, best_adj = -1, float("-inf")
        for i in order:
            if i in chosen or brands[i] in used_brands:
                continue
            n = cat_count.get(categories[i], 0)
            if n >= max_per_category:
                continue
            adj = scores[i] - category_penalty * n
            if adj > best_adj:
                best, best_adj = i, adj
            if n == 0:
                break  # candidates are sorted: no later item can beat an unpenalised one
        if best < 0:
            break
        chosen.append(best)
        used_brands.add(brands[best])
        cat_count[categories[best]] = cat_count.get(categories[best], 0) + 1
    return chosen


# --------------------------------------------------------------------------------------------------
# Explanations
# --------------------------------------------------------------------------------------------------
def explain(row: dict[str, Any], customer: dict[str, Any], card: dict[str, Any]) -> tuple[str, list[str]]:
    codes: list[str] = []
    parts: list[str] = []
    brand, cat = row["brand"], row["category"].replace("-", " ").title()
    level = customer["history_level"]
    if level == "NONE":
        codes.append("POPULAR_WITH_PEERS")
        parts.append(f"{brand} is popular with customers like you in {customer['district']}")
    else:
        if row["brand_cnt"] >= 3:
            codes.append("FREQUENT_MERCHANT")
            parts.append(f"You frequently shop at {brand} ({int(row['brand_cnt'])} purchases in your history)")
        elif row["brand_cnt"] >= 1:
            codes.append("PAST_MERCHANT")
            parts.append(f"You have shopped at {brand} before")
        if row["brand_cnt"] >= 1 and row["brand_recency"] <= 30:
            codes.append("RECENT_MERCHANT")
        if row["cat_aff"] >= 0.12 and "FREQUENT_MERCHANT" not in codes:
            codes.append("CATEGORY_AFFINITY")
            parts.append(f"{cat} is one of your main spending categories")
        if row["brand_cnt"] == 0 and row["collab"] >= 0.3:
            codes.append("SIMILAR_CUSTOMERS")
            parts.append(f"customers with similar spending also shop at {brand}")
        if row["brand_cnt"] == 0 and row.get("brand_total_cnt", 1) == 0:
            codes.append("NEW_MERCHANT")
            parts.append(f"{brand} is new and matches your interest in {cat}")
        if not parts:
            codes.append("CATEGORY_MATCH")
            parts.append(f"it matches your spending in {cat}")
    if row["proximity"] >= 1.0 and row["merchant_type"] == "PHYSICAL":
        codes.append("NEARBY_OUTLET")
        parts.append(f"there is an outlet in {customer['district']}")
    codes.append("CARD_ELIGIBLE")
    card_txt = f"this offer applies to your {card['card_network'].title()} {card['card_type'].lower()} card"
    text = parts[0]
    if len(parts) > 1:
        text += ", " + parts[1][0].lower() + parts[1][1:]
    return f"{text}, and {card_txt}.", codes
