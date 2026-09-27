# Listing Agent — web app

Next.js 15 (App Router) + TypeScript. Talks only to the API gateway.

## Run (development)

1. Start the backend from the repo root: `docker compose up`
2. In a second terminal:

   ```powershell
   cd apps\web
   copy .env.example .env.local     # set NEXT_PUBLIC_API_URL to your gateway (default http://localhost:8100)
   npm install
   npm run dev
   ```
3. Open http://localhost:3000

The gateway only accepts browser calls from the origins in `ALLOWED_ORIGINS` (root `.env`), which is
`http://localhost:3000` by default — so keep the web app on port 3000.

## Pages

| URL | Screen |
|---|---|
| `/` | Landing |
| `/signup`, `/login` | Account |
| `/products` | Dashboard: stats, filters, search |
| `/products/new` | Photos, notes (type or speak), languages, stores, research → Generate |
| `/products/[id]` | Workspace: EN/UR editor, agent questions, voice/typed commands, price & stock, research, publish |
| `/stores` | Connect / disconnect Shopify, WooCommerce, custom stores |
| `/credits` | Balance and history |

## Notes

- The session (access token, 60 min) is kept in the browser's localStorage; when it expires you're sent to sign in.
- With `AI_PROVIDER=stub` / `STT_PROVIDER=stub` the listing text and voice transcript are fixed sample text.
- Store connectors aren't built yet (next phase), so "Test & connect" answers "isn't available yet".
