// Server-only helpers shared by route handlers.
export const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";
export const TOKEN_COOKIE = "cc_token";
export const USER_COOKIE = "cc_user";

export function cookieOptions(maxAge: number, httpOnly = true) {
  return {
    httpOnly,
    secure: process.env.COOKIE_SECURE === "true",
    sameSite: "strict" as const,
    path: "/",
    maxAge,
  };
}
