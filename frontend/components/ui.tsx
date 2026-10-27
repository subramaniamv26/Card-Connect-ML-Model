"use client";

import { ReactNode } from "react";

export function Loading({ label = "Loading…" }: { label?: string }) {
  return <div className="state" role="status" aria-live="polite"><span className="spinner" aria-hidden /> {label}</div>;
}

export function ErrorBox({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="state state-error" role="alert">
      <strong>Something went wrong.</strong> <span>{message}</span>
      {onRetry && <button className="btn btn-small" onClick={onRetry}>Retry</button>}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="state state-empty">{children}</div>;
}

export function Card({ title, actions, children, className = "" }:
  { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && <header className="card-head">{title && <h2>{title}</h2>}{actions}</header>}
      {children}
    </section>
  );
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {hint && <div className="stat-hint">{hint}</div>}
    </div>
  );
}

const TONES: Record<string, string> = {
  APPROVED: "ok", COMPLETED: "ok", SUCCEEDED: "ok", FULL: "ok", ACTIVE: "ok", QUEUED_OK: "ok", GRANTED: "ok",
  PENDING_APPROVAL: "warn", COMPLETED_WITH_ERRORS: "warn", PARTIAL: "warn", QUEUED: "info", RUNNING: "info",
  VALIDATING: "info", LOADING: "info", RECEIVED: "info", DRAFT: "muted", CANDIDATE: "info", LIMITED: "warn",
  SUSPENDED: "warn", REJECTED: "warn", SKIPPED: "muted", EXPIRED: "muted", RETIRED: "muted", SUPERSEDED: "muted",
  REVOKED: "bad", FAILED: "bad", NONE: "bad", BLOCKED: "bad", INACTIVE: "muted",
};

export function Badge({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="badge badge-muted">—</span>;
  return <span className={`badge badge-${TONES[value] ?? "muted"}`}>{value.replaceAll("_", " ")}</span>;
}

export function Pager({ page, pages, total, onPage }: { page: number; pages: number; total: number; onPage: (p: number) => void }) {
  return (
    <nav className="pager" aria-label="Pagination">
      <span>{total.toLocaleString()} result{total === 1 ? "" : "s"}</span>
      <button className="btn btn-small" disabled={page <= 1} onClick={() => onPage(page - 1)}>Previous</button>
      <span aria-current="page">Page {page} of {Math.max(pages, 1)}</span>
      <button className="btn btn-small" disabled={page >= pages} onClick={() => onPage(page + 1)}>Next</button>
    </nav>
  );
}

export function Bars({ rows, format }: { rows: { label: string; value: number }[]; format?: (v: number) => string }) {
  const max = Math.max(...rows.map((r) => r.value), 1);
  return (
    <ul className="bars">
      {rows.map((r) => (
        <li key={r.label}>
          <span className="bars-label">{r.label}</span>
          <span className="bars-track" aria-hidden><span className="bars-fill" style={{ width: `${(100 * r.value) / max}%` }} /></span>
          <span className="bars-value">{format ? format(r.value) : r.value.toLocaleString()}</span>
        </li>
      ))}
    </ul>
  );
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}

export function Notice({ tone = "info", children }: { tone?: "info" | "warn" | "ok" | "bad"; children: ReactNode }) {
  return <div className={`notice notice-${tone}`} role={tone === "bad" ? "alert" : "status"}>{children}</div>;
}
