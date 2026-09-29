"""The AI agent's contract — what every AI provider must implement.

The rest of the app only talks to `ListingAI`, so we can switch providers
(or run the free "stub" during development) without touching routes or UI.

Pipeline for one product:
    photos + seller notes ──► analyze()          -> ProductFacts   (what is this?)
                          ──► research()         -> Research       (optional web search)
                          ──► write_listing()    -> ListingDraft   (per language / platform)
    voice/typed command   ──► parse_command()    -> CommandResult  (price, discount, stock...)
"""
from __future__ import annotations

import re

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ProductFacts:
    product_type: str                      # "oil strainer pot"
    brand: str | None = None               # set only when visible or typed -> triggers spec lookup
    model: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)   # {"color": "silver", "material": "stainless steel?"}
    features: list[str] = field(default_factory=list)
    suggested_category: str | None = None
    # True when none of the store's categories fits and the AI proposes a new one
    # (the store creates it on publish).
    category_is_new: bool = False
    # Details that matter for this product type but aren't knowable from the photo —
    # the UI / voice assistant asks the seller for these.
    questions_for_seller: list[str] = field(default_factory=list)


@dataclass
class ResearchSource:
    title: str
    url: str


@dataclass
class Research:
    mode: str                              # "specs" (branded) | "category" (generic) | "skipped" (handmade)
    facts: dict[str, str] = field(default_factory=dict)        # specs, only for branded products
    buyer_priorities: list[str] = field(default_factory=list)  # what shoppers care about for this type
    keywords: list[str] = field(default_factory=list)
    price_range: tuple[float, float] | None = None             # a suggestion only — seller sets the price
    sources: list[ResearchSource] = field(default_factory=list)


@dataclass
class ListingDraft:
    title: str
    highlights: list[str]
    description: str
    seo_title: str | None = None
    meta_description: str | None = None
    tags: list[str] = field(default_factory=list)
    category_suggestion: str | None = None


@dataclass
class CommandResult:
    """Structured fields pulled from 'is ka price 2500 rakho, 10 percent discount'.
    Only keys the seller actually mentioned are set; the UI confirms before saving."""
    price: float | None = None
    discount_pct: int | None = None
    remove_discount: bool = False
    stock: int | None = None
    sku: str | None = None
    free_shipping: bool | None = None
    publish: bool = False                  # "Shopify pe publish karo", "sab stores pe daal do"
    publish_to: list[str] = field(default_factory=list)   # store connection ids (matched from what was said)
    publish_mode: str | None = None        # "live" | "draft" | None (= the UI default)
    publish_language: str | None = None    # "en" | "ur" | None
    edit_instruction: str | None = None    # "make the title shorter" -> sent to write_listing
    confirmation_text: str = ""            # read back to the seller before anything is saved


ALL_WORDS = {"all", "all stores", "every store", "everywhere", "sab", "sab stores", "sare stores", "saray stores",
             "sb", "har store", "tamam stores", "both", "dono"}
PLATFORM_WORDS = {
    "shopify": {"shopify"},
    "woocommerce": {"woocommerce", "woo", "wordpress", "wp", "woo commerce"},
    "custom": {"custom", "custom store", "smart click", "smartclick", "my website", "website"},
    "daraz": {"daraz", "daraz pk", "daraz.pk"},
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def match_stores(targets: list[str], stores: list[dict]) -> list[str]:
    """Store names / platform words the seller said -> store connection ids (in the seller's order).
    stores: [{"id", "name", "platform"}]. "all"/"sab" -> every store."""
    ids: list[str] = []

    def add(found):
        for st in found:
            if st["id"] not in ids:
                ids.append(st["id"])

    for t in targets:
        n = _norm(t)
        if not n:
            continue
        if n in ALL_WORDS:
            return [st["id"] for st in stores]
        exact = [st for st in stores if _norm(st["name"]) == n]
        if exact:
            add(exact)
            continue
        by_platform = [st for st in stores if n in PLATFORM_WORDS.get(st["platform"], set())]
        if by_platform:
            add(by_platform)
            continue
        if len(n) >= 3:
            add([st for st in stores if n in _norm(st["name"]) or _norm(st["name"]) in n])
    return ids


def confirmation(parts: list[str], r: "CommandResult", stores: list[dict]) -> str:
    """Read back before anything is saved: 'Set price Rs. 2,500 and publish to Shopify store as live?'"""
    pub = publish_summary(r, stores) if r.publish and r.publish_to else None
    if parts and pub:
        return "Set " + ", ".join(parts) + " and " + pub + "?"
    if parts:
        return "Set " + ", ".join(parts) + "?"
    if pub:
        return pub[0].upper() + pub[1:] + "?"
    if r.publish:
        return "I couldn't tell which store you meant."
    return "I didn't catch any values."


def publish_summary(r: "CommandResult", stores: list[dict]) -> str:
    names = [st["name"] for st in stores if st["id"] in r.publish_to]
    where = ", ".join(names) if names else "your stores"
    mode = {"live": " as live", "draft": " as a draft"}.get(r.publish_mode or "", "")
    lang = {"ur": " (Urdu)", "en": " (English)"}.get(r.publish_language or "", "")
    return f"publish to {where}{mode}{lang}"


class ListingAI(Protocol):
    async def analyze(self, image_urls: list[str], seller_notes: str | None, categories: list[str]) -> ProductFacts: ...

    async def research(self, facts: ProductFacts) -> Research: ...

    async def write_listing(
        self,
        facts: ProductFacts,
        research: Research | None,
        seller_notes: str | None,
        language: str,              # "en" | "ur"
        platform: str | None,       # None = generic, else "shopify" | "woocommerce" | "custom"
        instruction: str | None = None,
    ) -> ListingDraft: ...

    async def parse_command(self, text: str, stores: list[dict] | None = None) -> CommandResult: ...
