"use client";

import { FormEvent, useState } from "react";
import { Field, Notice } from "@/components/ui";
import { api } from "@/lib/api";

export interface OfferData {
  offer_id: string; offer_title: string; offer_description: string; merchant_brand: string | null;
  merchant_id: string | null; merchant_category: string; offer_type: string; offer_value: number; currency: string;
  minimum_spend: number; maximum_benefit: number | null; valid_from: string; valid_to: string;
  eligible_card_networks: string; eligible_card_types: string; eligible_card_tiers: string; eligible_locations: string;
  redemption_limit_per_customer: number | null; total_redemption_limit: number | null; campaign_budget: number | null;
  terms_and_conditions: string; is_sample: boolean; version?: number; status?: string;
}

const CATEGORIES = ["GROCERY", "RESTAURANT", "FUEL", "RETAIL", "ELECTRONICS", "TRAVEL", "HOTEL", "ENTERTAINMENT",
  "HEALTHCARE", "UTILITIES", "EDUCATION", "E-COMMERCE"];
const TYPES = ["PERCENT_DISCOUNT", "PERCENT_CASHBACK", "FIXED_DISCOUNT", "FIXED_CASHBACK"];

const empty: OfferData = {
  offer_id: "", offer_title: "", offer_description: "", merchant_brand: "", merchant_id: null, merchant_category: "GROCERY",
  offer_type: "PERCENT_DISCOUNT", offer_value: 10, currency: "LKR", minimum_spend: 0, maximum_benefit: null,
  valid_from: new Date().toISOString().slice(0, 10), valid_to: "", eligible_card_networks: "ALL",
  eligible_card_types: "ALL", eligible_card_tiers: "ALL", eligible_locations: "ALL",
  redemption_limit_per_customer: null, total_redemption_limit: null, campaign_budget: null,
  terms_and_conditions: "", is_sample: false,
};

export default function OfferForm({ initial, onSaved, onCancel }:
  { initial?: OfferData; onSaved: (o: OfferData) => void; onCancel: () => void }) {
  const [o, setO] = useState<OfferData>(initial ?? empty);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const editing = !!initial;
  const set = (k: keyof OfferData, v: unknown) => setO({ ...o, [k]: v });
  const num = (v: string) => (v === "" ? null : Number(v));

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const body = { ...o, merchant_brand: o.merchant_brand || null, merchant_id: o.merchant_id || null };
    try {
      const saved = editing
        ? await api<OfferData>(`offers/${encodeURIComponent(o.offer_id)}`, { method: "PUT", body })
        : await api<OfferData>("offers", { method: "POST", body });
      onSaved(saved);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="stack">
      <Notice tone="info">Saved offers enter <strong>PENDING APPROVAL</strong>. Editing an approved offer sends it back for approval.
        Only approved offers within their validity are ever recommended.</Notice>
      <div className="form-grid">
        <Field label="Offer ID"><input required value={o.offer_id} disabled={editing} pattern="[A-Za-z0-9_\-]+" maxLength={30}
          onChange={(e) => set("offer_id", e.target.value)} /></Field>
        <Field label="Title"><input required maxLength={150} value={o.offer_title} onChange={(e) => set("offer_title", e.target.value)} /></Field>
        <Field label="Merchant brand" hint="Brand-wide offer (all participating outlets)">
          <input value={o.merchant_brand ?? ""} onChange={(e) => set("merchant_brand", e.target.value)} /></Field>
        <Field label="Merchant outlet ID" hint="Optional: restrict to one outlet">
          <input value={o.merchant_id ?? ""} onChange={(e) => set("merchant_id", e.target.value)} /></Field>
        <Field label="Category"><select value={o.merchant_category} onChange={(e) => set("merchant_category", e.target.value)}>
          {CATEGORIES.map((c) => <option key={c}>{c}</option>)}</select></Field>
        <Field label="Offer type"><select value={o.offer_type} onChange={(e) => set("offer_type", e.target.value)}>
          {TYPES.map((c) => <option key={c} value={c}>{c.replace("_", " ")}</option>)}</select></Field>
        <Field label={o.offer_type.startsWith("PERCENT") ? "Value (%)" : "Value (LKR)"}>
          <input required type="number" step="0.01" min="0.01" value={o.offer_value} onChange={(e) => set("offer_value", Number(e.target.value))} /></Field>
        <Field label="Minimum spend (LKR)"><input type="number" step="0.01" min="0" value={o.minimum_spend}
          onChange={(e) => set("minimum_spend", Number(e.target.value))} /></Field>
        <Field label="Maximum benefit (LKR)"><input type="number" step="0.01" min="0.01" value={o.maximum_benefit ?? ""}
          onChange={(e) => set("maximum_benefit", num(e.target.value))} /></Field>
        <Field label="Valid from"><input required type="date" value={o.valid_from} onChange={(e) => set("valid_from", e.target.value)} /></Field>
        <Field label="Valid to"><input required type="date" value={o.valid_to} onChange={(e) => set("valid_to", e.target.value)} /></Field>
        <Field label="Card networks" hint="ALL or e.g. VISA;AMEX"><input value={o.eligible_card_networks} onChange={(e) => set("eligible_card_networks", e.target.value)} /></Field>
        <Field label="Card types" hint="ALL, CREDIT or DEBIT"><input value={o.eligible_card_types} onChange={(e) => set("eligible_card_types", e.target.value)} /></Field>
        <Field label="Card tiers" hint="ALL or e.g. GOLD;PLATINUM"><input value={o.eligible_card_tiers} onChange={(e) => set("eligible_card_tiers", e.target.value)} /></Field>
        <Field label="Eligible locations" hint="ALL, ONLINE, DISTRICT:Colombo;PROVINCE:Western">
          <input value={o.eligible_locations} onChange={(e) => set("eligible_locations", e.target.value)} /></Field>
        <Field label="Redemptions per customer"><input type="number" min="1" value={o.redemption_limit_per_customer ?? ""}
          onChange={(e) => set("redemption_limit_per_customer", num(e.target.value))} /></Field>
        <Field label="Total redemption limit"><input type="number" min="1" value={o.total_redemption_limit ?? ""}
          onChange={(e) => set("total_redemption_limit", num(e.target.value))} /></Field>
        <Field label="Campaign budget (LKR)"><input type="number" step="0.01" min="0.01" value={o.campaign_budget ?? ""}
          onChange={(e) => set("campaign_budget", num(e.target.value))} /></Field>
      </div>
      <Field label="Description"><textarea required maxLength={1000} value={o.offer_description} onChange={(e) => set("offer_description", e.target.value)} /></Field>
      <Field label="Terms and conditions"><textarea required minLength={10} value={o.terms_and_conditions} onChange={(e) => set("terms_and_conditions", e.target.value)} /></Field>
      <label className="row small"><input type="checkbox" checked={o.is_sample} onChange={(e) => set("is_sample", e.target.checked)} /> Synthetic sample offer (testing only)</label>
      {error && <Notice tone="bad">{error}</Notice>}
      <div className="row">
        <button className="btn btn-primary" disabled={busy}>{busy ? "Saving…" : editing ? "Save changes" : "Create offer"}</button>
        <button type="button" className="btn" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}
