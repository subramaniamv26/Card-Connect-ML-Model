"use client";

import Link from "next/link";
import { use } from "react";
import OfferCard, { RecOffer } from "@/components/OfferCard";
import { Badge, Bars, Card, Empty, ErrorBox, Loading, Notice, Stat } from "@/components/ui";
import { fmtDateTime, fmtLKR, fmtNum, useApi } from "@/lib/api";

interface Recs {
  customer: Record<string, string | number | boolean | null>;
  recommendation_status: string; shortfall_reason: string | null; offers: RecOffer[];
  generated_at: string; data_as_of: string; model_version: string; aggregation_period_start: string;
  aggregation_period_end: string; pii_masked: boolean;
}
interface Profile {
  customer: Record<string, string | number | null>;
  window: { start: string; end_exclusive: string; data_cutoff: string; basis: string };
  rfm: { cnt_30: number; spend_30: number; cnt_90: number; spend_90: number; cnt_365: number; spend_365: number;
         last_transaction: string | null; recency_days: number | null; average_ticket: number | null };
  categories: { category: string; txns: number; spend_lkr: number }[];
  top_brands: { brand: string; category: string; txns: number; spend_lkr: number; last_txn: string }[];
  monthly: { month: string; txns: number; spend_lkr: number }[];
  time_of_day: { time_of_day: string; txns: number }[];
  day_of_week: { day_of_week: string; txns: number }[];
  cards: { card_id: string; card_network: string; card_type: string; card_tier: string; card_status: string; txns: number }[];
}

const DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"];
const TIMES = ["MORNING", "DAY", "EVENING", "NIGHT"];

export default function CustomerPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const recs = useApi<Recs>(`customers/${encodeURIComponent(id)}/recommendations`);
  const prof = useApi<Profile>(`customers/${encodeURIComponent(id)}/profile`);

  return (
    <div className="stack">
      <div className="row">
        <div>
          <p className="small"><Link href="/customers">← Customers</Link></p>
          <h1>{id}</h1>
          {recs.data && (
            <p className="sub" style={{ marginBottom: 0 }}>
              {String(recs.data.customer.customer_name)} · {String(recs.data.customer.customer_home_city)},{" "}
              {String(recs.data.customer.customer_home_district)}, {String(recs.data.customer.customer_home_province)}
            </p>
          )}
        </div>
      </div>

      <Card title="Top three offers" actions={recs.data && <Badge value={recs.data.recommendation_status} />}>
        {recs.loading ? <Loading /> : recs.error ? <ErrorBox message={recs.error} onRetry={recs.reload} /> : (
          <>
            {recs.data!.shortfall_reason && <Notice tone="warn">{recs.data!.shortfall_reason}</Notice>}
            {recs.data!.offers.length ? (
              <div className="grid grid-3">{recs.data!.offers.map((o) => <OfferCard key={o.offer_id} o={o} />)}</div>
            ) : <Empty>No eligible offers for this customer.</Empty>}
            <p className="small muted" style={{ marginTop: 10 }}>
              Model <span className="mono">{recs.data!.model_version}</span> · generated {fmtDateTime(recs.data!.generated_at)} ·
              data as of {recs.data!.data_as_of} · eligibility re-checked when this page loaded
              {recs.data!.pii_masked && " · contact and device details masked for your role"}
            </p>
          </>
        )}
      </Card>

      {prof.loading ? <Loading label="Loading spending profile…" /> : prof.error ? <ErrorBox message={prof.error} /> : (() => {
        const p = prof.data!;
        const dow = DAYS.map((d) => ({ label: d, value: p.day_of_week.find((x) => x.day_of_week === d)?.txns ?? 0 }));
        const tod = TIMES.map((t) => ({ label: t, value: p.time_of_day.find((x) => x.time_of_day === t)?.txns ?? 0 }));
        return (
          <>
            <div className="grid grid-4">
              <Stat label="Last 30 days" value={fmtLKR(p.rfm.spend_30)} hint={`${p.rfm.cnt_30} transactions`} />
              <Stat label="Last 90 days" value={fmtLKR(p.rfm.spend_90)} hint={`${p.rfm.cnt_90} transactions`} />
              <Stat label="History window" value={fmtLKR(p.rfm.spend_365)} hint={`${fmtNum(p.rfm.cnt_365)} transactions`} />
              <Stat label="Recency" value={p.rfm.recency_days === null ? "—" : `${p.rfm.recency_days} days`}
                    hint={`Average ticket ${fmtLKR(p.rfm.average_ticket)}`} />
            </div>
            <p className="small muted">Window {p.window.start} to {p.window.data_cutoff} (data cutoff). {p.window.basis}</p>
            <div className="grid grid-2">
              <Card title="Spend by category"><Bars rows={p.categories.map((c) => ({ label: c.category, value: c.spend_lkr }))} format={(v) => fmtLKR(v)} /></Card>
              <Card title="Monthly trend"><Bars rows={p.monthly.map((m) => ({ label: m.month, value: m.spend_lkr }))} format={(v) => fmtLKR(v)} /></Card>
              <Card title="Day of week"><Bars rows={dow} /></Card>
              <Card title="Time of day"><Bars rows={tod} /></Card>
              <Card title="Favourite brands">
                <div className="table-wrap"><table>
                  <thead><tr><th>Brand</th><th>Category</th><th className="num">Txns</th><th className="num">Spend</th><th>Last</th></tr></thead>
                  <tbody>{p.top_brands.map((b) => (
                    <tr key={b.brand}><td>{b.brand}</td><td>{b.category}</td><td className="num">{b.txns}</td>
                      <td className="num">{fmtLKR(b.spend_lkr)}</td><td>{b.last_txn?.slice(0, 10)}</td></tr>))}</tbody>
                </table></div>
              </Card>
              <Card title="Cards">
                <div className="table-wrap"><table>
                  <thead><tr><th>Card</th><th>Network</th><th>Type</th><th>Tier</th><th>Status</th><th className="num">Txns</th></tr></thead>
                  <tbody>{p.cards.map((c) => (
                    <tr key={c.card_id}><td className="mono">{c.card_id}</td><td>{c.card_network}</td><td>{c.card_type}</td>
                      <td>{c.card_tier}</td><td><Badge value={c.card_status} /></td><td className="num">{c.txns}</td></tr>))}</tbody>
                </table></div>
              </Card>
            </div>
          </>
        );
      })()}
    </div>
  );
}
