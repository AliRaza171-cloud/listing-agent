import Link from "next/link";

/** Switches between the one-product and bulk-batch upload pages. */
export function ModeSwitch({ active }: { active: "one" | "bulk" }) {
  return (
    <nav className="tabs" aria-label="Upload mode">
      <Link href="/products/new" aria-current={active === "one" ? "page" : undefined} className="tab-link">One product</Link>
      <Link href="/products/bulk" aria-current={active === "bulk" ? "page" : undefined} className="tab-link">Bulk batch</Link>
    </nav>
  );
}
