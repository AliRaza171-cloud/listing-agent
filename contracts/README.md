# Contracts

The **only** things services agree on. A service may change anything inside
itself freely, but a change to this folder is a change to the system: make it
backward compatible (add optional fields; never rename or remove), or bump the
event's `version` and let consumers handle both for a while.

## 1. Who owns what

| Service | Port | Database | Owns |
|---|---|---|---|
| gateway | 8000 | — | the only public entry point: routing, JWT check, rate limits |
| auth | 8001 | `auth` | users, passwords, token issuing |
| catalog | 8002 | `catalog` | products, images, listings (EN/UR, versions), batches |
| ai | 8003 | — | photo analysis, listing writing, research, command parsing |
| voice | 8004 | — | speech-to-text |
| store | 8005 | `store` | store connections and their encrypted credentials |
| publisher | 8006 | `publisher` | all calls to outside stores (connectors), publish jobs, retries |
| billing | 8007 | `billing` | credit balances, reservations, ledger, payments |
| notification | 8008 | `notification` | emails / push, delivery log |

**No service reads another service's database.** Cross-service references
are plain UUIDs (no foreign keys across services).

## 2. Requests between services

### 2.1 From the outside (web / mobile) — always through the gateway

`/api/<service>/...` is forwarded to that service with the `/api/<service>` prefix removed:
`GET /api/catalog/products` → catalog `GET /products`.

Public (no login): `POST /api/auth/register`, `POST /api/auth/login`, and `GET /api/catalog/media/<name>`
(product photos, so `<img>` tags work; names are random 32-hex, never listed).

Catalog photo and edit endpoints used by the web app:

| Endpoint | Body | Returns |
|---|---|---|
| `POST /api/catalog/uploads` | raw image bytes, `Content-Type: image/jpeg\|png\|webp`, max 8 MB | `201 {url: "/api/catalog/media/<name>"}` |
| `PATCH /api/catalog/products/{id}/listings/{listing_id}` | any of `title, highlights[], description, tags[], seo_title, meta_description` | the product |
| `DELETE /api/catalog/products/{id}` | — | `204` (`409` while generating) |
| `POST /api/catalog/batches` | `{name}` (1–120 chars) | `201` batch summary |
| `GET /api/catalog/batches` | — | the 50 newest: `[{id, name, created_at, total, draft, generating, ready, failed}]` |
| `POST /api/catalog/products` | `{image_urls[], seller_notes, batch_id?}` | the product (`404` if the batch isn't yours) |

Bulk upload: the web app creates a batch, then **all** its products, then calls `generate` on each.
A batch is complete (→ `batch.completed`) once none of its products is `draft` or `generating`,
so products that can't be started (e.g. out of credits) are deleted rather than left as drafts.

The gateway verifies the user's JWT (issued by auth) and forwards:

| Header | Meaning |
|---|---|
| `X-User-Id` | the authenticated user's id (absent on public routes like login) |
| `X-Internal-Token` | shared secret proving the call came through the gateway or another service |
| `X-Correlation-Id` | request id, passed along to every service and event it causes |

Services **reject** any request without a valid `X-Internal-Token`, and never
trust `X-User-Id` from anywhere else. Services are not exposed to the internet.

### 2.2 Service → service (internal, synchronous)

Only when the caller must wait for the answer. Paths start with `/internal/`
and are never routed by the gateway.

| Caller → callee | Call | Answer |
|---|---|---|
| catalog → billing | `POST /internal/reservations` `{user_id, amount, reason, ref_id}` | `201 {reservation_id}` or `402` not enough credits |
| catalog → publisher | `GET /internal/stores/{id}/categories?user_id=` | `[{id, name}]` |
| store → publisher | `POST /internal/connectors/test` `{platform, store_url, credentials}` | `204` works, or `422 {detail}` with a seller-friendly reason |
| publisher → store | `GET /internal/stores/{id}/credentials?user_id=` | `{platform, store_url, credentials}` — decrypted, never logged |

**Publisher is the only service that talks to Shopify, WooCommerce and custom
stores** (testing a connection, reading categories, creating products). Store
only keeps the connection records and their encrypted credentials.

## 3. Events (Redis Streams)

### 3.1 Envelope — every event looks like this

```json
{
  "id": "9f1c…",                     // unique per event; consumers de-duplicate on it
  "type": "listing.requested",
  "version": 1,
  "occurred_at": "2026-09-27T03:10:00Z",
  "correlation_id": "req-4b2…",       // from the request that caused it
  "producer": "catalog",
  "data": { … }                       // event-specific, below
}
```

Stored as field `event` (JSON) in stream `events:<type>` — e.g. `events:listing.requested`.

### 3.2 Delivery rules

- Each consuming **service** is one consumer group (group name = service name),
  so every interested service gets every event once, and multiple copies of a
  service share the work.
- **At-least-once delivery**: a handler can see the same event twice. Stateful
  services record handled event ids in `processed_events` and skip repeats.
- A handler that raises is **retried** (the message is re-claimed after 60 s idle).
  After **5** failed deliveries it moves to `events:dead-letter` with the error,
  and is acknowledged so the stream keeps flowing.
- Streams are trimmed to roughly the last 100,000 events.

### 3.3 Catalogue of events

| Event | Producer | Consumers | `data` |
|---|---|---|---|
| `user.registered` | auth | billing, notification | `user_id, email, full_name` |
| `listing.requested` | catalog | ai | `job_id, product_id, user_id, reservation_id, image_urls[], seller_notes, languages[], platforms[], research, categories[]` |
| `listing.generated` | ai | catalog, billing, notification | `job_id, product_id, user_id, reservation_id, facts, research, listings[]` |
| `listing.failed` | ai | catalog, billing, notification | `job_id, product_id, user_id, reservation_id, reason` |
| `publish.requested` | catalog | publisher | `publish_job_id, product_id, user_id, store_connection_id, mode, product` |
| `publish.succeeded` | publisher | catalog, notification | `publish_job_id, product_id, user_id, store_connection_id, external_id, external_url` |
| `publish.failed` | publisher | catalog, notification | `publish_job_id, product_id, user_id, store_connection_id, reason, retryable` |
| `credits.purchased` | billing | notification | `user_id, amount, balance, payment_id` |
| `batch.completed` | catalog | notification | `batch_id, user_id, succeeded, failed` |

`listings[]` items: `{language: "en"|"ur", platform: null|"shopify"|"woocommerce"|"custom", title, highlights[], description, seo_title, meta_description, tags[], category_suggestion}`.

`product` (in `publish.requested`): `{title, description, highlights[], price, discount_pct, stock, sku, free_shipping, image_urls[], tags[], category_id, seo_title, meta_description}`.

## 4. The two sagas

### 4.1 Generate a listing (credits are never lost)

```
seller clicks Generate
  catalog ──POST /internal/reservations──► billing      holds 1 credit (402 → "buy credits")
  catalog ──listing.requested──► ai
     ai ──listing.generated──► catalog  (saves listings)
                           ──► billing  (reservation → spent)
                           ──► notification
     ai ──listing.failed─────► catalog  (marks product failed)
                           ──► billing  (reservation → released, credit back)
```

A reservation nobody settles within 30 minutes is released automatically by billing.

### 4.2 Publish a product

```
seller clicks Publish (N stores)
  catalog ──publish.requested (one per store)──► publisher
     publisher ──GET /internal/stores/{id}/credentials──► store
     publisher → Shopify / WooCommerce / custom store (retries retryable errors)
     publisher ──publish.succeeded / publish.failed──► catalog, notification
```


## 5. Buying credits

| Endpoint | Who | Body / returns |
|---|---|---|
| `GET /api/billing/packs` | seller | `{packs:[{id,name,credits,prices:{PKR,USD}}], providers:{safepay,stripe}, stripe_currency}` |
| `POST /api/billing/checkout` | seller | `{pack_id, provider: "safepay"\|"stripe"}` → `201 {payment_id, checkout_url}` |
| `GET /api/billing/payments/{id}` | seller | the payment; for a pending Stripe payment billing also asks Stripe |
| `POST /api/billing/webhooks/stripe` | Stripe (public) | verified by `Stripe-Signature` |
| `POST /api/billing/webhooks/safepay` | Safepay (public) | verified by `X-SFPY-Signature` (HMAC-SHA512 of `data`) |
| `GET\|POST /api/billing/payments/safepay/return` | buyer's browser (public) | `tracker` + `sig` (HMAC-SHA256 of tracker, v1 secret) → `303` to `/credits?payment=<id>` |

A payment is credited **once** (`UPDATE … WHERE status <> 'paid'`), whichever confirmation arrives
first, then billing emits `credits.purchased`. Both Safepay (Payments 2.0) and Stripe take amounts in minor units (paisa / cents).

## 6. Custom-store Listing API (what a custom store must implement)

The `custom` connector (publisher) talks to any store exposing this, with header `X-Api-Key: <key>`:

| Endpoint | Returns |
|---|---|
| `GET /listing-api/ping` | `200` when the key is valid (`401` wrong key, `404` API off) |
| `GET /listing-api/categories` | `[{id, name}]` |
| `POST /listing-api/products` | multipart `data` = JSON `{title, description, highlights[], price, discount_pct, free_shipping, stock, sku, tags[], category, seo_title, meta_description, mode: draft\|live}`, `images` = files → `201 {id, url}` |
| `PUT /listing-api/products/{id}` | same body → `200 {id, url}`; `404` makes the connector create it again |

Errors: `429`/`5xx`/timeouts are retried (up to `MAX_ATTEMPTS`); other `4xx` fail with the store's `detail` message.
Photos: publisher downloads them from catalog over the internal network and uploads the bytes.
Local stores: `localhost` in the store URL is rewritten to `STORE_LOCALHOST_ALIAS` (host.docker.internal);
`http://` addresses are accepted only when `ALLOW_HTTP_STORES=true`.
