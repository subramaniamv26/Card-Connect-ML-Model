import { NextRequest, NextResponse } from "next/server";
import { BACKEND_URL, TOKEN_COOKIE } from "@/lib/server";

// Authenticated pass-through to the FastAPI service: /api/proxy/<path> -> BACKEND_URL/api/v1/<path>.
// Only the API path space is reachable; the bearer token is attached server-side from the httpOnly cookie.
async function forward(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  if (path.some((p) => p === ".." || p.includes("\\"))) {
    return NextResponse.json({ detail: "Bad path" }, { status: 400 });
  }
  const token = req.cookies.get(TOKEN_COOKIE)?.value;
  if (!token) return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });

  const url = `${BACKEND_URL}/api/v1/${path.map(encodeURIComponent).join("/")}${req.nextUrl.search}`;
  const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
  const ct = req.headers.get("content-type");
  if (ct) headers["Content-Type"] = ct;

  const init: RequestInit & { duplex?: string } = { method: req.method, headers, cache: "no-store" };
  if (!["GET", "HEAD"].includes(req.method)) {
    init.body = req.body;
    init.duplex = "half"; // stream large CSV uploads without buffering
  }
  const res = await fetch(url, init).catch(() => null);
  if (!res) return NextResponse.json({ detail: "API is unreachable" }, { status: 502 });

  const out = new NextResponse(res.body, { status: res.status });
  for (const h of ["content-type", "content-disposition"]) {
    const v = res.headers.get(h);
    if (v) out.headers.set(h, v);
  }
  out.headers.set("Cache-Control", "no-store");
  return out;
}

export const GET = forward;
export const POST = forward;
export const PUT = forward;
export const PATCH = forward;
export const DELETE = forward;
export const dynamic = "force-dynamic";
