from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from pathlib import Path
from app.db.sqlite_security import configure_sqlite_security


type SessionFactory = sessionmaker[Session]


def create_database_engine(database_url: str) -> Engine:
    options = (
        {"connect_args": {"check_same_thread": False}}
        if database_url.startswith("sqlite")
        else {}
    )
    engine = create_engine(database_url, hide_parameters=True, **options)
    configure_sqlite_security(engine, foreign_keys=True)
    if database_url.startswith("sqlite") and engine.url.database not in {
        None,
        ":memory:",
    }:
        database_path = Path(engine.url.database).resolve()
        with engine.connect():
            database_path.chmod(0o600)
    return engine


def create_session_factory(engine: Engine) -> SessionFactory:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
