// Where a seller sells. Keep in sync with libs/common/lagent_common/markets.py
export type MarketInfo = { code: string; name: string; currency: string; symbol: string };

export const MARKETS: MarketInfo[] = [
  { code: "PK", name: "Pakistan", currency: "PKR", symbol: "Rs." },
  { code: "US", name: "United States", currency: "USD", symbol: "$" },
  { code: "GB", name: "United Kingdom", currency: "GBP", symbol: "£" },
  { code: "CA", name: "Canada", currency: "CAD", symbol: "CA$" },
  { code: "AU", name: "Australia", currency: "AUD", symbol: "A$" },
  { code: "NZ", name: "New Zealand", currency: "NZD", symbol: "NZ$" },
  { code: "IE", name: "Ireland", currency: "EUR", symbol: "€" },
  { code: "DE", name: "Germany", currency: "EUR", symbol: "€" },
  { code: "FR", name: "France", currency: "EUR", symbol: "€" },
  { code: "IT", name: "Italy", currency: "EUR", symbol: "€" },
  { code: "ES", name: "Spain", currency: "EUR", symbol: "€" },
  { code: "NL", name: "Netherlands", currency: "EUR", symbol: "€" },
  { code: "AE", name: "United Arab Emirates", currency: "AED", symbol: "AED" },
  { code: "SA", name: "Saudi Arabia", currency: "SAR", symbol: "SAR" },
  { code: "QA", name: "Qatar", currency: "QAR", symbol: "QAR" },
  { code: "IN", name: "India", currency: "INR", symbol: "₹" },
  { code: "BD", name: "Bangladesh", currency: "BDT", symbol: "Tk" },
  { code: "LK", name: "Sri Lanka", currency: "LKR", symbol: "Rs." },
  { code: "MY", name: "Malaysia", currency: "MYR", symbol: "RM" },
  { code: "SG", name: "Singapore", currency: "SGD", symbol: "S$" },
  { code: "ZA", name: "South Africa", currency: "ZAR", symbol: "R" },
  { code: "TR", name: "Türkiye", currency: "TRY", symbol: "₺" },
];

export const CURRENCIES = Array.from(new Set(MARKETS.map((m) => m.currency))).sort();

export function marketOf(code: string | null | undefined): MarketInfo {
  return MARKETS.find((m) => m.code === (code || "").toUpperCase()) ?? MARKETS[0];
}

export function currencySymbol(currency: string | null | undefined): string {
  return MARKETS.find((m) => m.currency === currency)?.symbol ?? (currency || "Rs.");
}

const WHOLE = new Set(["PKR", "INR", "BDT", "LKR"]);
const TIGHT = new Set(["$", "£", "€", "₹", "₺", "CA$", "A$", "NZ$", "S$"]);

/** "Rs. 2,500", "$24.99", "£1,200" */
export function money(n: number | null | undefined, currency: string | null | undefined = "PKR"): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  const cur = currency || "PKR";
  const sym = currencySymbol(cur);
  const decimals = WHOLE.has(cur) || n >= 1000 ? 0 : n % 1 === 0 ? 0 : 2;
  const num = n.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  return TIGHT.has(sym) ? `${sym}${num}` : `${sym} ${num}`;
}

/** Guess the seller's country from the browser (only a starting value for the sign-up form). */
export function guessCountry(): string {
  if (typeof navigator === "undefined") return "PK";
  const region = (navigator.language || "").split("-")[1]?.toUpperCase();
  return region && MARKETS.some((m) => m.code === region) ? region : "PK";
}
