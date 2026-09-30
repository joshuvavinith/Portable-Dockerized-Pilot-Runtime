from __future__ import annotations

import http.server
import json
import threading

import psycopg
import pytest

from iris import db as db_module
from iris.config import Settings
from iris.db import DatabaseUnavailable, connect
from iris.migrate import MigrationError, discover
from iris.sources import SourceError, load_source
from tests.conftest import ROOT


def settings(env_factory, **extra):
    return Settings.from_env(env_factory(**extra))


# ---------------------------------------------------------------- sources
def test_fixture_source_loads(env_factory):
    data = load_source(settings(env_factory))
    assert data["type"] == "FeatureCollection" and len(data["features"]) == 5


@pytest.mark.parametrize("name", ["../pyproject.toml", "a/b.geojson", ".hidden", "..%2f"])
def test_fixture_path_traversal_rejected(env_factory, name):
    with pytest.raises(SourceError):
        load_source(settings(env_factory, IRIS_SOURCE_ENDPOINT=f"fixture://{name}"))


def test_missing_fixture_rejected(env_factory):
    with pytest.raises(SourceError, match="not found"):
        load_source(settings(env_factory, IRIS_SOURCE_ENDPOINT="fixture://nope.geojson"))


def test_non_featurecollection_rejected(env_factory, tmp_path):
    (tmp_path / "x.geojson").write_text(json.dumps({"type": "Feature"}))
    with pytest.raises(SourceError, match="FeatureCollection"):
        load_source(settings(env_factory, IRIS_FIXTURES_DIR=str(tmp_path),
                             IRIS_SOURCE_ENDPOINT="fixture://x.geojson"))


@pytest.fixture()
def http_server():
    body = (ROOT / "fixtures" / "sites.geojson").read_bytes()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/ok"):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/big"):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b" " * 5000)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_http_source_loads(env_factory, http_server):
    data = load_source(settings(env_factory, IRIS_SOURCE_ENDPOINT=f"{http_server}/ok"))
    assert len(data["features"]) == 5


def test_http_404_is_a_source_error(env_factory, http_server):
    with pytest.raises(SourceError, match="could not fetch"):
        load_source(settings(env_factory, IRIS_SOURCE_ENDPOINT=f"{http_server}/missing"))


def test_http_size_limit_enforced(env_factory, http_server):
    with pytest.raises(SourceError, match="exceeds"):
        load_source(settings(env_factory, IRIS_SOURCE_ENDPOINT=f"{http_server}/big",
                             IRIS_SOURCE_MAX_BYTES="1000"))


# ------------------------------------------------------- migration files
def test_repo_migrations_are_ordered_and_checksummed():
    found = discover(ROOT / "migrations")
    assert [m.version for m in found] == ["001_init.sql", "002_pilot_runs.sql"]
    assert all(len(m.checksum) == 64 for m in found)


def test_bad_migration_name_rejected(tmp_path):
    (tmp_path / "init.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="bad migration file name"):
        discover(tmp_path)


def test_duplicate_prefix_rejected(tmp_path):
    (tmp_path / "001_a.sql").write_text("SELECT 1;")
    (tmp_path / "001_b.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="same numeric prefix"):
        discover(tmp_path)


def test_empty_or_missing_dir_rejected(tmp_path):
    with pytest.raises(MigrationError):
        discover(tmp_path)
    with pytest.raises(MigrationError):
        discover(tmp_path / "missing")


# ---------------------------------------------------------- db connect
class _Err(psycopg.OperationalError):
    """libpq-style connection error: message only, no SQLSTATE."""

    def __init__(self, message="connection refused", sqlstate=None):
        self._sqlstate = sqlstate
        super().__init__(message)

    @property
    def sqlstate(self):
        return self._sqlstate


def test_connect_retries_until_ready(env_factory, monkeypatch):
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        if len(calls) < 3:
            raise _Err("connection refused")
        return "connection"

    monkeypatch.setattr(db_module.psycopg, "connect", fake)
    monkeypatch.setattr(db_module.time, "sleep", lambda s: None)
    s = settings(env_factory, IRIS_DB_CONNECT_RETRIES="5")
    assert connect(s) == "connection" and len(calls) == 3
    assert calls[0]["password"] == "test-password"


def test_connect_fails_fast_on_bad_credentials(env_factory, monkeypatch):
    calls = []

    def fake(**kwargs):
        calls.append(1)
        raise _Err('connection failed: FATAL:  password authentication failed for user "x"')

    monkeypatch.setattr(db_module.psycopg, "connect", fake)
    with pytest.raises(DatabaseUnavailable, match="password authentication failed"):
        connect(settings(env_factory, IRIS_DB_CONNECT_RETRIES="5"))
    assert len(calls) == 1


def test_connect_gives_up_after_bounded_retries(env_factory, monkeypatch):
    monkeypatch.setattr(db_module.psycopg, "connect", lambda **k: (_ for _ in ()).throw(_Err("connection refused")))
    monkeypatch.setattr(db_module.time, "sleep", lambda s: None)
    with pytest.raises(DatabaseUnavailable, match="after 3 attempts"):
        connect(settings(env_factory, IRIS_DB_CONNECT_RETRIES="2"))


@pytest.mark.parametrize(
    "message, fatal",
    [
        ("connection refused", False),
        ("could not translate host name \"db\" to address", False),
        ("FATAL:  the database system is starting up", False),
        ("FATAL:  password authentication failed for user \"iris\"", True),
        ("FATAL:  database \"nope\" does not exist", True),
        ("FATAL:  role \"nope\" does not exist", True),
    ],
)
def test_error_classification(message, fatal):
    assert db_module.is_fatal(_Err(message)) is fatal
