"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useRef, useState } from "react";
import OfferForm from "@/components/OfferForm";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Pager } from "@/components/ui";
import { atLeast, fmtLKR, qs, useApi, useUser } from "@/lib/api";

interface Offer {
  offer_id: string; offer_title: string; merchant_brand: string | null; merchant_category: string; offer_type: string;
  offer_value: number; minimum_spend: number; maximum_benefit: number | null; valid_from: string; valid_to: string;
  status: string; is_sample: boolean; eligible_card_networks: string; eligible_locations: string;
}
interface PageT { items: Offer[]; page: number; pages: number; total: number }
const STATUSES = ["", "DRAFT", "PENDING_APPROVAL", "APPROVED", "SUSPENDED", "REVOKED", "EXPIRED"];

function OffersInner() {
  const params = useSearchParams();
  const router = useRouter();
  const user = useUser();
  const [status, setStatus] = useState(params.get("status") ?? "");
  const [q, setQ] = useState("");
  const [applied, setApplied] = useState({ status: params.get("status") ?? "", q: "" });
  const [page, setPage] = useState(1);
  const dlg = useRef<HTMLDialogElement>(null);
  const { data, error, loading, reload } = useApi<PageT>(`offers${qs({ ...applied, page, page_size: 25 })}`);

  return (
    <div className="stack">
      <div className="row">
        <div><h1>Offer catalog</h1><p className="sub">Only approved offers within their validity period are recommended.</p></div>
        <span className="spacer" />
        {atLeast(user, "ADMIN") && <button className="btn btn-primary" onClick={() => dlg.current?.showModal()}>New offer</button>}
      </div>
      <Card>
        <form className="filters" onSubmit={(e) => { e.preventDefault(); setPage(1); setApplied({ status, q }); }}>
          <Field label="Search"><input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Title, ID or brand" /></Field>
          <Field label="Status"><select value={status} onChange={(e) => setStatus(e.target.value)}>
            {STATUSES.map((s) => <option key={s} value={s}>{s ? s.replaceAll("_", " ") : "Any"}</option>)}</select></Field>
          <button className="btn btn-primary">Filter</button>
        </form>
        {loading ? <Loading /> : error ? <ErrorBox message={error} onRetry={reload} /> : !data!.items.length ? (
          <Empty>No offers. {atLeast(user, "ADMIN") ? "Create one or import a catalog CSV on the Imports page." : ""}</Empty>
        ) : (
          <>
            <div className="table-wrap"><table>
              <thead><tr><th>Offer</th><th>Brand</th><th>Category</th><th>Benefit</th><th className="num">Min spend</th>
                <th>Validity</th><th>Cards</th><th>Status</th></tr></thead>
              <tbody>{data!.items.map((o) => (
                <tr key={o.offer_id}>
                  <td><Link href={`/offers/${encodeURIComponent(o.offer_id)}`}>{o.offer_title}</Link>
                    <div className="mono muted">{o.offer_id}{o.is_sample && " · sample"}</div></td>
                  <td>{o.merchant_brand ?? "—"}</td><td>{o.merchant_category}</td>
                  <td>{o.offer_type.startsWith("PERCENT") ? `${o.offer_value}%` : fmtLKR(o.offer_value)} {o.offer_type.split("_")[1].toLowerCase()}
                    {o.maximum_benefit ? <div className="muted small">cap {fmtLKR(o.maximum_benefit)}</div> : null}</td>
                  <td className="num">{fmtLKR(o.minimum_spend)}</td>
                  <td className="small">{o.valid_from.slice(0, 10)} → {o.valid_to.slice(0, 10)}</td>
                  <td className="small">{o.eligible_card_networks}</td><td><Badge value={o.status} /></td>
                </tr>))}</tbody>
            </table></div>
            <Pager page={data!.page} pages={data!.pages} total={data!.total} onPage={setPage} />
          </>
        )}
      </Card>
      <dialog ref={dlg} aria-label="New offer">
        <h2 style={{ marginBottom: 12 }}>New offer</h2>
        <OfferForm onCancel={() => dlg.current?.close()}
                   onSaved={(o) => { dlg.current?.close(); router.push(`/offers/${encodeURIComponent(o.offer_id)}`); }} />
      </dialog>
    </div>
  );
}

export default function OffersPage() {
  return <Suspense fallback={<Loading />}><OffersInner /></Suspense>;
}
