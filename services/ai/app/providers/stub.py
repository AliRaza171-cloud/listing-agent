"""Offline stand-in for the real AI provider.

Returns predictable fake data so the whole app (upload -> listing -> publish)
can be built and demoed without an API key or any cost. Swap to a real
provider by setting AI_PROVIDER in .env once that provider is implemented.
"""
import re

from lagent_common.markets import Market

from app.providers.base import (ALL_WORDS, PLATFORM_WORDS, CommandResult, ListingDraft, NotesSplit, ProductFacts,
                                Research, confirmation, match_stores, normalize_groups, normalize_notes)


class StubListingAI:
    async def analyze(self, image_urls, seller_notes, categories, market=None):
        return ProductFacts(
            product_type="sample product",
            attributes={"color": "silver"},
            features=["durable build", "easy to clean"],
            suggested_category=categories[0] if categories else None,
            questions_for_seller=["What is the size or capacity?", "What material is it made of?"],
        )

    async def research(self, facts, market=None):
        return Research(mode="skipped")

    async def write_listing(self, facts, research, seller_notes, language, platform, instruction=None, market=None):
        name = facts.product_type.title()
        if language == "ur":
            return ListingDraft(title=f"{name} (نمونہ)", highlights=["پائیدار", "صاف کرنے میں آسان"],
                                description="یہ ایک نمونہ تفصیل ہے۔")
        return ListingDraft(
            title=f"{name} – Durable & Easy to Clean",
            highlights=["Durable build", "Easy to clean", "Great value"],
            description=f"A sample description for a {facts.product_type}. " + (seller_notes or ""),
            seo_title=f"Buy {name} Online",
            tags=[facts.product_type, "home", "kitchen"],
            category_suggestion=facts.suggested_category,
        )

    async def parse_command(self, text, stores=None, market=None):
        stores = stores or []
        mk = market or Market()
        # Tiny rule-based parser so voice/typed commands can be tested offline.
        # The real provider handles Urdu / Roman Urdu / mixed phrasing properly.
        t = text.lower()
        result = CommandResult()
        if m := re.search(r"price\s*(?:is|to|=)?\s*(?:rs\.?|\$|£|€|usd|gbp|eur)?\s*(\d[\d,]*)", t):
            result.price = float(m.group(1).replace(",", ""))
        if m := re.search(r"(\d{1,2})\s*(?:%|percent)", t):
            result.discount_pct = int(m.group(1))
        if m := re.search(r"stock\s*(\d+)", t):
            result.stock = int(m.group(1))
        if "free shipping" in t:
            result.free_shipping = True
        parts = []
        if result.price is not None:
            parts.append(f"price {mk.money(result.price)}")
        if result.discount_pct is not None:
            parts.append(f"{result.discount_pct}% off")
        if result.stock is not None:
            parts.append(f"stock {result.stock}")
        if result.free_shipping:
            parts.append("free shipping")
        if stores and re.search(r"\b(publish|upload|daal|dal|laga|post|send|bhej)", t):
            result.publish = True
            words = [w for w in ALL_WORDS if re.search(rf"\b{re.escape(w)}\b", t)]
            words += [w for ws in PLATFORM_WORDS.values() for w in ws if re.search(rf"\b{re.escape(w)}\b", t)]
            words += [st["name"] for st in stores if st["name"].lower() in t]
            result.publish_to = match_stores(words, stores) if words else [st["id"] for st in stores]
            if re.search(r"\blive\b", t):
                result.publish_mode = "live"
            elif "draft" in t:
                result.publish_mode = "draft"
            if "urdu" in t:
                result.publish_language = "ur"
        result.confirmation_text = confirmation(parts, result, stores)
        return result

    async def group_photos(self, image_urls):
        # Offline: can't see the photos, so every photo stays its own product.
        return normalize_groups([], len(image_urls))

    async def split_notes(self, text, products, market=None):
        # Offline: sentence 1 -> product 1, sentence 2 -> product 2 ...; extra sentences are unmatched.
        sentences = [x.strip() for x in re.split(r"[.;\n]+", text) if x.strip()]
        raw, extra = [], []
        for i, sentence in enumerate(sentences):
            if i < len(products):
                price = re.search(r"(?:price|rs\.?)\s*(\d[\d,]*)", sentence, re.I)
                stock = re.search(r"stock\s*(\d+)", sentence, re.I)
                raw.append({"product": i + 1, "notes": sentence,
                            "price": price.group(1).replace(",", "") if price else None,
                            "stock": stock.group(1) if stock else None})
            else:
                extra.append(sentence)
        return NotesSplit(products=normalize_notes(raw, len(products)), unmatched=". ".join(extra))
