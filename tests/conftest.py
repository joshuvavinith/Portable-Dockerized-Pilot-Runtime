from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent


def base_env(**overrides: str) -> dict[str, str]:
    env = {
        "IRIS_COUNTRY_CODE": "DE",
        "IRIS_REGION_CODE": "NW",
        "IRIS_SOURCE_ENDPOINT": "fixture://sites.geojson",
        "IRIS_OUTPUT_PATH": "/tmp/iris-test-output",
        "IRIS_MIGRATIONS_DIR": str(ROOT / "migrations"),
        "IRIS_FIXTURES_DIR": str(ROOT / "fixtures"),
        "POSTGRES_DB": "iris",
        "POSTGRES_USER": "iris",
        "POSTGRES_PASSWORD": "test-password",
        "POSTGRES_HOST": "localhost",
        "POSTGRES_PORT": "5432",
        "IRIS_DB_CONNECT_RETRIES": "0",
    }
    env.update(overrides)
    return env


@pytest.fixture()
def env_factory():
    return base_env


@pytest.fixture()
def test_db(tmp_path):
    """A brand-new database per test. Needs POSTGRES_* env (superuser); else skipped."""
    needed = ("POSTGRES_HOST", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
    if not all(os.environ.get(name) for name in needed):
        pytest.skip("integration tests need POSTGRES_* env vars (run via docker compose test)")
    admin = dict(
        host=os.environ["POSTGRES_HOST"],
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
    )
    name = f"iris_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dbname=os.environ["POSTGRES_DB"], autocommit=True, **admin) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')

    def make_env(**overrides: str) -> dict[str, str]:
        return base_env(
            POSTGRES_HOST=admin["host"],
            POSTGRES_PORT=str(admin["port"]),
            POSTGRES_USER=admin["user"],
            POSTGRES_PASSWORD=admin["password"],
            POSTGRES_DB=name,
            IRIS_OUTPUT_PATH=str(tmp_path / "out"),
            **overrides,
        )

    yield make_env

    with psycopg.connect(dbname=os.environ["POSTGRES_DB"], autocommit=True, **admin) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
