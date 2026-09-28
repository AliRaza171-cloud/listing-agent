"""Shopify connector (Admin GraphQL API).

Credentials, either:
- access_token — an Admin API access token (from our OAuth app, or a legacy custom app), or
- client_id + client_secret — a Dev Dashboard app installed on a store in the same Shopify
  organization. We swap them for a 24-hour token (client credentials grant) and renew it as needed.
Scopes: write_products (required), read_locations + write_inventory (stock), write_publications
(show live products in the Online Store). Store URL: https://<shop>.myshopify.com

One productSet call creates or updates the whole product. Photos are uploaded as files through
Shopify's staged uploads, so they don't need to be on the public internet.
"""
from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import time

import httpx

from app.connectors.base import ConnectorError, ProductPayload, PublishResult, StoreCategory

log = logging.getLogger("lagent.publisher.shopify")
TIMEOUT = httpx.Timeout(45.0, connect=10.0)
# (shop, client_id, secret fingerprint) -> (token, expires_at). Client-credential tokens last 24 h;
# reuse until near expiry. The secret is part of the key so a wrong/changed secret is never served a cached token.
_TOKENS: dict[tuple[str, str, str], tuple[str, float]] = {}

PRODUCT_SET = """
mutation ProductSet($input: ProductSetInput!, $identifier: ProductSetIdentifiers) {
  productSet(synchronous: true, input: $input, identifier: $identifier) {
    product { id handle status onlineStoreUrl }
    userErrors { code field message }
  }
}"""

STAGED_UPLOADS = """
mutation Staged($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets { url resourceUrl parameters { name value } }
    userErrors { field message }
  }
}"""


def _shop_domain(store_url: str) -> str:
    host = re.sub(r"^https?://", "", store_url.strip().rstrip("/")).split("/")[0].lower()
    if not host.endswith(".myshopify.com"):
        raise ConnectorError("Use your store's myshopify.com address, e.g. https://yourstore.myshopify.com")
    return host


class ShopifyConnector:
    def __init__(self, store_url: str, credentials: dict):
        self.shop = _shop_domain(store_url)
        self.token = (credentials.get("access_token") or "").strip()
        self.client_id = (credentials.get("client_id") or "").strip()
        self.client_secret = (credentials.get("client_secret") or "").strip()
        self.uses_client_credentials = not self.token and bool(self.client_id and self.client_secret)
        version = os.environ.get("SHOPIFY_API_VERSION", "2026-07")
        self.endpoint = f"https://{self.shop}/admin/api/{version}/graphql.json"

    def _cache_key(self) -> tuple[str, str, str]:
        return self.shop, self.client_id, hashlib.sha256(self.client_secret.encode()).hexdigest()[:16]

    async def _client_token(self, *, fresh: bool = False) -> str:
        key = self._cache_key()
        cached = _TOKENS.get(key)
        if cached and not fresh and cached[1] > time.time() + 300:
            return cached[0]
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                r = await client.post(f"https://{self.shop}/admin/oauth/access_token", data={
                    "grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret})
        except httpx.HTTPError:
            raise ConnectorError(f"Couldn't reach {self.shop}.", retryable=True)
        try:
            body = r.json()
        except ValueError:
            body = {}
        token = body.get("access_token") if r.status_code == 200 else None
        if not token:
            err = str(body.get("error_description") or body.get("error") or body.get("errors") or "").strip()
            if r.status_code == 404:
                raise ConnectorError(f"No Shopify store at {self.shop}.")
            if "cannot be performed on this shop" in err.lower() or "shop_not_permitted" in err.lower():
                raise ConnectorError("Shopify says this app can't be used on this store. The app must be created in the "
                                     "same Shopify account (organization) as the store, and installed on it.")
            if r.status_code in (400, 401) and ("client" in err.lower() or not err):
                raise ConnectorError("Shopify rejected the Client ID or Client secret. Copy both again from the app's "
                                     "Settings in the Dev Dashboard, and check the app is installed on this store.")
            if r.status_code == 429 or r.status_code >= 500:
                raise ConnectorError(f"Shopify had a problem ({r.status_code}). We'll retry.", retryable=True)
            raise ConnectorError(f"Shopify didn't give an access token{': ' + err[:150] if err else ''}.")
        _TOKENS[key] = (token, time.time() + float(body.get("expires_in") or 86399))
        return token

    async def _gql(self, query: str, variables: dict | None = None, *, _retry: bool = True) -> dict:
        if self.uses_client_credentials:
            self.token = await self._client_token()
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                r = await client.post(self.endpoint, json={"query": query, "variables": variables or {}},
                                      headers={"X-Shopify-Access-Token": self.token})
        except httpx.TimeoutException:
            raise ConnectorError("Shopify took too long to answer.", retryable=True)
        except httpx.HTTPError:
            raise ConnectorError(f"Couldn't reach {self.shop}.", retryable=True)
        if r.status_code == 401 and self.uses_client_credentials and _retry:
            _TOKENS.pop(self._cache_key(), None)   # expired or revoked early: get a new one once
            self.token = await self._client_token(fresh=True)
            return await self._gql(query, variables, _retry=False)
        if r.status_code in (401, 403):
            raise ConnectorError("Shopify rejected the access token (or the app lacks the needed scopes).")
        if r.status_code == 404:
            raise ConnectorError(f"No Shopify store at {self.shop}.")
        if r.status_code == 429 or r.status_code >= 500:
            raise ConnectorError(f"Shopify had a problem ({r.status_code}). We'll retry.", retryable=True)
        body = r.json()
        errors = body.get("errors")
        if errors:
            msg = errors[0].get("message", str(errors)) if isinstance(errors, list) else str(errors)
            throttled = isinstance(errors, list) and any(
                (e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors)
            raise ConnectorError(f"Shopify: {msg}", retryable=throttled)
        return body.get("data") or {}

    # ---------------------------------------------------------------- interface

    async def test_connection(self) -> None:
        if not self.token and not self.uses_client_credentials:
            raise ConnectorError("Enter an Admin API access token, or the app's Client ID and Client secret.")
        if self.token and not self.token.startswith("shp"):
            raise ConnectorError("That isn't a Shopify access token (they start with shp…).")
        data = await self._gql("{ shop { name } }")
        if not (data.get("shop") or {}).get("name"):
            raise ConnectorError("Shopify didn't return the shop details.")

    async def list_categories(self) -> list[StoreCategory]:
        """Manual ('custom') collections — the ones products can be put into."""
        data = await self._gql('{ collections(first: 250, query: "collection_type:custom") { nodes { id title } } }')
        return [StoreCategory(id=c["id"], name=c["title"]) for c in (data.get("collections") or {}).get("nodes", [])]

    async def _upload_files(self, p: ProductPayload) -> list[dict]:
        if not p.image_files:
            return []
        data = await self._gql(STAGED_UPLOADS, {"input": [
            {"resource": "IMAGE", "filename": f.filename, "mimeType": f.content_type, "httpMethod": "POST"}
            for f in p.image_files]})
        res = data.get("stagedUploadsCreate") or {}
        if res.get("userErrors"):
            raise ConnectorError(f"Shopify photo upload: {res['userErrors'][0]['message']}")
        files = []
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            for f, target in zip(p.image_files, res.get("stagedTargets") or []):
                form = {prm["name"]: prm["value"] for prm in target["parameters"]}
                try:
                    up = await client.post(target["url"], data=form, files={"file": (f.filename, f.data, f.content_type)})
                except httpx.HTTPError:
                    raise ConnectorError("Uploading a photo to Shopify failed.", retryable=True)
                if up.status_code not in (200, 201, 204):
                    raise ConnectorError(f"Uploading a photo to Shopify failed ({up.status_code}).",
                                         retryable=up.status_code >= 500)
                files.append({"originalSource": target["resourceUrl"], "contentType": "IMAGE",
                              "alt": p.title[:120], "filename": f.filename})
        return files

    async def _location_id(self) -> str | None:
        try:
            data = await self._gql("{ locations(first: 1) { nodes { id } } }")
        except ConnectorError as exc:  # no read_locations scope: publish without stock
            log.info("no location for stock (%s)", exc)
            return None
        nodes = (data.get("locations") or {}).get("nodes") or []
        return nodes[0]["id"] if nodes else None

    async def _input(self, p: ProductPayload) -> dict:
        paras = [x.strip() for x in p.description.split("\n") if x.strip()]
        body_html = "".join(f"<p>{html.escape(x)}</p>" for x in paras)
        if p.highlights:
            body_html += "<ul>" + "".join(f"<li>{html.escape(h)}</li>" for h in p.highlights) + "</ul>"
        sale = round(p.price * (1 - p.discount_pct / 100), 2) if p.discount_pct else round(p.price, 2)
        variant: dict = {
            "optionValues": [{"optionName": "Title", "name": "Default Title"}],
            "price": sale,
        }
        if p.discount_pct:
            variant["compareAtPrice"] = round(p.price, 2)
        if p.sku:
            variant["sku"] = p.sku
        if p.stock is not None:
            location = await self._location_id()
            if location:
                variant["inventoryItem"] = {"tracked": True}
                variant["inventoryQuantities"] = [{"locationId": location, "name": "available", "quantity": p.stock}]
        product: dict = {
            "title": p.title,
            "descriptionHtml": body_html,
            "status": "ACTIVE" if p.publish_live else "DRAFT",
            "tags": p.tags[:250],
            "productOptions": [{"name": "Title", "position": 1, "values": [{"name": "Default Title"}]}],
            "variants": [variant],
            "files": await self._upload_files(p),
        }
        if p.seo_title or p.meta_description:
            product["seo"] = {k: v for k, v in (("title", p.seo_title), ("description", p.meta_description)) if v}
        if p.category_name:
            wanted = p.category_name.strip().lower()
            match = next((c for c in await self.list_categories() if c.name.strip().lower() == wanted), None)
            collection_id = match.id if match else await self._create_collection(p.category_name.strip())
            if collection_id:
                product["collections"] = [collection_id]
        return product

    async def _create_collection(self, title: str) -> str | None:
        """The listing's category doesn't exist as a collection yet: create a manual collection
        (best effort — the product is still published without it if this fails)."""
        try:
            data = await self._gql("""mutation C($input: CollectionInput!) {
                collectionCreate(input: $input) { collection { id } userErrors { message } } }""",
                                   {"input": {"title": title}})
        except ConnectorError as exc:
            log.info("couldn't create collection %r (%s)", title, exc)
            return None
        res = data.get("collectionCreate") or {}
        if res.get("userErrors"):
            log.info("couldn't create collection %r: %s", title, res["userErrors"][0].get("message"))
            return None
        collection_id = (res.get("collection") or {}).get("id")
        if collection_id:
            await self._publish_online_store(collection_id)   # so the new collection shows in the shop
        return collection_id

    async def _publish_online_store(self, product_id: str) -> None:
        """ACTIVE isn't enough to be visible: the product must be on the Online Store channel.
        Needs write_publications; skipped quietly without it."""
        try:
            data = await self._gql("{ publications(first: 20) { nodes { id name } } }")
            pub = next((n for n in (data.get("publications") or {}).get("nodes", [])
                        if n["name"].lower() == "online store"), None)
            if pub:
                await self._gql("""mutation P($id: ID!, $input: [PublicationInput!]!) {
                    publishablePublish(id: $id, input: $input) { userErrors { message } } }""",
                                {"id": product_id, "input": [{"publicationId": pub["id"]}]})
        except ConnectorError as exc:
            log.info("couldn't publish to Online Store (%s)", exc)

    async def _set(self, p: ProductPayload, existing_id: str | None) -> PublishResult:
        variables: dict = {"input": await self._input(p)}
        if existing_id:
            variables["identifier"] = {"id": existing_id}
        data = await self._gql(PRODUCT_SET, variables)
        res = data.get("productSet") or {}
        errors = res.get("userErrors") or []
        if errors:
            msg = errors[0].get("message", "unknown error")
            if existing_id and ("not exist" in msg.lower() or "not found" in msg.lower()):
                return await self._set(p, None)  # deleted in Shopify since -> create again
            raise ConnectorError(f"Shopify refused the product: {msg}")
        product = res.get("product") or {}
        if not product.get("id"):
            raise ConnectorError("Shopify didn't return the product.")
        if p.publish_live:
            await self._publish_online_store(product["id"])
        numeric = product["id"].rsplit("/", 1)[-1]
        url = product.get("onlineStoreUrl") or f"https://{self.shop}/admin/products/{numeric}"
        return PublishResult(external_id=product["id"], external_url=url)

    async def create_product(self, payload: ProductPayload) -> PublishResult:
        return await self._set(payload, None)

    async def update_product(self, external_id: str, payload: ProductPayload) -> PublishResult:
        return await self._set(payload, external_id)
