# Putting Listing Agent online

What goes where:

| Part | Where | Cost |
|---|---|---|
| Website (`apps/web`) | Vercel | free |
| Backend (9 services + Postgres + Redis) | one small VPS, e.g. Hetzner CX22 (2 CPU / 4 GB) | ≈ €4–5 / month |
| API address with HTTPS | DuckDNS (free subdomain) + Caddy (free certificate, automatic) | free |

You'll end up with:
- website: `https://<something>.vercel.app`
- API: `https://<name>.duckdns.org`

---

## 0. Push the latest code to GitHub (on your PC)

```powershell
cd E:\AI-Agent\listing-agent
git add -A
git commit -m "Phase 4 + deploy kit"
git push
```
`.env` is in `.gitignore`, so your secrets are **not** pushed. Check with `git status` that `.env` isn't listed.

## 1. Choose your API address (DuckDNS)

1. Go to **duckdns.org**, sign in (GitHub or Google).
2. Add a subdomain, e.g. `listingagent` → you get `listingagent.duckdns.org`.
3. Leave the IP for now; you'll fill it in at step 3.

## 2. Website on Vercel

1. vercel.com → **Add New… → Project** → import `listing-agent` from GitHub.
2. **Root Directory:** `apps/web` (click *Edit* next to it). Framework: Next.js (auto).
3. **Environment Variables:** `NEXT_PUBLIC_API_URL` = `https://listingagent.duckdns.org` (your DuckDNS address, with https, no trailing slash).
4. **Deploy.** Note the address it gives you, e.g. `https://listing-agent-xyz.vercel.app`.

(The site will say it can't reach the server until step 4 is done — that's expected.)

## 3. Create the server

1. Hetzner Cloud → new project → **Add Server**: Ubuntu **24.04**, type **CX22** (or any 2 CPU / 4 GB), any location.
   Add your SSH key if you have one; otherwise use the root password Hetzner emails you.
2. Copy the server's **IPv4 address**.
3. Back on DuckDNS, paste that IP next to your subdomain → **update ip**.

## 4. Install and start Listing Agent on the server

From PowerShell on your PC:
```powershell
ssh root@YOUR_SERVER_IP
```
Then on the server (copy line by line):
```bash
git clone https://github.com/AliRaza171-cloud/listing-agent.git
cd listing-agent
bash deploy/setup-server.sh
bash deploy/make-env.sh
nano .env          # add STRIPE_* and SAFEPAY_* keys, and CREDIT_PACKS prices if you changed them
bash deploy/update.sh
```
- If the repo is **private**, `git clone` asks for a username and password: use your GitHub username and a
  **personal access token** (GitHub → Settings → Developer settings → Fine-grained tokens → read-only
  access to *Contents* of `listing-agent`) as the password.
- `make-env.sh` asks for the DuckDNS address, the Vercel address and your Gemini key, and creates all other
  secrets itself. **Save the `CREDENTIALS_ENCRYPTION_KEY` line somewhere safe.**
- The first `update.sh` builds everything: ~5–10 minutes.

Check: open `https://listingagent.duckdns.org/health` in your browser → `{"status":"ok",...}`.
Then open your Vercel site, sign up, and you're live.

## 5. Payments online

- **Stripe** → Developers → Webhooks → Add endpoint:
  `https://listingagent.duckdns.org/api/billing/webhooks/stripe`
  Events: `checkout.session.completed`, `checkout.session.async_payment_succeeded`,
  `checkout.session.async_payment_failed`, `checkout.session.expired`.
  Copy the signing secret (`whsec_…`) into `STRIPE_WEBHOOK_SECRET` in `.env`.
- **Safepay** → Developer → Endpoints: `https://listingagent.duckdns.org/api/billing/webhooks/safepay`,
  and put its webhook secret in `SAFEPAY_WEBHOOK_SECRET`. (Payments are confirmed with Safepay directly
  anyway; the webhook is a backup.)
- Apply: `cd ~/listing-agent && bash deploy/update.sh`

## 6. Backups (once)

```bash
(crontab -l 2>/dev/null; echo "30 3 * * * bash $HOME/listing-agent/deploy/backup.sh") | crontab -
bash deploy/backup.sh     # test it now
```
Databases + product photos go to `~/backups` every night (7 days kept). Copy one to your PC now and then:
`scp root@YOUR_SERVER_IP:~/backups/db-*.sql.gz .`

## Everyday use

| Task | Command (on the server, in `~/listing-agent`) |
|---|---|
| Deploy new code (after `git push` from your PC) | `bash deploy/update.sh` |
| See what's running | `docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml ps` |
| Logs of one service | `docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml logs billing --tail 50` |
| Change a setting | `nano .env` then `bash deploy/update.sh` |

Tip: `alias dc='docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml'` in `~/.bashrc`
turns those into `dc ps`, `dc logs billing`.

## Connecting stores from the live site

- **Smart Click:** use its **backend** address (e.g. your Render URL), not the Vercel website, and the same
  `LISTING_API_KEY` that's set on Smart Click's host.
- **WooCommerce / Shopify:** as before. Photos now work for WooCommerce without a WordPress app password too,
  because `PUBLIC_BASE_URL` is set.
- `http://` store addresses are refused online (`ALLOW_HTTP_STORES=false`); stores must use https.
