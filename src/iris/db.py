"""Database connection with bounded, fail-fast-on-auth retry."""

from __future__ import annotations

import logging
import time

import psycopg

from iris.config import Settings

log = logging.getLogger(__name__)

# libpq does not attach a SQLSTATE to most connection-time failures, so we also
# classify by message. Retryable: socket not open yet (connection refused, DNS,
# timeout) and "the database system is starting up". Fatal: any other server
# FATAL (bad password, unknown role/database, pg_hba rejection).
_RETRYABLE_SQLSTATES = {None, "57P03"}
_STARTUP_MARKERS = ("the database system is", "not yet accepting connections")


class DatabaseUnavailable(RuntimeError):
    pass


def is_fatal(error: psycopg.OperationalError) -> bool:
    """True when retrying cannot help (credentials/database are wrong)."""
    if error.sqlstate not in _RETRYABLE_SQLSTATES:
        return True
    message = str(error)
    return "FATAL" in message and not any(m in message for m in _STARTUP_MARKERS)


def connect(settings: Settings) -> psycopg.Connection:
    attempts = settings.db_connect_retries + 1
    last: psycopg.OperationalError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return psycopg.connect(
                dbname=settings.postgres_db,
                user=settings.postgres_user,
                password=settings.postgres_password,
                host=settings.postgres_host,
                port=settings.postgres_port,
                connect_timeout=settings.db_connect_timeout_s,
                application_name="iris",
            )
        except psycopg.OperationalError as error:
            last = error
            if is_fatal(error):
                raise DatabaseUnavailable(
                    f"database rejected the connection: {str(error).strip()}"
                ) from error
            if attempt < attempts:
                log.info(
                    "database not ready (attempt %d/%d), retrying", attempt, attempts
                )
                time.sleep(settings.db_connect_retry_delay_s)
    raise DatabaseUnavailable(
        f"database {settings.postgres_host}:{settings.postgres_port} not reachable "
        f"after {attempts} attempts: {str(last).strip()}"
    ) from last
