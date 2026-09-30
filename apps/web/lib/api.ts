// Talks to the API gateway. Every call goes to /api/<service>/<path>.

export const API_URL = (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8100").replace(/\/$/, "");

const TOKEN_KEY = "la_session";

export type User = {
  id: string; email: string; full_name: string | null;
  country?: string; currency?: string;   // where the seller sells (older sessions: Pakistan / PKR)
};
type Session = { token: string; expiresAt: number; user: User };

export type Listing = {
  id: string;
  language: "en" | "ur";
  platform: "shopify" | "woocommerce" | "custom" | null;
  title: string;
  highlights: string[];
  description: string;
  seo_title: string | null;
  meta_description: string | null;
  tags: string[];
  category_suggestion: string | null;
  version: number;
};

export type Publication = {
  store_connection_id: string;
  status: "publishing" | "published" | "failed";
  mode: "draft" | "live";
  external_url: string | null;
  error: string | null;
};

export type Detected = {
  product_type?: string;
  brand?: string | null;
  model?: string | null;
  attributes?: Record<string, string>;
  features?: string[];
  suggested_category?: string | null;
  questions_for_seller?: string[];
  category_is_new?: boolean;
};

export type Research = {
  mode: "specs" | "category" | "skipped";
  facts?: Record<string, string>;
  buyer_priorities?: string[];
  keywords?: string[];
  price_range?: [number, number] | null;
  sources?: { title: string; url: string }[];
};

export type Product = {
  id: string;
  status: "draft" | "generating" | "ready" | "failed";
  batch_id: string | null;
  seller_notes: string | null;
  detected: Detected | null;
  research: Research | null;
  price: number | null;
  discount_pct: number | null;
  stock: number | null;
  sku: string | null;
  free_shipping: boolean;
  country?: string;              // the product's market; price is in `currency`
  currency?: string;
  weight_kg?: number | null;     // package for delivery (Daraz requires it)
  length_cm?: number | null;
  width_cm?: number | null;
  height_cm?: number | null;
  last_error: string | null;
  images: string[];
  listings: Listing[];
  publications: Publication[];
  updated_at: string;
};

export type Store = {
  id: string;
  platform: "shopify" | "woocommerce" | "custom" | "daraz" | "ebay";
  name: string;
  store_url: string;
  status: "active" | "error" | "disconnected";
  last_error: string | null;
  created_at: string;
};

export type Credits = { balance: number; history: { delta: number; reason: string; at: string }[] };

export type CommandResult = {
  price: number | null;
  discount_pct: number | null;
  remove_discount: boolean;
  stock: number | null;
  sku: string | null;
  free_shipping: boolean | null;
  publish: boolean;
  publish_to: string[];                 // store connection ids
  publish_mode: "live" | "draft" | null;
  publish_language: "en" | "ur" | null;
  edit_instruction: string | null;
  confirmation_text: string;
};

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

// ---------------- Shopify (Listing Agent inside the Shopify admin) ----------------
// There the page runs in an iframe with Shopify's App Bridge loaded (window.shopify). We sign in
// with the ID token App Bridge gives us and keep the session in memory only: browser storage in
// that iframe is shared by every shop a merchant opens (and blocked in some browsers).

type AppBridge = { idToken: () => Promise<string>; config?: { shop?: string } };
declare global {
  interface Window { shopify?: AppBridge }
}

let embeddedShopDomain: string | null = null;

export function isEmbedded(): boolean {
  if (typeof window === "undefined" || !window.shopify) return false;
  try {
    return window.top !== window.self;
  } catch {
    return true;   // a cross-origin parent: we're framed
  }
}

/** The shop this embedded page belongs to (e.g. coolshop.myshopify.com). */
export function embeddedShop(): string | null {
  if (!isEmbedded()) return null;
  if (!embeddedShopDomain) {
    const fromUrl = new URLSearchParams(window.location.search).get("shop");
    embeddedShopDomain = (window.shopify?.config?.shop || fromUrl || "").toLowerCase() || null;
  }
  return embeddedShopDomain;
}

let signingIn: Promise<User> | null = null;

/** Signs in (or re-signs in) with Shopify's ID token. Safe to call from many places at once. */
export function embeddedSignIn(): Promise<User> {
  if (!signingIn) {
    signingIn = (async () => {
      const idToken = await window.shopify!.idToken();
      const res = await api<TokenResponse & { shop: string }>("store/shopify/session", {
        method: "POST", json: { id_token: idToken }, auth: false,
      });
      embeddedShopDomain = res.shop;
      return saveSession(res);
    })().finally(() => { signingIn = null; });
  }
  return signingIn;
}

// ---------------- session ----------------

export function getSession(): Session | null {
  if (typeof window === "undefined") return null;
  if (isEmbedded()) {
    return memorySession && memorySession.expiresAt > Date.now() + 30_000 ? memorySession : null;
  }
  try {
    const raw = window.localStorage.getItem(TOKEN_KEY);
    if (!raw) return null;
    const s = JSON.parse(raw) as Session;
    if (!s.token || s.expiresAt < Date.now() + 30_000) {
      window.localStorage.removeItem(TOKEN_KEY);
      return null;
    }
    return s;
  } catch {
    return null;
  }
}

function saveSession(res: { access_token: string; expires_in: number; user: User }): User {
  const s: Session = { token: res.access_token, expiresAt: Date.now() + res.expires_in * 1000, user: res.user };
  memorySession = s;
  if (isEmbedded()) return res.user;       // memory only (see above)
  try {
    window.localStorage.setItem(TOKEN_KEY, JSON.stringify(s));
  } catch {
    /* private mode: session lasts for this tab only */
  }
  return res.user;
}

let memorySession: Session | null = null;

export function clearSession() {
  memorySession = null;
  try {
    window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* ignore */
  }
}

function token(): string | null {
  return (getSession() ?? memorySession)?.token ?? null;
}

// ---------------- requests ----------------

function messageFrom(body: unknown, status: number): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail) && detail.length) {
      // FastAPI validation errors: [{loc, msg}]
      const first = detail[0] as { msg?: string; loc?: unknown[] };
      const field = Array.isArray(first.loc) ? String(first.loc[first.loc.length - 1]) : "";
      const msg = (first.msg || "Invalid value").replace(/^Value error, /, "");
      return field && field !== "body" ? `${field.replace(/_/g, " ")}: ${msg}` : msg;
    }
  }
  if (status === 429) return "Too many requests — wait a minute and try again.";
  if (status >= 500) return "Something went wrong on our side. Try again in a moment.";
  return `Request failed (${status}).`;
}

type Options = { method?: string; json?: unknown; body?: BodyInit; contentType?: string; auth?: boolean; retried?: boolean };

export async function api<T>(path: string, opts: Options = {}): Promise<T> {
  const headers: Record<string, string> = {};
  let body = opts.body;
  if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.json);
  } else if (opts.contentType) {
    headers["Content-Type"] = opts.contentType;
  }
  const sentToken = opts.auth !== false ? token() : null;
  if (sentToken) headers["Authorization"] = `Bearer ${sentToken}`;

  let res: Response;
  try {
    res = await fetch(`${API_URL}/api/${path}`, { method: opts.method || "GET", headers, body });
  } catch {
    throw new ApiError(0, `Can't reach the server at ${API_URL}. Is docker compose running?`);
  }

  if (res.status === 401 && opts.auth !== false && isEmbedded()) {
    // Inside Shopify: our session ran out — sign in again with a fresh Shopify ID token, then retry once.
    // Several requests can fail together: only one signs in; the others wait for it or reuse its session.
    if (!opts.retried) {
      if (signingIn) await signingIn.catch(() => undefined);      // another request is already signing in
      else if (token() === sentToken) await embeddedSignIn();     // nobody has renewed it yet: we do
      return api<T>(path, { ...opts, retried: true });
    }
  } else if (res.status === 401 && opts.auth !== false) {
    clearSession();
    if (typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
      const next = encodeURIComponent(window.location.pathname);
      window.location.href = `/login?next=${next}`;
    }
  }
  if (res.status === 204) return undefined as T;

  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!res.ok) throw new ApiError(res.status, messageFrom(data, res.status));
  return data as T;
}

export function mediaUrl(url: string): string {
  return url.startsWith("/") ? `${API_URL}${url}` : url;
}

// ---------------- endpoints ----------------

type TokenResponse = { access_token: string; expires_in: number; user: User };

export async function register(email: string, password: string, full_name?: string, country?: string): Promise<User> {
  const res = await api<TokenResponse>("auth/register", {
    method: "POST", json: { email, password, full_name: full_name || null, country: country || null }, auth: false,
  });
  return saveSession(res);
}

export async function login(email: string, password: string): Promise<User> {
  const res = await api<TokenResponse>("auth/login", { method: "POST", json: { email, password }, auth: false });
  return saveSession(res);
}

export const getMe = () => api<User>("auth/me");

/** Settings: name and market. Keeps the saved session's copy of the user in step. */
export async function updateMe(patch: { full_name?: string | null; country?: string; currency?: string }): Promise<User> {
  const user = await api<User>("auth/me", { method: "PATCH", json: patch });
  const s = getSession();
  if (s && isEmbedded()) {
    memorySession = { ...s, user };
  } else if (s && typeof window !== "undefined") {
    try { window.localStorage.setItem(TOKEN_KEY, JSON.stringify({ ...s, user })); } catch { /* ignore */ }
  }
  return user;
}

/** The signed-in seller's market (from the saved session). */
export function myMarket(): { country: string; currency: string } {
  const u = getSession()?.user;
  return { country: u?.country || "PK", currency: u?.currency || "PKR" };
}
export const getCredits = () => api<Credits>("billing/credits");

export type Pack = { id: string; name: string; credits: number; prices: Record<string, number> };
export type PayProvider = "stripe" | "safepay" | "shopify";
export type PacksInfo = { packs: Pack[]; providers: { safepay: boolean; stripe: boolean; shopify?: boolean }; stripe_currency: string };
export type Payment = {
  id: string; status: "pending" | "paid" | "failed"; provider: PayProvider; pack_id: string;
  credits: number; amount: number; currency: string; error: string | null;
};
export const getPacks = () => api<PacksInfo>("billing/packs");
export const startCheckout = (pack_id: string, provider: PayProvider, shop?: string | null) =>
  api<{ payment_id: string; checkout_url: string }>("billing/checkout", {
    method: "POST", json: { pack_id, provider, shop: shop ?? null },
  });
export const getPayment = (id: string) => api<Payment>(`billing/payments/${id}`);

export const listProducts = () => api<Product[]>("catalog/products");
export const getProduct = (id: string) => api<Product>(`catalog/products/${id}`);
export const createProduct = (image_urls: string[], seller_notes: string | null, batch_id?: string) =>
  api<Product>("catalog/products", {
    method: "POST", json: { image_urls, seller_notes, batch_id: batch_id ?? null, ...myMarket() },
  });

export type Batch = {
  id: string; name: string; created_at: string;
  total: number; draft: number; generating: number; ready: number; failed: number;
};
export const createBatch = (name: string) => api<Batch>("catalog/batches", { method: "POST", json: { name } });
export const listBatches = () => api<Batch[]>("catalog/batches");

/** Bulk upload: the AI says which photos show the same product (photos = positions in `image_urls`). Free. */
export const groupPhotos = (image_urls: string[]) =>
  api<{ groups: { photos: number[]; label: string }[] }>("ai/batch/group", { method: "POST", json: { image_urls } });
export type ProductNotes = { notes: string; price: number | null; discount_pct: number | null; stock: number | null };
/** Bulk upload: one voice note / message about many products -> what was said about each one. Free. */
export const splitNotes = (text: string, products: { label: string; image_url: string | null }[]) =>
  api<{ products: ProductNotes[]; unmatched: string }>("ai/batch/notes", {
    method: "POST", json: { text, products, market: myMarket() },
  });
export const updateProduct = (id: string, patch: Partial<Pick<Product,
  "seller_notes" | "price" | "discount_pct" | "stock" | "sku" | "free_shipping" | "weight_kg" | "length_cm" | "width_cm" | "height_cm">>) =>
  api<Product>(`catalog/products/${id}`, { method: "PATCH", json: patch });
export const deleteProduct = (id: string) => api<void>(`catalog/products/${id}`, { method: "DELETE" });
export const updateListing = (productId: string, listingId: string,
  patch: Partial<Pick<Listing, "title" | "highlights" | "description" | "tags" | "category_suggestion">>) =>
  api<Product>(`catalog/products/${productId}/listings/${listingId}`, { method: "PATCH", json: patch });

export type AutoPublish = { store_connection_ids: string[]; mode: "draft" | "live"; language: "en" | "ur" };
export const generateListing = (id: string, body: {
  languages: string[]; platforms: string[]; research: boolean; store_connection_ids: string[];
  auto_publish?: AutoPublish | null;   // bulk only: publish as soon as the listing is written
}) => api<{ job_id: string; status: string }>(`catalog/products/${id}/generate`, { method: "POST", json: body });

export const publishProduct = (id: string, store_connection_ids: string[], mode: "draft" | "live", language: string) =>
  api<{ publishing_to: string[] }>(`catalog/products/${id}/publish`, {
    method: "POST", json: { store_connection_ids, mode, language },
  });

export async function uploadPhoto(file: File): Promise<string> {
  const res = await api<{ url: string }>("catalog/uploads", {
    method: "POST", body: file, contentType: file.type || "application/octet-stream",
  });
  return res.url;
}

export const listStores = () => api<Store[]>("store/stores");
export const connectStore = (body: {
  platform: Store["platform"]; name: string; store_url: string; credentials: Record<string, string>;
}) => api<Store>("store/stores", { method: "POST", json: body });
export const disconnectStore = (id: string) => api<void>(`store/stores/${id}`, { method: "DELETE" });

// One-click connections: the seller approves Listing Agent inside their own store admin.
export type ConnectRequest = {
  id: string; platform: Store["platform"]; status: "pending" | "connected" | "failed";
  error: string | null; store_url: string; name: string;
};
export type ConnectOptions = {
  shopify: boolean; woocommerce: boolean; custom?: boolean; daraz?: boolean; ebay?: boolean;
  ebay_marketplaces?: { id: string; name: string; currency: string }[];
  shopify_app_store_url?: string | null;   // once approved: installs start on the Shopify App Store
};
export const getConnectOptions = () => api<ConnectOptions>("store/connect/options");
export const startConnect = (platform: "shopify" | "woocommerce" | "custom" | "daraz" | "ebay", store: string, name?: string,
  extra?: { marketplace?: string; city?: string; postal_code?: string }) =>
  api<{ request_id: string; authorize_url: string }>(`store/connect/${platform}`, {
    method: "POST", json: { store, name: name || null, ...(extra ?? {}) },
  });
export type DetectedStore = { platform: string; name: string; supported: boolean; store: string; note: string };
/** The Stores page's single address box: which platform runs this site? */
export const detectStore = (store: string) =>
  api<DetectedStore>("store/connect/detect", { method: "POST", json: { store } });
export const getConnectRequest = (id: string) => api<ConnectRequest>(`store/connect/requests/${id}`);
/** Shopify Connect: back from Shopify with a one-time code (#confirm=…) — only the account that started it can finish it. */
export const confirmConnect = (id: string, code: string) =>
  api<ConnectRequest>(`store/connect/requests/${id}/confirm`, { method: "POST", json: { code } });

export async function transcribe(audio: Blob): Promise<{ text: string; language: string | null }> {
  // The voice service checks the plain type ("audio/webm"), so drop codec parameters.
  const type = (audio.type || "audio/webm").split(";")[0];
  const ext = type.split("/")[1] || "webm";
  const form = new FormData();
  form.append("audio", new Blob([audio], { type }), `recording.${ext}`);
  form.append("country", myMarket().country);
  return api("voice/transcribe", { method: "POST", body: form });
}

export const parseCommand = (text: string, stores: Pick<Store, "id" | "name" | "platform">[] = [],
  market?: { country?: string; currency?: string }) =>
  api<CommandResult>("ai/commands/parse", {
    method: "POST",
    json: { text, stores: stores.map(({ id, name, platform }) => ({ id, name, platform })), market: market ?? myMarket() },
  });
