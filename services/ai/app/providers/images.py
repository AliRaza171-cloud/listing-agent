"""Load product photos for the AI.

Photos live in catalog (/api/catalog/media/<name>). The AI service fetches them
over the internal network and shrinks them: Gemini accepts at most 20 MB per
request, and a 1280 px JPEG is plenty to recognise a product.
"""
from __future__ import annotations

import io
import logging

import httpx
from PIL import Image, ImageOps

log = logging.getLogger("lagent.ai.images")

MAX_SIDE = 1280
MAX_PHOTOS = 6


def shrink(data: bytes) -> bytes:
    """-> JPEG bytes, longest side <= MAX_SIDE, EXIF rotation applied, transparency flattened."""
    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        im.thumbnail((MAX_SIDE, MAX_SIDE))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=85, optimize=True)
        return out.getvalue()


async def load_photos(urls: list[str], catalog_url: str, headers: dict) -> list[bytes]:
    """Fetch up to MAX_PHOTOS photos; unreadable ones are skipped (logged), not fatal."""
    photos: list[bytes] = []
    async with httpx.AsyncClient(timeout=20) as client:
        for url in urls[:MAX_PHOTOS]:
            if url.startswith("/api/catalog/"):
                target, h = f"{catalog_url.rstrip('/')}/{url[len('/api/catalog/'):]}", headers
            else:
                target, h = url, {}
            try:
                r = await client.get(target, headers=h)
                r.raise_for_status()
                photos.append(shrink(r.content))
            except Exception as exc:  # noqa: BLE001 — one bad photo shouldn't sink the listing
                log.warning("skipping photo %s: %s", url, exc)
    return photos
