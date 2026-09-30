"""Load product photos for the AI.

Photos live in catalog (/api/catalog/media/<name>). The AI service fetches them
over the internal network and shrinks them: Gemini accepts at most 20 MB per
request, and a 1280 px JPEG is plenty to recognise a product.
"""
from __future__ import annotations

import asyncio
import io
import re
import logging

import httpx
from PIL import Image, ImageOps

log = logging.getLogger("lagent.ai.images")

MAX_SIDE = 1280
MAX_PHOTOS = 6


def shrink(data: bytes, max_side: int = MAX_SIDE) -> bytes:
    """-> JPEG bytes, longest side <= max_side, EXIF rotation applied, transparency flattened."""
    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=85, optimize=True)
        return out.getvalue()


async def load_photos(urls: list[str], catalog_url: str, headers: dict) -> list[bytes]:
    """Fetch up to MAX_PHOTOS photos; unreadable ones are skipped (logged), not fatal."""
    return [p for p in await _load(urls[:MAX_PHOTOS], catalog_url, headers, MAX_SIDE) if p is not None]


OWN_MEDIA = re.compile(r"/api/catalog/media/[A-Za-z0-9_-]{1,80}\.(?:jpg|jpeg|png|webp)")
THUMB_SIDE = 512
MAX_THUMBS = 50


async def load_thumbs(urls: list[str], catalog_url: str, headers: dict) -> list[bytes | None]:
    """Small copies of up to MAX_THUMBS of the seller's uploaded photos, in order (None = unreadable).
    Only our own uploads (/api/catalog/media/...) are fetched — never an outside address."""
    urls = urls[:MAX_THUMBS]
    own = [u if OWN_MEDIA.fullmatch(u or "") else None for u in urls]
    loaded = await _load([u for u in own if u], catalog_url, headers, THUMB_SIDE)
    it = iter(loaded)
    return [next(it) if u else None for u in own]


async def _load(urls: list[str], catalog_url: str, headers: dict, max_side: int) -> list[bytes | None]:
    """Fetched a few at a time, returned in the same order; a failed photo is None."""
    gate = asyncio.Semaphore(6)

    async def one(client: httpx.AsyncClient, url: str) -> bytes | None:
        if url.startswith("/api/catalog/"):
            target, h = f"{catalog_url.rstrip('/')}/{url[len('/api/catalog/'):]}", headers
        else:
            target, h = url, {}
        async with gate:
            try:
                r = await client.get(target, headers=h)
                r.raise_for_status()
                return await asyncio.to_thread(shrink, r.content, max_side)
            except Exception as exc:  # noqa: BLE001 — one bad photo shouldn't sink the listing
                log.warning("skipping photo %s: %s", url, exc)
                return None

    async with httpx.AsyncClient(timeout=20) as client:
        return list(await asyncio.gather(*(one(client, u) for u in urls)))
