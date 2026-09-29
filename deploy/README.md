# Putting Listing Agent online

(Examples use `listingagent.duckdns.org`; with your own domain use `api.yourdomain.com` instead.)

What goes where:

| Part | Where | Cost |
|---|---|---|
| Website (`apps/web`) | Vercel | free |
| Backend (9 services + Postgres + Redis) | one small server: Oracle Cloud Always Free (ARM) or Hetzner CX22 | free / ≈ €4–5 a month |
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

### Option A — Oracle Cloud Always Free ($0)

1. Sign up at **oracle.com/cloud/free** (a card is needed for verification only; Always Free resources aren't charged).
   Pick a **home region** close to you — it can't be changed later.
2. **Compute → Instances → Create instance**
   - **Image:** Canonical **Ubuntu 24.04** · **Shape:** Ampere **VM.Standard.A1.Flex**, **2 OCPU / 12 GB** (free limit is 4 / 24)
   - **Networking:** keep "Assign a public IPv4 address" on
   - **SSH keys:** *Generate a key pair for me* → **Save private key** (e.g. to `C:\Users\<you>\.ssh\oracle.key`)
   - Create. If it says **"Out of capacity"**, try another availability domain, or again a bit later.
3. **Open the web ports in Oracle's firewall:** on the instance page click the **subnet** →
   **Security Lists → Default Security List → Add Ingress Rules**, twice:
   Source CIDR `0.0.0.0/0`, IP protocol TCP, destination port `80` — and again for `443`.
4. Copy the instance's **Public IP** and put it in **DuckDNS → update ip**.
5. Log in from PowerShell (user is `ubuntu`, and you pass the key file):
   ```powershell
   ssh -i $env:USERPROFILE\.ssh\oracle.key ubuntu@YOUR_SERVER_IP
   ```
   Then become root for the setup: `sudo -i`

### Option B — Hetzner CX22 (≈ €4–5/month)

Ubuntu 24.04, 2 vCPU / 4 GB, public IPv4 on. Put its IP in DuckDNS, then `ssh root@YOUR_SERVER_IP`.

## 4. Install and start Listing Agent on the server

On the server, as root (copy line by line):
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

## 5b. One-click store connections

- **WooCommerce:** nothing to do — sellers enter their shop address, approve in WordPress, done.
  (Their WordPress needs "pretty" permalinks, i.e. anything except *Plain*.)
- **Shopify: Listing Agent as a public Shopify app** (one time, by you). Merchants install it from the
  Shopify App Store; it opens *inside* their Shopify admin, signs them in by itself (one Listing Agent
  account per shop) and they buy credits through their Shopify bill (Shopify requires that).
  1. **dev.shopify.com → Apps → Create app** "Listing Agent" (a new app — keep any test app you have).
  2. In the app's **version / configuration**:
     - **App URL:** `https://www.yourdomain.com` (your website — not the API). **Embed app in Shopify admin:** on.
     - **Use legacy install flow:** off (Shopify installs the app; Listing Agent then swaps Shopify's ID token for
       an access token — this only works with Shopify's own install).
     - **Scopes:** `write_products, read_locations, write_inventory, write_publications`
     - **Redirect URL:** `https://api.yourdomain.com/api/store/connect/shopify/callback` (the Connect button on your website)
     - **Webhooks** (API version 2026-07): the compliance topics `customers/data_request`, `customers/redact`,
       `shop/redact`, and `app/uninstalled` — all to `https://api.yourdomain.com/api/store/shopify/webhooks`
     - **Release** the version.
  3. Server `.env`: `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET` (from the app's Settings), and while Shopify
     reviews the app `SHOPIFY_TEST_CHARGES=true`. Then `bash deploy/update.sh`.
  4. Vercel → Environment Variables: `NEXT_PUBLIC_SHOPIFY_API_KEY` = the same Client ID → **Redeploy**.
  5. Test: Dev Dashboard → your app → **Test on development store** (or install it on your own store). It should
     open inside the Shopify admin, already signed in, with the store listed under Stores. On Credits, "Buy with
     Shopify" makes a test charge on a development store.
  6. **Distribution → Public distribution**, fill in the listing (screenshots, support email, privacy policy
     `https://www.yourdomain.com/privacy`, pricing: "Free to install · credit packs from $4, one-time charges"),
     a short **screencast** (install → a listing → publish → buy credits) and **submit for review**. Reviewers use
     a development store, so they don't need a login.
  7. When approved: `SHOPIFY_TEST_CHARGES=false` and `SHOPIFY_APP_STORE_URL=<the app's App Store page>` in `.env`,
     then `bash deploy/update.sh`. The Stores page on your website then sends Shopify sellers to the App Store.
  - The Connect button on your own website still works: after Shopify, the seller lands back on Stores and the
    connection is finished only by the Listing Agent account that pressed Connect (so a Connect link someone
    else sends can't attach a shop to their account).
  - Tokens: Listing Agent uses Shopify's expiring tokens (required for new public apps): 1 hour, renewed
    automatically; if a shop isn't used for ~90 days, opening the app in Shopify renews them.
  - Shops connected with their own app's keys ("Advanced") keep working as before; they just can't pay through Shopify.

## 5c. Daraz

1. Developer account at **open.daraz.com** (Daraz approves it — ID and app description needed).
2. App Console → create app "Listing Agent", **Callback URL:**
   `https://listingagent.duckdns.org/api/store/connect/daraz/callback`
3. Copy **App Key** and **App Secret** into `.env` as `DARAZ_APP_KEY` / `DARAZ_APP_SECRET`, then `bash deploy/update.sh`.
4. Sellers press **Connect Daraz** on the Stores page, log in to Seller Center and click Authorize.
   Access renews itself for 180 days; after that they press Connect again.
5. Daraz needs a **package weight and size** on each product (Your details) and checks new products (QC)
   before they show in the shop.

## 5d. eBay (US, UK, Canada, Australia)

1. Free account at **developer.ebay.com** → **Application Keys** → create a **Production** keyset.
2. **Alerts & Notifications → Marketplace account deletion** (eBay requires this before the keys work):
   - Endpoint: `https://listingagent.duckdns.org/api/store/ebay/account-deletion`
   - Verification token: 32–80 letters/digits you make up → also put it in `.env` as `EBAY_VERIFICATION_TOKEN`
     and run `bash deploy/update.sh` *before* pressing Save (eBay checks the endpoint immediately).
3. **User Tokens → Get a Token from eBay via Your Application → Add eBay Redirect URL**:
   - Your privacy policy URL: `https://<your-vercel-site>/privacy`
   - Auth accepted URL and Auth declined URL: `https://listingagent.duckdns.org/api/store/connect/ebay/callback`
   - eBay shows a **RuName** (e.g. `Ali_Raza-ListingA-PRD-…`) → `EBAY_RU_NAME` in `.env`.
4. `.env`: `EBAY_CLIENT_ID` (App ID), `EBAY_CLIENT_SECRET` (Cert ID), `EBAY_RU_NAME`, `EBAY_VERIFICATION_TOKEN`,
   then `bash deploy/update.sh`.
5. Sellers pick their eBay site + item location on the Stores page and log in. Before publishing they need
   **Business policies** (shipping + returns) in eBay Seller Hub. Product prices must be in that site's
   currency (Settings → where you sell).

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

- **Smart Click (one click):** enter the Smart Click **website** address (e.g. `frontend-smartclick.vercel.app`),
  log in there as admin and click Approve. Needs the Smart Click frontend with `/listing-agent/discover` and
  `/listing-agent/connect`, and its backend with `LISTING_API_CONNECT=true` (the default).
  *Advanced* still accepts the backend address + `LISTING_API_KEY`.
- **WooCommerce / Shopify:** as before. Photos now work for WooCommerce without a WordPress app password too,
  because `PUBLIC_BASE_URL` is set.
- `http://` store addresses are refused online (`ALLOW_HTTP_STORES=false`); stores must use https.
