"use client";

import { usePathname } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

function detailMessage(detail: unknown): string {
  if (!detail) return "Request failed";
  if (typeof detail === "string") return detail;
  if (typeof detail === "object" && detail !== null) {
    const d = detail as { message?: string; errors?: { field?: string; message?: string }[] };
    const errs = d.errors?.map((e) => `${e.field ? e.field + ": " : ""}${e.message}`).join("; ");
    return [d.message, errs].filter(Boolean).join(" — ") || JSON.stringify(detail);
  }
  return String(detail);
}

export async function api<T = unknown>(path: string, init?: { method?: string; body?: unknown; form?: FormData }): Promise<T> {
  const opts: RequestInit = { method: init?.method ?? "GET", cache: "no-store" };
  if (init?.form) opts.body = init.form;
  else if (init?.body !== undefined) {
    opts.body = JSON.stringify(init.body);
    opts.headers = { "Content-Type": "application/json" };
  }
  const res = await fetch(`/api/proxy/${path.replace(/^\//, "")}`, opts);
  if (res.status === 401 && typeof window !== "undefined" && !path.startsWith("auth/")) {
    window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`;
  }
  const text = await res.text();
  const data = text ? (() => { try { return JSON.parse(text); } catch { return text; } })() : null;
  if (!res.ok) throw new ApiError(res.status, detailMessage(data?.detail ?? data));
  return data as T;
}

export function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
}

export function useApi<T>(path: string | null, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(!!path);
  const load = useCallback(async () => {
    if (!path) return;
    setLoading(true);
    setError(null);
    try {
      setData(await api<T>(path));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed");
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, ...deps]);
  useEffect(() => { load(); }, [load]);
  return { data, error, loading, reload: load };
}

export type Role = "ADMIN" | "ANALYST" | "READ_ONLY";
export interface SessionUser { username: string; role: Role }

export function readUser(): SessionUser | null {
  if (typeof document === "undefined") return null;
  const raw = document.cookie.split("; ").find((c) => c.startsWith("cc_user="))?.slice(8);
  if (!raw) return null;
  try { return JSON.parse(decodeURIComponent(raw)); } catch { return null; }
}

export function useUser(): SessionUser | null {
  const [u, setU] = useState<SessionUser | null>(null);
  const path = usePathname();
  useEffect(() => setU(readUser()), [path]); // re-read after login/logout navigation
  return u;
}

const RANK: Record<Role, number> = { READ_ONLY: 0, ANALYST: 1, ADMIN: 2 };
export const atLeast = (u: SessionUser | null, r: Role) => !!u && RANK[u.role] >= RANK[r];

// ---- formatting (Asia/Colombo) ------------------------------------------------------------------
const TZ = "Asia/Colombo";
export const fmtLKR = (v: number | null | undefined, digits = 0) =>
  v === null || v === undefined ? "—" : `LKR ${Number(v).toLocaleString("en-LK", { maximumFractionDigits: digits, minimumFractionDigits: digits })}`;
export const fmtNum = (v: number | null | undefined, digits = 0) =>
  v === null || v === undefined ? "—" : Number(v).toLocaleString("en-LK", { maximumFractionDigits: digits, minimumFractionDigits: digits });
export const fmtPct = (v: number | null | undefined, digits = 1) =>
  v === null || v === undefined ? "—" : `${(v * 100).toFixed(digits)}%`;
export function fmtDateTime(v: string | null | undefined) {
  if (!v) return "—";
  const d = new Date(v);
  return isNaN(d.getTime()) ? v : d.toLocaleString("en-GB", { timeZone: TZ, dateStyle: "medium", timeStyle: "short" });
}
export const fmtDate = (v: string | null | undefined) => (v ? v.slice(0, 10) : "—");
