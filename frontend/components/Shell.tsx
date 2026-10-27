"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { ReactNode, useState } from "react";
import { useUser } from "@/lib/api";

const NAV = [
  { href: "/", label: "Overview" },
  { href: "/customers", label: "Customers" },
  { href: "/offers", label: "Offers" },
  { href: "/merchants", label: "Merchants" },
  { href: "/imports", label: "Imports" },
  { href: "/training", label: "Training" },
  { href: "/jobs", label: "Bulk jobs" },
  { href: "/reports", label: "Engagement" },
];

export default function Shell({ children }: { children: ReactNode }) {
  const path = usePathname();
  const router = useRouter();
  const user = useUser();
  const [open, setOpen] = useState(false);
  if (path === "/login") return <>{children}</>;

  async function logout() {
    await fetch("/api/session", { method: "DELETE" });
    router.push("/login");
  }

  return (
    <div className="shell">
      <a className="skip" href="#main">Skip to content</a>
      <aside className={`sidebar ${open ? "open" : ""}`}>
        <div className="brand">Card Connect <span>Offers</span></div>
        <nav aria-label="Main">
          <ul>
            {NAV.map((n) => {
              const active = n.href === "/" ? path === "/" : path.startsWith(n.href);
              return (
                <li key={n.href}>
                  <Link href={n.href} aria-current={active ? "page" : undefined} className={active ? "active" : ""}
                        onClick={() => setOpen(false)}>{n.label}</Link>
                </li>
              );
            })}
          </ul>
        </nav>
      </aside>
      <div className="main-col">
        <header className="topbar">
          <button className="btn btn-small menu-btn" aria-expanded={open} aria-label="Toggle navigation"
                  onClick={() => setOpen(!open)}>Menu</button>
          <div className="topbar-right">
            {user && <span className="who">{user.username} · <span className="role">{user.role.replace("_", " ")}</span></span>}
            <button className="btn btn-small" onClick={logout}>Sign out</button>
          </div>
        </header>
        <main id="main" className="content">{children}</main>
      </div>
    </div>
  );
}
