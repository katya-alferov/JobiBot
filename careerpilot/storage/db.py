"""Engine and session helpers."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from careerpilot.config import PROJECT_ROOT, Settings
from careerpilot.storage.models import Base


def make_engine(url: str) -> Engine:
    engine = create_engine(url, future=True)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    return engine


def run_migrations(url: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    from sqlalchemy import inspect

    probe = create_engine(url)
    try:
        tables = set(inspect(probe).get_table_names())
    finally:
        probe.dispose()
    if "job_posting" in tables and "alembic_version" not in tables:
        command.stamp(cfg, "head")  # created by create_all (tests/demo); adopt it
    else:
        command.upgrade(cfg, "head")


class Database:
    def __init__(self, settings: Settings, *, migrate: bool = True):
        settings.ensure_dirs()
        self.settings = settings
        self.url = settings.resolved_database_url()
        if migrate:
            run_migrations(self.url)
        self.engine = make_engine(self.url)
        if not migrate:
            Base.metadata.create_all(self.engine)
        self._factory = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
