// Talks to the API gateway. Every call goes to /api/<service>/<path>.

export const API_URL = (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8100").replace(/\/$/, "");

const TOKEN_KEY = "la_session";

export type User = { id: string; email: string; full_name: string | null };
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
  last_error: string | null;
  images: string[];
  listings: Listing[];
  publications: Publication[];
  updated_at: string;
};

export type Store = {
  id: string;
  platform: "shopify" | "woocommerce" | "custom";
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
  publish_to: string[];
  edit_instruction: string | null;
  confirmation_text: string;
};

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

// ---------------- session ----------------

export function getSession(): Session | null {
  if (typeof window === "undefined") return null;
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
  try {
    window.localStorage.setItem(TOKEN_KEY, JSON.stringify(s));
  } catch {
    /* private mode: session lasts for this tab only */
  }
  memorySession = s;
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

type Options = { method?: string; json?: unknown; body?: BodyInit; contentType?: string; auth?: boolean };

export async function api<T>(path: string, opts: Options = {}): Promise<T> {
  const headers: Record<string, string> = {};
  let body = opts.body;
  if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.json);
  } else if (opts.contentType) {
    headers["Content-Type"] = opts.contentType;
  }
  if (opts.auth !== false) {
    const t = token();
    if (t) headers["Authorization"] = `Bearer ${t}`;
  }

  let res: Response;
  try {
    res = await fetch(`${API_URL}/api/${path}`, { method: opts.method || "GET", headers, body });
  } catch {
    throw new ApiError(0, `Can't reach the server at ${API_URL}. Is docker compose running?`);
  }

  if (res.status === 401 && opts.auth !== false) {
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

export async function register(email: string, password: string, full_name?: string): Promise<User> {
  const res = await api<TokenResponse>("auth/register", {
    method: "POST", json: { email, password, full_name: full_name || null }, auth: false,
  });
  return saveSession(res);
}

export async function login(email: string, password: string): Promise<User> {
  const res = await api<TokenResponse>("auth/login", { method: "POST", json: { email, password }, auth: false });
  return saveSession(res);
}

export const getMe = () => api<User>("auth/me");
export const getCredits = () => api<Credits>("billing/credits");

export type Pack = { id: string; name: string; credits: number; prices: Record<string, number> };
export type PacksInfo = { packs: Pack[]; providers: { safepay: boolean; stripe: boolean }; stripe_currency: string };
export type Payment = {
  id: string; status: "pending" | "paid" | "failed"; provider: "stripe" | "safepay"; pack_id: string;
  credits: number; amount: number; currency: string; error: string | null;
};
export const getPacks = () => api<PacksInfo>("billing/packs");
export const startCheckout = (pack_id: string, provider: "stripe" | "safepay") =>
  api<{ payment_id: string; checkout_url: string }>("billing/checkout", { method: "POST", json: { pack_id, provider } });
export const getPayment = (id: string) => api<Payment>(`billing/payments/${id}`);

export const listProducts = () => api<Product[]>("catalog/products");
export const getProduct = (id: string) => api<Product>(`catalog/products/${id}`);
export const createProduct = (image_urls: string[], seller_notes: string | null, batch_id?: string) =>
  api<Product>("catalog/products", { method: "POST", json: { image_urls, seller_notes, batch_id: batch_id ?? null } });

export type Batch = {
  id: string; name: string; created_at: string;
  total: number; draft: number; generating: number; ready: number; failed: number;
};
export const createBatch = (name: string) => api<Batch>("catalog/batches", { method: "POST", json: { name } });
export const listBatches = () => api<Batch[]>("catalog/batches");
export const updateProduct = (id: string, patch: Partial<Pick<Product,
  "seller_notes" | "price" | "discount_pct" | "stock" | "sku" | "free_shipping">>) =>
  api<Product>(`catalog/products/${id}`, { method: "PATCH", json: patch });
export const deleteProduct = (id: string) => api<void>(`catalog/products/${id}`, { method: "DELETE" });
export const updateListing = (productId: string, listingId: string,
  patch: Partial<Pick<Listing, "title" | "highlights" | "description" | "tags">>) =>
  api<Product>(`catalog/products/${productId}/listings/${listingId}`, { method: "PATCH", json: patch });

export const generateListing = (id: string, body: {
  languages: string[]; platforms: string[]; research: boolean; store_connection_ids: string[];
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

export async function transcribe(audio: Blob): Promise<{ text: string; language: string | null }> {
  // The voice service checks the plain type ("audio/webm"), so drop codec parameters.
  const type = (audio.type || "audio/webm").split(";")[0];
  const ext = type.split("/")[1] || "webm";
  const form = new FormData();
  form.append("audio", new Blob([audio], { type }), `recording.${ext}`);
  return api("voice/transcribe", { method: "POST", body: form });
}

export const parseCommand = (text: string) =>
  api<CommandResult>("ai/commands/parse", { method: "POST", json: { text } });
