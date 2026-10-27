"""Training + bulk generation orchestration (runs only in the worker service).

run_training(run_id):
  1. data cutoff -> rolling windows (12 months history, 3 calendar months aggregation)
  2. extract transactions, apply net-spend rules
  3. label dataset (historical deliveries, features as of each wave) -> response ranker (chronological)
  4. transaction hold-out evaluation of: segment popularity, rules baseline, active model, candidate
  5. promotion decision: candidate NDCG@3 must be >= reference x (1 - tolerance), where reference is the
     active model (or the rules baseline when no model is active). Otherwise the candidate is REJECTED
     and the active model keeps serving.
  6. features as of the cutoff -> generate recommendations with the serving model
  7. eligibility compliance audit of every published top-3 row (must be 100%)
  8. write batch (BUILDING) -> publish atomically; record metrics, registry and run status.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from .. import db
from ..config import get_settings
from . import data as D
from .eligibility import Card, CustomerContext, OfferStats, evaluate
from .features import build_features
from .generate import generate, participating
from .model import RecModel, baseline_model
from .spend import net_spend_frame
from .store import active_model, promote, publish_batch, register_model, save_model, write_batch
from .training import batch_quality, build_label_dataset, holdout_evaluation, train_ranker
from .windows import add_months, compute_windows

log = logging.getLogger(__name__)


class TrainingError(RuntimeError):
    pass


def _now_local() -> datetime:
    return datetime.now(ZoneInfo(get_settings().timezone)).replace(tzinfo=None, microsecond=0)


def _now_utc() -> datetime:
    """Operational timestamps (*_at) are stored as naive UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _update_run(conn, run_id: int, **cols) -> None:
    allowed = {"status", "attempt", "model_version", "algorithm", "data_cutoff", "window_start", "window_end",
               "transaction_rows", "label_rows", "metrics", "baseline_metrics", "active_model_metrics", "promoted",
               "promotion_decision", "batch_id", "error_message", "duration_seconds"}
    sets = [f"{k} = :{k}" for k in cols if k in allowed]
    if cols.get("status") == "RUNNING":
        sets.append("started_at = SYS_EXTRACT_UTC(SYSTIMESTAMP)")
    if cols.get("status") in ("SUCCEEDED", "REJECTED", "FAILED", "SKIPPED"):
        sets.append("finished_at = SYS_EXTRACT_UTC(SYSTIMESTAMP)")
    db.execute(conn, f"UPDATE model_training_runs SET {', '.join(sets)} WHERE run_id = :run_id",
               {**{k: v for k, v in cols.items() if k in allowed}, "run_id": run_id})
    conn.commit()


def _load_inputs(conn, windows, extra_lookback_months: int = 12):
    customers = D.load_customers(conn)
    merchants = D.load_merchants(conn)
    cards = D.load_cards(conn)
    # Label features for early waves need history before the window start.
    start = datetime.combine(add_months(windows.window_start, -extra_lookback_months), datetime.min.time())
    txns = D.load_transactions(conn, start, windows.window_end_dt)
    spend = net_spend_frame(txns)
    return customers, merchants, cards, txns, spend


def _compliance(recs: pd.DataFrame, offers: list[dict], rules, customers: pd.DataFrame, cards: pd.DataFrame,
                merchants: pd.DataFrame, fs, redemptions, totals, dismissed, today) -> tuple[int, int, list]:
    """Independent re-check of every published (rank <= 3) row with the scalar eligibility function."""
    if recs.empty:
        return 0, 0, []
    top = recs[recs["candidate_rank"] <= 3]
    p_brands, p_outlets, _ = participating(merchants)
    offer_by_id = {o["offer_id"]: o for o in offers}
    brand_of = merchants.set_index("merchant_id")["merchant_brand"]
    cust = customers.set_index("customer_id")
    card_map = {r.card_id: Card(r.card_id, r.card_network, r.card_type, r.card_tier, r.card_status)
                for r in cards.itertuples(index=False)}
    by_cust = cards[cards["card_status"] == "ACTIVE"].groupby("customer_id")["card_id"].apply(list).to_dict()
    failures, examples = 0, []
    for r in top.itertuples(index=False):
        o = offer_by_id[r.offer_id]
        brand = o["merchant_brand"] or brand_of[o["merchant_id"]]
        c = cust.loc[r.customer_id]
        ci = fs.cust_index[r.customer_id]
        ctx = CustomerContext(r.customer_id, c["customer_segment"], c["customer_home_district"],
                              c["customer_home_province"], int(c["customer_tenure_months"]),
                              [card_map[x] for x in by_cust.get(r.customer_id, [])],
                              float(fs.cust.at[r.customer_id, "spend_90"]),
                              {brand} if fs.cnt[ci, fs.brand_index[brand]] > 0 else set(),
                              {r.offer_id: redemptions.get((r.customer_id, r.offer_id), 0)},
                              {r.offer_id} if (r.customer_id, r.offer_id) in dismissed else set())
        res = evaluate(o, rules.get(r.offer_id, []), ctx, today, OfferStats(totals.get(r.offer_id, 0)),
                       p_brands, p_outlets, brand)
        if not res.eligible or r.card_id not in {x.card_id for x in res.cards}:
            failures += 1
            if len(examples) < 5:
                examples.append({"customer_id": r.customer_id, "offer_id": r.offer_id, "failures": res.failures})
    return failures, len(top), examples


def generate_and_publish(conn, model: RecModel, run_id: int | None, windows, customers, merchants, cards,
                         spend) -> tuple[int, dict]:
    s = get_settings()
    now = _now_utc()
    today = _now_local().date()        # offer validity is judged on the Asia/Colombo calendar date
    fs = build_features(spend, customers, merchants, as_of=windows.window_end_dt, window_start=windows.window_start_dt)
    offers = D.load_offers(conn)
    rules = D.load_rules(conn)
    redemptions, totals, dismissed = D.load_event_stats(conn, s.dismissal_suppression_days, now)
    t = time.time()
    res = generate(fs, spend, windows, customers, cards, merchants, offers, rules, redemptions, totals, dismissed,
                   model, today, depth=s.reserve_depth)
    gen_seconds = round(time.time() - t, 1)
    fails, checked, examples = _compliance(res.recommendations, offers, rules, customers, cards, merchants, fs,
                                           redemptions, totals, dismissed, today)
    live_ids = [o["offer_id"] for o in offers]
    quality = batch_quality(res.recommendations, res.statuses, live_ids,
                            {o["offer_id"]: o["merchant_category"] for o in offers}, fails, checked)
    quality.update({"generation_seconds": gen_seconds, "generation": res.stats, "compliance_examples": examples})
    if fails:
        raise TrainingError(f"Eligibility compliance check failed for {fails} of {checked} rows; batch not published")
    batch_id = write_batch(conn, run_id, model.version, now, windows, res.recommendations, res.statuses)
    publish_batch(conn, batch_id)
    log.info("Published batch %s (%s customers, %s rows)", batch_id, len(res.statuses), len(res.recommendations))
    return batch_id, quality


def run_training(run_id: int) -> dict:
    s = get_settings()
    t0 = time.time()
    with db.connection() as conn:
        _update_run(conn, run_id, status="RUNNING")
        cutoff = D.data_cutoff(conn)
        if cutoff is None:
            _update_run(conn, run_id, status="SKIPPED", error_message="No transactions loaded",
                        duration_seconds=round(time.time() - t0, 1))
            return {"status": "SKIPPED"}
        w = compute_windows(cutoff, s.history_months, s.aggregation_months)
        _update_run(conn, run_id, data_cutoff=w.data_cutoff, window_start=w.window_start, window_end=w.window_end)

        customers, merchants, cards, txns, spend = _load_inputs(conn, w)
        in_window = int(((txns["transaction_datetime"] >= w.window_start_dt)).sum())
        labels = D.load_labels(conn)
        labels = labels[labels["offer_start_date"] >= pd.Timestamp(w.window_start)] if len(labels) else labels
        log.info("Run %s: cutoff %s, %s transactions in window, %s labelled deliveries", run_id, cutoff,
                 in_window, len(labels))

        version = f"hybrid-{_now_local():%Y%m%d-%H%M%S}"
        heuristic = RecModel(version=version + "-h", algorithm="hybrid heuristic")
        ds = build_label_dataset(labels, spend, customers, merchants, data_end=w.window_end_dt)
        ranker, ranker_info = train_ranker(ds, heuristic)
        candidate = RecModel(version=version, ranker=ranker,
                             algorithm="hybrid: affinity+collaborative+content" + (" + HGB response ranker" if ranker else ""))

        # Hold-out evaluation; the candidate's ranker for this check only sees waves that ended before the hold-out.
        holdout_start = w.days_before_end(30)
        ds_eval = ds[ds["wave_end"] < pd.Timestamp(holdout_start)] if len(ds) else ds
        eval_ranker, _ = train_ranker(ds_eval, heuristic) if len(ds_eval) else (None, {})
        active, active_row = active_model(conn)
        models = {"rules_baseline": baseline_model(), "hybrid_heuristic": heuristic,
                  "candidate": RecModel(version=version + "-eval", ranker=eval_ranker, algorithm="candidate (eval)")}
        if active_row is not None:
            models["active_model"] = active
        p_brands, _, _ = participating(merchants)
        holdout = holdout_evaluation(spend, customers, merchants, w.window_end_dt, models, p_brands)

        cand_ndcg = holdout["methods"]["candidate"]["ndcg_at_3"] or 0.0
        ref_name = "active_model" if active_row is not None else "rules_baseline"
        ref_ndcg = holdout["methods"][ref_name]["ndcg_at_3"] or 0.0
        promoted = cand_ndcg >= ref_ndcg * (1 - s.promotion_tolerance)
        decision = (f"candidate NDCG@3 {cand_ndcg:.4f} vs {ref_name} {ref_ndcg:.4f} "
                    f"(tolerance {s.promotion_tolerance:.0%}): {'PROMOTED' if promoted else 'REJECTED'}")
        if active_row is not None:
            decision += (". Note: the active model is evaluated frozen; its ranker may have seen labels that overlap "
                         "the hold-out, which biases this comparison in its favour (conservative gate).")
        candidate.metrics = {"holdout": holdout, "ranker": ranker_info}

        path, sha = save_model(candidate, s.model_dir)
        register_model(conn, candidate, run_id, path, sha, w.data_cutoff, "CANDIDATE")
        conn.commit()
        if promoted:
            promote(conn, candidate.version)
            conn.commit()
            serving = candidate
        else:
            db.execute(conn, "UPDATE model_registry SET status = 'REJECTED' WHERE model_version = :v",
                       {"v": candidate.version})
            conn.commit()
            serving = active
        log.info("Run %s: %s; serving model %s", run_id, decision, serving.version)

        batch_id, quality = generate_and_publish(conn, serving, run_id, w, customers, merchants, cards, spend)
        metrics = {"holdout": holdout, "ranker": ranker_info, "batch_quality": quality, "serving_model": serving.version}
        _update_run(conn, run_id, status="SUCCEEDED" if promoted else "REJECTED", model_version=candidate.version,
                    algorithm=candidate.algorithm[:100], transaction_rows=in_window, label_rows=len(ds),
                    metrics=json.dumps(metrics, default=str),
                    baseline_metrics=json.dumps(holdout["methods"]["rules_baseline"], default=str),
                    active_model_metrics=json.dumps(holdout["methods"].get("active_model"), default=str),
                    promoted="Y" if promoted else "N", promotion_decision=decision[:1000], batch_id=batch_id,
                    duration_seconds=round(time.time() - t0, 1))
        return {"status": "SUCCEEDED" if promoted else "REJECTED", "batch_id": batch_id, "decision": decision,
                "metrics": metrics}


def refresh_recommendations() -> dict:
    """Regenerate and publish with the ACTIVE model on current data (no training)."""
    s = get_settings()
    with db.connection() as conn:
        cutoff = D.data_cutoff(conn)
        if cutoff is None:
            raise TrainingError("No transactions loaded")
        w = compute_windows(cutoff, s.history_months, s.aggregation_months)
        customers, merchants, cards, _, spend = _load_inputs(conn, w, extra_lookback_months=0)
        model, _ = active_model(conn)
        batch_id, quality = generate_and_publish(conn, model, None, w, customers, merchants, cards, spend)
        return {"batch_id": batch_id, "model_version": model.version, "batch_quality": quality}
