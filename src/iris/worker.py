"""One-shot pilot worker: migrate -> load source -> validate -> ingest -> report."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from iris.config import ConfigError, Settings
from iris.db import DatabaseUnavailable, connect
from iris.ingest import IngestError, parse_features, upsert_sites
from iris.migrate import MigrationError, apply_migrations
from iris.sources import SourceError, load_source

log = logging.getLogger("iris.worker")

UNCERTAINTY_NOTICE = (
    "Preliminary prospecting material. Figures, eco-point estimates and site "
    "suitability are indicative and based on available source data and commercial "
    "screening assumptions. The 8 eco-points/m2 factor is the current commercial "
    "baseline, not certified compensation. Ownership, planning, grid capacity, "
    "environmental eligibility and transferability remain subject to "
    "project-specific verification. No permit, reservation or construction "
    "readiness is represented."
)


def summary_path(settings: Settings) -> Path:
    name = f"iris_run_summary_{settings.country_code}_{settings.region_code}.json"
    return settings.output_path / name


def write_summary(settings: Settings, summary: dict[str, Any]) -> Path:
    """Write atomically so a reader never sees a half-written file."""
    settings.output_path.mkdir(parents=True, exist_ok=True)
    target = summary_path(settings)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return target


def _record_failure(settings: Settings, started_at: datetime, error: Exception) -> None:
    """Best effort: a failed run must still be visible in pilot_runs."""
    try:
        with connect(settings) as conn:
            conn.execute(
                """
                INSERT INTO pilot_runs (country_code, region_code, source_endpoint,
                                        status, error_message, started_at)
                VALUES (%s, %s, %s, 'failed', %s, %s)
                """,
                (
                    settings.country_code,
                    settings.region_code,
                    settings.redacted_endpoint,
                    str(error)[:2000],
                    started_at,
                ),
            )
    except Exception:  # never mask the original error
        log.warning("could not record failed run in pilot_runs", exc_info=True)


def run(settings: Settings) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    with connect(settings) as connection:
        migrations_applied = apply_migrations(connection, settings.migrations_dir)
    try:
        collection = load_source(settings)
        parsed = parse_features(collection, settings)
        with connect(settings) as connection:
            # ingest + run log commit together, or not at all
            ingested = upsert_sites(connection, parsed.records, settings.redacted_endpoint)
            scoped_total = connection.execute(
                "SELECT count(*) FROM sites WHERE country_code = %s AND region_code = %s",
                (settings.country_code, settings.region_code),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO pilot_runs (country_code, region_code, source_endpoint, status,
                                        features_seen, sites_ingested, started_at)
                VALUES (%s, %s, %s, 'succeeded', %s, %s, %s)
                """,
                (
                    settings.country_code,
                    settings.region_code,
                    settings.redacted_endpoint,
                    parsed.features_seen,
                    ingested,
                    started_at,
                ),
            )
    except Exception as error:
        _record_failure(settings, started_at, error)
        raise

    summary = {
        "country_code": settings.country_code,
        "region_code": settings.region_code,
        "source_endpoint": settings.redacted_endpoint,
        "migrations_applied_this_run": migrations_applied,
        "features_seen": parsed.features_seen,
        "skipped_out_of_scope": parsed.skipped_out_of_scope,
        "sites_ingested": ingested,
        "scoped_site_count": scoped_total,
        "notice": UNCERTAINTY_NOTICE,
    }
    write_summary(settings, summary)
    return summary


def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigError as error:
        print(f"IRIS worker FAILED: {error}", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        summary = run(settings)
    except (DatabaseUnavailable, MigrationError, SourceError, IngestError) as error:
        print(f"IRIS worker FAILED: {error}", file=sys.stderr)
        return 1
    log.info(
        "run complete country=%s region=%s ingested=%d scoped_sites=%d summary=%s",
        settings.country_code,
        settings.region_code,
        summary["sites_ingested"],
        summary["scoped_site_count"],
        summary_path(settings),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
