"""Minimal OpenAI client with the same interface as GeminiClient.

So the listing logic (prompts, schemas, Urdu writing, checks) works unchanged with either
provider:  client.generate(parts, system=..., schema=..., search=..., temperature=...) -> Reply

- Chat Completions API (https://api.openai.com/v1/chat/completions), plain httpx.
- `schema` is written in Gemini's style (OBJECT/STRING, nullable); it's converted to JSON Schema
  and sent as a Structured Output (response_format json_schema).
- Photos (Part.blob image/*) go as data-URL images.
- `search=True` isn't offered here (OpenAI's web search tool is billed per call); it raises a
  400-style GeminiError so callers fall back to model knowledge, exactly like a free Gemini key.
- Errors are raised as GeminiError (the shared "AI error" type the services already handle).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random

import httpx

from lagent_common.gemini import GeminiError, Reply

log = logging.getLogger("lagent.openai")

API_BASE = "https://api.openai.com/v1"
RETRY_STATUSES = {429, 500, 502, 503, 504}
_TYPES = {"OBJECT": "object", "STRING": "string", "NUMBER": "number", "INTEGER": "integer",
          "BOOLEAN": "boolean", "ARRAY": "array"}


def to_json_schema(s: dict) -> dict:
    """Gemini responseSchema -> JSON Schema (types lower-cased, nullable -> [type, "null"])."""
    if not isinstance(s, dict):
        return s
    out: dict = {}
    t = s.get("type")
    jt = _TYPES.get(str(t).upper(), str(t).lower()) if t else None
    if jt:
        out["type"] = [jt, "null"] if s.get("nullable") else jt
    if "enum" in s:
        out["enum"] = list(s["enum"]) + ([None] if s.get("nullable") else [])
    if "properties" in s:
        out["properties"] = {k: to_json_schema(v) for k, v in s["properties"].items()}
        out["additionalProperties"] = False
    if "required" in s:
        out["required"] = list(s["required"])
    if "items" in s:
        out["items"] = to_json_schema(s["items"])
    if "description" in s:
        out["description"] = s["description"]
    return out


def _content(parts: list[dict]) -> list[dict]:
    content = []
    for p in parts:
        if "text" in p:
            content.append({"type": "text", "text": p["text"]})
        elif "inline_data" in p:
            mime, data = p["inline_data"]["mime_type"], p["inline_data"]["data"]
            if not mime.startswith("image/"):
                raise GeminiError(f"OpenAI chat can't take '{mime}' here.", status=400)
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
    return content


def _parse_json(text: str):
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned[cleaned.find("{"):] if "{" in cleaned else cleaned
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                pass
    raise GeminiError("The AI answer wasn't valid JSON.")


def _is_reasoning(model: str) -> bool:
    m = model.lower()
    return m.startswith(("o1", "o3", "o4", "gpt-5", "gpt-6"))


def _error(r: httpx.Response) -> tuple[str, str]:
    try:
        e = r.json().get("error") or {}
        return str(e.get("message") or r.text[:300]), str(e.get("code") or e.get("type") or "")
    except ValueError:
        return r.text[:300], ""


class OpenAIClient:
    def __init__(self, api_key: str, model: str, *, timeout: float = 120.0, max_retries: int = 3,
                 base_url: str = API_BASE, fallback_model: str | None = None, reasoning_effort: str = "low"):
        if not api_key:
            raise GeminiError("No OpenAI API key configured (AI_API_KEY / STT_API_KEY in .env).")
        self.api_key = api_key
        self.model = model
        self.fallback_model = fallback_model if fallback_model and fallback_model != model else None
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.reasoning_effort = reasoning_effort
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))

    async def generate(self, parts: list[dict], *, system: str | None = None, schema: dict | None = None,
                       search: bool = False, temperature: float | None = None, model: str | None = None) -> Reply:
        if search:
            raise GeminiError("Web search isn't enabled for the OpenAI provider.", status=400)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": _content(parts)})

        def body_for(m: str) -> dict:
            body: dict = {"model": m, "messages": messages}
            if schema is not None:
                body["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": "answer", "schema": to_json_schema(schema), "strict": False}}
            if _is_reasoning(m):
                if self.reasoning_effort:
                    body["reasoning_effort"] = self.reasoning_effort
            elif temperature is not None:
                body["temperature"] = temperature
            return body

        primary = model or self.model
        try:
            data = await self._post(body_for(primary))
        except GeminiError as exc:
            if not self.fallback_model or primary == self.fallback_model or not (exc.retryable or exc.status == 404):
                raise
            log.warning("model %s unavailable (%s); using backup model %s", primary, exc, self.fallback_model)
            data = await self._post(body_for(self.fallback_model))

        choices = data.get("choices") or []
        if not choices:
            raise GeminiError("The AI returned no answer.")
        msg = choices[0].get("message") or {}
        if msg.get("refusal"):
            raise GeminiError(f"The AI refused this request ({str(msg['refusal'])[:120]}).")
        text = msg.get("content") or ""
        if not text.strip():
            raise GeminiError(f"The AI returned an empty answer ({choices[0].get('finish_reason') or 'no reason given'}).")
        return Reply(text=text, json=_parse_json(text) if schema is not None else None)

    async def _post(self, body: dict) -> dict:
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
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
                msg, code = _error(r)
                if r.status_code == 401:
                    raise GeminiError(f"The OpenAI API key was rejected: {msg}", status=401)
                if code == "insufficient_quota":
                    raise GeminiError("The OpenAI account has no credit left — add credit at "
                                      "platform.openai.com/settings/organization/billing.", status=429)
                if r.status_code == 404 or code == "model_not_found":
                    raise GeminiError(f"OpenAI model '{body.get('model')}' wasn't found — check AI_MODEL in .env. "
                                      f"({msg})", status=404)
                if r.status_code in (400, 403):
                    raise GeminiError(f"OpenAI rejected the request: {msg}", status=r.status_code)
                err = GeminiError(
                    "The AI is busy (rate limit) — try again in a minute." if r.status_code == 429
                    else f"The AI service had a problem ({r.status_code}: {msg[:120]}).",
                    retryable=r.status_code in RETRY_STATUSES, status=r.status_code)
                if not err.retryable:
                    raise err
            if attempt == self.max_retries:
                raise err
            delay = min(2 ** attempt * 2, 20) + random.uniform(0, 1)
            log.warning("openai attempt %d failed (%s); retrying in %.1fs", attempt + 1, err, delay)
            await asyncio.sleep(delay)
        raise GeminiError("unreachable")

    async def transcribe(self, audio: bytes, content_type: str, *, model: str, prompt: str = "") -> str:
        """Speech to text (/v1/audio/transcriptions). Returns the text."""
        ext = (content_type.split("/")[-1] or "webm").split(";")[0].replace("x-", "").replace("mpeg", "mp3")
        files = {"file": (f"voice.{ext}", audio, content_type)}
        data = {"model": model, "response_format": "json"}
        if prompt:
            data["prompt"] = prompt
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            r = await self._http.post(f"{self.base_url}/audio/transcriptions", headers=headers, data=data, files=files)
        except httpx.HTTPError as exc:
            raise GeminiError(f"Couldn't reach the speech service ({exc.__class__.__name__}).", retryable=True)
        if r.status_code != 200:
            msg, code = _error(r)
            if code == "insufficient_quota":
                raise GeminiError("The OpenAI account has no credit left.", status=429)
            raise GeminiError(f"Speech to text failed ({r.status_code}: {msg[:150]})",
                              retryable=r.status_code in RETRY_STATUSES, status=r.status_code)
        return str((r.json() or {}).get("text") or "").strip()

    async def close(self) -> None:
        await self._http.aclose()
