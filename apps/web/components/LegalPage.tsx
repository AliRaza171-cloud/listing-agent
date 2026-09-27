import Link from "next/link";
import type { ReactNode } from "react";
import { LogoMark } from "@/components/Icons";

// Set NEXT_PUBLIC_CONTACT_EMAIL on Vercel (and in apps/web/.env) to the address people should write to.
export const CONTACT_EMAIL = process.env.NEXT_PUBLIC_CONTACT_EMAIL || "privacy@listingagent.example";
export const UPDATED = "27 September 2026";

export default function LegalPage({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="legal-wrap">
      <header className="l-header">
        <Link href="/" className="brand">
          <span className="brand-mark"><LogoMark /></span>
          <span className="brand-name">Listing Agent</span>
        </Link>
      </header>
      <main className="legal">
        <h1>{title}</h1>
        <p className="muted">Last updated: {UPDATED}</p>
        {children}
      </main>
      <footer className="l-footer">
        <span>© Listing Agent</span>
        <span style={{ display: "flex", gap: 24 }}>
          <Link href="/privacy" style={{ color: "var(--muted)" }}>Privacy</Link>
          <Link href="/terms" style={{ color: "var(--muted)" }}>Terms</Link>
          <a href={`mailto:${CONTACT_EMAIL}`} style={{ color: "var(--muted)" }}>Contact</a>
        </span>
      </footer>
    </div>
  );
}
