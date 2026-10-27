"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Pager } from "@/components/ui";
import { qs, useApi } from "@/lib/api";

interface Row {
  customer_id: string; customer_name: string; customer_segment: string; customer_home_city: string;
  customer_home_district: string; customer_tenure_months: number; number_of_cards: number;
  recommendation_status: string | null; history_level: string | null;
}
interface PageT { items: Row[]; page: number; pages: number; total: number }

const SEGMENTS = ["", "STUDENT", "YOUNG_PROFESSIONAL", "SALARIED", "SELF_EMPLOYED", "FAMILY", "AFFLUENT", "PREMIUM"];

export default function CustomersPage() {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [applied, setApplied] = useState({ q: "", district: "", segment: "", status: "" });
  const [district, setDistrict] = useState("");
  const [segment, setSegment] = useState("");
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(1);
  const { data, error, loading, reload } = useApi<PageT>(`customers${qs({ ...applied, page, page_size: 25 })}`);

  return (
    <div className="stack">
      <div><h1>Customers</h1><p className="sub">Search customers and open their spending profile and top three offers.</p></div>
      <Card>
        <form className="filters" onSubmit={(e) => { e.preventDefault(); setPage(1); setApplied({ q, district, segment, status }); }}>
          <Field label="Customer ID or name"><input value={q} onChange={(e) => setQ(e.target.value)} placeholder="CUSTOMER_000123" /></Field>
          <Field label="Home district"><input value={district} onChange={(e) => setDistrict(e.target.value)} placeholder="Colombo" /></Field>
          <Field label="Segment">
            <select value={segment} onChange={(e) => setSegment(e.target.value)}>
              {SEGMENTS.map((s) => <option key={s} value={s}>{s ? s.replaceAll("_", " ") : "Any"}</option>)}
            </select>
          </Field>
          <Field label="Recommendations">
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">Any</option><option value="FULL">Full (3)</option><option value="PARTIAL">Partial</option><option value="NONE">None</option>
            </select>
          </Field>
          <button className="btn btn-primary">Search</button>
        </form>
        {loading ? <Loading /> : error ? <ErrorBox message={error} onRetry={reload} /> : !data!.items.length ? (
          <Empty>No customers match these filters.</Empty>
        ) : (
          <>
            <div className="table-wrap">
              <table>
                <thead><tr><th>Customer</th><th>Name</th><th>Segment</th><th>Home</th><th className="num">Tenure (m)</th>
                  <th className="num">Cards</th><th>History</th><th>Recommendations</th></tr></thead>
                <tbody>
                  {data!.items.map((r) => (
                    <tr key={r.customer_id} className="clickable" tabIndex={0}
                        onClick={() => router.push(`/customers/${r.customer_id}`)}
                        onKeyDown={(e) => e.key === "Enter" && router.push(`/customers/${r.customer_id}`)}>
                      <td className="mono">{r.customer_id}</td><td>{r.customer_name}</td>
                      <td>{r.customer_segment.replaceAll("_", " ")}</td>
                      <td>{r.customer_home_city}, {r.customer_home_district}</td>
                      <td className="num">{r.customer_tenure_months}</td><td className="num">{r.number_of_cards}</td>
                      <td><Badge value={r.history_level} /></td><td><Badge value={r.recommendation_status} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pager page={data!.page} pages={data!.pages} total={data!.total} onPage={setPage} />
          </>
        )}
      </Card>
    </div>
  );
}
