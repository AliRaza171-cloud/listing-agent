"""Product photos: upload and serve.

POST /uploads          raw image bytes (Content-Type image/jpeg|png|webp) -> {"url": "/api/catalog/media/<name>"}
GET  /media/<name>     the file (the gateway lets this one through without a login, so <img> tags work;
                       names are random, so photos can't be guessed or listed)

Files live on the `media` volume (MEDIA_DIR). Swap for S3/R2 later without changing the URL shape.
"""
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from lagent_common.internal import current_user_id, require_internal

from app.core import settings

router = APIRouter(dependencies=[Depends(require_internal)])

TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
MEDIA_TYPES = {v: k for k, v in TYPES.items()}
NAME = re.compile(r"^[0-9a-f]{32}\.(jpg|png|webp)$")


def _media_dir() -> Path:
    path = Path(settings.MEDIA_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _looks_like(ext: str, head: bytes) -> bool:
    if ext == "jpg":
        return head.startswith(b"\xff\xd8\xff")
    if ext == "png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    return head[:4] == b"RIFF" and head[8:12] == b"WEBP"


@router.post("/uploads", status_code=201)
async def upload(request: Request, _user=Depends(current_user_id)):
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    ext = TYPES.get(content_type)
    if not ext:
        raise HTTPException(415, "Use a JPG, PNG or WebP photo.")
    data = await request.body()
    if not data:
        raise HTTPException(422, "The photo was empty.")
    if len(data) > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"Photos can be up to {settings.MAX_UPLOAD_MB} MB.")
    if not _looks_like(ext, data[:16]):
        raise HTTPException(415, "That file isn't a valid image.")
    name = f"{uuid.uuid4().hex}.{ext}"
    (_media_dir() / name).write_bytes(data)
    return {"url": f"/api/catalog/media/{name}"}


@router.get("/media/{name}")
def media(name: str):
    if not NAME.match(name):
        raise HTTPException(404, "Not found.")
    path = _media_dir() / name
    if not path.is_file():
        raise HTTPException(404, "Not found.")
    return FileResponse(path, media_type=MEDIA_TYPES[name.rsplit(".", 1)[1]],
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})
