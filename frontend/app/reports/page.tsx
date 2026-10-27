"use client";

import { Badge, Card, Empty, ErrorBox, Loading, Notice, Stat } from "@/components/ui";
import { fmtLKR, fmtNum, fmtPct, useApi } from "@/lib/api";

interface Row {
  offer_id: string; offer_title: string; merchant_brand: string | null; merchant_category: string; status: string;
  is_sample: boolean; campaign_budget: number | null; budget_consumed: number | null; impressions: number; clicks: number;
  redemptions: number; dismissals: number; recommended_customers: number; click_through_rate: number | null;
  redemption_rate_per_impression: number | null;
}
interface Report {
  catalog_offers: Row[];
  live_event_totals: Record<string, number>;
  historical_campaigns: { delivered: number; redeemed: number; note: string;
    by_targeting_type: { targeting_type: string; delivered: number; redeemed: number }[] };
}

export default function ReportsPage() {
  const { data, error, loading, reload } = useApi<Report>("reports/engagement");
  if (loading) return <Loading />;
  if (error) return <ErrorBox message={error} onRetry={reload} />;
  const d = data!;
  const t = d.live_event_totals;
  const h = d.historical_campaigns;
  return (
    <div className="stack">
      <div><h1>Engagement and redemptions</h1><p className="sub">Live feedback recorded through the events API, plus historical campaign outcomes from the source data.</p></div>
      <div className="grid grid-4">
        <Stat label="Impressions" value={fmtNum(t.impressions)} />
        <Stat label="Clicks" value={fmtNum(t.clicks)} hint={t.impressions ? `CTR ${fmtPct(t.clicks / t.impressions)}` : undefined} />
        <Stat label="Redemptions" value={fmtNum(t.redemptions)} />
        <Stat label="Dismissals" value={fmtNum(t.dismissals)} />
      </div>
      {t.impressions === 0 && <Notice>No live engagement events have been recorded yet. Integrations post them to <span className="mono">POST /api/v1/events</span>.</Notice>}
      <Card title="Catalog offers">
        {!d.catalog_offers.length ? <Empty>No offers in the catalog.</Empty> : (
          <div className="table-wrap"><table>
            <thead><tr><th>Offer</th><th>Status</th><th className="num">Recommended to</th><th className="num">Impressions</th><th className="num">Clicks</th>
              <th className="num">CTR</th><th className="num">Redemptions</th><th className="num">Dismissals</th><th className="num">Budget used</th></tr></thead>
            <tbody>{d.catalog_offers.map((r) => (
              <tr key={r.offer_id}><td>{r.offer_title}<div className="muted small">{r.merchant_brand} · {r.merchant_category}</div></td>
                <td><Badge value={r.status} /></td><td className="num">{fmtNum(r.recommended_customers)}</td>
                <td className="num">{fmtNum(r.impressions)}</td><td className="num">{fmtNum(r.clicks)}</td><td className="num">{fmtPct(r.click_through_rate)}</td>
                <td className="num">{fmtNum(r.redemptions)}</td><td className="num">{fmtNum(r.dismissals)}</td>
                <td className="num">{r.campaign_budget ? `${fmtLKR(r.budget_consumed)} / ${fmtLKR(r.campaign_budget)}` : "—"}</td></tr>))}</tbody>
          </table></div>
        )}
      </Card>
      <Card title="Historical campaigns (source data)">
        <p className="small muted">{h.note}</p>
        <div className="grid grid-4" style={{ marginBottom: 12 }}>
          <Stat label="Delivered" value={fmtNum(h.delivered)} />
          <Stat label="Redeemed" value={fmtNum(h.redeemed)} hint={h.delivered ? `${fmtPct(h.redeemed / h.delivered)} redemption rate` : undefined} />
        </div>
        <div className="table-wrap"><table>
          <thead><tr><th>Targeting type</th><th className="num">Delivered</th><th className="num">Redeemed</th><th className="num">Rate</th></tr></thead>
          <tbody>{h.by_targeting_type.map((r) => (
            <tr key={r.targeting_type}><td>{r.targeting_type}</td><td className="num">{fmtNum(r.delivered)}</td><td className="num">{fmtNum(r.redeemed)}</td>
              <td className="num">{fmtPct(r.delivered ? r.redeemed / r.delivered : null)}</td></tr>))}</tbody>
        </table></div>
      </Card>
    </div>
  );
}
