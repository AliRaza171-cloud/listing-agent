"""Minimal Google Gemini client (REST, models/{model}:generateContent).

Used by the ai service (listings, research, commands) and the voice service
(speech to text). Plain httpx, no SDK, so there's one small dependency to keep
up to date. Docs: https://ai.google.dev/api/generate-content

    client = GeminiClient(api_key, model="gemini-3.8-flash")
    reply = await client.generate([Part.text("Hi")], schema={...})
    reply.json   # parsed JSON when a schema was given
    reply.sources  # [(title, url)] when google_search grounding was used
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("lagent.gemini")

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class GeminiError(Exception):
    """Seller-safe message; `retryable` for rate limits / temporary outages; `status` = HTTP status."""

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class Part:
    @staticmethod
    def text(value: str) -> dict:
        return {"text": value}

    @staticmethod
    def blob(data: bytes, mime_type: str) -> dict:
        return {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(data).decode()}}


@dataclass
class Reply:
    text: str
    json: dict | list | None = None
    sources: list[tuple[str, str]] = field(default_factory=list)


def _error_message(r: httpx.Response) -> str:
    try:
        return r.json().get("error", {}).get("message") or r.text[:300]
    except ValueError:
        return r.text[:300]


class GeminiClient:
    def __init__(self, api_key: str, model: str, *, timeout: float = 90.0, max_retries: int = 3,
                 base_url: str = API_BASE, fallback_model: str | None = None):
        if not api_key:
            raise GeminiError("No Gemini API key configured (AI_API_KEY / STT_API_KEY in .env).")
        self.api_key = api_key
        self.model = model
        # Used when the main model is overloaded (429/5xx after retries) or doesn't exist (404).
        self.fallback_model = fallback_model if fallback_model and fallback_model != model else None
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))

    async def generate(
        self,
        parts: list[dict],
        *,
        system: str | None = None,
        schema: dict | None = None,
        search: bool = False,
        temperature: float | None = None,
        model: str | None = None,
    ) -> Reply:
        body: dict = {"contents": [{"role": "user", "parts": parts}]}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        config: dict = {}
        if schema is not None:
            config["responseMimeType"] = "application/json"
            config["responseSchema"] = schema
        if temperature is not None:
            config["temperature"] = temperature
        if config:
            body["generationConfig"] = config
        if search:
            body["tools"] = [{"google_search": {}}]

        primary = model or self.model
        try:
            data = await self._post(primary, body)
        except GeminiError as exc:
            if not self.fallback_model or primary == self.fallback_model or not (exc.retryable or exc.status == 404):
                raise
            log.warning("model %s unavailable (%s); using backup model %s", primary, exc, self.fallback_model)
            data = await self._post(self.fallback_model, body)
        return self._parse(data, want_json=schema is not None)

    async def _post(self, model: str, body: dict) -> dict:
        url = f"{self.base_url}/models/{model}:generateContent"
        headers = {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}
        for attempt in range(self.max_retries + 1):
            try:
                r = await self._http.post(url, headers=headers, json=body)
            except httpx.TimeoutException:
                err = GeminiError("The AI took too long to answer.", retryable=True)
            except httpx.HTTPError as exc:
                err = GeminiError(f"Couldn't reach the AI service ({exc.__class__.__name__}).", retryable=True)
            else:
                if r.status_code == 200:
                    return r.json()
                msg = _error_message(r)
                if r.status_code in (401, 403):
                    raise GeminiError(f"The Gemini API key was rejected: {msg}", status=r.status_code)
                if r.status_code == 404:
                    raise GeminiError(f"Gemini model '{model}' wasn't found — check AI_MODEL in .env. ({msg})",
                                      status=404)
                if r.status_code == 400:
                    raise GeminiError(f"Gemini rejected the request: {msg}", status=400)
                err = GeminiError(
                    "The AI is busy (rate limit) — try again in a minute." if r.status_code == 429
                    else f"The AI service is overloaded — try again in a minute. ({r.status_code}: {msg[:120]})"
                    if r.status_code == 503 else f"The AI service had a problem ({r.status_code}: {msg[:120]}).",
                    retryable=r.status_code in RETRY_STATUSES, status=r.status_code)
                if not err.retryable:
                    raise GeminiError(f"{err} {msg}", status=r.status_code)
            if attempt == self.max_retries:
                raise err
            delay = min(2 ** attempt * 2, 20) + random.uniform(0, 1)
            log.warning("gemini attempt %d failed (%s); retrying in %.1fs", attempt + 1, err, delay)
            await asyncio.sleep(delay)
        raise GeminiError("unreachable")

    @staticmethod
    def _parse(data: dict, *, want_json: bool) -> Reply:
        feedback = data.get("promptFeedback") or {}
        if feedback.get("blockReason"):
            raise GeminiError(f"The AI refused this request ({feedback['blockReason']}).")
        candidates = data.get("candidates") or []
        if not candidates:
            raise GeminiError("The AI returned no answer.")
        cand = candidates[0]
        text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []) if not p.get("thought"))
        reason = cand.get("finishReason")
        if not text.strip():
            raise GeminiError(f"The AI returned an empty answer ({reason or 'no reason given'}).")
        if reason in ("SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"):
            raise GeminiError(f"The AI stopped for content reasons ({reason}).")

        sources = []
        for chunk in (cand.get("groundingMetadata") or {}).get("groundingChunks", []) or []:
            web = chunk.get("web") or {}
            if web.get("uri"):
                sources.append((web.get("title") or web["uri"], web["uri"]))

        parsed = None
        if want_json:
            cleaned = text.strip()
            if cleaned.startswith("```"):  # tolerate fenced JSON
                cleaned = cleaned.strip("`")
                cleaned = cleaned[cleaned.find("{"):] if "{" in cleaned else cleaned
            try:
                parsed = json.loads(cleaned)
            except json.JSONDecodeError:
                start, end = cleaned.find("{"), cleaned.rfind("}")
                if start == -1 or end <= start:
                    raise GeminiError("The AI answer wasn't valid JSON.")
                try:
                    parsed = json.loads(cleaned[start:end + 1])
                except json.JSONDecodeError:
                    raise GeminiError("The AI answer wasn't valid JSON.")
        return Reply(text=text, json=parsed, sources=sources)

    async def close(self) -> None:
        await self._http.aclose()
