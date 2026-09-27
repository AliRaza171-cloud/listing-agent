"""WooCommerce connector (REST API v3, /wp-json/wc/v3).

Credentials (WooCommerce > Settings > Advanced > REST API, permission Read/Write):
    consumer_key, consumer_secret           required
    wp_username, wp_app_password            optional (WordPress user > Profile > Application Passwords):
                                            lets us upload photos straight into the media library

Photos: WooCommerce can't receive files through its product API, only links it downloads.
So either we upload them via the WordPress media API (needs the application password), or,
when Listing Agent is online, we pass public links (PUBLIC_BASE_URL + /api/catalog/media/...).
"""
from __future__ import annotations

import html
import os

import httpx

from app.connectors.base import ConnectorError, ProductPayload, PublishResult, StoreCategory

TIMEOUT = httpx.Timeout(45.0, connect=10.0)


def _detail(r: httpx.Response) -> str | None:
    try:
        body = r.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        return body.get("message") or body.get("code")
    return None


def build_description(p: ProductPayload) -> tuple[str, str]:
    """-> (description HTML, short_description HTML)."""
    paras = [x.strip() for x in p.description.split("\n") if x.strip()]
    long_html = "".join(f"<p>{html.escape(x)}</p>" for x in paras)
    short_html = ""
    if p.highlights:
        short_html = "<ul>" + "".join(f"<li>{html.escape(h)}</li>" for h in p.highlights) + "</ul>"
    return long_html, short_html


class WooCommerceConnector:
    def __init__(self, store_url: str, credentials: dict):
        self.base = store_url.rstrip("/")
        self.key = credentials.get("consumer_key", "").strip()
        self.secret = credentials.get("consumer_secret", "").strip()
        self.wp_user = (credentials.get("wp_username") or "").strip()
        self.wp_pass = (credentials.get("wp_app_password") or "").replace(" ", "").strip()
        self._query_auth = False  # some hosts strip the Authorization header; then we fall back

    # ---------------------------------------------------------------- HTTP

    async def _request(self, method: str, url: str, *, auth_mode: str = "wc", **kw) -> httpx.Response:
        if not self.base.lower().startswith("https://"):
            raise ConnectorError("WooCommerce only accepts API keys over https:// — use your store's https address.")
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
                if auth_mode == "wp":
                    return await client.request(method, url, auth=(self.wp_user, self.wp_pass), **kw)
                if self._query_auth:
                    params = dict(kw.pop("params", {}) or {}, consumer_key=self.key, consumer_secret=self.secret)
                    return await client.request(method, url, params=params, **kw)
                r = await client.request(method, url, auth=(self.key, self.secret), **kw)
                if r.status_code == 401:  # header stripped by the host? try query-string auth once
                    params = dict(kw.pop("params", {}) or {}, consumer_key=self.key, consumer_secret=self.secret)
                    r2 = await client.request(method, url, params=params, **kw)
                    if r2.status_code != 401:
                        self._query_auth = True
                        return r2
                return r
        except httpx.TimeoutException:
            raise ConnectorError("The WooCommerce store took too long to answer.", retryable=True)
        except httpx.HTTPError:
            raise ConnectorError(f"Couldn't reach the store at {self.base}.", retryable=True)

    async def _wc(self, method: str, path: str, **kw) -> httpx.Response:
        r = await self._request(method, f"{self.base}/wp-json/wc/v3{path}", **kw)
        if r.status_code == 401:
            raise ConnectorError(_detail(r) or "WooCommerce rejected the API keys.")
        if r.status_code == 403:
            raise ConnectorError((_detail(r) or "Permission denied") + " — the API key needs Read/Write permission.")
        if r.status_code == 404 and path in ("/products", "/products/categories"):
            raise ConnectorError("No WooCommerce API at this address — check the URL, and that permalinks aren't "
                                 "set to 'Plain' in WordPress > Settings > Permalinks.")
        if r.status_code == 429 or r.status_code >= 500:
            raise ConnectorError(f"The store had a problem ({r.status_code}). We'll retry.", retryable=True)
        return r

    # ---------------------------------------------------------------- interface

    async def test_connection(self) -> None:
        if not self.key.startswith("ck_") or not self.secret.startswith("cs_"):
            raise ConnectorError("WooCommerce keys start with ck_ (consumer key) and cs_ (consumer secret).")
        r = await self._wc("GET", "/products", params={"per_page": 1})
        if r.status_code != 200:
            raise ConnectorError(_detail(r) or f"WooCommerce answered with an error ({r.status_code}).")
        if self.wp_user and self.wp_pass:
            me = await self._request("GET", f"{self.base}/wp-json/wp/v2/users/me", auth_mode="wp")
            if me.status_code != 200:
                raise ConnectorError("WordPress rejected the username/application password (used for photos).")

    async def list_categories(self) -> list[StoreCategory]:
        out: list[StoreCategory] = []
        for page in range(1, 6):  # up to 500 categories
            r = await self._wc("GET", "/products/categories", params={"per_page": 100, "page": page})
            if r.status_code != 200:
                raise ConnectorError(_detail(r) or "Couldn't read the store's categories.")
            rows = r.json()
            out += [StoreCategory(id=str(c["id"]), name=html.unescape(c["name"])) for c in rows
                    if c.get("slug") != "uncategorized"]
            if len(rows) < 100:
                break
        return out

    async def _images(self, p: ProductPayload) -> list[dict]:
        if self.wp_user and self.wp_pass and p.image_files:
            ids = []
            for f in p.image_files:
                r = await self._request(
                    "POST", f"{self.base}/wp-json/wp/v2/media", auth_mode="wp", content=f.data,
                    headers={"Content-Type": f.content_type,
                             "Content-Disposition": f'attachment; filename="{f.filename}"'})
                if r.status_code not in (200, 201):
                    raise ConnectorError(f"Photo upload to WordPress failed: {_detail(r) or r.status_code}",
                                         retryable=r.status_code >= 500)
                ids.append({"id": r.json()["id"]})
            return ids
        public = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
        if public and not any(h in public for h in ("localhost", "127.0.0.1", "host.docker.internal")):
            return [{"src": f"{public}{u}" if u.startswith("/") else u} for u in p.image_urls]
        raise ConnectorError(
            "WooCommerce needs a way to get the photos: add a WordPress username + application password "
            "to this store's connection (Stores page), or put Listing Agent online and set PUBLIC_BASE_URL.")

    async def _body(self, p: ProductPayload) -> dict:
        long_html, short_html = build_description(p)
        body: dict = {
            "name": p.title,
            "type": "simple",
            "status": "publish" if p.publish_live else "draft",
            "description": long_html,
            "short_description": short_html,
            "regular_price": f"{p.price:.2f}",
            "sale_price": f"{p.price * (1 - p.discount_pct / 100):.2f}" if p.discount_pct else "",
            "images": await self._images(p),
        }
        if p.sku:
            body["sku"] = p.sku
        if p.stock is not None:
            body["manage_stock"] = True
            body["stock_quantity"] = p.stock
        if p.category_name:
            wanted = p.category_name.strip().lower()
            match = next((c for c in await self.list_categories() if c.name.strip().lower() == wanted), None)
            if match:
                body["categories"] = [{"id": int(match.id)}]
        return body

    def _result(self, r: httpx.Response) -> PublishResult:
        if r.status_code not in (200, 201):
            raise ConnectorError(_detail(r) or f"WooCommerce refused the product ({r.status_code}).")
        d = r.json()
        return PublishResult(external_id=str(d["id"]), external_url=d.get("permalink"))

    async def create_product(self, payload: ProductPayload) -> PublishResult:
        return self._result(await self._wc("POST", "/products", json=await self._body(payload)))

    async def update_product(self, external_id: str, payload: ProductPayload) -> PublishResult:
        r = await self._wc("PUT", f"/products/{external_id}", json=await self._body(payload))
        if r.status_code == 404 or (r.status_code == 400 and "invalid" in (_detail(r) or "").lower()
                                    and "id" in (_detail(r) or "").lower()):
            return await self.create_product(payload)
        return self._result(r)
