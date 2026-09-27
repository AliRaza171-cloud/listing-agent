from app.providers.base import ListingAI
from app.providers.stub import StubListingAI


def get_listing_ai(settings) -> ListingAI:
    """Pick the AI provider from AI_PROVIDER in .env."""
    provider = settings.AI_PROVIDER.lower()
    if provider == "stub":
        return StubListingAI()
    if provider == "gemini":
        from app.providers.gemini import GeminiListingAI

        return GeminiListingAI(
            settings.AI_API_KEY, settings.AI_MODEL or "gemini-3.8-flash",
            catalog_url=settings.CATALOG_URL, internal_token=settings.INTERNAL_TOKEN,
            use_search=settings.AI_WEB_SEARCH, fallback_model=settings.AI_FALLBACK_MODEL or None,
        )
    raise RuntimeError(f"AI provider '{settings.AI_PROVIDER}' is not implemented (use 'stub' or 'gemini').")
