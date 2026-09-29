"""Daraz connector (Daraz Open Platform, Pakistan).

Credentials (saved by the store service after the seller's Daraz login, renewed there too):
access_token (+ refresh_token, expires_at, seller_id…). The app key/secret come from this
service's settings (DARAZ_APP_KEY / DARAZ_APP_SECRET), never from the seller.

Publishing one product:
1. category — Daraz has a fixed category tree (sellers can't add categories). We ask Daraz's
   own suggestion API for the title, then fall back to matching our category name/title against
   the tree's leaf categories.
2. required details — each category lists attributes; mandatory ones are filled from the
   listing (name, description, highlights), brand ("No Brand" if unknown), warranty ("No Warranty")
   and what the AI saw (color, material…). Anything we can't fill is reported to the seller.
3. photos — uploaded to Daraz's image server (/image/upload), max 8.
4. /product/create (or /product/update on re-publish) with an XML payload; the SKU carries price,
   stock, discount and the package weight/size Daraz needs for delivery.
Daraz reviews new products (QC) before they show in the shop.
"""
from __future__ import annotations

import html
import logging
import os
import re
import time
from datetime import datetime, timedelta
from xml.sax.saxutils import escape

from lagent_common import daraz
from app.connectors.base import ConnectorError, ProductPayload, PublishResult, StoreCategory

log = logging.getLogger("lagent.publisher.daraz")
MAX_IMAGES = 8
PRODUCT_URL = "https://www.daraz.pk/products/-i{item_id}.html"
_TREE: dict[str, object] = {"at": 0.0, "leaves": []}     # category tree cache (refreshed daily)

STOP = {"and", "the", "for", "with", "of", "in", "a", "an", "&", "other", "others", "accessories", "set"}


def _words(s: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9]+", str(s or "").lower()):
        if w in STOP or len(w) < 2:
            continue
        out.add(w[:-1] if len(w) > 3 and w.endswith("s") else w)   # dryers -> dryer
    return out


def _cdata(s: str) -> str:
    return "<![CDATA[" + str(s).replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _num(v) -> str:
    return f"{float(v):g}"


def _client() -> daraz.DarazClient:
    return daraz.DarazClient(os.environ.get("DARAZ_APP_KEY", ""), os.environ.get("DARAZ_APP_SECRET", ""),
                             api_url=os.environ.get("DARAZ_API_URL", daraz.API_URL),
                             auth_url=os.environ.get("DARAZ_AUTH_URL", daraz.AUTH_URL))


class DarazConnector:
    def __init__(self, store_url: str, credentials: dict):
        self.store_url = store_url
        self.token = (credentials.get("access_token") or "").strip()
        try:
            self.api = _client()
        except daraz.DarazError as exc:
            raise ConnectorError(str(exc))

    async def _call(self, path: str, params: dict | None = None, **kw) -> dict:
        try:
            return (await self.api.call(path, params, access_token=self.token, **kw)).get("data") or {}
        except daraz.DarazError as exc:
            if exc.auth:
                raise ConnectorError("Daraz says the login has expired — press Connect on Daraz again (Stores page).")
            raise ConnectorError(str(exc), retryable=exc.retryable)

    # ---------------------------------------------------------------- interface

    async def test_connection(self) -> None:
        if not self.token:
            raise ConnectorError("Daraz didn't give an access token.")
        seller = await self._call("/seller/get")
        if not (seller.get("name") or seller.get("seller_id") or seller.get("email")):
            raise ConnectorError("Daraz didn't return the shop details.")

    async def list_categories(self) -> list[StoreCategory]:
        # Daraz's tree has thousands of categories — too many to hand to the AI. The AI suggests a
        # plain category name and we map it to Daraz's tree when publishing (see _pick_category).
        return []

    async def create_product(self, p: ProductPayload) -> PublishResult:
        return await self._publish(None, p)

    async def update_product(self, external_id: str, p: ProductPayload) -> PublishResult:
        return await self._publish(external_id, p)

    # ---------------------------------------------------------------- category

    async def _leaves(self) -> list[dict]:
        if time.time() - float(_TREE["at"]) < 86400 and _TREE["leaves"]:
            return _TREE["leaves"]  # type: ignore[return-value]
        tree = await self._call("/category/tree/get", {"language_code": "en_US"})
        nodes = tree if isinstance(tree, list) else []
        leaves: list[dict] = []

        def walk(ns, path):
            for n in ns or []:
                name = str(n.get("name") or "")
                kids = n.get("children") or []
                if n.get("leaf") or not kids:
                    leaves.append({"id": str(n.get("category_id")), "name": name, "path": " > ".join(path + [name])})
                else:
                    walk(kids, path + [name])
        walk(nodes, [])
        _TREE.update(at=time.time(), leaves=leaves)
        return leaves

    async def _pick_category(self, p: ProductPayload) -> tuple[str, str]:
        """-> (category_id, path). Daraz's own suggestion first, then our best match in the tree."""
        try:
            data = await self._call("/product/category/suggestion/get", {"product_name": p.title[:255]})
            sugg = data.get("categorySuggestions") or data.get("category_suggestions") or []
            if sugg:
                s = sugg[0]
                return str(s.get("categoryId") or s.get("category_id")), str(s.get("categoryPath") or s.get("categoryName") or "")
        except ConnectorError as exc:
            if exc.retryable:
                raise
            log.info("Daraz category suggestion unavailable (%s); matching the tree", exc)
        want_name, want_title = _words(p.category_name or ""), _words(p.title)
        best, score = None, 0.0
        for leaf in await self._leaves():
            name, path = _words(leaf["name"]), _words(leaf["path"])
            sc = 3 * len(name & want_name) + 2 * len(name & want_title) + 0.5 * len(path & (want_name | want_title))
            if name and name <= (want_name | want_title):
                sc += 2          # every word of the category name was mentioned
            if sc > score:
                best, score = leaf, sc
        if not best or score < 2:
            raise ConnectorError(f"Couldn't find a matching Daraz category for “{p.category_name or p.title}”. "
                                 "Set a clearer category (e.g. “Hair Dryers”) in the listing and publish again.")
        return best["id"], best["path"]

    # ---------------------------------------------------------------- attributes

    def _value_for(self, attr: dict, p: ProductPayload) -> str | None:
        name = str(attr.get("name") or "").lower()
        label = str(attr.get("label") or name).lower()
        options = [str(o.get("name")) for o in (attr.get("options") or []) if isinstance(o, dict) and o.get("name")]

        def pick(value: str | None) -> str | None:
            if value is None or value == "":
                return None
            if not options:
                return str(value)
            v = str(value).lower()
            for o in options:
                if o.lower() == v:
                    return o
            for o in options:
                if v in o.lower() or o.lower() in v:
                    return o
            return None

        def fallback_option() -> str | None:
            for o in options:
                if o.lower() in ("no brand", "no warranty", "not specified", "none", "other", "others", "multicolor"):
                    return o
            return None

        if name == "name":
            return p.title[:255]
        if name == "description":
            body = "".join(f"<p>{html.escape(x)}</p>" for x in p.description.split("\n") if x.strip())
            return body or html.escape(p.title)
        if name == "short_description":
            return "<ul>" + "".join(f"<li>{html.escape(h)}</li>" for h in p.highlights[:8]) + "</ul>" if p.highlights else None
        if name == "brand":
            # Daraz only accepts brands from its own brand list; when we can't match one, "No Brand"
            # is the accepted choice (the seller can change it in Seller Center).
            return (pick(p.brand) if options else None) or fallback_option() or "No Brand"
        if name == "warranty_type":
            return pick("No Warranty") or fallback_option() or "No Warranty"
        if name in ("model", "model_no") and p.attributes.get("model"):
            return str(p.attributes["model"])
        # What the AI saw: match the attribute by name or label ("color_family" <- "color")
        for k, v in (p.attributes or {}).items():
            k2 = k.lower().replace(" ", "_")
            if k2 == name or k2 in name or k.lower() in label:
                got = pick(v)
                if got:
                    return got
        return None

    async def _attributes(self, category_id: str) -> list[dict]:
        data = await self._call("/category/attributes/get", {"primary_category_id": category_id, "language_code": "en_US"})
        return data if isinstance(data, list) else []

    # ---------------------------------------------------------------- photos

    async def _upload_images(self, p: ProductPayload) -> list[str]:
        urls = []
        for f in p.image_files[:MAX_IMAGES]:
            data = await self._call("/image/upload", method="POST", files={"image": (f.filename, f.data, f.content_type)})
            url = (data.get("image") or {}).get("url")
            if url:
                urls.append(url)
        if not urls:
            raise ConnectorError("Daraz needs at least one photo — add a photo to the product.")
        return urls

    # ---------------------------------------------------------------- publish

    async def _publish(self, external_id: str | None, p: ProductPayload) -> PublishResult:
        if (p.currency or "PKR").upper() != "PKR":
            raise ConnectorError(f"Daraz sells in Pakistani rupees, but this product is priced in {p.currency}. "
                                 "Products for Daraz need a Pakistan (PKR) price — see Settings.")
        missing_pkg = [n for n, v in (("weight", p.weight_kg), ("length", p.length_cm), ("width", p.width_cm),
                                      ("height", p.height_cm)) if not v]
        if missing_pkg:
            raise ConnectorError("Daraz needs the package weight and size for delivery — fill them in under "
                                 "“Your details” (weight in kg, size in cm) and publish again.")
        item_id, seller_sku = (external_id.split(":", 1) + [""])[:2] if external_id else ("", "")
        seller_sku = seller_sku or (p.sku or "").strip() or "LA-" + re.sub(r"[^A-Z0-9]+", "-", p.title.upper())[:30].strip("-") \
            + "-" + str(int(time.time()))[-6:]

        category_id, path = await self._pick_category(p)
        attrs = await self._attributes(category_id)
        normal, sku_vals, missing = {}, {}, []
        for a in attrs:
            name = str(a.get("name") or "")
            if not name or name in ("SellerSku", "quantity", "price", "special_price", "special_from_date",
                                    "special_to_date", "package_weight", "package_length", "package_width",
                                    "package_height", "__images__", "color_thumbnail") or a.get("input_type") == "img":
                continue
            value = self._value_for(a, p)
            if value is None:
                if str(a.get("is_mandatory")) in ("1", "true", "True"):
                    missing.append(str(a.get("label") or name))
                continue
            (sku_vals if a.get("attribute_type") == "sku" else normal)[name] = value
        normal.setdefault("name", p.title[:255])
        normal.setdefault("brand", "No Brand")
        if missing:
            raise ConnectorError(f"Daraz needs more details for the category “{path}”: {', '.join(missing[:8])}. "
                                 "Add them to the product notes (e.g. “color black, material plastic”), "
                                 "write the listing again, and publish.")

        images = await self._upload_images(p)
        price = float(p.price)
        sku = {"SellerSku": seller_sku, "quantity": str(int(p.stock or 0)), "price": _num(price),
               "package_weight": _num(p.weight_kg), "package_length": _num(p.length_cm),
               "package_width": _num(p.width_cm), "package_height": _num(p.height_cm),
               "Status": "active" if p.publish_live else "inactive", **sku_vals}
        if p.discount_pct:
            today = datetime.utcnow()
            sku.update(special_price=_num(round(price * (1 - p.discount_pct / 100))),
                       special_from_date=today.strftime("%Y-%m-%d"),
                       special_to_date=(today + timedelta(days=365)).strftime("%Y-%m-%d"))

        def field_xml(k: str, v: str) -> str:
            rich = k in ("description", "short_description", "description_ms", "short_description_en")
            return f"<{k}>{_cdata(v) if rich else escape(str(v))}</{k}>"

        xml = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?><Request><Product>"
               + (f"<ItemId>{escape(item_id)}</ItemId>" if item_id else "")
               + f"<PrimaryCategory>{escape(category_id)}</PrimaryCategory>"
               + "<Images>" + "".join(f"<Image>{escape(u)}</Image>" for u in images) + "</Images>"
               + "<Attributes>" + "".join(field_xml(k, v) for k, v in normal.items()) + "</Attributes>"
               + "<Skus><Sku>" + "".join(field_xml(k, v) for k, v in sku.items())
               + "<Images>" + "".join(f"<Image>{escape(u)}</Image>" for u in images) + "</Images>"
               + "</Sku></Skus></Product></Request>")

        path_api = "/product/update" if item_id else "/product/create"
        data = await self._call(path_api, {"payload": xml}, method="POST")
        item_id = str(data.get("item_id") or item_id or "")
        if not item_id:
            raise ConnectorError("Daraz accepted the product but didn't return its ID.")
        return PublishResult(external_id=f"{item_id}:{seller_sku}", external_url=PRODUCT_URL.format(item_id=item_id))
