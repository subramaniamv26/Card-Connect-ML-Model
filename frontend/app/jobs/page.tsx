"use client";

import { FormEvent, useEffect, useState } from "react";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Notice, Pager } from "@/components/ui";
import { api, atLeast, fmtDateTime, fmtNum, qs, useApi, useUser } from "@/lib/api";

interface Job {
  job_id: number; job_type: string; status: string; requested_by: string; include_pii: string; batch_id: number | null;
  progress_pct: number; row_count: number | null; error_message: string | null; created_at: string;
  finished_at: string | null; expires_at: string | null; download_url: string | null;
  parameters: { format: string; filters: Record<string, string | null> } | null;
}
interface PageT { items: Job[]; page: number; pages: number; total: number }

export default function JobsPage() {
  const user = useUser();
  const [page, setPage] = useState(1);
  const jobs = useApi<PageT>(`recommendation-jobs${qs({ page, page_size: 15 })}`);
  const [msg, setMsg] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);
  const running = jobs.data?.items.some((j) => ["QUEUED", "RUNNING"].includes(j.status));
  useEffect(() => {
    if (!running) return;
    const t = setInterval(jobs.reload, 3000);
    return () => clearInterval(t);
  }, [running, jobs.reload]);

  async function create(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const filters: Record<string, string> = {};
    for (const k of ["status", "district", "province", "segment", "offer_id"]) {
      const v = String(f.get(k) ?? "").trim();
      if (v) filters[k] = v;
    }
    try {
      const r = await api<{ job_id: number }>("recommendation-jobs", { method: "POST", body: {
        job_type: f.get("job_type"), format: f.get("format"), include_pii: f.get("include_pii") === "on", filters } });
      setMsg({ tone: "ok", text: `Job #${r.job_id} queued.` });
      jobs.reload();
    } catch (err) {
      setMsg({ tone: "bad", text: err instanceof Error ? err.message : "Could not create job" });
    }
  }

  return (
    <div className="stack">
      <div><h1>Bulk recommendation jobs</h1><p className="sub">Exports read the current published batch with live eligibility checks. Personal data is masked unless an administrator requests it; every download is audited.</p></div>
      {atLeast(user, "ANALYST") ? (
        <Card title="New job">
          <form className="filters" onSubmit={create}>
            <Field label="Job"><select name="job_type">
              <option value="RECOMMENDATION_EXPORT">Export recommendations</option>
              {atLeast(user, "ADMIN") && <option value="RECOMMENDATION_REFRESH">Regenerate with active model</option>}
            </select></Field>
            <Field label="Format"><select name="format"><option value="csv">CSV (one row per offer)</option><option value="jsonl">JSON lines</option></select></Field>
            <Field label="Status"><select name="status"><option value="">Any</option><option>FULL</option><option>PARTIAL</option><option>NONE</option></select></Field>
            <Field label="District"><input name="district" /></Field>
            <Field label="Province"><input name="province" /></Field>
            <Field label="Segment"><input name="segment" /></Field>
            <Field label="Offer ID"><input name="offer_id" /></Field>
            {atLeast(user, "ADMIN") && <label className="row small"><input type="checkbox" name="include_pii" /> Include contact and device details</label>}
            <button className="btn btn-primary">Start job</button>
          </form>
          {msg && <Notice tone={msg.tone}>{msg.text}</Notice>}
        </Card>
      ) : <Notice>Creating exports requires the analyst or administrator role.</Notice>}
      <Card title="Jobs">
        {jobs.loading && !jobs.data ? <Loading /> : jobs.error ? <ErrorBox message={jobs.error} onRetry={jobs.reload} /> : !jobs.data!.items.length ? <Empty>No jobs yet.</Empty> : (
          <>
            <div className="table-wrap"><table>
              <thead><tr><th>#</th><th>Type</th><th>Status</th><th className="num">Progress</th><th className="num">Rows</th><th>Filters</th>
                <th>PII</th><th>By</th><th>Finished</th><th>Expires</th><th /></tr></thead>
              <tbody>{jobs.data!.items.map((j) => (
                <tr key={j.job_id}><td>{j.job_id}</td><td className="small">{j.job_type.replace("RECOMMENDATION_", "").toLowerCase()}</td>
                  <td><Badge value={j.status} />{j.error_message && <div className="small" style={{ color: "var(--bad)" }}>{j.error_message}</div>}</td>
                  <td className="num">{j.progress_pct.toFixed(0)}%</td><td className="num">{fmtNum(j.row_count)}</td>
                  <td className="small">{Object.entries(j.parameters?.filters ?? {}).filter(([, v]) => v).map(([k, v]) => `${k}=${v}`).join(", ") || "all"}</td>
                  <td>{j.include_pii === "Y" ? "Yes" : "Masked"}</td><td>{j.requested_by}</td>
                  <td className="small">{fmtDateTime(j.finished_at)}</td><td className="small">{fmtDateTime(j.expires_at)}</td>
                  <td>{j.download_url && atLeast(user, "ANALYST") && <a className="btn btn-small" href={`/api/proxy/recommendation-jobs/${j.job_id}/download`}>Download</a>}</td>
                </tr>))}</tbody>
            </table></div>
            <Pager page={jobs.data!.page} pages={jobs.data!.pages} total={jobs.data!.total} onPage={setPage} />
          </>
        )}
      </Card>
    </div>
  );
}
