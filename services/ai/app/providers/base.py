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
    publish_to: list[str] = field(default_factory=list)
    edit_instruction: str | None = None    # "make the title shorter" -> sent to write_listing
    confirmation_text: str = ""            # read back to the seller before anything is saved


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

    async def parse_command(self, text: str) -> CommandResult: ...
