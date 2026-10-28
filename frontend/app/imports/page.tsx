"use client";

import { FormEvent, useEffect, useState } from "react";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Notice, Pager } from "@/components/ui";
import { api, atLeast, fmtDateTime, fmtNum, qs, useApi, useUser } from "@/lib/api";

interface Dataset { dataset: string; label: string; load_order: number; example_file: string; required_columns: string[]; optional_columns: string[] }
interface Imp {
  import_id: number; dataset: string; file_name: string; status: string; rows_total: number; rows_valid: number;
  rows_rejected: number; rows_inserted: number; rows_updated: number; rows_unchanged: number; uploaded_by: string;
  created_at: string; finished_at: string | null;
  error_summary: { error_counts?: Record<string, number>; message?: string; ignored_columns?: string[]; absent_optional_columns?: string[] } | null;
}
interface Rej { row_number: number; column_name: string | null; error_code: string; error_message: string; raw_record: string | null }
interface PageT<T> { items: T[]; page: number; pages: number; total: number }
const ACTIVE = ["RECEIVED", "VALIDATING", "LOADING"];

export default function ImportsPage() {
  const user = useUser();
  const ds = useApi<Dataset[]>("imports/datasets");
  const [page, setPage] = useState(1);
  const hist = useApi<PageT<Imp>>(`imports${qs({ page, page_size: 15 })}`);
  const [sel, setSel] = useState<Imp | null>(null);
  const [rejPage, setRejPage] = useState(1);
  const rej = useApi<PageT<Rej>>(sel ? `imports/${sel.import_id}/rejections${qs({ page: rejPage, page_size: 20 })}` : null);
  const [dataset, setDataset] = useState("customers");
  const [msg, setMsg] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const running = hist.data?.items.some((i) => ACTIVE.includes(i.status));
  useEffect(() => {
    if (!running) return;
    const t = setInterval(hist.reload, 3000);
    return () => clearInterval(t);
  }, [running, hist.reload]);

  async function upload(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    setBusy(true);
    setMsg(null);
    try {
      const r = await api<{ import_id: number }>("imports", { method: "POST", form });
      setMsg({ tone: "ok", text: `Upload accepted as import #${r.import_id}; the worker is validating it.` });
      (e.target as HTMLFormElement).reset();
      hist.reload();
    } catch (err) {
      setMsg({ tone: "bad", text: err instanceof Error ? err.message : "Upload failed" });
    } finally {
      setBusy(false);
    }
  }

  const current = ds.data?.find((d) => d.dataset === dataset);
  return (
    <div className="stack">
      <div><h1>CSV imports</h1><p className="sub">Validated, repeatable imports. Re-importing a file never creates duplicates; rows are counted as inserted, updated or unchanged.</p></div>
      {atLeast(user, "ADMIN") ? (
        <Card title="Upload">
          <form className="filters" onSubmit={upload}>
            <Field label="Dataset"><select name="dataset" value={dataset} onChange={(e) => setDataset(e.target.value)}>
              {ds.data?.map((d) => <option key={d.dataset} value={d.dataset}>{d.load_order}. {d.label}</option>)}</select></Field>
            <Field label="CSV file"><input name="file" type="file" accept=".csv,text/csv" required /></Field>
            <button className="btn btn-primary" disabled={busy}>{busy ? "Uploading…" : "Upload and validate"}</button>
            <a className="btn" href={`/api/proxy/imports/templates/${dataset}`}>Download template</a>
          </form>
          <p className="small muted">CSV in UTF-8 with a header row, up to 2 GB.</p>
          {current && <p className="small muted">Required: <span className="mono">{current.required_columns.join(", ")}</span>
            {current.optional_columns.length > 0 && <> · optional: <span className="mono">{current.optional_columns.join(", ")}</span></>}</p>}
          {msg && <Notice tone={msg.tone}>{msg.text}</Notice>}
        </Card>
      ) : <Notice>Uploading files requires the administrator role.</Notice>}

      <Card title="Import history">
        {hist.loading && !hist.data ? <Loading /> : hist.error ? <ErrorBox message={hist.error} onRetry={hist.reload} /> : !hist.data!.items.length ? <Empty>No imports yet.</Empty> : (
          <>
            <div className="table-wrap"><table>
              <thead><tr><th>#</th><th>Dataset</th><th>File</th><th>Status</th><th className="num">Rows</th><th className="num">Inserted</th>
                <th className="num">Updated</th><th className="num">Unchanged</th><th className="num">Rejected</th><th>By</th><th>Finished</th></tr></thead>
              <tbody>{hist.data!.items.map((i) => (
                <tr key={i.import_id} className="clickable" tabIndex={0} onClick={() => { setSel(i); setRejPage(1); }}
                    onKeyDown={(e) => e.key === "Enter" && setSel(i)}>
                  <td>{i.import_id}</td><td>{i.dataset}</td><td className="small">{i.file_name}</td><td><Badge value={i.status} /></td>
                  <td className="num">{fmtNum(i.rows_total)}</td><td className="num">{fmtNum(i.rows_inserted)}</td>
                  <td className="num">{fmtNum(i.rows_updated)}</td><td className="num">{fmtNum(i.rows_unchanged)}</td>
                  <td className="num">{fmtNum(i.rows_rejected)}</td><td>{i.uploaded_by}</td><td className="small">{fmtDateTime(i.finished_at)}</td>
                </tr>))}</tbody>
            </table></div>
            <Pager page={hist.data!.page} pages={hist.data!.pages} total={hist.data!.total} onPage={setPage} />
          </>
        )}
      </Card>

      {sel && (
        <Card title={`Import #${sel.import_id} — ${sel.dataset}`} actions={<button className="btn btn-small" onClick={() => setSel(null)}>Close</button>}>
          {sel.error_summary?.message && <Notice tone="bad">{sel.error_summary.message}</Notice>}
          {sel.error_summary?.error_counts && Object.keys(sel.error_summary.error_counts).length > 0 && (
            <p className="small">Errors by code: {Object.entries(sel.error_summary.error_counts).map(([k, v]) => `${k} (${v})`).join(", ")}</p>)}
          {!!sel.error_summary?.absent_optional_columns?.length && (
            <p className="small muted">Optional columns not supplied: {sel.error_summary.absent_optional_columns.join(", ")}</p>)}
          {rej.loading ? <Loading /> : rej.error ? <ErrorBox message={rej.error} /> : !rej.data?.items.length ? <Empty>No rejected rows.</Empty> : (
            <>
              <div className="table-wrap"><table>
                <thead><tr><th className="num">Line</th><th>Column</th><th>Code</th><th>Message</th><th>Record (masked)</th></tr></thead>
                <tbody>{rej.data.items.map((r, k) => (
                  <tr key={k}><td className="num">{r.row_number}</td><td className="mono">{r.column_name ?? "—"}</td><td>{r.error_code}</td>
                    <td>{r.error_message}</td><td className="mono small" style={{ maxWidth: 420, overflowWrap: "anywhere" }}>{r.raw_record ?? "hidden for your role"}</td></tr>))}</tbody>
              </table></div>
              <Pager page={rej.data.page} pages={rej.data.pages} total={rej.data.total} onPage={setRejPage} />
            </>
          )}
        </Card>
      )}
    </div>
  );
}
