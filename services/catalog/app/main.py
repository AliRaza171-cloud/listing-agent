from pathlib import Path

from lagent_common.service import create_service

from app import handlers
from app.core import bus, engine
from app.media import router as media_router
from app.routes import router

handlers.register(bus)

app = create_service(
    "catalog", routers=[router, media_router], bus=bus, engine=engine,
    migrations_dir=Path(__file__).parent.parent / "migrations",
)
