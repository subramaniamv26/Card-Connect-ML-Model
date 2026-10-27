import { NextRequest, NextResponse } from "next/server";
import { BACKEND_URL, TOKEN_COOKIE, USER_COOKIE, cookieOptions } from "@/lib/server";

// POST: log in. The bearer token is stored in an httpOnly cookie, never exposed to browser JavaScript.
export async function POST(req: NextRequest) {
  let body: { username?: string; password?: string };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ detail: "Invalid request" }, { status: 400 });
  }
  const res = await fetch(`${BACKEND_URL}/api/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: body.username ?? "", password: body.password ?? "" }),
    cache: "no-store",
  }).catch(() => null);
  if (!res) return NextResponse.json({ detail: "API is unreachable" }, { status: 502 });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return NextResponse.json({ detail: data.detail ?? "Login failed" }, { status: res.status });

  const out = NextResponse.json({ username: data.username, role: data.role });
  out.cookies.set(TOKEN_COOKIE, data.access_token, cookieOptions(data.expires_in));
  // Display-only copy of the identity (the API enforces permissions from the token, not this cookie).
  out.cookies.set(USER_COOKIE, JSON.stringify({ username: data.username, role: data.role }),
    cookieOptions(data.expires_in, false));
  return out;
}

// DELETE: log out.
export async function DELETE() {
  const out = NextResponse.json({ ok: true });
  out.cookies.set(TOKEN_COOKIE, "", cookieOptions(0));
  out.cookies.set(USER_COOKIE, "", cookieOptions(0, false));
  return out;
}
