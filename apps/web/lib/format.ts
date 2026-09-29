import type { Product } from "./api";

export function rs(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return `Rs. ${Math.round(n).toLocaleString("en-US")}`;
}

export function finalPrice(p: Pick<Product, "price" | "discount_pct">): number | null {
  if (p.price === null) return null;
  return p.discount_pct ? p.price * (1 - p.discount_pct / 100) : p.price;
}

// Backend timestamps are UTC without a zone marker.
export function parseUtc(iso: string): Date {
  return new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`);
}

export function timeAgo(iso: string): string {
  const secs = Math.max(0, (Date.now() - parseUtc(iso).getTime()) / 1000);
  if (secs < 45) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days === 1) return "Yesterday";
  if (days < 7) return `${days} days ago`;
  return parseUtc(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

export type Display = { key: "ready" | "generating" | "published" | "attention" | "draft"; label: string };

export function displayStatus(p: Product): Display {
  if (p.status === "generating") return { key: "generating", label: "Writing listing…" };
  if (p.status === "failed") return { key: "attention", label: "Generation failed" };
  if (p.publications.some((x) => x.status === "failed")) return { key: "attention", label: "Publish failed" };
  if (p.status === "draft") return { key: "draft", label: "Not generated" };
  if (p.publications.some((x) => x.status === "publishing")) return { key: "generating", label: "Publishing…" };
  if (p.publications.some((x) => x.status === "published")) return { key: "published", label: "Published" };
  return { key: "ready", label: "Ready to publish" };
}

export function productTitle(p: Product): string {
  const en = p.listings.find((l) => l.language === "en" && l.platform === null)
    ?? p.listings.find((l) => l.language === "en")
    ?? p.listings[0];
  if (en) return en.title;
  if (p.seller_notes) return p.seller_notes.length > 60 ? `${p.seller_notes.slice(0, 57)}…` : p.seller_notes;
  return "Untitled product";
}

export const PLATFORM_NAMES: Record<string, string> = {
  shopify: "Shopify",
  woocommerce: "WooCommerce",
  custom: "Custom store",
  daraz: "Daraz",
  ebay: "eBay",
};

export const REASON_NAMES: Record<string, string> = {
  signup_bonus: "Welcome credits",
  reserved: "Listing",
  released: "Returned (listing not written)",
  purchase: "Credit pack",
};
