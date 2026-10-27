"""Feature engineering over the rolling history window.

Everything is computed "as of" a timestamp using only transactions strictly before it, so the same code
builds leakage-free features for historical offer waves, for the chronological hold-out evaluation and
for production scoring.

Signals (customer c, brand b):
* RFM per customer: counts/spend over 30/90/365 days, recency, average ticket, trend (30d vs 90d rate).
* Brand affinity: 0.5 * txn share + 0.3 * spend share + 0.2 * exp(-recency/45d) -- the same weighting as the
  source system's merchant_preference_score, so it is directly comparable.
* Category affinity: same formula at category level.
* Collaborative: item-item cosine similarity between brands on the customer x brand log-count matrix;
  collab(c,b) = similarity-weighted average of c's affinities to other brands.
* Content: cosine between brand attribute vectors (category, merchant type, price level, online) and the
  customer's affinity-weighted profile over the same attributes.
* Time pattern match: dot product of the customer's and the brand's time-of-day and weekend distributions.
* Popularity: share of customers in the same segment / home district who used the brand (cold start).
* Proximity: outlet in home district (1.0), home province (0.6), online/service brand (0.8), otherwise 0.3.

Names, NICs, phone numbers and device ids are never inputs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

TIME_BUCKETS = ("MORNING", "DAY", "EVENING", "NIGHT")
CATEGORIES = ("GROCERY", "RESTAURANT", "FUEL", "RETAIL", "ELECTRONICS", "TRAVEL", "HOTEL", "ENTERTAINMENT",
              "HEALTHCARE", "UTILITIES", "EDUCATION", "E-COMMERCE")
MERCHANT_TYPES = ("PHYSICAL", "ONLINE", "SERVICE")
LIMITED_HISTORY_TXNS = 5

PAIR_FEATURES = [
    "brand_aff", "brand_used", "brand_cnt_log", "brand_spend_log", "brand_cnt90_log", "brand_recency",
    "cat_aff", "collab", "content", "time_match", "seg_pop", "dist_pop", "proximity", "brand_trend",
    "cust_cnt90_log", "cust_spend90_log", "cust_trend", "cust_recency", "cust_avg_ticket_log",
]


def time_bucket(hours: np.ndarray) -> np.ndarray:
    """MORNING 05-11, DAY 12-16, EVENING 17-20, NIGHT 21-04 (as in the source data)."""
    out = np.full(hours.shape, 3, dtype=np.int8)
    out[(hours >= 5) & (hours <= 11)] = 0
    out[(hours >= 12) & (hours <= 16)] = 1
    out[(hours >= 17) & (hours <= 20)] = 2
    return out


def _affinity(cnt: np.ndarray, spend: np.ndarray, recency_days: np.ndarray) -> np.ndarray:
    tot_cnt = cnt.sum(axis=1, keepdims=True)
    tot_spend = spend.sum(axis=1, keepdims=True)
    with np.errstate(divide="ignore", invalid="ignore"):
        cs = np.where(tot_cnt > 0, cnt / tot_cnt, 0.0)
        ss = np.where(tot_spend > 0, spend / tot_spend, 0.0)
    rec = np.where(cnt > 0, np.exp(-recency_days / 45.0), 0.0)
    return (0.5 * cs + 0.3 * ss + 0.2 * rec).astype(np.float32)


@dataclass
class FeatureStore:
    as_of: datetime
    window_start: datetime
    customers: pd.DataFrame                    # index customer_id; segment, district, province, tenure
    brands: list[str]
    brand_index: dict[str, int]
    cust_index: dict[str, int]
    brand_category: np.ndarray                 # [B] category code
    brand_type: np.ndarray                     # [B] merchant type code
    brand_median_ticket: np.ndarray            # [B] LKR, population median
    cnt: np.ndarray                            # [C,B] counted txns in window
    spend: np.ndarray                          # [C,B] net LKR (floored at 0)
    cnt90: np.ndarray
    recency: np.ndarray                        # [C,B] days since last txn (999 if never)
    brand_aff: np.ndarray
    cat_aff: np.ndarray                        # [C,K]
    cat_cnt: np.ndarray
    collab: np.ndarray
    content: np.ndarray
    time_match: np.ndarray
    seg_pop: np.ndarray                        # [C,B] (gathered by customer segment)
    dist_pop: np.ndarray
    proximity: np.ndarray
    brand_trend: np.ndarray                    # [B]
    cust: pd.DataFrame                         # customer-level RFM / profile, index customer_id
    card_brand_cnt: pd.DataFrame               # customer_id, card_id, brand, cnt
    card_cat_cnt: pd.DataFrame                 # customer_id, card_id, category, cnt
    card_cnt: pd.DataFrame                     # customer_id, card_id, cnt
    extra: dict = field(default_factory=dict)

    # ---- pair access -------------------------------------------------------------------------
    def pair_matrix(self, ci: np.ndarray, bi: np.ndarray) -> pd.DataFrame:
        """Feature rows for (customer index, brand index) pairs."""
        cust = self.cust
        c_cnt90 = cust["cnt_90"].to_numpy()[ci]
        df = pd.DataFrame({
            "brand_aff": self.brand_aff[ci, bi],
            "brand_used": (self.cnt[ci, bi] > 0).astype(np.float32),
            "brand_cnt_log": np.log1p(self.cnt[ci, bi]),
            "brand_spend_log": np.log1p(self.spend[ci, bi]),
            "brand_cnt90_log": np.log1p(self.cnt90[ci, bi]),
            "brand_recency": np.minimum(self.recency[ci, bi], 400.0),
            "cat_aff": self.cat_aff[ci, self.brand_category[bi]],
            "collab": self.collab[ci, bi],
            "content": self.content[ci, bi],
            "time_match": self.time_match[ci, bi],
            "seg_pop": self.seg_pop[ci, bi],
            "dist_pop": self.dist_pop[ci, bi],
            "proximity": self.proximity[ci, bi],
            "brand_trend": self.brand_trend[bi],
            "cust_cnt90_log": np.log1p(c_cnt90),
            "cust_spend90_log": np.log1p(np.maximum(cust["spend_90"].to_numpy()[ci], 0)),
            "cust_trend": cust["trend"].to_numpy()[ci],
            "cust_recency": np.minimum(cust["recency_days"].to_numpy()[ci], 400.0),
            "cust_avg_ticket_log": np.log1p(cust["avg_ticket"].to_numpy()[ci]),
        })
        return df[PAIR_FEATURES].astype(np.float32)


def build_features(spend_df: pd.DataFrame, customers: pd.DataFrame, merchants: pd.DataFrame,
                   as_of: datetime, history_days: int | None = None, window_start: datetime | None = None
                   ) -> FeatureStore:
    """spend_df: output of net_spend_frame with transaction_datetime (datetime64) and merchant_id.
    customers: customer_id, customer_segment, customer_home_district, customer_home_province.
    merchants: merchant_id, merchant_brand, merchant_category, merchant_type, merchant_district,
               merchant_province, merchant_city."""
    if window_start is None:
        window_start = as_of - timedelta(days=history_days or 365)
    s = spend_df[(spend_df["transaction_datetime"] >= window_start) & (spend_df["transaction_datetime"] < as_of)]

    cust_ids = customers["customer_id"].tolist()
    cust_index = {c: i for i, c in enumerate(cust_ids)}
    brands = sorted(merchants["merchant_brand"].unique())
    brand_index = {b: i for i, b in enumerate(brands)}
    C, B, K = len(cust_ids), len(brands), len(CATEGORIES)
    cat_code = {c: i for i, c in enumerate(CATEGORIES)}
    type_code = {t: i for i, t in enumerate(MERCHANT_TYPES)}

    m = merchants.set_index("merchant_id")
    brand_first = merchants.drop_duplicates("merchant_brand").set_index("merchant_brand")
    brand_category = np.array([cat_code[brand_first.at[b, "merchant_category"]] for b in brands], dtype=np.int16)
    brand_type = np.array([type_code[brand_first.at[b, "merchant_type"]] for b in brands], dtype=np.int16)

    ci = s["customer_id"].map(cust_index).to_numpy()
    valid = ~pd.isna(ci)
    s = s[valid]
    ci = ci[valid].astype(np.int64)
    bi = s["merchant_id"].map(m["merchant_brand"]).map(brand_index).to_numpy().astype(np.int64)
    amt = s["signed_amount"].to_numpy(dtype=np.float64)
    cntv = s["txn_count"].to_numpy(dtype=np.float64)
    dt = s["transaction_datetime"]
    age_days = ((as_of - dt).dt.total_seconds() / 86400.0).to_numpy()

    cnt = np.zeros((C, B), np.float32)
    spend = np.zeros((C, B), np.float64)
    cnt90 = np.zeros((C, B), np.float32)
    np.add.at(cnt, (ci, bi), cntv)
    np.add.at(spend, (ci, bi), amt)
    np.add.at(cnt90, (ci[age_days <= 90], bi[age_days <= 90]), cntv[age_days <= 90])
    spend = np.maximum(spend, 0.0)
    recency = np.full((C, B), 999.0, np.float32)
    counted = cntv > 0
    np.minimum.at(recency, (ci[counted], bi[counted]), age_days[counted].astype(np.float32))
    brand_aff = _affinity(cnt, spend, recency)

    # category level
    cat_of = brand_category.astype(np.int64)
    cat_cnt = np.zeros((C, K), np.float32)
    cat_spend = np.zeros((C, K), np.float64)
    cat_rec = np.full((C, K), 999.0, np.float32)
    for b in range(B):
        k = cat_of[b]
        cat_cnt[:, k] += cnt[:, b]
        cat_spend[:, k] += spend[:, b]
        cat_rec[:, k] = np.minimum(cat_rec[:, k], recency[:, b])
    cat_aff = _affinity(cat_cnt, cat_spend, cat_rec)

    # collaborative: item-item cosine on log counts
    X = np.log1p(cnt).astype(np.float32)
    norms = np.linalg.norm(X, axis=0)
    norms[norms == 0] = 1.0
    S = (X.T @ X) / np.outer(norms, norms)
    np.fill_diagonal(S, 0.0)
    S = np.maximum(S, 0.0)
    denom = S.sum(axis=0)
    denom[denom == 0] = 1.0
    collab = (brand_aff @ S) / denom
    mx = collab.max(axis=1, keepdims=True)
    collab = np.where(mx > 0, collab / np.where(mx > 0, mx, 1), 0.0).astype(np.float32)

    # content: brand attribute vectors
    ticket = pd.Series(np.where(cntv > 0, amt, np.nan)).groupby(bi).median()
    brand_median_ticket = np.array([ticket.get(b, np.nan) for b in range(B)], dtype=np.float64)
    fallback = np.nanmedian(brand_median_ticket) if np.isfinite(brand_median_ticket).any() else 1000.0
    brand_median_ticket = np.where(np.isfinite(brand_median_ticket), brand_median_ticket, fallback)
    lt = np.log1p(brand_median_ticket)
    lt = (lt - lt.mean()) / (lt.std() or 1.0)
    V = np.zeros((B, K + len(MERCHANT_TYPES) + 1), np.float32)
    V[np.arange(B), brand_category] = 1.0
    V[np.arange(B), K + brand_type] = 1.0
    V[:, -1] = lt * 0.5
    P = brand_aff @ V
    pn = np.linalg.norm(P, axis=1, keepdims=True)
    vn = np.linalg.norm(V, axis=1, keepdims=True).T
    with np.errstate(divide="ignore", invalid="ignore"):
        content = np.where(pn > 0, (P @ V.T) / (pn * vn), 0.0).astype(np.float32)

    # time-of-day / weekend pattern match
    hours = dt.dt.hour.to_numpy()
    tb = time_bucket(hours)
    wk = (dt.dt.dayofweek.to_numpy() >= 5).astype(np.int8)
    slot = tb * 2 + wk                                    # 8 slots: bucket x weekend
    ct = np.zeros((C, 8), np.float32)
    bt = np.zeros((B, 8), np.float32)
    np.add.at(ct, (ci[counted], slot[counted]), 1)
    np.add.at(bt, (bi[counted], slot[counted]), 1)
    ct = ct / np.maximum(ct.sum(axis=1, keepdims=True), 1)
    bt = bt / np.maximum(bt.sum(axis=1, keepdims=True), 1)
    time_match = (ct @ bt.T).astype(np.float32)
    tm = time_match.max(axis=1, keepdims=True)
    time_match = np.where(tm > 0, time_match / np.where(tm > 0, tm, 1), 0.0).astype(np.float32)

    # popularity by segment and home district
    used = (cnt > 0).astype(np.float32)
    seg = customers["customer_segment"].fillna("UNKNOWN").to_numpy()
    dist = customers["customer_home_district"].fillna("UNKNOWN").to_numpy()
    seg_pop = _group_share(used, seg)
    dist_pop = _group_share(used, dist)

    # proximity
    prov = customers["customer_home_province"].fillna("UNKNOWN").to_numpy()
    phys = merchants[merchants["merchant_type"] == "PHYSICAL"]
    in_dist = _presence(phys, "merchant_district", brand_index, dist)
    in_prov = _presence(phys, "merchant_province", brand_index, prov)
    remote = np.isin(brand_type, [type_code["ONLINE"], type_code["SERVICE"]])
    proximity = np.where(in_dist, 1.0, np.where(in_prov, 0.6, 0.3)).astype(np.float32)
    proximity[:, remote] = 0.8

    # brand momentum: last-30-day spend rate vs prior-90-day rate (population)
    b30 = np.bincount(bi[age_days <= 30], weights=amt[age_days <= 30], minlength=B)
    b120 = np.bincount(bi[(age_days > 30) & (age_days <= 120)], weights=amt[(age_days > 30) & (age_days <= 120)],
                       minlength=B)
    with np.errstate(divide="ignore", invalid="ignore"):
        brand_trend = np.where(b120 > 0, np.clip((b30 / 30) / (b120 / 90), 0, 3), 1.0).astype(np.float32)

    cust = _customer_profile(s, ci, C, cust_ids, as_of, age_days, amt, cntv, tb, wk)

    card_rows = s.assign(brand=s["merchant_id"].map(m["merchant_brand"]),
                         category=s["merchant_id"].map(m["merchant_category"]))
    card_rows = card_rows[card_rows["txn_count"] > 0]
    card_brand_cnt = card_rows.groupby(["customer_id", "card_id", "brand"], observed=True).size().rename("cnt").reset_index()
    card_cat_cnt = card_rows.groupby(["customer_id", "card_id", "category"], observed=True).size().rename("cnt").reset_index()
    card_cnt = card_rows.groupby(["customer_id", "card_id"], observed=True).size().rename("cnt").reset_index()

    return FeatureStore(
        as_of=as_of, window_start=window_start, customers=customers.set_index("customer_id"), brands=brands,
        brand_index=brand_index, cust_index=cust_index, brand_category=brand_category, brand_type=brand_type,
        brand_median_ticket=brand_median_ticket, cnt=cnt, spend=spend, cnt90=cnt90, recency=recency,
        brand_aff=brand_aff, cat_aff=cat_aff, cat_cnt=cat_cnt, collab=collab, content=content,
        time_match=time_match, seg_pop=seg_pop, dist_pop=dist_pop, proximity=proximity, brand_trend=brand_trend,
        cust=cust, card_brand_cnt=card_brand_cnt, card_cat_cnt=card_cat_cnt, card_cnt=card_cnt,
    )


def _group_share(used: np.ndarray, groups: np.ndarray) -> np.ndarray:
    codes, inv = np.unique(groups, return_inverse=True)
    sums = np.zeros((len(codes), used.shape[1]), np.float32)
    np.add.at(sums, inv, used)
    sizes = np.bincount(inv, minlength=len(codes)).astype(np.float32)[:, None]
    share = sums / np.maximum(sizes, 1)
    return share[inv]


def _presence(phys: pd.DataFrame, col: str, brand_index: dict[str, int], cust_geo: np.ndarray) -> np.ndarray:
    geos, inv = np.unique(cust_geo, return_inverse=True)
    gi = {g: i for i, g in enumerate(geos)}
    pres = np.zeros((len(geos), len(brand_index)), bool)
    for b, g in zip(phys["merchant_brand"], phys[col]):
        if g in gi:
            pres[gi[g], brand_index[b]] = True
    return pres[inv]


def _customer_profile(s, ci, C, cust_ids, as_of, age_days, amt, cntv, tb, wk) -> pd.DataFrame:
    def agg(mask):
        c = np.bincount(ci[mask], weights=cntv[mask], minlength=C)
        a = np.bincount(ci[mask], weights=amt[mask], minlength=C)
        return c, a

    c30, a30 = agg(age_days <= 30)
    c90, a90 = agg(age_days <= 90)
    c365, a365 = agg(age_days <= 366)
    counted = cntv > 0
    last_age = np.full(C, np.inf)
    np.minimum.at(last_age, ci[counted], age_days[counted])
    first_age = np.zeros(C)
    np.maximum.at(first_age, ci[counted], age_days[counted])
    gross = np.bincount(ci[counted], weights=amt[counted], minlength=C)
    with np.errstate(divide="ignore", invalid="ignore"):
        avg_ticket = np.where(c365 > 0, gross / np.maximum(c365, 1), 0.0)
        trend = np.where(c90 > 0, np.clip((c30 / 30) / (c90 / 90), 0, 5), 0.0)
    observed_months = np.maximum(np.where(np.isfinite(last_age), first_age, 0) / 30.44, 1.0)
    slot_cnt = np.zeros((C, 4), np.float32)
    np.add.at(slot_cnt, (ci[counted], tb[counted]), 1)
    wk_cnt = np.bincount(ci[counted], weights=wk[counted].astype(float), minlength=C)
    fav = np.array(TIME_BUCKETS)[slot_cnt.argmax(axis=1)]
    level = np.where(c365 == 0, "NONE", np.where(c365 < LIMITED_HISTORY_TXNS, "LIMITED", "ESTABLISHED"))
    return pd.DataFrame({
        "cnt_30": c30, "spend_30": a30, "cnt_90": c90, "spend_90": a90, "cnt_365": c365, "spend_365": a365,
        "recency_days": np.where(np.isfinite(last_age), last_age, 999.0),
        "avg_ticket": avg_ticket, "trend": trend, "txn_per_month": c365 / observed_months,
        "weekend_share": np.where(c365 > 0, wk_cnt / np.maximum(c365, 1), 0.0),
        "favourite_time_of_day": np.where(c365 > 0, fav, None), "history_level": level,
    }, index=pd.Index(cust_ids, name="customer_id"))
