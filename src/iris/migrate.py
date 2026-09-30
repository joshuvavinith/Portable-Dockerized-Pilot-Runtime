"""Deterministic, checksummed, lock-protected SQL migrations."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg

MIGRATION_NAME_RE = re.compile(r"^(\d{3,})_[a-z0-9_]+\.sql$")
ADVISORY_LOCK_KEY = 0x49524953  # "IRIS"


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    checksum: str


def discover(migrations_dir: Path) -> list[Migration]:
    """Return migrations ordered by numeric prefix; reject ambiguous layouts."""
    if not migrations_dir.is_dir():
        raise MigrationError(f"migrations directory not found: {migrations_dir}")
    found: list[tuple[int, Migration]] = []
    for path in migrations_dir.glob("*.sql"):
        match = MIGRATION_NAME_RE.match(path.name)
        if not match:
            raise MigrationError(
                f"bad migration file name {path.name!r}; expected NNN_snake_case.sql"
            )
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        found.append((int(match.group(1)), Migration(path.name, path, checksum)))
    if not found:
        raise MigrationError(f"no migrations found in {migrations_dir}")
    numbers = [number for number, _ in found]
    if len(numbers) != len(set(numbers)):
        raise MigrationError("two migrations share the same numeric prefix")
    return [migration for _, migration in sorted(found, key=lambda item: item[0])]


def apply_migrations(connection: psycopg.Connection, migrations_dir: Path) -> list[str]:
    """Apply pending migrations; return the versions applied by *this* call.

    * ordered by numeric prefix (same result on every host)
    * one transaction per migration (a failure leaves no half-applied file)
    * an advisory lock serialises concurrent workers
    * applied files are checksummed; edited history is refused, not ignored
    """
    migrations = discover(migrations_dir)
    connection.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
    connection.commit()
    applied_now: list[str] = []
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    TEXT PRIMARY KEY,
                checksum   TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        connection.commit()
        applied = dict(
            connection.execute("SELECT version, checksum FROM schema_migrations").fetchall()
        )
        connection.commit()

        known = {migration.version for migration in migrations}
        unknown = sorted(set(applied) - known)
        if unknown:
            raise MigrationError(
                f"database has migrations this build does not know: {unknown}"
            )
        for migration in migrations:
            recorded = applied.get(migration.version)
            if recorded is not None:
                if recorded != migration.checksum:
                    raise MigrationError(
                        f"{migration.version} was modified after being applied "
                        "(checksum mismatch); add a new migration instead"
                    )
                continue
            with connection.transaction():
                connection.execute(migration.path.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                    (migration.version, migration.checksum),
                )
            applied_now.append(migration.version)
        return applied_now
    finally:
        connection.rollback()
        connection.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
        connection.commit()
