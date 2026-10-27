"use client";

import { useEffect, useState } from "react";
import { Badge, Card, Empty, ErrorBox, Loading, Notice, Pager, Stat } from "@/components/ui";
import { api, atLeast, fmtDateTime, fmtNum, fmtPct, qs, useApi, useUser } from "@/lib/api";

interface MethodMetrics { precision_at_3: number | null; recall_at_3: number | null; ndcg_at_3: number | null;
  discovery_recall_at_3: number | null; share_of_recs_new_to_customer: number; evaluated_customers: number }
interface Run {
  run_id: number; trigger_type: string; requested_by: string; status: string; attempt: number; model_version: string | null;
  data_cutoff: string | null; window_start: string | null; transaction_rows: number | null; label_rows: number | null;
  promoted: string; promotion_decision: string | null; batch_id: number | null; error_message: string | null;
  created_at: string; started_at: string | null; finished_at: string | null; duration_seconds: number | null;
  metrics: { candidate?: MethodMetrics; batch_quality?: Record<string, number | null | Record<string, number>>;
             ranker_validation?: Record<string, number | string | string[] | null>; ranker_decision?: string; serving_model?: string } | null;
}
interface Status { last_run: Run | null; last_successful_run: Run | null; active_model: { model_version: string; algorithm: string; promoted_at?: string };
  next_scheduled_run: string; schedule: string; worker_heartbeat: string | null; queued_or_running: number }
interface PageT { items: Run[]; page: number; pages: number; total: number }
interface FullRun extends Omit<Run, "metrics"> { metrics: { holdout: { holdout_start: string; holdout_end_exclusive: string;
  sampled_customers: number; candidate_brands: number; methods: Record<string, MethodMetrics> } } | null }

const n4 = (v: number | null | undefined) => (v === null || v === undefined ? "—" : v.toFixed(4));

export default function TrainingPage() {
  const user = useUser();
  const st = useApi<Status>("training/status");
  const [page, setPage] = useState(1);
  const runs = useApi<PageT>(`training/runs${qs({ page, page_size: 10 })}`);
  const okId = st.data?.last_successful_run?.run_id;
  const full = useApi<FullRun>(okId ? `training/runs/${okId}` : null, [okId]);
  const [msg, setMsg] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);

  const active = (st.data?.queued_or_running ?? 0) > 0;
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => { st.reload(); runs.reload(); }, 5000);
    return () => clearInterval(t);
  }, [active, st.reload, runs.reload]);

  async function trainNow() {
    try {
      const r = await api<{ run_id: number }>("training/runs", { method: "POST" });
      setMsg({ tone: "ok", text: `Training run #${r.run_id} queued. The worker will pick it up shortly.` });
      st.reload(); runs.reload();
    } catch (e) {
      setMsg({ tone: "bad", text: e instanceof Error ? e.message : "Could not start training" });
    }
  }

  if (st.loading) return <Loading />;
  if (st.error) return <ErrorBox message={st.error} onRetry={st.reload} />;
  const s = st.data!;
  const q = s.last_successful_run?.metrics?.batch_quality;
  const rv = s.last_successful_run?.metrics?.ranker_validation;
  return (
    <div className="stack">
      <div className="row">
        <div><h1>Model training</h1><p className="sub">Scheduled {s.schedule} on the latest rolling 12 months of transactions. Runs never overlap.</p></div>
        <span className="spacer" />
        {atLeast(user, "ADMIN") && <button className="btn btn-primary" disabled={active} onClick={trainNow}>{active ? "Run in progress…" : "Train now"}</button>}
      </div>
      {msg && <Notice tone={msg.tone}>{msg.text}</Notice>}
      <div className="grid grid-4">
        <Stat label="Active model" value={<span className="mono" style={{ fontSize: "0.95rem" }}>{s.active_model.model_version}</span>} hint={s.active_model.algorithm} />
        <Stat label="Last run" value={s.last_run ? <Badge value={s.last_run.status} /> : "—"} hint={s.last_run ? `#${s.last_run.run_id} · ${fmtDateTime(s.last_run.finished_at ?? s.last_run.created_at)}` : undefined} />
        <Stat label="Last successful run" value={s.last_successful_run ? `#${s.last_successful_run.run_id}` : "—"}
              hint={s.last_successful_run ? `${fmtDateTime(s.last_successful_run.finished_at)} · data to ${s.last_successful_run.data_cutoff?.slice(0, 10)}` : undefined} />
        <Stat label="Next scheduled run" value={<span style={{ fontSize: "1rem" }}>{fmtDateTime(s.next_scheduled_run)}</span>}
              hint={s.worker_heartbeat ? `Worker heartbeat ${fmtDateTime(s.worker_heartbeat)}` : "No worker heartbeat yet"} />
      </div>

      <Card title="Hold-out evaluation (last successful run)">
        {!okId ? <Empty>No successful run yet.</Empty> : full.loading ? <Loading /> : full.error ? <ErrorBox message={full.error} /> : full.data?.metrics ? (
          <>
            <p className="small muted">Features built as of {full.data.metrics.holdout.holdout_start}; ground truth = participating brands purchased from
              {" "}{full.data.metrics.holdout.holdout_start} to {full.data.metrics.holdout.holdout_end_exclusive} (exclusive) by {fmtNum(full.data.metrics.holdout.sampled_customers)} sampled
              customers over {full.data.metrics.holdout.candidate_brands} brands. This measures purchase prediction, not offer-caused redemption.</p>
            <div className="table-wrap"><table>
              <thead><tr><th>Method</th><th className="num">Precision@3</th><th className="num">Recall@3</th><th className="num">NDCG@3</th>
                <th className="num">Discovery recall@3</th><th className="num">New-to-customer share</th></tr></thead>
              <tbody>{Object.entries(full.data.metrics.holdout.methods).map(([k, m]) => (
                <tr key={k}><td>{k.replaceAll("_", " ")}</td><td className="num">{n4(m.precision_at_3)}</td><td className="num">{n4(m.recall_at_3)}</td>
                  <td className="num">{n4(m.ndcg_at_3)}</td><td className="num">{n4(m.discovery_recall_at_3)}</td><td className="num">{fmtPct(m.share_of_recs_new_to_customer)}</td></tr>))}</tbody>
            </table></div>
          </>
        ) : <Empty>No metrics recorded.</Empty>}
      </Card>

      <div className="grid grid-2">
        <Card title="Response ranker (historical offer redemptions)">
          {rv ? (
            <>
              <dl className="kv">
                <dt>Validation wave</dt><dd>{String(rv.wave_start)} ({fmtNum(rv.rows as number)} deliveries, {fmtNum(rv.positives as number)} redeemed)</dd>
                <dt>Ranker AUC</dt><dd>{n4(rv.ranker_auc as number)}</dd>
                <dt>Heuristic AUC</dt><dd>{n4(rv.heuristic_auc as number)}</dd>
                <dt>Rules baseline AUC</dt><dd>{n4(rv.baseline_auc as number)}</dd>
                <dt>Average precision</dt><dd>{n4(rv.ranker_average_precision as number)}</dd>
              </dl>
              <p className="small muted" style={{ marginTop: 8 }}>{s.last_successful_run?.metrics?.ranker_decision}</p>
              <p className="small muted">Scores rank offers; they are not calibrated redemption probabilities (historical deliveries were targeted, not randomised).</p>
            </>
          ) : <Empty>No ranker validation available.</Empty>}
        </Card>
        <Card title="Published batch quality">
          {q ? (
            <dl className="kv">
              <dt>Coverage (≥1 offer)</dt><dd>{fmtPct(q.coverage_any as number)}</dd>
              <dt>Coverage (3 offers)</dt><dd>{fmtPct(q.coverage_full_3 as number)}</dd>
              <dt>Category diversity</dt><dd>{fmtPct(q.intra_list_category_diversity as number)}</dd>
              <dt>Catalog coverage</dt><dd>{fmtPct(q.catalog_coverage as number)}</dd>
              <dt>Eligibility compliance</dt><dd>{fmtPct(q.eligibility_compliance as number, 2)} of {fmtNum(q.eligibility_checked as number)} rows re-checked</dd>
            </dl>
          ) : <Empty>No batch metrics.</Empty>}
        </Card>
      </div>

      <Card title="Run history">
        {runs.loading && !runs.data ? <Loading /> : runs.error ? <ErrorBox message={runs.error} /> : !runs.data!.items.length ? <Empty>No runs yet.</Empty> : (
          <>
            <div className="table-wrap"><table>
              <thead><tr><th>#</th><th>Trigger</th><th>Status</th><th>Model</th><th>Data cutoff</th><th className="num">Txns</th>
                <th className="num">Duration</th><th>Promotion / error</th><th>Finished</th></tr></thead>
              <tbody>{runs.data!.items.map((r) => (
                <tr key={r.run_id}><td>{r.run_id}</td><td>{r.trigger_type.toLowerCase()} · {r.requested_by}</td>
                  <td><Badge value={r.status} />{r.attempt > 1 && <div className="small muted">attempt {r.attempt}</div>}</td>
                  <td className="mono small">{r.model_version ?? "—"}</td><td>{r.data_cutoff?.slice(0, 10) ?? "—"}</td>
                  <td className="num">{fmtNum(r.transaction_rows)}</td><td className="num">{r.duration_seconds ? `${Math.round(r.duration_seconds)} s` : "—"}</td>
                  <td className="small" style={{ maxWidth: 380 }}>{r.error_message ?? r.promotion_decision ?? "—"}</td>
                  <td className="small">{fmtDateTime(r.finished_at)}</td></tr>))}</tbody>
            </table></div>
            <Pager page={runs.data!.page} pages={runs.data!.pages} total={runs.data!.total} onPage={setPage} />
          </>
        )}
      </Card>
    </div>
  );
}
