"use client";

import Link from "next/link";
import { FormEvent, Fragment, use, useRef, useState } from "react";
import OfferForm, { OfferData } from "@/components/OfferForm";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Notice } from "@/components/ui";
import { api, atLeast, fmtDateTime, fmtLKR, useApi, useUser } from "@/lib/api";

interface Rule { rule_id: number; rule_type: string; operator: string; rule_value: string | null; description: string | null; is_active: string }
interface Offer extends OfferData {
  status: string; version: number; budget_consumed: number; revoked_reason: string | null; created_by: string;
  approved_by: string | null; approved_at: string | null; updated_at: string; rules: Rule[]; engagement: Record<string, number>;
}

const RULE_TYPES: Record<string, string[]> = {
  CARD_NETWORK: ["IN", "NOT_IN"], CARD_TYPE: ["IN", "NOT_IN"], CARD_TIER: ["IN", "NOT_IN"],
  CUSTOMER_SEGMENT: ["IN", "NOT_IN"], HOME_DISTRICT: ["IN", "NOT_IN"], HOME_PROVINCE: ["IN", "NOT_IN"],
  MIN_TENURE_MONTHS: ["GTE"], MIN_SPEND_LAST_90D_LKR: ["GTE"], NEW_TO_BRAND: ["IS_TRUE"], EXISTING_BRAND_CUSTOMER: ["IS_TRUE"],
};
const ACTIONS: Record<string, { label: string; from: string[]; reason?: boolean; danger?: boolean }> = {
  submit: { label: "Submit for approval", from: ["DRAFT"] },
  approve: { label: "Approve", from: ["PENDING_APPROVAL"] },
  reject: { label: "Reject", from: ["PENDING_APPROVAL"], reason: true },
  suspend: { label: "Suspend", from: ["APPROVED"], reason: true },
  resume: { label: "Resume", from: ["SUSPENDED"] },
  revoke: { label: "Revoke", from: ["DRAFT", "PENDING_APPROVAL", "APPROVED", "SUSPENDED", "EXPIRED"], reason: true, danger: true },
};

export default function OfferPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const user = useUser();
  const admin = atLeast(user, "ADMIN");
  const { data: o, error, loading, reload } = useApi<Offer>(`offers/${encodeURIComponent(id)}`);
  const [msg, setMsg] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);
  const editDlg = useRef<HTMLDialogElement>(null);
  const [rt, setRt] = useState("CUSTOMER_SEGMENT");

  async function act(action: string) {
    const meta = ACTIONS[action];
    let body: { reason: string } | undefined;
    if (meta.reason) {
      const reason = window.prompt(`Reason to ${action} this offer:`);
      if (!reason || reason.trim().length < 3) return;
      body = { reason: reason.trim() };
    }
    if (action === "revoke" && !window.confirm("Revoking is permanent. Continue?")) return;
    try {
      await api(`offers/${encodeURIComponent(id)}/actions/${action}`, { method: "POST", body });
      setMsg({ tone: "ok", text: `Offer ${action} succeeded.` });
      reload();
    } catch (e) {
      setMsg({ tone: "bad", text: e instanceof Error ? e.message : "Action failed" });
    }
  }

  async function addRule(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    try {
      const r = await api<{ offer_status: string }>(`offers/${encodeURIComponent(id)}/rules`, { method: "POST", body: {
        rule_type: f.get("rule_type"), operator: f.get("operator"), rule_value: f.get("rule_value") || null,
        description: f.get("description") || null, is_active: true } });
      setMsg({ tone: "ok", text: `Rule added. Offer status is now ${r.offer_status}.` });
      reload();
    } catch (err) {
      setMsg({ tone: "bad", text: err instanceof Error ? err.message : "Could not add rule" });
    }
  }

  async function removeRule(ruleId: number) {
    if (!window.confirm("Remove this rule?")) return;
    try {
      await api(`offers/${encodeURIComponent(id)}/rules/${ruleId}`, { method: "DELETE" });
      reload();
    } catch (err) {
      setMsg({ tone: "bad", text: err instanceof Error ? err.message : "Could not remove rule" });
    }
  }

  if (loading) return <Loading />;
  if (error) return <ErrorBox message={error} onRetry={reload} />;
  const offer = o!;
  const formInitial: OfferData = { ...offer, valid_from: offer.valid_from.slice(0, 10), valid_to: offer.valid_to.slice(0, 10) };

  return (
    <div className="stack">
      <div>
        <p className="small"><Link href="/offers">← Offer catalog</Link></p>
        <div className="row"><h1>{offer.offer_title}</h1><Badge value={offer.status} />{offer.is_sample && <span className="badge badge-warn">SAMPLE</span>}</div>
        <p className="sub mono">{offer.offer_id} · version {offer.version}</p>
      </div>
      {msg && <Notice tone={msg.tone}>{msg.text}</Notice>}
      {offer.status === "REVOKED" && <Notice tone="bad">Revoked: {offer.revoked_reason}</Notice>}
      {admin && (
        <div className="row">
          {Object.entries(ACTIONS).filter(([, m]) => m.from.includes(offer.status)).map(([a, m]) => (
            <button key={a} className={`btn ${a === "approve" ? "btn-primary" : ""} ${m.danger ? "btn-danger" : ""}`} onClick={() => act(a)}>{m.label}</button>
          ))}
          {offer.status !== "REVOKED" && <button className="btn" onClick={() => editDlg.current?.showModal()}>Edit terms</button>}
        </div>
      )}
      <div className="grid grid-2">
        <Card title="Terms">
          <dl className="kv">
            <dt>Merchant</dt><dd>{offer.merchant_brand ?? "—"} {offer.merchant_id && <span className="mono">({offer.merchant_id})</span>}</dd>
            <dt>Category</dt><dd>{offer.merchant_category}</dd>
            <dt>Benefit</dt><dd>{offer.offer_type.replace("_", " ")} · {offer.offer_type.startsWith("PERCENT") ? `${offer.offer_value}%` : fmtLKR(offer.offer_value)}</dd>
            <dt>Minimum spend</dt><dd>{fmtLKR(offer.minimum_spend)}</dd>
            <dt>Maximum benefit</dt><dd>{offer.maximum_benefit ? fmtLKR(offer.maximum_benefit) : "No cap"}</dd>
            <dt>Valid</dt><dd>{offer.valid_from.slice(0, 10)} → {offer.valid_to.slice(0, 10)}</dd>
            <dt>Card networks</dt><dd>{offer.eligible_card_networks}</dd>
            <dt>Card types / tiers</dt><dd>{offer.eligible_card_types} / {offer.eligible_card_tiers}</dd>
            <dt>Locations</dt><dd>{offer.eligible_locations}</dd>
            <dt>Limits</dt><dd>{offer.redemption_limit_per_customer ?? "∞"} per customer · {offer.total_redemption_limit ?? "∞"} total</dd>
            <dt>Budget</dt><dd>{offer.campaign_budget ? `${fmtLKR(offer.budget_consumed)} of ${fmtLKR(offer.campaign_budget)}` : "—"}</dd>
          </dl>
          <p className="small" style={{ marginTop: 10 }}>{offer.offer_description}</p>
          <details><summary>Terms and conditions</summary><p className="small">{offer.terms_and_conditions}</p></details>
        </Card>
        <Card title="Governance and engagement">
          <dl className="kv">
            <dt>Created by</dt><dd>{offer.created_by}</dd>
            <dt>Approved by</dt><dd>{offer.approved_by ?? "—"} {offer.approved_at && `· ${fmtDateTime(offer.approved_at)}`}</dd>
            <dt>Last updated</dt><dd>{fmtDateTime(offer.updated_at)}</dd>
            {["IMPRESSION", "CLICK", "REDEMPTION", "DISMISSAL"].map((k) => (
              <Fragment key={k}><dt>{k.toLowerCase()}s</dt><dd>{offer.engagement[k] ?? 0}</dd></Fragment>))}
          </dl>
        </Card>
      </div>
      <Card title="Eligibility rules">
        <p className="small muted">Rules narrow eligibility beyond the card and location settings. Changing rules on an approved offer sends it back for approval.</p>
        {offer.rules.length ? (
          <div className="table-wrap"><table>
            <thead><tr><th>Rule</th><th>Operator</th><th>Value</th><th>Description</th><th>Active</th>{admin && <th />}</tr></thead>
            <tbody>{offer.rules.map((r) => (
              <tr key={r.rule_id}><td>{r.rule_type}</td><td>{r.operator}</td><td>{r.rule_value ?? "—"}</td><td>{r.description ?? ""}</td>
                <td>{r.is_active === "Y" ? "Yes" : "No"}</td>
                {admin && <td><button className="btn btn-small btn-danger" onClick={() => removeRule(r.rule_id)}>Remove</button></td>}</tr>))}</tbody>
          </table></div>
        ) : <Empty>No additional rules.</Empty>}
        {admin && offer.status !== "REVOKED" && (
          <form className="filters" style={{ marginTop: 12 }} onSubmit={addRule}>
            <Field label="Rule type"><select name="rule_type" value={rt} onChange={(e) => setRt(e.target.value)}>
              {Object.keys(RULE_TYPES).map((t) => <option key={t}>{t}</option>)}</select></Field>
            <Field label="Operator"><select name="operator">{RULE_TYPES[rt].map((op) => <option key={op}>{op}</option>)}</select></Field>
            {RULE_TYPES[rt][0] !== "IS_TRUE" && <Field label="Value" hint="Use ; between list values"><input name="rule_value" required /></Field>}
            <Field label="Description"><input name="description" /></Field>
            <button className="btn">Add rule</button>
          </form>
        )}
      </Card>
      <dialog ref={editDlg} aria-label="Edit offer">
        <h2 style={{ marginBottom: 12 }}>Edit offer</h2>
        <OfferForm initial={formInitial} onCancel={() => editDlg.current?.close()}
                   onSaved={() => { editDlg.current?.close(); setMsg({ tone: "ok", text: "Saved. The offer needs approval again." }); reload(); }} />
      </dialog>
    </div>
  );
}
