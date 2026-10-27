"use client";

import { useState } from "react";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Notice, Pager } from "@/components/ui";
import { api, atLeast, qs, useApi, useUser } from "@/lib/api";

interface M {
  merchant_id: string; merchant_brand: string; merchant_category: string; merchant_location: string; merchant_city: string;
  merchant_district: string; merchant_type: string; merchant_offer_supported: string; is_active: string;
  merchant_email: string | null; merchant_contact_no: string | null;
}
interface PageT { items: M[]; page: number; pages: number; total: number }
const CATEGORIES = ["", "GROCERY", "RESTAURANT", "FUEL", "RETAIL", "ELECTRONICS", "TRAVEL", "HOTEL", "ENTERTAINMENT",
  "HEALTHCARE", "UTILITIES", "EDUCATION", "E-COMMERCE"];

export default function MerchantsPage() {
  const user = useUser();
  const admin = atLeast(user, "ADMIN");
  const [f, setF] = useState({ q: "", category: "", district: "", offer_supported: "" });
  const [applied, setApplied] = useState(f);
  const [page, setPage] = useState(1);
  const [msg, setMsg] = useState<string | null>(null);
  const { data, error, loading, reload } = useApi<PageT>(`merchants${qs({ ...applied, page, page_size: 25 })}`);

  async function patch(m: M, body: Record<string, boolean>) {
    try {
      await api(`merchants/${encodeURIComponent(m.merchant_id)}`, { method: "PATCH", body });
      setMsg(`Updated ${m.merchant_id}. Changes apply to recommendations immediately at serving time.`);
      reload();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Update failed");
    }
  }

  return (
    <div className="stack">
      <div><h1>Merchants</h1><p className="sub">Outlets, offer participation and activation. Inactive or non-participating outlets are never recommended.</p></div>
      {msg && <Notice>{msg}</Notice>}
      <Card>
        <form className="filters" onSubmit={(e) => { e.preventDefault(); setPage(1); setApplied(f); }}>
          <Field label="Brand or ID"><input value={f.q} onChange={(e) => setF({ ...f, q: e.target.value })} /></Field>
          <Field label="Category"><select value={f.category} onChange={(e) => setF({ ...f, category: e.target.value })}>
            {CATEGORIES.map((c) => <option key={c} value={c}>{c || "Any"}</option>)}</select></Field>
          <Field label="District"><input value={f.district} onChange={(e) => setF({ ...f, district: e.target.value })} /></Field>
          <Field label="Offers"><select value={f.offer_supported} onChange={(e) => setF({ ...f, offer_supported: e.target.value })}>
            <option value="">Any</option><option value="true">Participating</option><option value="false">Not participating</option></select></Field>
          <button className="btn btn-primary">Filter</button>
        </form>
        {loading ? <Loading /> : error ? <ErrorBox message={error} onRetry={reload} /> : !data!.items.length ? <Empty>No merchants found.</Empty> : (
          <>
            <div className="table-wrap"><table>
              <thead><tr><th>Outlet</th><th>Brand</th><th>Category</th><th>Location</th><th>Type</th><th>Contact</th><th>Offers</th><th>Status</th>{admin && <th>Actions</th>}</tr></thead>
              <tbody>{data!.items.map((m) => (
                <tr key={m.merchant_id}>
                  <td className="mono">{m.merchant_id}</td><td>{m.merchant_brand}</td><td>{m.merchant_category}</td>
                  <td className="small">{m.merchant_location}<div className="muted">{m.merchant_city}, {m.merchant_district}</div></td>
                  <td>{m.merchant_type}</td>
                  <td className="small">{m.merchant_email}<div className="muted">{m.merchant_contact_no}</div></td>
                  <td><Badge value={m.merchant_offer_supported === "YES" ? "APPROVED" : "INACTIVE"} /></td>
                  <td><Badge value={m.is_active === "Y" ? "ACTIVE" : "INACTIVE"} /></td>
                  {admin && <td className="row">
                    <button className="btn btn-small" onClick={() => patch(m, { merchant_offer_supported: m.merchant_offer_supported !== "YES" })}>
                      {m.merchant_offer_supported === "YES" ? "Stop offers" : "Allow offers"}</button>
                    <button className="btn btn-small" onClick={() => patch(m, { is_active: m.is_active !== "Y" })}>
                      {m.is_active === "Y" ? "Deactivate" : "Activate"}</button>
                  </td>}
                </tr>))}</tbody>
            </table></div>
            <Pager page={data!.page} pages={data!.pages} total={data!.total} onPage={setPage} />
          </>
        )}
      </Card>
    </div>
  );
}
