"""Forward-only schema versions. Each entry runs once, in its own transaction."""

from __future__ import annotations

from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.database.models import Base

SCHEMA_VERSION = 2


def _apply(conn: Connection) -> None:
    current = 0
    if inspect(conn).has_table("schema_version"):
        current = conn.execute(text("SELECT MAX(version) FROM schema_version")).scalar() or 0
    else:
        conn.execute(text("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)"))
    if current > SCHEMA_VERSION:
        raise RuntimeError(f"database schema {current} is newer than this application ({SCHEMA_VERSION})")
    if current < 1:
        Base.metadata.create_all(conn)
        conn.execute(text("INSERT INTO schema_version (version) VALUES (2)"))
        return
    if current < 2:
        conn.execute(text("ALTER TABLE notifications ADD COLUMN channels JSON"))
        conn.execute(text("INSERT INTO schema_version (version) VALUES (2)"))


async def migrate(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(_apply)
