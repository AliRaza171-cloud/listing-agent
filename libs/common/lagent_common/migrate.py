"""Tiny SQL migration runner: applies migrations/NNNN_*.sql once each, in order.

Runs at service startup. A Postgres advisory lock makes it safe when several
copies of a service start at the same moment.
"""
import logging
import zlib
from pathlib import Path

from sqlalchemy import text

log = logging.getLogger("lagent.migrate")


def run_migrations(engine, migrations_dir: str | Path, service: str) -> list[str]:
    files = sorted(Path(migrations_dir).glob("[0-9][0-9][0-9][0-9]_*.sql"))
    applied_now: list[str] = []
    lock_id = zlib.crc32(f"lagent-migrate-{service}".encode())
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:id)"), {"id": lock_id})
        try:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " filename VARCHAR PRIMARY KEY,"
                " applied_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'))"
            ))
            conn.commit()
            done = {row[0] for row in conn.execute(text("SELECT filename FROM schema_migrations"))}
            conn.commit()  # end the SELECT's auto-started transaction so conn.begin() below can start one
            for f in files:
                if f.name in done:
                    continue
                with conn.begin():
                    conn.exec_driver_sql(f.read_text(encoding="utf-8"))
                    conn.execute(text("INSERT INTO schema_migrations (filename) VALUES (:f)"), {"f": f.name})
                applied_now.append(f.name)
                log.info("[%s] applied migration %s", service, f.name)
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": lock_id})
            conn.commit()
    return applied_now
