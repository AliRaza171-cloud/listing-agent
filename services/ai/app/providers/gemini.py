"""Google Gemini provider — the real AI behind Listing Agent.

One Gemini call per step (fits the free tier's limits):
    analyze()        photos + seller notes + store categories -> ProductFacts
    research()       Google Search grounding when the key allows it, else model knowledge
    write_listing()  one call per language/platform, EN or natural Urdu
    parse_command()  "is ka price 2500 rakho, 10% off" -> CommandResult
"""
from __future__ import annotations

import logging
import re

from lagent_common.correlation import outgoing_headers
from lagent_common.gemini import GeminiClient, GeminiError, Part

from app.providers.base import (CommandResult, ListingDraft, ProductFacts, Research, ResearchSource, confirmation,
                                match_stores)
from app.providers.images import load_photos

log = logging.getLogger("lagent.ai.gemini")

S, N, I, B = {"type": "STRING"}, {"type": "NUMBER"}, {"type": "INTEGER"}, {"type": "BOOLEAN"}
S_NULL = {"type": "STRING", "nullable": True}
LIST_S = {"type": "ARRAY", "items": S}
PAIRS = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"name": S, "value": S},
                                    "required": ["name", "value"]}}

SYSTEM = (
    "You are the listing writer for small online sellers in Pakistan. You write accurate, "
    "persuasive product listings. Never invent specifications: state only what is visible in the "
    "photos, written in the seller's notes, or given as research facts. Prices are in Pakistani "
    "rupees (Rs.). Never put the price, discount or stock in listing text — the store shows those."
)

PLATFORM_HINTS = {
    None: "a general online store",
    "custom": "a custom online store (plain text, no HTML)",
    "shopify": "Shopify (SEO title max 70 characters, meta description max 160)",
    "woocommerce": "WooCommerce (short, scannable highlights work best)",
}


def _pairs_to_dict(pairs) -> dict[str, str]:
    out = {}
    for p in pairs or []:
        if isinstance(p, dict) and p.get("name") and p.get("value") not in (None, ""):
            out[str(p["name"]).strip()] = str(p["value"]).strip()
    return out


def _match_category(raw, categories) -> tuple[str | None, bool]:
    """-> (category, is_new). An existing store category is returned with its exact spelling;
    anything else is a new category the store will create (tidied, max 40 chars)."""
    name = re.sub(r"\s+", " ", str(raw or "")).strip(" .-")[:40]
    if not name:
        return None, False
    def key(x: str) -> str:
        return re.sub(r"[^a-z0-9]", "", x.lower().replace("&", "and"))
    for c in categories or []:
        if key(c) == key(name):
            return c, False
    return name, bool(categories)


def _clean_list(items, limit: int, max_len: int = 200) -> list[str]:
    seen, out = set(), []
    for x in items or []:
        s = str(x).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s[:max_len])
    return out[:limit]


class GeminiListingAI:
    def __init__(self, api_key: str, model: str, *, catalog_url: str, internal_token: str,
                 use_search: bool = True, client: GeminiClient | None = None, fallback_model: str | None = None):
        self.client = client or GeminiClient(api_key, model, fallback_model=fallback_model)
        self.catalog_url = catalog_url
        self.internal_token = internal_token
        self.use_search = use_search

    # ---------------------------------------------------------------- 1. what is it?

    async def analyze(self, image_urls, seller_notes, categories) -> ProductFacts:
        photos = await load_photos(image_urls, self.catalog_url, outgoing_headers(self.internal_token))
        if not photos and not (seller_notes or "").strip():
            raise GeminiError("No usable photos or notes to work from.")
        category_prop = {"type": "STRING", "nullable": True}
        schema = {
            "type": "OBJECT",
            "properties": {
                "product_type": S, "brand": S_NULL, "model": S_NULL,
                "attributes": PAIRS, "features": LIST_S,
                "suggested_category": category_prop,
                "questions_for_seller": LIST_S,
            },
            "required": ["product_type", "attributes", "features", "questions_for_seller", "suggested_category"],
        }
        prompt = [
            f"Identify this product from {len(photos)} photo(s)" + (" and the seller's notes." if seller_notes else "."),
            "- product_type: short common name buyers search for (e.g. 'oil strainer pot').",
            "- brand / model: ONLY if printed on the product or in the notes; otherwise null.",
            "- attributes: visible or stated facts (colour, material, size, capacity, pieces...). "
            "Mark guesses with '?', e.g. 'stainless steel?'.",
            "- features: what the product does or has, as short phrases.",
            "- questions_for_seller: up to 4 short questions about details that matter to buyers of this "
            "kind of product but can't be known from the photos or notes (skip anything already answered).",
        ]
        if categories:
            prompt.append(
                "- suggested_category: the store's categories are listed below. Use one ONLY if this product "
                "clearly belongs in it (a hair dryer does not belong in 'Kitchen & Dining'). If none fits, write a "
                "NEW short category name a shop would use for this product (Title Case, 1-3 words, e.g. "
                "'Personal Care', 'Smoking Accessories'), not a product name.\n"
                "Store categories: " + "; ".join(categories))
        else:
            prompt.append("- suggested_category: a short shop category for this product (Title Case, 1-3 words).")
        if seller_notes:
            prompt.append(f"\nSeller's notes:\n{seller_notes.strip()}")
        parts = [Part.text("\n".join(prompt))] + [Part.blob(p, "image/jpeg") for p in photos]
        reply = await self.client.generate(parts, system=SYSTEM, schema=schema, temperature=0.2)
        d = reply.json or {}
        suggested, is_new = _match_category(d.get("suggested_category"), categories)
        return ProductFacts(
            product_type=str(d.get("product_type") or "product").strip(),
            brand=(d.get("brand") or None),
            model=(d.get("model") or None),
            attributes=_pairs_to_dict(d.get("attributes")),
            features=_clean_list(d.get("features"), 10),
            suggested_category=suggested,
            category_is_new=is_new,
            questions_for_seller=_clean_list(d.get("questions_for_seller"), 4),
        )

    # ---------------------------------------------------------------- 2. research

    async def research(self, facts: ProductFacts) -> Research:
        branded = bool(facts.brand)
        schema = {
            "type": "OBJECT",
            "properties": {
                "facts": PAIRS, "buyer_priorities": LIST_S, "keywords": LIST_S,
                "price_min_pkr": {"type": "NUMBER", "nullable": True},
                "price_max_pkr": {"type": "NUMBER", "nullable": True},
            },
            "required": ["facts", "buyer_priorities", "keywords"],
        }
        what = f"{facts.brand or ''} {facts.model or ''} {facts.product_type}".strip()
        prompt = (
            f"Product: {what}\nKnown attributes: {facts.attributes}\n\n"
            + ("It is a branded product: give its official specifications as facts (only ones you are sure of). "
               if branded else "It is a generic product: leave facts empty. ")
            + "buyer_priorities: what shoppers in Pakistan care about most when buying this kind of product. "
            "keywords: 5-10 phrases people type when searching for it (English, plus Roman Urdu if common). "
            "price_min_pkr / price_max_pkr: typical selling price range in Pakistan in rupees, or null if unsure."
        )
        mode = "specs" if branded else "category"
        reply = None
        if self.use_search:
            try:
                reply = await self.client.generate([Part.text(prompt)], system=SYSTEM, schema=schema,
                                                   search=True, temperature=0.2)
            except GeminiError as exc:
                # Search grounding isn't on the free tier (and some models don't allow search+JSON).
                if exc.status in (400, 403, 429) or not exc.retryable:
                    log.info("research without web search (%s)", exc)
                else:
                    raise
        if reply is None:
            reply = await self.client.generate([Part.text(prompt)], system=SYSTEM, schema=schema, temperature=0.2)
        d = reply.json or {}
        lo, hi = d.get("price_min_pkr"), d.get("price_max_pkr")
        price_range = (float(lo), float(hi)) if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) \
            and 0 < lo <= hi else None
        return Research(
            mode=mode,
            facts=_pairs_to_dict(d.get("facts")) if branded else {},
            buyer_priorities=_clean_list(d.get("buyer_priorities"), 6),
            keywords=_clean_list(d.get("keywords"), 10, 60),
            price_range=price_range,
            sources=[ResearchSource(title=t[:150], url=u) for t, u in reply.sources[:5]],
        )

    # ---------------------------------------------------------------- 3. write

    async def write_listing(self, facts, research, seller_notes, language, platform, instruction=None) -> ListingDraft:
        schema = {
            "type": "OBJECT",
            "properties": {
                "title": S, "highlights": LIST_S, "description": S,
                "seo_title": S, "meta_description": S, "tags": LIST_S,
            },
            "required": ["title", "highlights", "description", "seo_title", "meta_description", "tags"],
        }
        if language == "ur":
            lang_rules = (
                "Write in natural Urdu (Urdu script) as Pakistani online shops write it — not a word-for-word "
                "translation of English. Keep brand and model names in English letters, use Western digits "
                "(1.3, 20), and keep units like 'L', 'ml', 'cm'. tags: mix of Urdu and common Roman Urdu / English "
                "search words."
            )
        else:
            lang_rules = "Write in clear, simple English that Pakistani shoppers read easily."
        lines = [
            f"Write a product listing for {PLATFORM_HINTS.get(platform, PLATFORM_HINTS[None])}.",
            lang_rules,
            "- title: max 70 characters; product type + the 2-3 most important facts (brand, size, material).",
            "- highlights: 3-5 short benefit-led bullet points, no emojis.",
            "- description: 2 short paragraphs of plain text (no HTML, no markdown), about 60-120 words.",
            "- seo_title: max 60 characters. meta_description: max 155 characters.",
            "- tags: 5-10 search phrases.",
            "Only use facts given below. Don't mention anything marked with '?' as certain.",
            f"\nFacts: type={facts.product_type}; brand={facts.brand}; model={facts.model}; "
            f"attributes={facts.attributes}; features={facts.features}",
        ]
        if research and research.mode != "skipped":
            lines.append(f"Research: specs={research.facts}; buyers care about={research.buyer_priorities}; "
                         f"search keywords={research.keywords}")
        if seller_notes:
            notes = re.sub(r"(?i)\b(price|rs\.?|pkr|discount|stock)\b[^\n.,]*", "", seller_notes).strip()
            if notes:
                lines.append(f"Seller's notes (the seller's word is final): {notes}")
        if instruction:
            lines.append(f"Seller's change request: {instruction}")
        reply = await self.client.generate([Part.text("\n".join(lines))], system=SYSTEM, schema=schema,
                                           temperature=0.7)
        d = reply.json or {}
        title = str(d.get("title") or "").strip()
        description = str(d.get("description") or "").strip()
        if not title or not description:
            raise GeminiError("The AI returned an incomplete listing.")
        return ListingDraft(
            title=title[:200],
            highlights=_clean_list(d.get("highlights"), 6),
            description=description,
            seo_title=(str(d.get("seo_title") or "").strip()[:200] or None),
            meta_description=(str(d.get("meta_description") or "").strip()[:400] or None),
            tags=_clean_list(d.get("tags"), 10, 60),
            category_suggestion=facts.suggested_category,
        )

    # ---------------------------------------------------------------- 4. voice / typed commands

    async def parse_command(self, text: str, stores: list[dict] | None = None) -> CommandResult:
        stores = stores or []
        schema = {
            "type": "OBJECT",
            "properties": {
                "price": {"type": "NUMBER", "nullable": True},
                "discount_pct": {"type": "INTEGER", "nullable": True},
                "remove_discount": B,
                "stock": {"type": "INTEGER", "nullable": True},
                "sku": S_NULL,
                "free_shipping": {"type": "BOOLEAN", "nullable": True},
                "edit_instruction": S_NULL,
                "publish": B,
                "publish_to": {"type": "ARRAY", "items": {"type": "STRING"}},
                "publish_mode": {"type": "STRING", "enum": ["live", "draft"], "nullable": True},
                "publish_language": {"type": "STRING", "enum": ["en", "ur"], "nullable": True},
            },
            "required": ["remove_discount", "publish", "publish_to"],
        }
        store_list = "; ".join(f"{st['name']} ({st['platform']})" for st in stores) or "none connected"
        prompt = (
            "A seller gave this instruction about ONE product, in English, Urdu or Roman Urdu "
            "(e.g. 'is ka price 2500 rakho', 'das percent discount', 'stock bees', 'free delivery'). "
            "Extract only what they actually said; everything else null. Numbers may be words in any "
            "of these languages (bees=20, das=10, pachees sau=2500, dhai hazar=2500). "
            "price is in rupees. remove_discount=true only if they ask to remove the discount. "
            "edit_instruction: a request to change the listing text (e.g. 'title chota karo'), in English.\n"
            "publish=true only if they ask to publish/upload/send/put the product on their store(s) "
            "(e.g. 'Shopify pe publish karo', 'sab stores pe daal do', 'upload to woo and smart click'). "
            "publish_to: the stores they named, copied from this list by name, or the platform word they used "
            "(shopify / woocommerce / daraz / custom), or [\"all\"] for all/sab/everywhere/dono; [] if they didn't name any. "
            "publish_mode: 'live' if they say live/visible/active/show it, 'draft' if they say draft/hidden; else null. "
            "publish_language: 'ur' if they want the Urdu listing, 'en' for English; else null.\n"
            f"The seller's stores: {store_list}\n\n"
            f"Instruction: {text}"
        )
        reply = await self.client.generate([Part.text(prompt)], schema=schema, temperature=0)
        d = reply.json or {}

        def num(v, cast):
            try:
                return cast(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        r = CommandResult(
            price=num(d.get("price"), float),
            discount_pct=num(d.get("discount_pct"), int),
            remove_discount=bool(d.get("remove_discount")),
            stock=num(d.get("stock"), int),
            sku=(str(d["sku"]).strip() or None) if d.get("sku") else None,
            free_shipping=d.get("free_shipping") if isinstance(d.get("free_shipping"), bool) else None,
            edit_instruction=(str(d["edit_instruction"]).strip() or None) if d.get("edit_instruction") else None,
            publish=bool(d.get("publish")) and bool(stores),
            publish_mode=d.get("publish_mode") if d.get("publish_mode") in ("live", "draft") else None,
            publish_language=d.get("publish_language") if d.get("publish_language") in ("en", "ur") else None,
        )
        if r.publish:
            said = [str(x) for x in (d.get("publish_to") or []) if x]
            # Nothing named -> every connected store (same as the Publish card's default).
            r.publish_to = match_stores(said, stores) if said else [st["id"] for st in stores]
        if r.price is not None and r.price <= 0:
            r.price = None
        if r.discount_pct is not None and not 1 <= r.discount_pct <= 95:
            r.discount_pct = None
        if r.stock is not None and r.stock < 0:
            r.stock = None
        parts = []
        if r.price is not None:
            parts.append(f"price Rs. {r.price:,.0f}")
        if r.remove_discount:
            parts.append("no discount")
        elif r.discount_pct is not None:
            parts.append(f"{r.discount_pct}% off")
        if r.stock is not None:
            parts.append(f"stock {r.stock}")
        if r.free_shipping is not None:
            parts.append("free shipping" if r.free_shipping else "no free shipping")
        r.confirmation_text = confirmation(parts, r, stores)
        return r
