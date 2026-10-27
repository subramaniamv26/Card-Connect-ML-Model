"""Net-spend rules applied before any aggregation or feature engineering.

* Only transaction_status = APPROVED counts. DECLINED, CANCELLED and REVERSED are excluded.
* PURCHASE, BILL_PAYMENT, RECURRING_PAYMENT add +amount_lkr and count as one transaction.
* REFUND subtracts its amount_lkr from spend with that merchant and does not change the count.
  (A refund only reduces totals; aggregated spend is floored at 0 per customer-merchant pair.)
* REVERSAL with related_transaction_id cancels the referenced transaction completely (amount and count);
  the reversal row itself is not counted. A reversal whose original is outside the window is ignored.
* All amounts are in LKR: amount_lkr = transaction_amount x fx_rate_to_lkr, computed at import from
  FX_RATES (USD fixed at 300 for this synthetic dataset). Currencies are never mixed unconverted.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

COUNTED_TYPES = ("PURCHASE", "BILL_PAYMENT", "RECURRING_PAYMENT")


def net_spend_frame(txns: pd.DataFrame) -> pd.DataFrame:
    """Input columns: transaction_id, transaction_datetime, customer_id, card_id, merchant_id, amount_lkr,
    transaction_type, transaction_status, related_transaction_id.
    Output: one row per counted movement with columns signed_amount (LKR) and txn_count (0/1)."""
    df = txns[txns["transaction_status"] == "APPROVED"]
    reversals = df[df["transaction_type"] == "REVERSAL"]
    reversed_ids = set(reversals["related_transaction_id"].dropna())
    df = df[(df["transaction_type"] != "REVERSAL") & ~df["transaction_id"].isin(reversed_ids)]
    df = df[df["transaction_type"].isin(COUNTED_TYPES + ("REFUND",))].copy()
    is_refund = (df["transaction_type"] == "REFUND").to_numpy()
    amount = df["amount_lkr"].astype("float64").to_numpy()
    df["signed_amount"] = np.where(is_refund, -amount, amount)
    df["txn_count"] = np.where(is_refund, 0, 1).astype("int32")
    return df
