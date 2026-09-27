"""API Gateway — the only public entry point (contracts/README.md §2.1).

/api/<service>/<path>  ->  <service> /<path>
- verifies the user's JWT (except public routes) and forwards X-User-Id
- adds X-Internal-Token and X-Correlation-Id
- never forwards /internal/ paths
- rate limits per client IP (Redis, fixed 1-minute window)
"""
import logging
import time

import httpx
import redis.asyncio as redis
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from jose import JWTError, jwt
from pydantic_settings import BaseSettings

from lagent_common.correlation import HEADER as CORRELATION_HEADER, correlation_id_var
from lagent_common.service import create_service

log = logging.getLogger("lagent.gateway")


class Settings(BaseSettings):
    JWT_SECRET: str
    INTERNAL_TOKEN: str
    REDIS_URL: str = "redis://redis:6379/0"
    RATE_LIMIT_PER_MINUTE: int = 120
    ALLOWED_ORIGINS: list[str] = ["http://localhost:3000"]
    AUTH_URL: str = "http://auth:8000"
    CATALOG_URL: str = "http://catalog:8000"
    AI_URL: str = "http://ai:8000"
    VOICE_URL: str = "http://voice:8000"
    STORE_URL: str = "http://store:8000"
    BILLING_URL: str = "http://billing:8000"
    NOTIFICATION_URL: str = "http://notification:8000"
    # publisher has no public routes — it is reached only through events and /internal calls


settings = Settings()

ROUTES = {
    "auth": settings.AUTH_URL,
    "catalog": settings.CATALOG_URL,
    "ai": settings.AI_URL,
    "voice": settings.VOICE_URL,
    "store": settings.STORE_URL,
    "billing": settings.BILLING_URL,
    "notification": settings.NOTIFICATION_URL,
}
PUBLIC = {
    ("auth", "POST", "register"), ("auth", "POST", "login"),
    # payment providers call these; billing checks their signatures
    ("billing", "POST", "webhooks/stripe"), ("billing", "POST", "webhooks/safepay"),
    ("billing", "GET", "payments/safepay/return"), ("billing", "POST", "payments/safepay/return"),
    # one-click store connections: WooCommerce posts keys here, Shopify sends the seller back here;
    # the store service only accepts them for a pending, one-time connect request
    ("store", "POST", "connect/woocommerce/callback"), ("store", "GET", "connect/shopify/callback"),
    ("store", "POST", "shopify/webhooks"),   # verified with Shopify's HMAC header
}
# X- headers are normally dropped (they could spoof ours); these provider signatures are kept.
PASS_HEADERS = {"x-sfpy-signature", "x-shopify-hmac-sha256", "x-shopify-topic", "x-shopify-shop-domain"}
HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade", "host", "content-length"}

_client = httpx.AsyncClient(timeout=30)
_redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
router = APIRouter()


async def _rate_limited(ip: str) -> bool:
    key = f"ratelimit:{ip}:{int(time.time() // 60)}"
    count = await _redis.incr(key)
    if count == 1:
        await _redis.expire(key, 70)
    return count > settings.RATE_LIMIT_PER_MINUTE


def _user_from_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "Sign in required.")
    try:
        claims = jwt.decode(auth[7:], settings.JWT_SECRET, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(401, "Your session expired — sign in again.")
    if claims.get("type") != "access" or not claims.get("sub"):
        raise HTTPException(401, "Invalid token.")
    return claims["sub"]


@router.api_route("/api/{service}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(service: str, path: str, request: Request):
    base = ROUTES.get(service)
    if base is None:
        raise HTTPException(404, "Unknown service.")
    if path.startswith("internal") or "/internal/" in f"/{path}/":
        raise HTTPException(404, "Not found.")

    client_ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    if await _rate_limited(client_ip):
        raise HTTPException(429, "Too many requests — slow down a little.")

    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in HOP_BY_HOP and (not k.lower().startswith("x-") or k.lower() in PASS_HEADERS)}
    headers["X-Internal-Token"] = settings.INTERNAL_TOKEN
    headers[CORRELATION_HEADER] = correlation_id_var.get() or ""
    public_media = service == "catalog" and request.method == "GET" and path.startswith("media/")
    if (service, request.method, path) not in PUBLIC and not public_media:
        headers["X-User-Id"] = _user_from_token(request)

    upstream = await _client.request(
        request.method, f"{base}/{path}", params=request.query_params, headers=headers, content=await request.body()
    )
    out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP}
    return Response(content=upstream.content, status_code=upstream.status_code, headers=out_headers)


app = create_service("gateway", routers=[router])
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
    expose_headers=[CORRELATION_HEADER],
)
