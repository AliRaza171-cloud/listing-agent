"""AI service — stateless worker.

Consumes listing.requested, runs the pipeline, emits listing.generated or
listing.failed. Also answers /commands/parse directly (the seller waits for it).

Duplicate deliveries are harmless: catalog ignores a listing.generated for a job
that's no longer queued, and billing settles a reservation only once.
"""
import asyncio
import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

from lagent_common.bus import EventBus
from lagent_common.internal import current_user_id, require_internal
from lagent_common.service import create_service

from app.providers import get_listing_ai
from app.providers.stub import StubListingAI
from lagent_common.gemini import GeminiError

log = logging.getLogger("lagent.ai")


class Settings(BaseSettings):
    REDIS_URL: str = "redis://redis:6379/0"
    AI_PROVIDER: str = "stub"
    AI_API_KEY: str = ""
    AI_MODEL: str = ""                 # empty = provider default (gemini-3.8-flash / gpt-6-luna)
    AI_FALLBACK_MODEL: str = "gemini-3.5-flash-lite"   # used automatically when AI_MODEL is overloaded
    AI_REASONING_EFFORT: str = "low"   # OpenAI reasoning models: none | low | medium | high
    AI_WEB_SEARCH: bool = True         # Google Search grounding for research (paid Gemini keys); falls back if unavailable
    AI_TIMEOUT_SECONDS: int = 180
    CATALOG_URL: str = "http://catalog:8000"   # where product photos are fetched from
    INTERNAL_TOKEN: str = ""


settings = Settings()
bus = EventBus(settings.REDIS_URL, "ai")
ai = get_listing_ai(settings)


async def run_pipeline(data: dict) -> dict:
    facts = await ai.analyze(data["image_urls"], data.get("seller_notes"), data.get("categories") or [])
    research = await ai.research(facts) if data.get("research") else None
    listings = []
    for language in data["languages"]:
        for platform in (data["platforms"] or [None]):
            draft = await ai.write_listing(facts, research, data.get("seller_notes"), language, platform)
            listings.append({"language": language, "platform": platform, **asdict(draft)})
    return {
        "facts": asdict(facts),
        "research": asdict(research) if research else None,
        "listings": listings,
    }


async def on_listing_requested(event: dict) -> None:
    data = event["data"]
    ids = {k: data[k] for k in ("job_id", "product_id", "user_id", "reservation_id")}
    try:
        result = await asyncio.wait_for(run_pipeline(data), timeout=settings.AI_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — the seller gets a clear failure and their credit back
        log.exception("listing generation failed for job %s", data["job_id"])
        if isinstance(exc, asyncio.TimeoutError):
            reason = "The AI took too long — please try again."
        elif isinstance(exc, GeminiError):
            reason = str(exc)[:300]
        else:
            reason = "The AI couldn't write this listing — please try again."
        await bus.publish("listing.failed", {**ids, "reason": reason})
        return
    await bus.publish("listing.generated", {**ids, **result})


bus.subscribe("listing.requested", on_listing_requested)


class CommandIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


router = APIRouter(dependencies=[Depends(require_internal)])


@router.post("/commands/parse")
async def parse_command(data: CommandIn, _user=Depends(current_user_id)):
    """Voice/typed command -> structured fields. The UI shows confirmation_text and
    saves nothing until the seller confirms."""
    try:
        return asdict(await ai.parse_command(data.text))
    except GeminiError as exc:
        # The AI is unavailable (rate limit, no key...): fall back to the simple offline parser,
        # which handles "price 2500, 10% off, stock 20" style commands.
        log.warning("command parsing fell back to rules: %s", exc)
        return asdict(await StubListingAI().parse_command(data.text))


app = create_service("ai", routers=[router], bus=bus)
