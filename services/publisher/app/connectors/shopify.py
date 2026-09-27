"""Shopify connector (Admin GraphQL API).

Credentials: access_token — the Admin API access token of a custom app installed on the store,
with scopes write_products (required), read_locations + write_inventory (stock), write_publications
(show live products in the Online Store). Store URL: https://<shop>.myshopify.com

One productSet call creates or updates the whole product. Photos are uploaded as files through
Shopify's staged uploads, so they don't need to be on the public internet.
"""
from __future__ import annotations

import html
import logging
import os
import re

import httpx

from app.connectors.base import ConnectorError, ProductPayload, PublishResult, StoreCategory

log = logging.getLogger("lagent.publisher.shopify")
TIMEOUT = httpx.Timeout(45.0, connect=10.0)

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
        version = os.environ.get("SHOPIFY_API_VERSION", "2026-07")
        self.endpoint = f"https://{self.shop}/admin/api/{version}/graphql.json"

    async def _gql(self, query: str, variables: dict | None = None) -> dict:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                r = await client.post(self.endpoint, json={"query": query, "variables": variables or {}},
                                      headers={"X-Shopify-Access-Token": self.token})
        except httpx.TimeoutException:
            raise ConnectorError("Shopify took too long to answer.", retryable=True)
        except httpx.HTTPError:
            raise ConnectorError(f"Couldn't reach {self.shop}.", retryable=True)
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
        if not self.token.startswith("shp"):
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
            if match:
                product["collections"] = [match.id]
        return product

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
