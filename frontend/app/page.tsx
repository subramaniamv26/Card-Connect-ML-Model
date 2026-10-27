"use client";

import Link from "next/link";
import { Badge, Bars, Card, ErrorBox, Loading, Notice, Stat } from "@/components/ui";
import { fmtDateTime, fmtLKR, fmtNum, fmtPct, useApi } from "@/lib/api";

interface Overview {
  counts: { customers: number; cards: number; merchants: number; transactions: number; last_txn: string | null;
            live_offers: number; pending_offers: number; catalog_offers: number };
  recommendations: null | { batch_id: number; model_version: string; generated_at: string; data_as_of: string;
                            customers: number; full: number; partial: number; none: number; coverage_any: number;
                            coverage_full: number };
  top_recommended_offers: { offer_id: string; offer_title: string; merchant_category: string; customers: number }[];
  category_spend_last_30_days: { category: string; txns: number; spend_lkr: number }[];
}

interface TrainingStatus {
  last_run: { run_id: number; status: string; finished_at: string | null } | null;
  last_successful_run: { run_id: number; finished_at: string; data_cutoff: string } | null;
  active_model: { model_version: string };
  next_scheduled_run: string;
  worker_heartbeat: string | null;
}

export default function OverviewPage() {
  const ov = useApi<Overview>("dashboard/overview");
  const tr = useApi<TrainingStatus>("training/status");
  if (ov.loading) return <Loading />;
  if (ov.error) return <ErrorBox message={ov.error} onRetry={ov.reload} />;
  const d = ov.data!;
  const r = d.recommendations;
  return (
    <div className="stack">
      <div>
        <h1>Overview</h1>
        <p className="sub">Transactions to {d.counts.last_txn ? d.counts.last_txn.slice(0, 10) : "—"} · recommendations
          {r ? ` generated ${fmtDateTime(r.generated_at)}` : " not generated yet"}</p>
      </div>
      {d.counts.pending_offers > 0 && (
        <Notice tone="warn">{d.counts.pending_offers} offer{d.counts.pending_offers > 1 ? "s are" : " is"} awaiting approval
          and will not be recommended until approved. <Link href="/offers?status=PENDING_APPROVAL">Review</Link></Notice>
      )}
      <div className="grid grid-4">
        <Stat label="Customers" value={fmtNum(d.counts.customers)} hint={`${fmtNum(d.counts.cards)} cards`} />
        <Stat label="Transactions" value={fmtNum(d.counts.transactions)} hint={`${fmtNum(d.counts.merchants)} merchant outlets`} />
        <Stat label="Live approved offers" value={fmtNum(d.counts.live_offers)} hint={`${fmtNum(d.counts.catalog_offers)} in catalog`} />
        <Stat label="Recommendation coverage" value={r ? fmtPct(r.coverage_any) : "—"}
              hint={r ? `${fmtPct(r.coverage_full)} with all three offers` : "Run training to generate"} />
      </div>
      <div className="grid grid-2">
        <Card title="Recommendation batch" actions={r && <Link href="/customers" className="small">Browse customers</Link>}>
          {r ? (
            <dl className="kv">
              <dt>Batch</dt><dd>#{r.batch_id}</dd>
              <dt>Model</dt><dd className="mono">{r.model_version}</dd>
              <dt>Data as of</dt><dd>{r.data_as_of}</dd>
              <dt>Full (3 offers)</dt><dd>{fmtNum(r.full)}</dd>
              <dt>Partial</dt><dd>{fmtNum(r.partial)}</dd>
              <dt>None</dt><dd>{fmtNum(r.none)}</dd>
            </dl>
          ) : <p className="muted">No batch has been published yet.</p>}
        </Card>
        <Card title="Training" actions={<Link href="/training" className="small">Details</Link>}>
          {tr.loading ? <Loading /> : tr.error ? <ErrorBox message={tr.error} /> : (
            <dl className="kv">
              <dt>Active model</dt><dd className="mono">{tr.data!.active_model.model_version}</dd>
              <dt>Last run</dt><dd>{tr.data!.last_run ? <>#{tr.data!.last_run.run_id} <Badge value={tr.data!.last_run.status} /></> : "—"}</dd>
              <dt>Last success</dt><dd>{fmtDateTime(tr.data!.last_successful_run?.finished_at)}</dd>
              <dt>Next scheduled</dt><dd>{fmtDateTime(tr.data!.next_scheduled_run)}</dd>
              <dt>Worker heartbeat</dt><dd>{fmtDateTime(tr.data!.worker_heartbeat)}</dd>
            </dl>
          )}
        </Card>
        <Card title="Most recommended offers">
          {d.top_recommended_offers.length ? (
            <Bars rows={d.top_recommended_offers.map((o) => ({ label: o.offer_title.replace("[SAMPLE] ", ""), value: o.customers }))} />
          ) : <p className="muted">No recommendations yet.</p>}
        </Card>
        <Card title="Approved spend by category (last 30 days)">
          <Bars rows={d.category_spend_last_30_days.map((c) => ({ label: c.category, value: c.spend_lkr }))}
                format={(v) => fmtLKR(v / 1e6, 1) + "M"} />
        </Card>
      </div>
    </div>
  );
}
