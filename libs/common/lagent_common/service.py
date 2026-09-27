"""One way to build every service, so they all start, log and report health the same.

    bus = EventBus(settings.REDIS_URL, "catalog")
    bus.subscribe("listing.generated", on_listing_generated)
    app = create_service("catalog", routers=[products.router], bus=bus,
                         engine=engine, migrations_dir="migrations")
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI
from sqlalchemy import text

from lagent_common.bus import EventBus
from lagent_common.correlation import CorrelationMiddleware, correlation_id_var
from lagent_common.migrate import run_migrations


class _CorrelationFilter(logging.Filter):
    def filter(self, record):
        record.correlation_id = correlation_id_var.get() or "-"
        return True


def setup_logging(service: str) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(_CorrelationFilter())
    handler.setFormatter(logging.Formatter(
        f"%(asctime)s %(levelname)s [{service}] [%(correlation_id)s] %(name)s: %(message)s"
    ))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


def create_service(
    name: str,
    *,
    routers: list[APIRouter] = (),
    bus: EventBus | None = None,
    engine=None,
    migrations_dir: str | Path | None = None,
    background: list = (),
) -> FastAPI:
    """`background`: async functions taking a stop Event, run for the service's lifetime
    (e.g. billing's expired-reservation sweeper)."""
    setup_logging(name)
    log = logging.getLogger(f"lagent.{name}")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if engine is not None and migrations_dir is not None:
            await asyncio.to_thread(run_migrations, engine, migrations_dir, name)
        stop = asyncio.Event()
        tasks = [asyncio.create_task(bus.run(stop))] if bus else []
        tasks += [asyncio.create_task(job(stop)) for job in background]
        log.info("%s started", name)
        yield
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if bus:
            await bus.close()

    app = FastAPI(title=f"listing-agent {name}", lifespan=lifespan)
    app.add_middleware(CorrelationMiddleware)

    @app.get("/health", tags=["health"])
    async def health():
        checks = {}
        if engine is not None:
            def _db():
                with engine.connect() as c:
                    c.execute(text("SELECT 1"))
            await asyncio.to_thread(_db)
            checks["database"] = "ok"
        if bus is not None:
            await bus.redis.ping()
            checks["redis"] = "ok"
        return {"service": name, "status": "ok", **checks}

    for router in routers:
        app.include_router(router)
    return app
