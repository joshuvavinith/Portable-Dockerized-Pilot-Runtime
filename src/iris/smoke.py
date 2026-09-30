"""Smoke test: proves the stack works end to end for the *configured* scope.

Run after the worker (compose does this via `depends_on`):

    docker compose --profile smoke run --rm smoke
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass

import psycopg

from iris.config import ConfigError, Settings
from iris.db import DatabaseUnavailable, connect
from iris.migrate import MigrationError, discover
from iris.worker import UNCERTAINTY_NOTICE, summary_path

MIN_POSTGRES = 160000
MIN_POSTGIS = (3, 4)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


def run_checks(settings: Settings, connection: psycopg.Connection) -> list[Check]:
    checks: list[Check] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append(Check(name, bool(ok), detail))

    country, region = settings.country_code, settings.region_code

    def fetch(sql: str, *params):
        """Run a query; on error roll back and return None so a check fails cleanly."""
        try:
            row = connection.execute(sql, params).fetchone()
            connection.commit()
            return row
        except psycopg.Error:
            connection.rollback()
            return None

    version_num = int(connection.execute("SHOW server_version_num").fetchone()[0])
    connection.commit()
    add("PostgreSQL >= 16", version_num >= MIN_POSTGRES, f"server_version_num={version_num}")

    row = fetch("SELECT PostGIS_Lib_Version()")
    postgis = row[0] if row else None
    postgis_ok = bool(postgis) and (
        tuple(int(p) for p in re.findall(r"\d+", postgis)[:2]) >= MIN_POSTGIS
    )
    add("PostGIS >= 3.4", postgis_ok, f"PostGIS {postgis}")

    try:
        expected = {m.version: m.checksum for m in discover(settings.migrations_dir)}
        applied = dict(
            connection.execute("SELECT version, checksum FROM schema_migrations").fetchall()
        )
        connection.commit()
        add(
            "all migrations applied, checksums match",
            applied == expected,
            f"{len(applied)}/{len(expected)} applied",
        )
    except (MigrationError, psycopg.Error) as error:
        connection.rollback()
        add("all migrations applied, checksums match", False, str(error).strip())

    geom_info = fetch(
        """
        SELECT type, srid FROM geometry_columns
        WHERE f_table_name = 'sites' AND f_geometry_column = 'geom'
        """
    )
    add(
        "sites.geom is geometry(Polygon, 4326)",
        geom_info is not None and tuple(geom_info) == ("POLYGON", 4326),
        f"found {geom_info}",
    )

    rows = connection.execute(
        """
        SELECT table_name, is_nullable FROM information_schema.columns
        WHERE table_schema = 'public' AND column_name = 'country_code'
          AND table_name IN ('sites', 'pilot_runs')
        """
    ).fetchall()
    connection.commit()
    nullable = [name for name, flag in rows if flag == "YES"]
    add(
        "country_code is NOT NULL on business tables",
        len(rows) == 2 and not nullable,
        f"tables found={len(rows)}, nullable in {nullable}",
    )

    row = fetch(
        "SELECT count(*) FROM sites WHERE country_code = %s AND region_code = %s",
        country,
        region,
    )
    scoped = row[0] if row else 0
    add(f"sites exist for scope {country}/{region}", scoped > 0, f"{scoped} rows")

    row = fetch("SELECT count(*) FROM sites WHERE NOT ST_IsValid(geom)")
    add("all stored geometries are valid", row is not None and row[0] == 0, f"{row}")

    last = fetch(
        """
        SELECT status, sites_ingested FROM pilot_runs
        WHERE country_code = %s AND region_code = %s
        ORDER BY finished_at DESC, run_id DESC LIMIT 1
        """,
        country,
        region,
    )
    add("latest worker run for scope succeeded", bool(last and last[0] == "succeeded"), f"{last}")

    path = summary_path(settings)
    summary = None
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        add("output summary is written and readable", False, f"{path}: {error}")
    if summary is not None:
        consistent = (
            summary.get("country_code") == country
            and summary.get("region_code") == region
            and summary.get("scoped_site_count") == scoped
            and summary.get("notice") == UNCERTAINTY_NOTICE
        )
        add("output summary matches config and database", consistent, str(path))
    return checks


def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigError as error:
        print(f"SMOKE TEST FAILED: {error}", file=sys.stderr)
        return 2
    try:
        with connect(settings) as connection:
            checks = run_checks(settings, connection)
    except (DatabaseUnavailable, psycopg.Error) as error:
        print(f"SMOKE TEST FAILED: database check error: {error}", file=sys.stderr)
        return 1

    print(f"IRIS smoke test  scope={settings.country_code}/{settings.region_code}")
    for check in checks:
        print(f"  [{'PASS' if check.ok else 'FAIL'}] {check.name}  ({check.detail})")
    failed = [c for c in checks if not c.ok]
    print("SMOKE TEST PASSED" if not failed else f"SMOKE TEST FAILED: {len(failed)} check(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
