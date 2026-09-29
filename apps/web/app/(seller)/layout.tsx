"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { CoinIcon, GearIcon, CloseIcon, GridIcon, LogoMark, LogoutIcon, MenuIcon, PlusIcon, StoreIcon } from "@/components/Icons";
import { clearSession, getCredits, getSession, type User } from "@/lib/api";
import { SessionContext } from "@/lib/session";

const NAV = [
  { href: "/products", label: "Products", icon: GridIcon, match: (p: string) => p === "/products" || /^\/products\/(?!new$|bulk$)/.test(p) },
  { href: "/products/new", label: "New listing", icon: PlusIcon, match: (p: string) => p === "/products/new" || p === "/products/bulk" },
  { href: "/stores", label: "Stores", icon: StoreIcon, match: (p: string) => p.startsWith("/stores") },
  { href: "/credits", label: "Credits", icon: CoinIcon, match: (p: string) => p.startsWith("/credits") },
  { href: "/settings", label: "Settings", icon: GearIcon, match: (p: string) => p.startsWith("/settings") },
];

export default function SellerLayout({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname() || "/products";
  const [user, setUser] = useState<User | null>(null);
  const [credits, setCredits] = useState<number | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);

  const refreshCredits = useCallback(async () => {
    try {
      const c = await getCredits();
      setCredits(c.balance);
      return c;
    } catch {
      return null; /* the sidebar just keeps the last number */
    }
  }, []);

  useEffect(() => {
    const session = getSession();
    if (!session) {
      router.replace(`/login?next=${encodeURIComponent(window.location.pathname)}`);
      return;
    }
    setUser(session.user);
    // Right after sign-up the free credits arrive a moment later (billing gets them by event).
    let retry: number | undefined;
    refreshCredits().then((c) => {
      if (c && c.balance === 0 && c.history.length === 0) retry = window.setTimeout(refreshCredits, 1500);
    });
    return () => window.clearTimeout(retry);
  }, [router, refreshCredits]);

  useEffect(() => setMenuOpen(false), [pathname]);

  function signOut() {
    clearSession();
    router.replace("/login");
  }

  if (!user) {
    return <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center" }}><span className="spinner" /></div>;
  }

  return (
    <SessionContext.Provider value={{ user, credits, refreshCredits }}>
      <div className="shell">
        <div className="topbar">
          <Link href="/products" className="brand">
            <span className="brand-mark"><LogoMark size={16} /></span>
            <span className="brand-name">Listing Agent</span>
          </Link>
          <button type="button" aria-label="Open menu" onClick={() => setMenuOpen(true)}><MenuIcon size={22} /></button>
        </div>

        <aside className={`sidebar${menuOpen ? " open" : ""}`}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <Link href="/products" className="brand">
              <span className="brand-mark"><LogoMark size={16} /></span>
              <span className="brand-name">Listing Agent</span>
            </Link>
            {menuOpen && (
              <button type="button" aria-label="Close menu" onClick={() => setMenuOpen(false)}
                style={{ background: "none", border: "none", color: "var(--bg)", width: 44, height: 44, cursor: "pointer" }}>
                <CloseIcon size={22} />
              </button>
            )}
          </div>
          <nav className="nav" aria-label="Main">
            {NAV.map(({ href, label, icon: Icon, match }) => (
              <Link key={href} href={href} className={match(pathname) ? "active" : undefined}
                aria-current={match(pathname) ? "page" : undefined}>
                <Icon />{label}
              </Link>
            ))}
          </nav>
          <div className="credit-box">
            <span className="label">Credits</span>
            <span className="num">{credits ?? "–"} <span>left</span></span>
            <Link href="/credits">Buy more</Link>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <span className="side-user" title={user.email}>{user.full_name || user.email}</span>
            <nav className="nav"><button type="button" onClick={signOut}><LogoutIcon />Sign out</button></nav>
          </div>
        </aside>

        <main className="main">{children}</main>
      </div>
    </SessionContext.Provider>
  );
}
