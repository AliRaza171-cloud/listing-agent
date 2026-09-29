"""eBay connector (Sell Inventory API) for the English-language eBay sites (US, UK, Canada, Australia).

Credentials (saved and renewed by the store service after the seller's eBay login): access_token,
marketplace (EBAY_US…), username, and the item location (city, postal_code). The app's own keys
(EBAY_CLIENT_ID / EBAY_CLIENT_SECRET) come from settings and are used for the Taxonomy API.

Publishing one product:
1. the product's currency must be the eBay site's (USD for eBay US…)
2. item location — created once ("listing-agent") from the city/postal code given when connecting
3. business policies — the seller's shipping, return (and payment) policies on that eBay site
4. category — eBay's own suggestion for the title (Taxonomy API)
5. item specifics — required ones filled from the listing / what the AI saw ("Unbranded",
   "Does Not Apply" where eBay allows); anything else missing is reported to the seller
6. photos — uploaded to eBay's picture service (Media API)
7. inventory item (PUT, by SKU) -> offer (create or update) -> publish when "live";
   "draft" keeps an unpublished offer (and withdraws a live listing)
"""
from __future__ import annotations

import html
import logging
import os
import re
import time

import httpx

from lagent_common import ebay
from app.connectors.base import ConnectorError, ProductPayload, PublishResult, StoreCategory

log = logging.getLogger("lagent.publisher.ebay")
TIMEOUT = httpx.Timeout(45.0, connect=10.0)
MAX_IMAGES = 12
LOCATION_KEY = "listing-agent"
_TREES: dict[str, str] = {}               # marketplace -> category tree id
_AUTH: dict[str, ebay.EbayAuth] = {}      # one app-token cache per process


def _auth() -> ebay.EbayAuth:
    key = os.environ.get("EBAY_CLIENT_ID", "")
    if key not in _AUTH:
        try:
            _AUTH[key] = ebay.EbayAuth(key, os.environ.get("EBAY_CLIENT_SECRET", ""),
                                       api=os.environ.get("EBAY_API_URL", ebay.API))
        except ebay.EbayError as exc:
            raise ConnectorError(str(exc))
    return _AUTH[key]


class EbayConnector:
    def __init__(self, store_url: str, credentials: dict):
        self.token = (credentials.get("access_token") or "").strip()
        self.market = ebay.MARKETPLACES.get(credentials.get("marketplace") or "") or ebay.marketplace_for_site(store_url)
        if not self.market:
            raise ConnectorError("This eBay connection doesn't say which eBay site it is — connect eBay again.")
        self.city, self.postal = credentials.get("city", ""), credentials.get("postal_code", "")
        self.api = os.environ.get("EBAY_API_URL", ebay.API).rstrip("/")
        self.apim = os.environ.get("EBAY_MEDIA_URL", ebay.APIM).rstrip("/")
        self.apiz = os.environ.get("EBAY_IDENTITY_URL", ebay.APIZ).rstrip("/")
        self.http = httpx.AsyncClient(timeout=TIMEOUT)

    # ---------------------------------------------------------------- HTTP

    async def _req(self, method: str, url: str, *, token: str | None = None, ok=(200, 201, 204), **kw) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token or self.token}", "Accept": "application/json",
                   "Content-Language": self.market.language, **kw.pop("headers", {})}
        if "json" in kw:
            headers["Content-Type"] = "application/json"
        try:
            r = await self.http.request(method, url, headers=headers, **kw)
        except httpx.TimeoutException:
            raise ConnectorError("eBay took too long to answer.", retryable=True)
        except httpx.HTTPError:
            raise ConnectorError("Couldn't reach eBay.", retryable=True)
        if r.status_code in ok:
            return r
        if r.status_code == 401:
            raise ConnectorError("eBay says the login has expired — press Connect on eBay again (Stores page).")
        if r.status_code == 429 or r.status_code >= 500:
            raise ConnectorError(f"eBay had a problem ({r.status_code}). We'll retry.", retryable=True)
        raise ConnectorError(f"eBay: {ebay.error_message(r)}")

    async def _json(self, method: str, url: str, **kw) -> dict:
        r = await self._req(method, url, **kw)
        try:
            return r.json() if r.content else {}
        except ValueError:
            return {}

    # ---------------------------------------------------------------- interface

    async def test_connection(self) -> None:
        if not self.token:
            raise ConnectorError("eBay didn't give an access token.")
        who = await self._json("GET", f"{self.apiz}/commerce/identity/v1/user/")
        if not (who.get("username") or who.get("userId")):
            raise ConnectorError("eBay didn't return the account details.")

    async def list_categories(self) -> list[StoreCategory]:
        return []   # eBay's category tree is huge; the AI suggests a name and eBay maps the title (below)

    async def create_product(self, p: ProductPayload) -> PublishResult:
        return await self._publish("", p)

    async def update_product(self, external_id: str, p: ProductPayload) -> PublishResult:
        return await self._publish(external_id, p)

    # ---------------------------------------------------------------- setup the listing needs

    async def _location(self) -> str:
        d = await self._json("GET", f"{self.api}/sell/inventory/v1/location", params={"limit": 100})
        for loc in d.get("locations") or []:
            if loc.get("merchantLocationKey") == LOCATION_KEY or loc.get("merchantLocationStatus") == "ENABLED":
                return loc["merchantLocationKey"]
        if not (self.city and self.postal):
            raise ConnectorError("eBay needs an item location — connect eBay again and enter your city and postal code.")
        await self._req("POST", f"{self.api}/sell/inventory/v1/location/{LOCATION_KEY}", json={
            "location": {"address": {"city": self.city, "postalCode": self.postal, "country": self.market.country}},
            "locationTypes": ["WAREHOUSE"], "merchantLocationStatus": "ENABLED", "name": "Listing Agent"})
        return LOCATION_KEY

    async def _policies(self) -> dict:
        out = {}
        for kind, key, field in (("fulfillment_policy", "fulfillmentPolicies", "fulfillmentPolicyId"),
                                 ("return_policy", "returnPolicies", "returnPolicyId"),
                                 ("payment_policy", "paymentPolicies", "paymentPolicyId")):
            try:
                d = await self._json("GET", f"{self.api}/sell/account/v1/{kind}", params={"marketplace_id": self.market.id})
            except ConnectorError as exc:
                if exc.retryable:
                    raise
                d = {}
            items = d.get(key) or []
            if items:
                out[field] = str(items[0][field])
        missing = [n for f, n in (("fulfillmentPolicyId", "shipping"), ("returnPolicyId", "returns")) if f not in out]
        if missing:
            raise ConnectorError(f"eBay needs your {' and '.join(missing)} settings for {self.market.name} first: in eBay "
                                 "Seller Hub go to Account → Business policies and create them, then publish again.")
        return out

    async def _category(self, p: ProductPayload) -> tuple[str, str]:
        app = await _auth().app_token()
        base = f"{self.api}/commerce/taxonomy/v1"
        tree = _TREES.get(self.market.id)
        if not tree:
            d = await self._json("GET", f"{base}/get_default_category_tree_id", token=app,
                                 params={"marketplace_id": self.market.id})
            tree = _TREES[self.market.id] = str(d.get("categoryTreeId") or "0")
        q = " ".join(x for x in (p.title, p.category_name) if x)[:350]
        d = await self._json("GET", f"{base}/category_tree/{tree}/get_category_suggestions", token=app, params={"q": q})
        sugg = d.get("categorySuggestions") or []
        if not sugg:
            raise ConnectorError(f"eBay couldn't suggest a category for “{p.title}”. Make the title clearer and publish again.")
        cat = sugg[0]["category"]
        return str(cat["categoryId"]), str(cat.get("categoryName") or "")

    async def _aspects(self, category_id: str, p: ProductPayload) -> tuple[dict, list[str]]:
        app = await _auth().app_token()
        tree = _TREES.get(self.market.id, "0")
        d = await self._json("GET", f"{self.api}/commerce/taxonomy/v1/category_tree/{tree}/get_item_aspects_for_category",
                             token=app, params={"category_id": category_id})
        def norm(k: str) -> str:   # eBay UK/AU say "Colour"; the AI says "color"
            return k.lower().replace("colour", "color").strip()
        attrs = {norm(k): str(v) for k, v in (p.attributes or {}).items()}
        out, missing = {}, []
        for a in d.get("aspects") or []:
            name = str(a.get("localizedAspectName") or "")
            c = a.get("aspectConstraint") or {}
            required = bool(c.get("aspectRequired"))
            free = c.get("aspectMode", "FREE_TEXT") == "FREE_TEXT"
            values = [str(v.get("localizedValue")) for v in a.get("aspectValues") or [] if v.get("localizedValue")]

            def pick(value: str | None) -> str | None:
                if not value:
                    return None
                if not values:
                    return value if free else None
                for v in values:
                    if v.lower() == value.lower():
                        return v
                for v in values:
                    if v.lower() in value.lower() or value.lower() in v.lower():
                        return v
                return value if free else None

            key = norm(name)
            guess = None
            if key == "brand":
                guess = pick(p.brand) or pick("Unbranded")
            elif key in ("mpn", "manufacturer part number"):
                guess = pick(attrs.get("model")) or pick("Does Not Apply")
            else:
                for k, v in attrs.items():
                    if k == key or k in key or key in k:
                        guess = pick(v)
                        if guess:
                            break
                if not guess and key == "type":
                    guess = pick(p.category_name)
            if guess:
                out[name] = [guess[:65]]
            elif required:
                missing.append(name)
        return out, missing

    async def _photos(self, p: ProductPayload) -> list[str]:
        urls = []
        for f in p.image_files[:MAX_IMAGES]:
            r = await self._req("POST", f"{self.apim}/commerce/media/v1_beta/image/create_image_from_file",
                                files={"image": (f.filename, f.data, f.content_type)})
            location = r.headers.get("location", "")
            data = r.json() if r.content else {}
            url = data.get("imageUrl")
            if not url and location:
                url = (await self._json("GET", location)).get("imageUrl")
            if url:
                urls.append(url)
        if not urls:
            raise ConnectorError("eBay needs at least one photo — add a photo to the product.")
        return urls

    # ---------------------------------------------------------------- publish

    async def _publish(self, external_id: str, p: ProductPayload) -> PublishResult:
        if (p.currency or "").upper() != self.market.currency:
            raise ConnectorError(f"{self.market.name} sells in {self.market.currency}, but this product is priced in "
                                 f"{p.currency}. Use an eBay site in your currency, or change your market in Settings "
                                 "for new products.")
        parts = (external_id or "").split("|")
        sku = parts[0] if parts and parts[0] else (
            (p.sku or "").strip() or "LA-" + re.sub(r"[^A-Z0-9]+", "-", p.title.upper())[:30].strip("-") + "-" + str(int(time.time()))[-6:])
        offer_id = parts[1] if len(parts) > 1 else ""

        location = await self._location()
        policies = await self._policies()
        category_id, category_name = await self._category(p)
        aspects, missing = await self._aspects(category_id, p)
        if missing:
            raise ConnectorError(f"eBay needs more item details for “{category_name}”: {', '.join(missing[:8])}. "
                                 "Add them to the product notes, write the listing again, and publish.")
        images = await self._photos(p)

        desc = "".join(f"<p>{html.escape(x)}</p>" for x in p.description.split("\n") if x.strip())
        if p.highlights:
            desc += "<ul>" + "".join(f"<li>{html.escape(h)}</li>" for h in p.highlights[:8]) + "</ul>"
        quantity = int(p.stock) if p.stock is not None else 1
        price = float(p.price) * (1 - (p.discount_pct or 0) / 100)
        item = {"product": {"title": p.title[:80], "description": desc, "aspects": aspects, "imageUrls": images},
                "condition": "NEW", "availability": {"shipToLocationAvailability": {"quantity": quantity}}}
        if p.weight_kg and p.length_cm and p.width_cm and p.height_cm:
            item["packageWeightAndSize"] = {
                "weight": {"value": float(p.weight_kg), "unit": "KILOGRAM"},
                "dimensions": {"length": float(p.length_cm), "width": float(p.width_cm),
                               "height": float(p.height_cm), "unit": "CENTIMETER"}}
        await self._req("PUT", f"{self.api}/sell/inventory/v1/inventory_item/{sku}", json=item)

        offer = {"sku": sku, "marketplaceId": self.market.id, "format": "FIXED_PRICE", "availableQuantity": quantity,
                 "categoryId": category_id, "listingDescription": desc, "listingPolicies": policies,
                 "pricingSummary": {"price": {"value": f"{price:.2f}", "currency": self.market.currency}},
                 "merchantLocationKey": location}
        if not offer_id:
            existing = await self._json("GET", f"{self.api}/sell/inventory/v1/offer",
                                        params={"sku": sku, "marketplace_id": self.market.id}, ok=(200, 404))
            offers = existing.get("offers") or []
            offer_id = str(offers[0]["offerId"]) if offers else ""
        if offer_id:
            await self._req("PUT", f"{self.api}/sell/inventory/v1/offer/{offer_id}", json=offer)
        else:
            offer_id = str((await self._json("POST", f"{self.api}/sell/inventory/v1/offer", json=offer))["offerId"])

        listing_id = parts[2] if len(parts) > 2 else ""
        if p.publish_live:
            listing_id = str((await self._json("POST", f"{self.api}/sell/inventory/v1/offer/{offer_id}/publish"))
                             .get("listingId") or listing_id)
        elif listing_id:
            await self._req("POST", f"{self.api}/sell/inventory/v1/offer/{offer_id}/withdraw")
            listing_id = ""
        return PublishResult(external_id=f"{sku}|{offer_id}|{listing_id}",
                             external_url=f"{self.market.site}/itm/{listing_id}" if listing_id else None)
