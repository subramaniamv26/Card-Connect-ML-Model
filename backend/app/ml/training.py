"""Model training and offline evaluation.

Two kinds of ground truth exist in this dataset and they are used for different purposes:

1. Historical offer deliveries -> redemptions (HISTORICAL_OFFER_TARGETING, ~87k delivered, ~26k redeemed,
   six monthly waves). These are real outcome labels, so a pointwise response ranker
   (HistGradientBoostingClassifier) is trained on them. Features are computed AS OF each wave's start
   date (only earlier transactions). Waves are split chronologically: train on earlier waves, validate on
   the latest completed wave. Deliveries whose redemption window was still open at the data cutoff have
   incomplete outcomes and are excluded entirely. Caveat: deliveries were targeted by the source system's rules, not randomised, so the model
   learns redemption among targeted customers; its scores are used only for ranking.

2. Future transactions. The last 30 days of the window are held out; features are rebuilt as of the
   hold-out start, every method ranks the offer-participating brands for each customer and is scored with
   Precision@3, Recall@3 and NDCG@3 against brands actually purchased in the hold-out. This measures
   whether recommendations match what customers go on to buy, NOT whether an offer causes a redemption.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, log_loss, roc_auc_score

from .features import PAIR_FEATURES, FeatureStore, build_features
from .model import RecModel, baseline_relevance

log = logging.getLogger(__name__)
MIN_POSITIVES = 200
MIN_WAVES = 2
RANKER_MIN_AUC_GAIN = 0.01
HOLDOUT_DAYS = 30
EVAL_SAMPLE = 20_000


# --------------------------------------------------------------------------------------------------
# Label dataset
# --------------------------------------------------------------------------------------------------
def build_label_dataset(labels: pd.DataFrame, spend: pd.DataFrame, customers: pd.DataFrame,
                        merchants: pd.DataFrame, data_end: datetime, history_months_days: int = 365,
                        max_wave_end: datetime | None = None) -> pd.DataFrame:
    """One feature row per delivered offer, with features as of the offer start date."""
    if labels.empty:
        return pd.DataFrame()
    lab = labels.copy()
    # Censoring: deliveries whose redemption window had not closed by the data end have incomplete outcomes.
    # The whole delivery is dropped (keeping only its early redemptions would bias the labels towards 1).
    lab = lab[lab["offer_end_date"] < pd.Timestamp(data_end)]
    if max_wave_end is not None:
        lab = lab[lab["offer_end_date"] < pd.Timestamp(max_wave_end)]
    parts = []
    for start, wave in lab.groupby("offer_start_date"):
        as_of = start.to_pydatetime()
        fs = build_features(spend, customers, merchants, as_of=as_of, history_days=history_months_days)
        wave = wave[wave["customer_id"].isin(fs.cust_index) & wave["brand"].isin(fs.brand_index)]
        if wave.empty:
            continue
        ci = wave["customer_id"].map(fs.cust_index).to_numpy()
        bi = wave["brand"].map(fs.brand_index).to_numpy()
        X = fs.pair_matrix(ci, bi)
        X["history_level"] = fs.cust["history_level"].to_numpy()[ci]
        X["label"] = wave["label"].to_numpy()
        X["wave_start"] = start
        X["wave_end"] = wave["offer_end_date"].max()
        X["customer_id"] = wave["customer_id"].to_numpy()
        parts.append(X)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def _new_ranker(seed: int = 7) -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(max_iter=250, learning_rate=0.05, max_leaf_nodes=31,
                                          min_samples_leaf=50, l2_regularization=1.0, early_stopping=False,
                                          random_state=seed)


def _auc(y, s) -> float | None:
    return float(roc_auc_score(y, s)) if len(set(y)) == 2 else None


def train_ranker(ds: pd.DataFrame, heuristic: RecModel) -> tuple[HistGradientBoostingClassifier | None, dict]:
    """Chronological train/validation by wave; returns (ranker refit on all completed waves, metrics)."""
    info: dict = {"label_rows": int(len(ds))}
    if ds.empty:
        info["decision"] = "no labelled deliveries available - ranker not trained"
        return None, info
    waves = sorted(ds["wave_start"].unique())
    pos = int(ds["label"].sum())
    info.update({"waves": len(waves), "positives": pos, "base_rate": round(float(ds["label"].mean()), 4)})
    if len(waves) < MIN_WAVES + 1 or pos < MIN_POSITIVES:
        info["decision"] = (f"insufficient labels (waves={len(waves)}, positives={pos}); "
                            "using heuristic hybrid without ranker")
        return None, info
    valid_wave = waves[-1]
    tr, va = ds[ds["wave_start"] < valid_wave], ds[ds["wave_start"] == valid_wave]
    model = _new_ranker().fit(tr[PAIR_FEATURES], tr["label"])
    p = model.predict_proba(va[PAIR_FEATURES])[:, 1]
    h = heuristic.relevance(va[PAIR_FEATURES], va["history_level"].to_numpy())
    b = baseline_relevance(va[PAIR_FEATURES])
    y = va["label"].to_numpy()
    info["validation"] = {
        "wave_start": str(pd.Timestamp(valid_wave).date()), "rows": int(len(va)), "positives": int(y.sum()),
        "ranker_auc": _auc(y, p), "ranker_average_precision": float(average_precision_score(y, p)),
        "ranker_log_loss": float(log_loss(y, np.clip(p, 1e-6, 1 - 1e-6))),
        "heuristic_auc": _auc(y, h), "baseline_auc": _auc(y, b),
        "train_waves": [str(pd.Timestamp(w).date()) for w in waves[:-1]],
    }
    gain = (info["validation"]["ranker_auc"] or 0) - (info["validation"]["heuristic_auc"] or 0)
    if gain < RANKER_MIN_AUC_GAIN:
        info["decision"] = f"ranker AUC gain {gain:.4f} < {RANKER_MIN_AUC_GAIN}; ranker not used"
        return None, info
    final = _new_ranker().fit(ds[PAIR_FEATURES], ds["label"])
    info["decision"] = f"ranker accepted (validation AUC gain {gain:.4f}); refit on all {len(waves)} completed waves"
    imp = _permutation_importance(model, va, y)
    info["feature_importance"] = imp
    return final, info


def _permutation_importance(model, va: pd.DataFrame, y: np.ndarray, top: int = 8) -> dict[str, float]:
    rng = np.random.default_rng(0)
    base = roc_auc_score(y, model.predict_proba(va[PAIR_FEATURES])[:, 1])
    out = {}
    for f in PAIR_FEATURES:
        X = va[PAIR_FEATURES].copy()
        X[f] = rng.permutation(X[f].to_numpy())
        out[f] = round(float(base - roc_auc_score(y, model.predict_proba(X)[:, 1])), 4)
    return dict(sorted(out.items(), key=lambda kv: -kv[1])[:top])


# --------------------------------------------------------------------------------------------------
# Transaction hold-out ranking evaluation
# --------------------------------------------------------------------------------------------------
def ranking_metrics(top: np.ndarray, truth: list[set[int]], k: int = 3) -> dict[str, float]:
    """top: [N, k] item indices; truth: list of sets. Customers with empty truth are skipped."""
    p, r, n = [], [], []
    disc = 1.0 / np.log2(np.arange(2, k + 2))
    for row, gt in zip(top, truth):
        if not gt:
            continue
        hits = np.array([1.0 if i in gt else 0.0 for i in row[:k]])
        p.append(hits.sum() / k)
        r.append(hits.sum() / len(gt))
        ideal = disc[: min(len(gt), k)].sum()
        n.append(float((hits * disc).sum() / ideal))
    if not p:
        return {"precision_at_3": None, "recall_at_3": None, "ndcg_at_3": None, "evaluated_customers": 0}
    return {"precision_at_3": round(float(np.mean(p)), 4), "recall_at_3": round(float(np.mean(r)), 4),
            "ndcg_at_3": round(float(np.mean(n)), 4), "evaluated_customers": len(p)}


def holdout_evaluation(spend: pd.DataFrame, customers: pd.DataFrame, merchants: pd.DataFrame,
                       window_end: datetime, models: dict[str, RecModel], participating_brands: set[str],
                       sample: int = EVAL_SAMPLE, seed: int = 11) -> dict:
    t0 = window_end - timedelta(days=HOLDOUT_DAYS)
    fs = build_features(spend, customers, merchants, as_of=t0, history_days=365)
    brands = [b for b in fs.brands if b in participating_brands]
    bidx = np.array([fs.brand_index[b] for b in brands])
    rng = np.random.default_rng(seed)
    all_ci = np.arange(len(fs.cust_index))
    ci = np.sort(rng.choice(all_ci, size=min(sample, len(all_ci)), replace=False))

    future = spend[(spend["transaction_datetime"] >= t0) & (spend["transaction_datetime"] < window_end)
                   & (spend["txn_count"] > 0)]
    m = merchants.set_index("merchant_id")["merchant_brand"]
    fut = pd.DataFrame({"c": future["customer_id"].map(fs.cust_index), "b": future["merchant_id"].map(m)})
    fut = fut[fut["b"].isin(set(brands))].dropna()
    pos_in_list = {fs.brand_index[b]: j for j, b in enumerate(brands)}
    truth_all: dict[int, set[int]] = {}
    for c, b in zip(fut["c"].astype(int), fut["b"]):
        truth_all.setdefault(c, set()).add(pos_in_list[fs.brand_index[b]])
    truth = [truth_all.get(int(c), set()) for c in ci]
    used_before = fs.cnt[np.ix_(ci, bidx)] > 0
    truth_new = [{j for j in t if not used_before[n, j]} for n, t in enumerate(truth)]

    cc = np.repeat(ci, len(bidx))
    bb = np.tile(bidx, len(ci))
    X = fs.pair_matrix(cc, bb)
    level = fs.cust["history_level"].to_numpy()[cc]
    results = {"holdout_start": str(t0.date()), "holdout_end_exclusive": str(window_end.date()),
               "candidate_brands": len(brands), "sampled_customers": int(len(ci)),
               "customers_with_future_purchases": int(sum(1 for t in truth if t)), "methods": {}}
    scorers = {"popularity_segment": lambda: X["seg_pop"].to_numpy()}
    for name, mdl in models.items():
        scorers[name] = (lambda mdl=mdl: mdl.relevance(X, level))
    for name, fn in scorers.items():
        S = fn().reshape(len(ci), len(bidx))
        top = np.argsort(-S, axis=1, kind="stable")[:, :3]
        res = ranking_metrics(top, truth)
        S_new = np.where(used_before, -np.inf, S)
        top_new = np.argsort(-S_new, axis=1, kind="stable")[:, :3]
        res["discovery_recall_at_3"] = ranking_metrics(top_new, truth_new)["recall_at_3"]
        res["share_of_recs_new_to_customer"] = round(float(1 - np.take_along_axis(used_before, top, 1).mean()), 4)
        results["methods"][name] = res
    return results


# --------------------------------------------------------------------------------------------------
# Batch quality
# --------------------------------------------------------------------------------------------------
def batch_quality(recs: pd.DataFrame, statuses: pd.DataFrame, live_offer_ids: list[str],
                  offer_category: dict[str, str], compliance_failures: int, compliance_checked: int) -> dict:
    top = recs[recs["candidate_rank"] <= 3] if len(recs) else recs
    n = len(statuses)
    per = top.groupby("customer_id")["offer_id"].apply(list) if len(top) else pd.Series(dtype=object)
    div = [len({offer_category[o] for o in offs}) / len(offs) for offs in per] if len(per) else []
    return {
        "customers": n,
        "coverage_any": round(float((statuses["status"] != "NONE").mean()), 4) if n else 0.0,
        "coverage_full_3": round(float((statuses["status"] == "FULL").mean()), 4) if n else 0.0,
        "avg_offers_per_customer": round(float(len(top) / n), 3) if n else 0.0,
        "intra_list_category_diversity": round(float(np.mean(div)), 4) if div else None,
        "catalog_coverage": round(len(set(top["offer_id"])) / len(live_offer_ids), 4) if live_offer_ids and len(top) else 0.0,
        "eligibility_compliance": round(1 - compliance_failures / compliance_checked, 6) if compliance_checked else None,
        "eligibility_checked": compliance_checked,
        "history_levels": statuses["history_level"].value_counts().to_dict() if n else {},
    }
