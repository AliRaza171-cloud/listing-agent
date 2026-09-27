"""Database helpers shared by the stateful services (each with its OWN database)."""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker


def _with_driver(url: str) -> str:
    """Name the driver explicitly. A bare postgresql:// lets SQLAlchemy pick its default,
    which changed from psycopg2 to psycopg 3 in SQLAlchemy 2.1."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg2://" + url[len(prefix):]
    return url


def make_db(database_url: str):
    engine = create_engine(_with_driver(database_url), pool_pre_ping=True)  # Neon sleeps; pre-ping avoids dead connections
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    return engine, SessionLocal, get_db


def mark_processed(db: Session, event: dict) -> bool:
    """Idempotency for at-least-once delivery.

    Call inside the same transaction as the handler's own changes:
        if not mark_processed(db, event): return   # seen before
        ...changes...
        db.commit()
    If the handler fails, the rollback also forgets the mark, so the retry runs again.
    Every stateful service's first migration creates `processed_events`.
    """
    result = db.execute(
        text("INSERT INTO processed_events (event_id, event_type) VALUES (:id, :type) ON CONFLICT DO NOTHING"),
        {"id": event["id"], "type": event["type"]},
    )
    return result.rowcount == 1
