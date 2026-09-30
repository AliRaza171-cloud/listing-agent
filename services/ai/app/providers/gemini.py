"""Google Gemini provider — the real AI behind Listing Agent.

One Gemini call per step (fits the free tier's limits):
    analyze()        photos + seller notes + store categories -> ProductFacts
    research()       Google Search grounding when the key allows it, else model knowledge
    write_listing()  one call per language/platform, EN or natural Urdu
    parse_command()  "is ka price 2500 rakho, 10% off" -> CommandResult
    group_photos()   bulk upload: which photos show the same product (one call for all photos)
    split_notes()    bulk upload: one voice note about many products -> notes per product
"""
from __future__ import annotations

import logging
import re

from lagent_common.correlation import outgoing_headers
from lagent_common.gemini import GeminiClient, GeminiError, Part

from app.providers.base import (CommandResult, ListingDraft, NotesSplit, PhotoGroup, ProductFacts, Research,
                                ResearchSource, confirmation, match_stores, normalize_groups, normalize_notes)
from app.providers.images import load_photos, load_thumbs
from lagent_common.markets import Market

log = logging.getLogger("lagent.ai.gemini")

S, N, I, B = {"type": "STRING"}, {"type": "NUMBER"}, {"type": "INTEGER"}, {"type": "BOOLEAN"}
S_NULL = {"type": "STRING", "nullable": True}
LIST_S = {"type": "ARRAY", "items": S}
PAIRS = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"name": S, "value": S},
                                    "required": ["name", "value"]}}

def system_for(m: Market) -> str:
    return (
        f"You are the listing writer for small online sellers in {m.country_name}. You write accurate, "
        "persuasive product listings. Never invent specifications: state only what is visible in the "
        "photos, written in the seller's notes, or given as research facts. Prices are in "
        f"{m.currency} ({m.symbol}). Never put the price, discount or stock in listing text — the store shows those."
    )


SYSTEM = system_for(Market())   # Pakistan (the default market)

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
            # values are facts, not commentary: drop bracketed asides like "(seller notes: ...)"
            value = re.sub(r"\s*\([^)]*\)", "", str(p["value"])).strip(" ,;")
            if value:
                out[str(p["name"]).strip()] = value[:80]
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

    async def analyze(self, image_urls, seller_notes, categories, market: Market | None = None) -> ProductFacts:
        m = market or Market()
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
                "in_the_box": LIST_S, "use_cases": LIST_S,
            },
            "required": ["product_type", "attributes", "features", "questions_for_seller", "suggested_category",
                         "in_the_box", "use_cases"],
        }
        prompt = [
            f"Identify this product from {len(photos)} photo(s)" + (" and the seller's notes." if seller_notes else "."),
            "Photos may be plain product shots or marketing images with text and icons: text printed on the product, "
            "its box or the image (model names, capacities, 'USB', 'waterproof', battery life) counts as visible.",
            "- product_type: the short common name buyers search for (e.g. 'hair trimmer', 'oil strainer pot').",
            "- brand / model: ONLY if printed on the product/box/image or in the notes; otherwise null.",
            "- attributes: key facts as name/value pairs (colour, material, size, capacity, power, pieces, "
            "compatibility...). Values are short (1-5 words), no explanations or brackets. If several colours are "
            "shown, list them ('gold, silver, black'). Mark a guess with '?' (e.g. 'stainless steel?').",
            "- features: what it does or has, 3-8 short phrases (e.g. 'USB rechargeable', 'T-blade cutter').",
            "- in_the_box: items that visibly come with it (e.g. '4 guide combs', 'charging cable'); [] if unknown.",
            "- use_cases: who it's for or when it's used, 1-3 short phrases (e.g. 'beard and hair lines').",
            "- questions_for_seller: up to 4 short questions about details that matter to buyers of this "
            "kind of product but can't be known from the photos or notes (skip anything already answered).",
            "The seller's notes can include private remarks for you (quality, cost, supplier, condition). Use them "
            "to stay accurate, but never turn a negative remark into an attribute or feature.",
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
        reply = await self.client.generate(parts, system=system_for(m), schema=schema, temperature=0.2)
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
            in_the_box=_clean_list(d.get("in_the_box"), 10, 60),
            use_cases=_clean_list(d.get("use_cases"), 3, 80),
        )

    # ---------------------------------------------------------------- 2. research

    async def research(self, facts: ProductFacts, market: Market | None = None) -> Research:
        m = market or Market()
        branded = bool(facts.brand)
        schema = {
            "type": "OBJECT",
            "properties": {
                "facts": PAIRS, "buyer_priorities": LIST_S, "keywords": LIST_S,
                "price_min": {"type": "NUMBER", "nullable": True},
                "price_max": {"type": "NUMBER", "nullable": True},
            },
            "required": ["facts", "buyer_priorities", "keywords"],
        }
        what = f"{facts.brand or ''} {facts.model or ''} {facts.product_type}".strip()
        prompt = (
            f"Product: {what}\nKnown attributes: {facts.attributes}\n\n"
            + ("It is a branded product: give its official specifications as facts (only ones you are sure of). "
               if branded else "It is a generic product: leave facts empty. ")
            + f"buyer_priorities: what shoppers in {m.country_name} care about most when buying this kind of product. "
            + ("keywords: 5-10 phrases people type when searching for it (English, plus Roman Urdu if common). "
               if m.country == "PK" else
               f"keywords: 5-10 phrases shoppers in {m.country_name} type when searching for it. ")
            + f"price_min / price_max: typical selling price range in {m.country_name} in {m.currency}, or null if unsure."
        )
        mode = "specs" if branded else "category"
        reply = None
        if self.use_search:
            try:
                reply = await self.client.generate([Part.text(prompt)], system=system_for(m), schema=schema,
                                                   search=True, temperature=0.2)
            except GeminiError as exc:
                # Search grounding isn't on the free tier (and some models don't allow search+JSON).
                if exc.status in (400, 403, 429) or not exc.retryable:
                    log.info("research without web search (%s)", exc)
                else:
                    raise
        if reply is None:
            reply = await self.client.generate([Part.text(prompt)], system=system_for(m), schema=schema, temperature=0.2)
        d = reply.json or {}
        lo, hi = d.get("price_min"), d.get("price_max")
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

    async def write_listing(self, facts, research, seller_notes, language, platform, instruction=None,
                            market: Market | None = None) -> ListingDraft:
        m = market or Market()
        schema = {
            "type": "OBJECT",
            "properties": {
                "title": S, "highlights": LIST_S, "description": S,
                "seo_title": S, "meta_description": S, "tags": LIST_S,
            },
            "required": ["title", "highlights", "description", "seo_title", "meta_description", "tags"],
        }
        prompt = listing_brief(facts, research, seller_notes, language, platform, instruction, m)
        draft = None
        for attempt in range(2):
            reply = await self.client.generate([Part.text(prompt)], system=system_for(m), schema=schema, temperature=0.5)
            draft = tidy_listing(reply.json or {}, facts)
            problems = listing_problems(draft, language)
            if not problems:
                break
            log.info("listing needs another pass: %s", problems)
            prompt = listing_brief(facts, research, seller_notes, language, platform, instruction, m,
                                   fix="Your last draft had these problems — fix them: " + "; ".join(problems))
        if not draft.title or not draft.description:
            raise GeminiError("The AI returned an incomplete listing.")
        return draft

    # ---------------------------------------------------------------- 4. voice / typed commands

    async def parse_command(self, text: str, stores: list[dict] | None = None,
                            market: Market | None = None) -> CommandResult:
        stores = stores or []
        m = market or Market()
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
            f"price is in {m.currency}. remove_discount=true only if they ask to remove the discount. "
            "edit_instruction: a request to change the listing text (e.g. 'title chota karo'), in English.\n"
            "publish=true only if they ask to publish/upload/send/put the product on their store(s) "
            "(e.g. 'Shopify pe publish karo', 'sab stores pe daal do', 'upload to woo and smart click'). "
            "publish_to: the stores they named, copied from this list by name, or the platform word they used "
            "(shopify / woocommerce / daraz / ebay / custom), or [\"all\"] for all/sab/everywhere/dono; [] if they didn't name any. "
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
            parts.append(f"price {m.money(r.price)}")
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

    # ---------------------------------------------------------------- 5. bulk upload helpers

    async def group_photos(self, image_urls: list[str]) -> list[PhotoGroup]:
        thumbs = await load_thumbs(image_urls, self.catalog_url, outgoing_headers(self.internal_token))
        usable = [t is not None for t in thumbs]
        if sum(usable) < 2:
            return normalize_groups([], len(thumbs), usable)
        schema = {
            "type": "OBJECT",
            "properties": {"groups": {"type": "ARRAY", "items": {
                "type": "OBJECT", "properties": {"photos": {"type": "ARRAY", "items": I}, "label": S},
                "required": ["photos", "label"]}}},
            "required": ["groups"],
        }
        prompt = (
            f"A seller uploaded {sum(usable)} photos for a bulk listing, numbered below. Put photos of the SAME "
            "product in one group, so each group becomes one product listing: the same item from other angles, "
            "close-ups of it, its label, box or packaging, or it being used/worn.\n"
            "Rules:\n"
            "- Different products go in different groups, even if they are the same kind of thing "
            "(two different bottles, two shirt designs). A different colour or print of an item is a different "
            "product unless the photo shows them together as one set.\n"
            "- A photo showing several items sold together (a set, a bundle) is one product.\n"
            "- When unsure, keep photos apart — the seller can combine them later.\n"
            "- Every photo number appears in exactly one group (a group may have one photo).\n"
            "- label: a short product name buyers would use, 2-5 words (e.g. 'steel water bottle', "
            "'black leather wallet'). Include the brand only if it is printed on the product."
        )
        parts = [Part.text(prompt)]
        for i, t in enumerate(thumbs):
            if t is not None:
                parts += [Part.text(f"Photo {i + 1}:"), Part.blob(t, "image/jpeg")]
        reply = await self.client.generate(parts, schema=schema, temperature=0)
        raw = [(g.get("photos"), g.get("label")) for g in (reply.json or {}).get("groups") or []
               if isinstance(g, dict)]
        return normalize_groups(raw, len(thumbs), usable)

    async def split_notes(self, text: str, products: list[dict], market: Market | None = None) -> NotesSplit:
        m = market or Market()
        count = len(products)
        covers = await load_thumbs([str(p.get("image_url") or "") for p in products], self.catalog_url,
                                   outgoing_headers(self.internal_token))
        schema = {
            "type": "OBJECT",
            "properties": {
                "items": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                    "product": I, "notes": S,
                    "price": {"type": "NUMBER", "nullable": True},
                    "discount_pct": {"type": "INTEGER", "nullable": True},
                    "stock": {"type": "INTEGER", "nullable": True},
                }, "required": ["product", "notes"]}},
                "unmatched": S,
            },
            "required": ["items", "unmatched"],
        }
        prompt = (
            f"A seller described several of the {count} products below in one message (English, Urdu or Roman "
            "Urdu, maybe a transcribed voice note). Work out which product each part is about and split it up.\n"
            "- A product may be named ('the bottle', 'wallet wala'), described by look ('the red one', 'kaala "
            "wala', 'bara wala') or by position ('first', 'pehla', 'number 3', 'second last', 'aakhri'). Use the "
            "photos and names to match.\n"
            "- If something is said about all of them ('sab steel ke hain', 'all are imported', 'har aik ka "
            "stock 10'), apply it to every product.\n"
            "- notes: the facts said about that product (brand, size, material, colour, condition, what's in the "
            "box...) as short, clear English notes. Keep names, sizes and numbers exactly as said. Never add "
            "anything that wasn't said. Leave price, discount and stock out of notes.\n"
            f"- price (in {m.currency}), discount_pct and stock: only when said for that product, else null. "
            "Numbers may be words (bees=20, das=10, pachees sau=2500, dhai hazar=2500, 2.5k=2500).\n"
            "- Only include products something was said about.\n"
            "- unmatched: anything that can't be tied to a product, in English; '' if nothing.\n\n"
            f"Seller's message:\n{text.strip()}\n\nProducts:"
        )
        parts = [Part.text(prompt)]
        for i, (p, cover) in enumerate(zip(products, covers)):
            label = str(p.get("label") or "").strip()[:60]
            parts.append(Part.text(f"Product {i + 1}" + (f": {label}" if label else "")))
            if cover is not None:
                parts.append(Part.blob(cover, "image/jpeg"))
        reply = await self.client.generate(parts, system=system_for(m), schema=schema, temperature=0)
        d = reply.json or {}
        return NotesSplit(products=normalize_notes(d.get("items") or [], count),
                          unmatched=str(d.get("unmatched") or "").strip()[:500])


# ---------------------------------------------------------------- listing writing: brief + checks

# Words that promise something the photos can't prove; only allowed when the facts/notes say so.
HYPE = ("premium", "high quality", "high-quality", "top quality", "best quality", "luxury", "professional grade",
        "professional-grade", "100%", "guaranteed", "best", "cheapest", "no.1", "number one", "world-class")
PROMO = re.compile(r"(?i)\b(sale|discount|free shipping|free delivery|cash on delivery|cod|order now|buy now|"
                   r"limited offer|hot deal)\b")


def listing_brief(facts, research, seller_notes, language, platform, instruction, m: Market, fix: str = "") -> str:
    known = " ".join([str(facts.attributes), " ".join(facts.features), seller_notes or ""]).lower()
    allowed_hype = [w for w in HYPE if w in known]
    if language == "ur":
        lang_rules = (
            "LANGUAGE: natural Urdu in Urdu script, the way Pakistani online shops write — not a word-for-word "
            "translation of English. Keep brand/model names in English letters, Western digits (1.3, 20) and units "
            "('L', 'ml', 'cm', 'mAh'). tags: a mix of Urdu and the Roman Urdu / English words people type.")
    else:
        lang_rules = f"LANGUAGE: clear, simple English that shoppers in {m.country_name} read easily" + (
            "." if m.country in ("PK", "IN", "BD", "LK") else f", with {m.country_name}'s spelling and units.")
    lines = [
        f"Write a product listing for {PLATFORM_HINTS.get(platform, PLATFORM_HINTS[None])} that ranks in search "
        "and makes a shopper want to buy — using ONLY the facts below.",
        lang_rules,
        "",
        "TITLE (50-70 characters): [Brand] [Model] [product type buyers search for] – [2-3 most useful facts: "
        "size / capacity / material / power / pieces]. The product type comes in the first 4 words. Title Case, "
        "no ALL CAPS (except brand or model codes), no '!', no emojis, no price, no words like sale/free/best.",
        "HIGHLIGHTS: 4-5 bullets, each 35-80 characters, format 'Benefit – the fact behind it' "
        "(e.g. 'Cordless freedom – USB rechargeable, runs about 2 hours'). Each covers a different point. "
        "No full stop at the end, no emojis.",
        "DESCRIPTION: plain text, 120-220 words, with blank lines between these parts:",
        "  1) 2-3 sentences: the product type in the first sentence, who it's for and the main benefit.",
        "  2) 3-4 sentences turning features into benefits the shopper feels (how it's used, what it solves).",
        "  3) If items in the box are known: a line 'In the box:' then one item per line starting with '• '.",
        "  4) A line 'Specifications:' then one known fact per line as '• Name: value' (only facts given below).",
        "  No HTML, no markdown (**), no price, discount, stock or delivery promises.",
        "SEO_TITLE: max 60 characters, main keyword first. META_DESCRIPTION: 130-155 characters, one benefit and a "
        "gentle call to action.",
        "TAGS: 6-10 lowercase search phrases shoppers really type (2-4 words each, include the product type and "
        "useful long-tail variations), no duplicates.",
        "",
        "HONESTY: never invent specs, materials, certifications, warranty or battery life. Anything marked '?' is "
        "a guess: leave it out or say it softly ('silver-tone finish'), never as certain. Don't use hype words "
        f"({', '.join(HYPE[:8])}...)" + (f" except: {', '.join(allowed_hype)}" if allowed_hype else "") + ".",
        "The seller's notes may include private remarks meant only for you (quality, cost, supplier, condition "
        "issues). Respect them — make no claims they contradict — but never repeat a negative remark in the listing.",
        "",
        f"FACTS: product type = {facts.product_type}; brand = {facts.brand or 'unknown'}; "
        f"model = {facts.model or 'unknown'}; attributes = {facts.attributes}; features = {facts.features}",
    ]
    if getattr(facts, "in_the_box", None):
        lines.append(f"In the box: {facts.in_the_box}")
    if getattr(facts, "use_cases", None):
        lines.append(f"Used for / by: {facts.use_cases}")
    if research and research.mode != "skipped":
        lines.append(f"RESEARCH: specs = {research.facts}; buyers care about = {research.buyer_priorities}; "
                     f"search keywords to use naturally (no stuffing) = {research.keywords}")
    if seller_notes:
        notes = re.sub(r"(?i)(\b(price|rs\.?|pkr|usd|gbp|eur|aed|sar|inr|discount|stock)\b|[$£€₹])[^\n.,]*", "",
                       seller_notes).strip()
        if notes:
            lines.append(f"SELLER'S NOTES (their word is final on facts): {notes}")
    if instruction:
        lines.append(f"SELLER'S CHANGE REQUEST (follow it): {instruction}")
    if fix:
        lines.append(fix)
    return "\n".join(lines)


def _cut(text: str, limit: int) -> str:
    """Shorten at a word boundary, without a dangling dash or comma."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit + 1].rsplit(" ", 1)[0] if " " in text[:limit + 1] else text[:limit]
    return cut.rstrip(" -–—,;:/&|")


def tidy_listing(d: dict, facts) -> ListingDraft:
    """Clean what the model returned: stray markdown, guesses, over-long fields, duplicate tags."""
    def clean(x) -> str:
        return re.sub(r"\*\*|__|`|#+\s", "", str(x or "")).strip()

    title = _cut(clean(d.get("title")).strip(" .!"), 80)
    highlights = []
    for h in d.get("highlights") or []:
        h = clean(h).lstrip("•-*· ").rstrip(" .")
        if h and "?" not in h and h.lower() not in (x.lower() for x in highlights):
            highlights.append(_cut(h, 100))
    description = clean(d.get("description")).replace("\r", "")
    description = re.sub(r"[ \t]+\n", "\n", description)
    description = re.sub(r"\n{3,}", "\n\n", description).strip()
    tags, seen = [], set()
    for t in d.get("tags") or []:
        t = re.sub(r"\s+", " ", clean(t).lstrip("#")).strip().lower()
        if t and t not in seen and len(t) <= 60:
            seen.add(t)
            tags.append(t)
    return ListingDraft(
        title=title, highlights=highlights[:6], description=description,
        seo_title=_cut(clean(d.get("seo_title")), 60) or None,
        meta_description=_cut(clean(d.get("meta_description")), 160) or None,
        tags=tags[:10], category_suggestion=facts.suggested_category,
    )


def listing_problems(draft: ListingDraft, language: str) -> list[str]:
    """What's still wrong after tidying (-> one more pass). Kept short and objective."""
    problems = []
    if not draft.title:
        problems.append("the title is missing")
    elif len(draft.title) > 72:
        problems.append(f"the title is {len(draft.title)} characters — keep it 50-70")
    elif len(draft.title) < 25:
        problems.append("the title is too short — add the most useful facts (size, material, pieces)")
    words = len(draft.description.split())
    if words < (60 if language == "ur" else 80):
        problems.append(f"the description is only {words} words — write 120-220")
    if len(draft.highlights) < 3:
        problems.append("give 4-5 highlights")
    if PROMO.search(draft.title) or "!" in draft.title:
        problems.append("the title has promotional words or '!' — remove them")
    guess = re.compile(r"\w\?(?:[,;)]| [a-z])")      # "steel? body" — a '?' guess left in the middle of text
    if guess.search(draft.title) or guess.search(draft.description) or any(guess.search(h + " x") for h in draft.highlights):
        problems.append("a guess marked '?' was written into the text — leave it out or phrase it softly")
    return problems
