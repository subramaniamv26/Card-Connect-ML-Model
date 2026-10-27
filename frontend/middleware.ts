import { NextRequest, NextResponse } from "next/server";

// Redirect unauthenticated visitors to /login. Authorisation itself is enforced by the API.
export function middleware(req: NextRequest) {
  const { pathname } = req.nextUrl;
  const open = pathname === "/login" || pathname.startsWith("/api/") || pathname.startsWith("/_next") ||
    pathname === "/favicon.ico" || pathname === "/healthz";
  if (open || req.cookies.get("cc_token")) return NextResponse.next();
  const url = req.nextUrl.clone();
  url.pathname = "/login";
  url.search = pathname === "/" ? "" : `?next=${encodeURIComponent(pathname)}`;
  return NextResponse.redirect(url);
}

export const config = { matcher: ["/((?!_next/static|_next/image).*)"] };
