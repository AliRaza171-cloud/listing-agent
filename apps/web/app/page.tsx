import Link from "next/link";
import { CameraIcon, LogoMark, MicIcon } from "@/components/Icons";
import { CONTACT_EMAIL } from "@/components/LegalPage";

export default function Landing() {
  return (
    <div className="landing">
      <header className="l-header">
        <Link href="/" className="brand">
          <span className="brand-mark"><LogoMark /></span>
          <span className="brand-name">Listing Agent</span>
        </Link>
        <nav className="l-nav" aria-label="Main">
          <a href="#how" className="hide-sm">How it works</a>
          <a href="#stores" className="hide-sm">Stores</a>
          <a href="#pricing" className="hide-sm">Pricing</a>
          <Link href="/login">Sign in</Link>
          <Link href="/signup" className="btn btn-dark">Start free</Link>
        </nav>
      </header>

      <section className="l-hero">
        <div className="col" style={{ gap: 28 }}>
          <span className="kicker-pill">For Shopify, WooCommerce &amp; custom stores</span>
          <h1>Photo in.<br />Listing out.<br /><span>Published.</span></h1>
          <p className="lead">
            Upload product photos, tell the agent your price out loud, and it writes the listing in English and
            Urdu — then puts it in your store as a draft for you to check.
          </p>
          <div style={{ display: "flex", gap: 14, flexWrap: "wrap" }}>
            <Link href="/signup" className="btn btn-primary btn-lg">Start with 10 free listings</Link>
            <a href="#how" className="btn btn-outline btn-lg">See how it works</a>
          </div>
        </div>

        <div className="hero-art" aria-hidden="true">
          <div className="hero-card">
            <div className="ph"><CameraIcon size={30} /></div>
            <div style={{ padding: "22px 24px", display: "flex", flexDirection: "column", gap: 12 }}>
              <div style={{ display: "flex", gap: 6 }}>
                <span className="badge" style={{ background: "var(--ink)", color: "var(--bg)" }}>English</span>
                <span className="badge urdu" style={{ background: "var(--bg)", color: "var(--ink-2)", lineHeight: 1.6 }}>اردو</span>
              </div>
              <div className="display" style={{ fontWeight: 700, fontSize: 21, lineHeight: 1.2 }}>
                Stainless Steel Oil Strainer Pot, 1.3L — Fine Mesh, Non-Stick Inside
              </div>
              <div style={{ fontSize: 14, color: "var(--ink-2)" }}>
                Strain and store used cooking oil in one pot. Rust-free steel, a fine mesh that catches crumbs, and a
                lid that keeps dust out.
              </div>
              <div className="chips">
                <span className="chip">oil strainer pot</span>
                <span className="chip">oil filter jug</span>
                <span className="chip">kitchen storage</span>
              </div>
            </div>
          </div>
          <div className="hero-said">
            <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, color: "var(--side-text)" }}>
              <MicIcon size={16} style={{ color: "var(--flame)" }} /> You said
            </div>
            <div style={{ fontSize: 15 }}>“Price 2500, ten percent off, stock 20.”</div>
          </div>
          <div className="hero-pub">
            <div style={{ fontSize: 12, fontWeight: 600, color: "var(--muted)", textTransform: "uppercase", letterSpacing: "0.06em" }}>Published</div>
            {["Shopify", "WooCommerce", "My store"].map((s) => (
              <div key={s} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: 14 }}>
                <span>{s}</span><span className="badge badge-published" style={{ fontSize: 12 }}>Draft ✓</span>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section id="how" className="l-dark">
        <h2 className="l-h2">Three steps. No typing marathons.</h2>
        <div className="steps">
          <div className="step"><span className="num">01</span><h3>Upload photos</h3>
            <p>One product or a whole batch. The agent works out what each item is and which details buyers care about.</p></div>
          <div className="step"><span className="num">02</span><h3>Talk or type the details</h3>
            <p>Say the price, discount and stock in English or Urdu. You confirm every number before it’s saved.</p></div>
          <div className="step"><span className="num">03</span><h3>Review &amp; publish</h3>
            <p>Edit anything, then publish to all your stores in one click — as drafts, so nothing goes live unchecked.</p></div>
        </div>
      </section>

      <section id="stores" className="l-section">
        <h2 className="l-h2">Built for how you actually sell</h2>
        <div className="features">
          <div className="feature"><h3>English <span className="muted" style={{ fontWeight: 500 }}>+</span> <span className="urdu" style={{ fontSize: 18 }}>اردو</span></h3>
            <p>Every listing in both languages, written naturally — not word-for-word translation.</p></div>
          <div className="feature"><h3>Voice commands</h3>
            <p>“Is ka price 2500 rakho” works. So does “make the title shorter”.</p></div>
          <div className="feature"><h3>Bulk batches</h3>
            <p>Drop in dozens of products; listings appear one by one as they’re ready.</p></div>
          <div className="feature"><h3>Research with sources</h3>
            <p>Branded items get real specs; generic ones get the keywords buyers search for. Sources shown.</p></div>
          <div className="feature"><h3>Your store’s categories</h3>
            <p>The agent picks from the categories you already have — no messy mapping.</p></div>
          <div className="feature"><h3>Drafts first</h3>
            <p>Products land unpublished so you get a final look. Switch to live when you trust it.</p></div>
        </div>
        <div className="platforms">Publishes to <span>Shopify</span><span>WooCommerce</span><span>Custom stores</span></div>
      </section>

      <section id="pricing" className="l-cta">
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <h2 className="l-h2" style={{ fontSize: 38 }}>10 free listings to start</h2>
          <p style={{ fontSize: 17, color: "var(--ink-2)" }}>
            Then buy credit packs when you need them. Pay with JazzCash, EasyPaisa or card.
          </p>
        </div>
        <Link href="/signup" className="btn btn-primary btn-lg">Create your first listing</Link>
      </section>

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
