"""The AI agent's contract — what every AI provider must implement.

The rest of the app only talks to `ListingAI`, so we can switch providers
(or run the free "stub" during development) without touching routes or UI.

Pipeline for one product:
    photos + seller notes ──► analyze()          -> ProductFacts   (what is this?)
                          ──► research()         -> Research       (optional web search)
                          ──► write_listing()    -> ListingDraft   (per language / platform)
    voice/typed command   ──► parse_command()    -> CommandResult  (price, discount, stock...)

Bulk upload helpers (the seller waits for these; no credit is used):
    many photos           ──► group_photos()     -> list[PhotoGroup]  (which photos are the same product)
    one voice note        ──► split_notes()      -> NotesSplit        (what was said about each product)
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


@dataclass
class PhotoGroup:
    photos: list[int]                      # 0-based positions in the photos sent
    label: str = ""                        # short product name, e.g. "steel water bottle"


@dataclass
class ProductNotes:
    notes: str = ""                        # what the seller said about this product ("" = nothing)
    price: float | None = None             # only when the seller said it (in the seller's currency)
    discount_pct: int | None = None
    stock: int | None = None


@dataclass
class NotesSplit:
    products: list[ProductNotes]           # one per product, in the order sent
    unmatched: str = ""                    # what couldn't be tied to any product


MAX_PHOTOS_PER_PRODUCT = 8


def normalize_groups(raw: list[tuple[list, str]], count: int, usable: list[bool] | None = None,
                     max_per: int = MAX_PHOTOS_PER_PRODUCT) -> list[PhotoGroup]:
    """Turn the AI's answer into a clean grouping of photos 0..count-1:
    raw = [(1-based photo numbers, label), ...]. Every photo ends up in exactly one group:
    unknown numbers and repeats are dropped, photos the AI left out (or couldn't see) stay on their own,
    big groups are split at max_per. Groups keep the order of their first photo."""
    usable = usable or [True] * count
    taken: set[int] = set()
    groups: list[PhotoGroup] = []
    for numbers, label in raw:
        members = []
        for n in numbers or []:
            try:
                i = int(n) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= i < count and usable[i] and i not in taken:
                taken.add(i)
                members.append(i)
        members.sort()
        for k in range(0, len(members), max_per):
            groups.append(PhotoGroup(members[k:k + max_per], str(label or "").strip()[:60]))
    groups += [PhotoGroup([i]) for i in range(count) if i not in taken]
    groups.sort(key=lambda g: g.photos[0])
    return groups


def _num(v, cast, lo, hi):
    try:
        x = cast(v)
    except (TypeError, ValueError):
        return None
    return x if lo <= x <= hi else None


def normalize_notes(raw: list[dict], count: int, max_len: int = 1000) -> list[ProductNotes]:
    """[{"product": 1-based number, "notes", "price", "discount_pct", "stock"}, ...] -> one ProductNotes per
    product. Numbers outside sensible ranges are dropped; notes said twice about one product are joined."""
    out = [ProductNotes() for _ in range(count)]
    for d in raw or []:
        if not isinstance(d, dict):
            continue
        i = _num(d.get("product"), int, 1, count)
        if i is None:
            continue
        p = out[i - 1]
        text = re.sub(r"\s+", " ", str(d.get("notes") or "")).strip()
        if text:
            p.notes = (f"{p.notes} {text}" if p.notes else text)[:max_len]
        if d.get("price") is not None:
            price = _num(d.get("price"), float, 0.01, 100_000_000)
            p.price = price if price is not None else p.price
        if d.get("discount_pct") is not None:
            p.discount_pct = _num(d.get("discount_pct"), int, 1, 95) or p.discount_pct
        if d.get("stock") is not None:
            stock = _num(d.get("stock"), int, 0, 1_000_000)
            p.stock = stock if stock is not None else p.stock
    return out


ALL_WORDS = {"all", "all stores", "every store", "everywhere", "sab", "sab stores", "sare stores", "saray stores",
             "sb", "har store", "tamam stores", "both", "dono"}
PLATFORM_WORDS = {
    "shopify": {"shopify"},
    "woocommerce": {"woocommerce", "woo", "wordpress", "wp", "woo commerce"},
    "custom": {"custom", "custom store", "smart click", "smartclick", "my website", "website"},
    "daraz": {"daraz", "daraz pk", "daraz.pk"},
    "ebay": {"ebay", "e bay"},
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
    async def analyze(self, image_urls: list[str], seller_notes: str | None, categories: list[str],
                      market=None) -> ProductFacts: ...

    async def research(self, facts: ProductFacts, market=None) -> Research: ...   # market: lagent_common.markets.Market

    async def write_listing(
        self,
        facts: ProductFacts,
        research: Research | None,
        seller_notes: str | None,
        language: str,              # "en" | "ur"
        platform: str | None,       # None = generic, else "shopify" | "woocommerce" | "custom"
        instruction: str | None = None,
        market=None,
    ) -> ListingDraft: ...

    async def parse_command(self, text: str, stores: list[dict] | None = None, market=None) -> CommandResult: ...

    async def group_photos(self, image_urls: list[str]) -> list[PhotoGroup]: ...

    async def split_notes(self, text: str, products: list[dict], market=None) -> NotesSplit: ...
    # products: [{"label": str, "image_url": str | None}] in the order shown to the seller
