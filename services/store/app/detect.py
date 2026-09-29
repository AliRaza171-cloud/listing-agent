"""Which e-commerce platform runs a website? Used by the Stores page's single "store address" box.

We fetch a few public pages of the site (never anything on a private network) and recognise the
platform from what comes back. Supported platforms go straight to their one-click connection;
others are named, so the seller knows why it can't connect yet.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from dataclasses import asdict, dataclass
from urllib.parse import urljoin, urlsplit

import httpx

MAX_BYTES = 400_000          # enough for any homepage's <head>; we never download more
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
MAX_REDIRECTS = 4
UA = {"User-Agent": "ListingAgent/1.0 (+https://www.ecommercelistingagent.com)", "Accept": "text/html,application/json,*/*"}


class DetectError(Exception):
    """Seller-safe message."""


@dataclass
class Detected:
    platform: str            # woocommerce | shopify | custom | daraz | ebay | wordpress | wix | … | unknown
    name: str                # shown to the seller, e.g. "WooCommerce", "Wix"
    supported: bool          # Listing Agent can connect to it
    store: str = ""          # what the connector needs: site URL, or the *.myshopify.com domain
    note: str = ""

    def out(self) -> dict:
        return asdict(self)


MARKETPLACES = {
    "daraz": ("Daraz", ("daraz.pk", "daraz.com.bd", "daraz.lk", "daraz.com.np", "shop.com.mm")),
    "ebay": ("eBay", ("ebay.com", "ebay.co.uk", "ebay.ca", "ebay.com.au", "ebay.de", "ebay.fr", "ebay.it", "ebay.es")),
}

# Platforms we recognise but can't publish to yet: (id, name, fingerprints in the page or headers, lower-case)
OTHERS = [
    ("wix", "Wix", ("static.wixstatic.com", "x-wix-request-id", "wix.com website builder")),
    ("squarespace", "Squarespace", ("static1.squarespace.com", "squarespace-cdn.com")),
    ("bigcommerce", "BigCommerce", ("cdn11.bigcommerce.com", "bigcommerce.com/s-")),
    ("magento", "Magento (Adobe Commerce)", ("text/x-magento-init", "mage/cookies", "/static/version")),
    ("prestashop", "PrestaShop", ("prestashop", "/modules/ps_")),
    ("opencart", "OpenCart", ("index.php?route=common", "catalog/view/theme")),
    ("ecwid", "Ecwid", ("app.ecwid.com",)),
    ("webflow", "Webflow", ("data-wf-site", "webflow.js")),
    ("weebly", "Square Online (Weebly)", ("editmysite.com", "weebly.com")),
    ("godaddy", "GoDaddy Website Builder", ("img1.wsimg.com",)),
    ("zid", "Zid", ("zid.store", "zidcdn")),
    ("salla", "Salla", ("salla.sa", "cdn.salla")),
]

SHOP_RE = re.compile(r"([a-z0-9][a-z0-9-]*\.myshopify\.com)")


def normalize(raw: str, *, allow_http: bool) -> str:
    url = (raw or "").strip().rstrip("/")
    if not url:
        raise DetectError("Enter your store's address, e.g. yourstore.com")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host or "." not in host and host != "localhost":
        raise DetectError("Enter your store's address, e.g. yourstore.com")
    if parts.scheme.lower() == "http" and not allow_http:
        url = "https://" + url.split("://", 1)[1]
    return f"{urlsplit(url).scheme.lower()}://{parts.netloc.lower()}" + (parts.path.rstrip("/") if parts.path not in ("", "/") else "")


async def _host_is_public(host: str) -> bool:
    """Only fetch sites on the public internet: never localhost, private or cloud-metadata addresses."""
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_global
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    addrs = {info[4][0] for info in infos}
    return bool(addrs) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs)


async def safe_get(http: httpx.AsyncClient, url: str, *, allow_local: bool = False, alias: str = "",
                   resolve_ok=None) -> httpx.Response | None:
    """GET a public https URL, following up to 4 redirects, each checked again. None if not reachable.
    allow_local (local development only): http and localhost allowed; localhost -> `alias` inside Docker."""
    check = resolve_ok or _host_is_public
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if allow_local:
            if alias and host in ("localhost", "127.0.0.1"):
                url = parts._replace(netloc=alias + (f":{parts.port}" if parts.port else "")).geturl()
        elif parts.scheme != "https" or not await check(host):
            return None
        try:
            async with http.stream("GET", url, headers=UA, timeout=TIMEOUT, follow_redirects=False) as r:
                if r.is_redirect and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                body = b""
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) >= MAX_BYTES:
                        break
                return httpx.Response(r.status_code, headers=r.headers, content=body[:MAX_BYTES],
                                      request=httpx.Request("GET", url))
        except (httpx.HTTPError, ValueError):
            return None
    return None


ADMIN_STORE_RE = re.compile(r"^https://admin\.shopify\.com/store/([a-z0-9][a-z0-9-]*)(?:[/?#]|$)")


async def shopify_name_from_admin(http: httpx.AsyncClient, origin: str, *, allow_local: bool = False,
                                  resolve_ok=None) -> str | None:
    """A Shopify store on its own domain sends <domain>/admin to its real admin address, which names the
    store (https://admin.shopify.com/store/<name>/… or https://<name>.myshopify.com/admin). We only read
    that redirect — nothing is followed and no login happens."""
    url = f"{origin}/admin"
    for _ in range(3):
        parts = urlsplit(url)
        if not allow_local and (parts.scheme != "https" or not await (resolve_ok or _host_is_public)(parts.hostname or "")):
            return None
        try:
            r = await http.get(url, headers=UA, timeout=TIMEOUT, follow_redirects=False)
        except httpx.HTTPError:
            return None
        loc = r.headers.get("location") if r.is_redirect else None
        if not loc:
            return None
        loc = urljoin(url, loc)
        host = (urlsplit(loc).hostname or "").lower()
        if host.endswith(".myshopify.com") and SHOP_RE.fullmatch(host):
            return host
        m = ADMIN_STORE_RE.match(loc.lower())
        if m:
            return f"{m.group(1)}.myshopify.com"
        url = loc              # e.g. mydomain.com/admin -> www.mydomain.com/admin
    return None


def _json(r: httpx.Response | None) -> dict | None:
    if r is None or r.status_code != 200:
        return None
    try:
        data = json.loads(r.content)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _origin(r: httpx.Response, fallback: str) -> str:
    parts = urlsplit(str(r.request.url)) if r is not None else None
    return f"{parts.scheme}://{parts.netloc}" if parts and parts.netloc else fallback


async def detect(raw: str, http: httpx.AsyncClient, *, allow_local: bool = False, alias: str = "",
                 resolve_ok=None) -> Detected:
    site = normalize(raw, allow_http=allow_local)
    host = urlsplit(site).hostname or ""
    get = lambda u: safe_get(http, u, allow_local=allow_local, alias=alias, resolve_ok=resolve_ok)  # noqa: E731

    for pid, (name, domains) in MARKETPLACES.items():
        if any(host == d or host.endswith("." + d) for d in domains):
            return Detected(pid, name, True, note=f"Use the “Connect {name}” button — you'll log in to {name} there.")
    if host.endswith(".myshopify.com"):
        return Detected("shopify", "Shopify", True, store=host)

    # Sites that support Listing Agent's own connection (e.g. Smart Click)
    info = _json(await get(f"{site}/listing-agent/discover"))
    if info and re.match(r"^https?://", str(info.get("api_url") or "")):
        return Detected("custom", str(info.get("store_name") or "Listing Agent–ready store")[:80], True, store=site)

    home = await get(site + "/")
    if home is None:
        raise DetectError("Couldn't open that website. Check the address (it needs https://) and try again.")
    origin = _origin(home, site)
    text = home.content.decode("utf-8", "replace").lower()
    headers = " ".join(f"{k}:{v}" for k, v in home.headers.items()).lower()

    shopify_marks = ("cdn.shopify.com", "shopify.theme", "/cdn/shop/", "shopify-digital-wallet", "myshopify.com")
    if "x-shopid" in headers or "x-shopify-stage" in headers or any(m in text for m in shopify_marks):
        m = re.search(r'shopify\.shop\s*=\s*"([a-z0-9-]+\.myshopify\.com)"', text) or SHOP_RE.search(text)
        if m:
            return Detected("shopify", "Shopify", True, store=m.group(1))
        name = await shopify_name_from_admin(http, origin, allow_local=allow_local, resolve_ok=resolve_ok)
        if name:
            return Detected("shopify", "Shopify", True, store=name)
        return Detected("shopify", "Shopify", True,
                        note="This is a Shopify store. Enter its Shopify name (yourstore.myshopify.com) in the Shopify card.")

    wp = _json(await get(f"{origin}/wp-json/"))
    namespaces = [str(n) for n in (wp or {}).get("namespaces") or []]
    if any(n.startswith("wc/") for n in namespaces) or "woocommerce" in text:
        return Detected("woocommerce", "WooCommerce", True, store=origin)
    if wp is not None or "wp-content/" in text:
        return Detected("wordpress", "WordPress (without WooCommerce)", False,
                        note="Install the free WooCommerce plugin on this WordPress site, then connect it here.")

    for pid, name, marks in OTHERS:
        if any(m in text or m in headers for m in marks):
            return Detected(pid, name, False,
                            note=f"This store runs on {name}. Listing Agent can't publish to {name} yet.")
    return Detected("unknown", "Unknown", False,
                    note="Couldn't tell which platform this store uses. Listing Agent connects to WooCommerce, "
                         "Shopify, Daraz, eBay and stores with the Listing Agent connection (like Smart Click).")
