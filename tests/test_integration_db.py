"""Integration tests against a real PostgreSQL/PostGIS (skipped without POSTGRES_* env)."""

from __future__ import annotations

import json
import shutil

import psycopg
import pytest

from iris import smoke
from iris.config import Settings
from iris.db import connect
from iris.ingest import IngestError
from iris.migrate import MigrationError, apply_migrations
from iris.worker import UNCERTAINTY_NOTICE, run, summary_path
from tests.conftest import ROOT

pytestmark = pytest.mark.integration


def scalar(settings, sql, *params):
    with connect(settings) as conn:
        return conn.execute(sql, params).fetchone()[0]


def test_migrations_apply_once_in_order_and_are_idempotent(test_db):
    s = Settings.from_env(test_db())
    with connect(s) as conn:
        assert apply_migrations(conn, s.migrations_dir) == ["001_init.sql", "002_pilot_runs.sql"]
        assert apply_migrations(conn, s.migrations_dir) == []


def test_edited_migration_is_refused(test_db, tmp_path):
    copy = tmp_path / "migrations"
    shutil.copytree(ROOT / "migrations", copy)
    s = Settings.from_env(test_db(IRIS_MIGRATIONS_DIR=str(copy)))
    with connect(s) as conn:
        apply_migrations(conn, copy)
        (copy / "001_init.sql").write_text((copy / "001_init.sql").read_text() + "\n-- edited\n")
        with pytest.raises(MigrationError, match="checksum mismatch"):
            apply_migrations(conn, copy)


def test_failed_migration_leaves_nothing_half_applied(test_db, tmp_path):
    copy = tmp_path / "migrations"
    copy.mkdir()
    (copy / "001_ok.sql").write_text("CREATE TABLE t1 (id int);")
    (copy / "002_bad.sql").write_text("CREATE TABLE t2 (id int); SELECT 1/0;")
    s = Settings.from_env(test_db(IRIS_MIGRATIONS_DIR=str(copy)))
    with connect(s) as conn:
        with pytest.raises(psycopg.errors.DivisionByZero):
            apply_migrations(conn, copy)
        assert conn.execute("SELECT to_regclass('t2')").fetchone()[0] is None
        assert [r[0] for r in conn.execute("SELECT version FROM schema_migrations")] == ["001_ok.sql"]


def test_switching_de_nw_to_nl_li_needs_no_code_change(test_db):
    de, nl = _make_two_scopes(test_db)

    first = run(de)
    assert first["sites_ingested"] == 2 and first["scoped_site_count"] == 2
    assert first["migrations_applied_this_run"] == ["001_init.sql", "002_pilot_runs.sql"]

    second = run(nl)  # same code, same DB, different env only
    assert second["country_code"] == "NL" and second["region_code"] == "LI"
    assert second["sites_ingested"] == 2 and second["migrations_applied_this_run"] == []


def _make_two_scopes(test_db):
    env = test_db()  # one factory call == one database; reuse it for both scopes
    de = Settings.from_env({**env, "IRIS_COUNTRY_CODE": "DE", "IRIS_REGION_CODE": "NW"})
    nl = Settings.from_env({**env, "IRIS_COUNTRY_CODE": "NL", "IRIS_REGION_CODE": "LI"})
    return de, nl


def test_country_scoped_identity_same_id_coexists(test_db):
    de, nl = _make_two_scopes(test_db)
    run(de)
    run(nl)
    assert scalar(de, "SELECT count(*) FROM sites WHERE id = 'site-001'") == 2
    assert scalar(de, "SELECT count(*) FROM sites WHERE country_code = 'DE'") == 2
    assert scalar(de, "SELECT count(*) FROM sites WHERE country_code = 'NL'") == 2
    assert scalar(de, "SELECT count(*) FROM sites WHERE country_code = 'DE' AND region_code = 'BY'") == 0
    assert summary_path(de) != summary_path(nl)


def test_rerun_is_idempotent(test_db):
    de, _ = _make_two_scopes(test_db)
    run(de)
    run(de)
    assert scalar(de, "SELECT count(*) FROM sites") == 2
    assert scalar(de, "SELECT count(*) FROM pilot_runs WHERE status = 'succeeded'") == 2


def test_bad_batch_is_atomic_and_failure_is_logged(test_db, tmp_path):
    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    collection = json.loads((ROOT / "fixtures" / "sites.geojson").read_text())
    nw = [f for f in collection["features"] if f["properties"]["region_code"] == "NW"]
    del nw[1]["properties"]["source_date"]
    collection["features"] = nw
    (fixtures / "sites.geojson").write_text(json.dumps(collection))
    s = Settings.from_env({**test_db(IRIS_FIXTURES_DIR=str(fixtures))})

    with pytest.raises(IngestError, match="missing source_date"):
        run(s)
    assert scalar(s, "SELECT count(*) FROM sites") == 0  # valid first feature not kept
    assert scalar(s, "SELECT status FROM pilot_runs") == "failed"
    assert not summary_path(s).exists()


def test_database_constraints_guard_the_contracts(test_db):
    s = Settings.from_env(test_db())
    run(s)
    good = "ST_GeomFromText('POLYGON((0 0,0 1,1 1,0 0))', %s)"
    insert = (
        "INSERT INTO sites (country_code,id,region_code,source_endpoint,source_date,"
        "area_m2,uncertainty_m2,geom) VALUES (%s,'x','NW','e','2026-01-01',1,0," + good + ")"
    )
    with connect(s) as conn:
        with pytest.raises(psycopg.errors.NotNullViolation):
            conn.execute(insert, (None, 4326))
        conn.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(insert, ("de", 4326))
        conn.rollback()
        with pytest.raises(psycopg.Error):  # wrong SRID rejected by geometry typmod
            conn.execute(insert, ("DE", 3857))


def test_stored_area_agrees_with_postgis_geography(test_db):
    de, nl = _make_two_scopes(test_db)
    run(de)
    run(nl)
    with connect(de) as conn:
        rows = conn.execute("SELECT id, area_m2, ST_Area(geom::geography) FROM sites").fetchall()
    assert len(rows) == 4
    for site_id, declared, geodesic in rows:
        assert geodesic == pytest.approx(declared, rel=0.02), site_id


def test_smoke_passes_after_worker_and_fails_when_it_should(test_db):
    de, _ = _make_two_scopes(test_db)
    with connect(de) as conn:
        checks = smoke.run_checks(de, conn)  # nothing migrated yet
    assert not all(c.ok for c in checks)

    run(de)
    with connect(de) as conn:
        checks = smoke.run_checks(de, conn)
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]

    summary_path(de).unlink()
    with connect(de) as conn:
        assert not all(c.ok for c in smoke.run_checks(de, conn))


def test_smoke_fails_for_scope_that_was_never_ingested(test_db):
    de, nl = _make_two_scopes(test_db)
    run(de)
    with connect(nl) as conn:
        failed = [c.name for c in smoke.run_checks(nl, conn) if not c.ok]
    assert any("sites exist for scope NL/LI" in name for name in failed)


def test_summary_carries_uncertainty_notice_and_no_secrets(test_db):
    de, _ = _make_two_scopes(test_db)
    run(de)
    text = summary_path(de).read_text()
    assert json.loads(text)["notice"] == UNCERTAINTY_NOTICE
    assert de.postgres_password not in text


def test_migration_sql_is_safe_to_execute_directly_twice(test_db):
    """Local tooling may run the raw SQL again; IF NOT EXISTS keeps that harmless."""
    s = Settings.from_env(test_db())
    with connect(s) as conn:
        for _ in range(2):
            for path in sorted(s.migrations_dir.glob("*.sql")):
                conn.execute(path.read_text(encoding="utf-8"))
                conn.commit()


def test_run_counts_cannot_be_negative(test_db):
    s = Settings.from_env(test_db())
    run(s)
    with connect(s) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO pilot_runs (country_code, region_code, source_endpoint, status,"
                " features_seen, started_at) VALUES ('DE','NW','e','failed',-1, now())"
            )
