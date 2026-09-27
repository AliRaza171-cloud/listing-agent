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
    if provider == "openai":
        # Same listing logic (prompts, schemas, Urdu writing, checks) with OpenAI underneath.
        from app.providers.gemini import GeminiListingAI
        from lagent_common.openai_client import OpenAIClient

        model = settings.AI_MODEL if settings.AI_MODEL and not settings.AI_MODEL.startswith("gemini") else "gpt-6-luna"
        fallback = settings.AI_FALLBACK_MODEL or ""
        if not fallback or fallback.startswith("gemini"):  # the .env default is a Gemini model
            fallback = "gpt-5-mini"
        client = OpenAIClient(settings.AI_API_KEY, model, fallback_model=fallback,
                              reasoning_effort=getattr(settings, "AI_REASONING_EFFORT", "low") or "")
        return GeminiListingAI(
            settings.AI_API_KEY, model,
            catalog_url=settings.CATALOG_URL, internal_token=settings.INTERNAL_TOKEN,
            use_search=False, client=client,
        )
    raise RuntimeError(f"AI provider '{settings.AI_PROVIDER}' is not implemented (use 'stub', 'gemini' or 'openai').")
