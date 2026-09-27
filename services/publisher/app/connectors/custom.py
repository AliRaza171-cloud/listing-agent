"""Custom-store connector — stores that implement the Listing API (Smart Click is the first).

The store exposes, behind an X-Api-Key header:
    GET  /listing-api/ping            connection test
    GET  /listing-api/categories      [{id, name}]
    POST /listing-api/products        multipart: data=<JSON>, images=<files>  -> {id, url}
    PUT  /listing-api/products/{id}   same body (re-publish updates, no duplicates)
"""
from __future__ import annotations

import json
import os
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.connectors.base import ConnectorError, ProductPayload, PublishResult, StoreCategory

TIMEOUT = httpx.Timeout(30.0, connect=8.0)


def _base_url(store_url: str) -> str:
    """Inside Docker, "localhost" is the container itself. For a store running on the
    developer's own machine we swap it for STORE_LOCALHOST_ALIAS (host.docker.internal)."""
    alias = os.environ.get("STORE_LOCALHOST_ALIAS", "")
    parts = urlsplit(store_url.rstrip("/"))
    if alias and parts.hostname in ("localhost", "127.0.0.1"):
        netloc = alias + (f":{parts.port}" if parts.port else "")
        parts = parts._replace(netloc=netloc)
    return urlunsplit(parts)


def _detail(r: httpx.Response) -> str | None:
    try:
        d = r.json().get("detail")
    except (ValueError, AttributeError):
        return None
    if isinstance(d, str):
        return d
    if isinstance(d, list) and d and isinstance(d[0], dict):
        return d[0].get("msg")
    return None


class CustomStoreConnector:
    def __init__(self, store_url: str, credentials: dict):
        self.base = _base_url(store_url)
        self.headers = {"X-Api-Key": credentials.get("api_key", "")}

    async def _call(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, headers=self.headers) as client:
                r = await client.request(method, f"{self.base}/listing-api{path}", **kwargs)
        except httpx.TimeoutException:
            raise ConnectorError("The store took too long to answer.", retryable=True)
        except httpx.HTTPError:
            raise ConnectorError(f"Couldn't reach the store at {self.base}. Is it running?", retryable=True)
        if r.status_code == 401:
            raise ConnectorError("The store rejected the API key.")
        if r.status_code == 429 or r.status_code >= 500:
            raise ConnectorError(f"The store had a problem ({r.status_code}). We'll retry.", retryable=True)
        return r

    async def test_connection(self) -> None:
        r = await self._call("GET", "/ping")
        if r.status_code == 404:
            raise ConnectorError(_detail(r) or "No Listing API found at this address.")
        if r.status_code != 200:
            raise ConnectorError(_detail(r) or f"The store answered with an error ({r.status_code}).")

    async def list_categories(self) -> list[StoreCategory]:
        r = await self._call("GET", "/categories")
        if r.status_code != 200:
            raise ConnectorError(_detail(r) or "Couldn't read the store's categories.")
        return [StoreCategory(id=str(c["id"]), name=c["name"]) for c in r.json()]

    def _body(self, p: ProductPayload) -> tuple[dict, list]:
        data = {
            "title": p.title, "description": p.description, "highlights": p.highlights,
            "price": p.price, "discount_pct": p.discount_pct, "free_shipping": p.free_shipping,
            "stock": p.stock, "sku": p.sku, "tags": p.tags, "category": p.category_name,
            "seo_title": p.seo_title, "meta_description": p.meta_description,
            "mode": "live" if p.publish_live else "draft",
        }
        if not p.image_files:
            raise ConnectorError("This product has no photos — the store needs at least one.")
        files = [("images", (f.filename, f.data, f.content_type)) for f in p.image_files]
        return {"data": json.dumps(data)}, files

    def _result(self, r: httpx.Response) -> PublishResult:
        if r.status_code not in (200, 201):
            raise ConnectorError(_detail(r) or f"The store refused the product ({r.status_code}).")
        body = r.json()
        return PublishResult(external_id=str(body["id"]), external_url=body.get("url"))

    async def create_product(self, payload: ProductPayload) -> PublishResult:
        form, files = self._body(payload)
        return self._result(await self._call("POST", "/products", data=form, files=files))

    async def update_product(self, external_id: str, payload: ProductPayload) -> PublishResult:
        form, files = self._body(payload)
        r = await self._call("PUT", f"/products/{external_id}", data=form, files=files)
        if r.status_code == 404:  # deleted in the store since last time -> publish it fresh
            return await self.create_product(payload)
        return self._result(r)
