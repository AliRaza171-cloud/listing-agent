"""Voice service — speech to text (Urdu, English, and a mix of both).

Stateless. Returns text only; understanding the text is the AI service's job
(/api/ai/commands/parse), and nothing is saved until the seller confirms.
"""
from typing import Protocol

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic_settings import BaseSettings

from lagent_common.gemini import GeminiError
from lagent_common.internal import current_user_id, require_internal
from lagent_common.service import create_service


class Settings(BaseSettings):
    STT_PROVIDER: str = "stub"
    STT_API_KEY: str = ""
    STT_MODEL: str = ""        # empty = provider default (gemini: gemini-3.8-flash)
    STT_FALLBACK_MODEL: str = "gemini-3.5-flash-lite"   # used automatically when STT_MODEL is overloaded
    MAX_AUDIO_MB: int = 10


settings = Settings()
ALLOWED = {"audio/webm", "audio/ogg", "audio/mpeg", "audio/mp4", "audio/wav", "audio/x-wav", "audio/m4a"}


class SpeechToText(Protocol):
    async def transcribe(self, audio: bytes, content_type: str) -> dict:
        """-> {"text": str, "language": "ur" | "en" | None}"""


class StubSTT:
    """Offline stand-in: returns a fixed phrase so the voice flow can be built and demoed."""

    async def transcribe(self, audio: bytes, content_type: str) -> dict:
        return {"text": "Price 2500, 10% discount, stock 20", "language": "en"}


class GeminiSTT:
    """Gemini listens to the recording and writes down exactly what was said."""

    SCHEMA = {
        "type": "OBJECT",
        "properties": {"text": {"type": "STRING"}, "language": {"type": "STRING", "enum": ["ur", "en", "mixed"]}},
        "required": ["text", "language"],
    }
    PROMPT = (
        "Transcribe this short voice note from a seller in Pakistan exactly as spoken. "
        "Urdu speech: write it in Roman Urdu (Latin letters, e.g. 'is ka price 2500 rakho'). "
        "English: write English. Write numbers as digits. Don't translate, summarise or add anything. "
        "language: 'ur', 'en' or 'mixed'. If there is no speech, text is an empty string."
    )

    def __init__(self, api_key: str, model: str, fallback_model: str | None = None):
        from lagent_common.gemini import GeminiClient

        self.client = GeminiClient(api_key, model, timeout=60, fallback_model=fallback_model)

    async def transcribe(self, audio: bytes, content_type: str) -> dict:
        from lagent_common.gemini import Part

        reply = await self.client.generate(
            [Part.text(self.PROMPT), Part.blob(audio, content_type)], schema=self.SCHEMA, temperature=0)
        d = reply.json or {}
        lang = d.get("language")
        return {"text": str(d.get("text") or "").strip(), "language": "ur" if lang in ("ur", "mixed") else "en"}


class OpenAISTT:
    """OpenAI's transcription model writes down the voice note; Urdu comes back in Roman letters."""

    PROMPT = ("Seller in Pakistan giving product details in Urdu and English, written in Roman Urdu: "
              "is ka price 2500 rakho, 10 percent discount do, stock 20 hai, free shipping.")

    def __init__(self, api_key: str, model: str):
        from lagent_common.openai_client import OpenAIClient

        self.model = model
        self.client = OpenAIClient(api_key, "unused", timeout=60)

    async def transcribe(self, audio: bytes, content_type: str) -> dict:
        text = await self.client.transcribe(audio, content_type, model=self.model, prompt=self.PROMPT)
        urdu_script = any("\u0600" <= ch <= "\u06ff" for ch in text)
        roman_urdu = any(w in text.lower().split() for w in ("hai", "ka", "ki", "ko", "rakho", "karo", "do", "aur"))
        return {"text": text, "language": "ur" if urdu_script or roman_urdu else "en"}


def get_stt(settings) -> SpeechToText:
    provider = settings.STT_PROVIDER.lower()
    if provider == "stub":
        return StubSTT()
    if provider == "gemini":
        return GeminiSTT(settings.STT_API_KEY, settings.STT_MODEL or "gemini-3.8-flash",
                         settings.STT_FALLBACK_MODEL or None)
    if provider == "openai":
        model = settings.STT_MODEL if settings.STT_MODEL and not settings.STT_MODEL.startswith("gemini") \
            else "gpt-4o-mini-transcribe"
        return OpenAISTT(settings.STT_API_KEY, model)
    raise RuntimeError(f"Speech-to-text provider '{settings.STT_PROVIDER}' is not implemented (use 'stub', 'gemini' or 'openai').")


stt = get_stt(settings)
router = APIRouter(dependencies=[Depends(require_internal)])


@router.post("/transcribe")
async def transcribe(audio: UploadFile = File(...), _user=Depends(current_user_id)):
    if audio.content_type not in ALLOWED:
        raise HTTPException(415, "Unsupported audio format.")
    data = await audio.read()
    if len(data) > settings.MAX_AUDIO_MB * 1024 * 1024:
        raise HTTPException(413, f"Recording is too long (max {settings.MAX_AUDIO_MB} MB).")
    if not data:
        raise HTTPException(422, "The recording was empty.")
    try:
        result = await stt.transcribe(data, audio.content_type)
    except GeminiError as exc:
        raise HTTPException(503 if exc.retryable else 502, f"Couldn't transcribe: {exc}")
    if not result["text"]:
        raise HTTPException(422, "I couldn't hear any words — try again a little closer to the mic.")
    return result


app = create_service("voice", routers=[router])
