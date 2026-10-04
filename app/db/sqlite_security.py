"""SQLite deletion policy and filesystem permissions for credential storage."""

from pathlib import Path

from sqlalchemy import event


def configure_sqlite_security(engine, *, foreign_keys=False):
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def secure_connection(connection, record):
        connection.execute("PRAGMA secure_delete=ON")
        if foreign_keys:
            connection.execute("PRAGMA foreign_keys=ON")

    if engine.url.database not in {None, ":memory:"}:
        path = Path(engine.url.database).resolve()
        for candidate in (
            path,
            *(Path(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal")),
        ):
            if candidate.exists():
                candidate.chmod(0o600)
