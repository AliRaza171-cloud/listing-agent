"""Correlation IDs: one id follows a request through every service and event it causes."""
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware

correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)

HEADER = "X-Correlation-Id"


class CorrelationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        cid = request.headers.get(HEADER) or str(uuid.uuid4())
        token = correlation_id_var.set(cid)
        try:
            response = await call_next(request)
        finally:
            correlation_id_var.reset(token)
        response.headers[HEADER] = cid
        return response


def outgoing_headers(internal_token: str, user_id: str | None = None) -> dict:
    """Headers for a service -> service call (contracts/README.md §2)."""
    headers = {"X-Internal-Token": internal_token, HEADER: correlation_id_var.get() or str(uuid.uuid4())}
    if user_id:
        headers["X-User-Id"] = str(user_id)
    return headers
