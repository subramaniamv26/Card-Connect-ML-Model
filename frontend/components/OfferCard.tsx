"use client";

import { fmtDate, fmtLKR, fmtNum } from "@/lib/api";

export interface RecOffer {
  rank: number;
  offer_id: string;
  offer_title: string;
  offer_description: string;
  recommendation_score: number;
  recommendation_reason: string;
  reason_codes: string[];
  offer_type: string;
  offer_value: number;
  minimum_spend: number;
  maximum_benefit: number | null;
  estimated_customer_saving: number | null;
  estimated_customer_saving_assumptions: string | null;
  currency: string;
  valid_from: string;
  valid_to: string;
  terms_and_conditions: string;
  is_sample_offer: boolean;
  card: { card_id: string; card_network: string; card_type: string };
  merchant: Record<string, string>;
  customer_merchant_spend: {
    merchant_transaction_amount_last_3_months: number;
    merchant_transaction_count_last_3_months: number;
    last_transaction_date_with_merchant: string | null;
    average_transaction_amount_with_merchant: number | null;
    aggregation_period_start: string;
    aggregation_period_end: string;
  };
}

const valueText = (o: RecOffer) =>
  o.offer_type.startsWith("PERCENT") ? `${o.offer_value}% ${o.offer_type.endsWith("CASHBACK") ? "cashback" : "off"}`
    : `${fmtLKR(o.offer_value)} ${o.offer_type.endsWith("CASHBACK") ? "cashback" : "off"}`;

export default function OfferCard({ o }: { o: RecOffer }) {
  const s = o.customer_merchant_spend;
  return (
    <article className="offer" aria-label={`Rank ${o.rank}: ${o.offer_title}`}>
      <div className="row">
        <span className="offer-rank" aria-hidden>{o.rank}</span>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div className="offer-title">{o.offer_title}</div>
          <div className="muted small">{o.merchant.merchant_brand} · {o.merchant.merchant_category} · {valueText(o)}</div>
        </div>
        {o.is_sample_offer && <span className="badge badge-warn" title="Synthetic sample offer for testing">SAMPLE</span>}
      </div>
      <p className="offer-reason">{o.recommendation_reason}</p>
      <dl className="kv">
        <dt>Estimated saving</dt><dd>{fmtLKR(o.estimated_customer_saving)}</dd>
        <dt>Min spend / cap</dt><dd>{fmtLKR(o.minimum_spend)} / {o.maximum_benefit ? fmtLKR(o.maximum_benefit) : "no cap"}</dd>
        <dt>Card</dt><dd>{o.card.card_network} {o.card.card_type} · <span className="mono">{o.card.card_id}</span></dd>
        <dt>Outlet</dt><dd>{o.merchant.merchant_location}, {o.merchant.merchant_district}</dd>
        <dt>Last 3 months</dt><dd>{fmtLKR(s.merchant_transaction_amount_last_3_months)} · {fmtNum(s.merchant_transaction_count_last_3_months)} txns</dd>
        <dt>Last visit / avg</dt><dd>{fmtDate(s.last_transaction_date_with_merchant)} · {fmtLKR(s.average_transaction_amount_with_merchant)}</dd>
        <dt>Valid</dt><dd>{o.valid_from} → {o.valid_to}</dd>
        <dt>Score</dt><dd>{o.recommendation_score.toFixed(3)} <span className="muted small">(ranking score, not a probability)</span></dd>
      </dl>
      <details>
        <summary>Assumptions and terms</summary>
        <p className="small muted" style={{ marginTop: 6 }}>{o.estimated_customer_saving_assumptions}</p>
        <p className="small">{o.terms_and_conditions}</p>
        <p className="small muted">Aggregation period {s.aggregation_period_start} to {s.aggregation_period_end} (LKR, net of refunds).</p>
      </details>
    </article>
  );
}
