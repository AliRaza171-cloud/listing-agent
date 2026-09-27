# Listing Agent

AI agent that turns product photos, typed details and voice commands into store-ready
listings (English + Urdu) and publishes them to Shopify, WooCommerce and custom stores.

**Architecture: microservices** — 9 services, each with one job and (if it stores
anything) its own database, talking through REST (when a user is waiting) and
**Redis Streams** events (for everything slow).

```
Web / Mobile ──► gateway ──► auth · catalog · ai · voice · store · billing · notification
                                     │
                     Redis Streams event bus  ◄──►  ai · publisher · billing · notification
                                     │
          Postgres: one database per service (auth, catalog, store, publisher, billing, notification)
```

| Service | Job |
|---|---|
| **gateway** | only public entry; checks login tokens; routes `/api/<service>/…`; rate limits |
| **auth** | accounts, login, tokens → emits `user.registered` |
| **catalog** | products, photos, listings (EN/UR, versions), batches; starts the generate & publish sagas |
| **ai** | photo analysis, listing writing, web research, voice-command parsing (stub provider for now) |
| **voice** | speech-to-text, Urdu + English (stub provider for now) |
| **store** | connected stores + **encrypted** credentials |
| **publisher** | the only service that talks to Shopify / WooCommerce / custom stores; retries |
| **billing** | credits: free signup credits, reservations, ledger (payments in Phase 7) |
| **notification** | in-app notifications + emails from events |

Read **`contracts/README.md`** before changing anything between services — it is the
source of truth for routes, events and the two sagas.


## Web app

`apps/web` — Next.js seller app (landing, sign-up, products, new listing, workspace, stores, credits).
Start the backend with `docker compose up`, then `cd apps/web && npm install && npm run dev` →
http://localhost:3000. Details in [apps/web/README.md](apps/web/README.md).

## Run it locally

Needs Docker Desktop.

```bash
copy .env.example .env        # macOS/Linux: cp .env.example .env
# fill in INTERNAL_TOKEN, JWT_SECRET, CREDENTIALS_ENCRYPTION_KEY (commands are in the file)
docker compose up --build
```

Everything starts behind **http://localhost:8000**. Each service creates its own tables
on startup (numbered SQL files in `services/<name>/migrations/`).

Quick check:
```bash
curl -X POST http://localhost:8000/api/auth/register -H "Content-Type: application/json" ^
     -d "{\"email\":\"you@example.com\",\"password\":\"test12345\"}"
# copy access_token from the answer, then:
curl http://localhost:8000/api/billing/credits -H "Authorization: Bearer <token>"
# -> {"balance": 10, ...}   (billing heard user.registered and granted signup credits)
```

## Repo layout

```
contracts/          routes + events + sagas every service follows (events.json is checked on publish)
libs/common/        shared INFRASTRUCTURE only: event bus, correlation ids, internal auth,
                    migrations, service factory. No business logic.
services/<name>/    app/ (code), migrations/ (its own SQL), requirements.txt
services/Dockerfile one image recipe for all services (--build-arg SERVICE=<name>)
infra/postgres/     creates one database per service
docker-compose.yml  local environment
```

## Rules of the house

1. **A service never touches another service's database.** Ask its API or react to its events.
2. **Events are delivered at least once.** Handlers must be idempotent (`mark_processed`).
3. **Change contracts compatibly**: add optional fields; never rename/remove; bump `version` otherwise.
4. **Secrets only in `.env`** (never committed). Store credentials are encrypted at rest.
5. **Migrations are append-only**: never edit an applied `NNNN_*.sql`; add the next number.
6. Every request carries an `X-Correlation-Id` — grep one id across all service logs to follow a listing end to end.

## Status

✅ Phase 0 — architecture, contracts, all 9 services scaffolded with real sagas, DB schemas, event bus
⬜ Phase 1+ — see the roadmap (web app, real AI provider, store connectors, voice, mobile, payments)
