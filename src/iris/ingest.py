"""Validate and ingest GeoJSON sites under explicit data contracts.

Contracts enforced here (nothing is silently repaired or invented):

* CRS      WGS 84 lon/lat only; coordinates must be inside the lon/lat range
* geometry valid, non-empty, 2-D Polygon
* units    source must declare ``source_units == "m2"``
* area     declared ``area_m2`` must agree with the geometry within
           max(declared uncertainty, IRIS_AREA_TOLERANCE_PCT)
* dates    ``source_date`` is required, ISO YYYY-MM-DD
* scope    every feature carries country_code / region_code / id; only the
           configured country+region is ingested, the rest is counted and skipped
* batch    all-or-nothing: one bad in-scope feature rejects the whole batch
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from math import cos, radians
from typing import Any

import psycopg
from shapely.geometry import Polygon, shape
from shapely.ops import transform
from shapely.validation import explain_validity

from iris.config import COUNTRY_CODE_RE, REGION_CODE_RE, Settings

EARTH_RADIUS_M = 6_371_008.8
_ACCEPTED_CRS = {
    "EPSG:4326",
    "urn:ogc:def:crs:EPSG::4326",
    "urn:ogc:def:crs:OGC:1.3:CRS84",
    "OGC:CRS84",
}
_MAX_REPORTED_ERRORS = 20


class IngestError(ValueError):
    """Carries every validation failure found in the batch."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        shown = errors[:_MAX_REPORTED_ERRORS]
        more = len(errors) - len(shown)
        text = "; ".join(shown) + (f"; ... and {more} more" if more > 0 else "")
        super().__init__(f"source rejected: {text}")


@dataclass(frozen=True)
class SiteRecord:
    country_code: str
    id: str
    region_code: str
    source_date: date
    area_m2: float
    uncertainty_m2: float
    geometry: Polygon


@dataclass(frozen=True)
class ParseResult:
    records: list[SiteRecord]
    features_seen: int
    skipped_out_of_scope: int


def check_crs(collection: dict[str, Any]) -> None:
    crs = collection.get("crs")
    if crs is None:  # RFC 7946: absent CRS means WGS 84 lon/lat
        return
    name = ((crs.get("properties") or {}).get("name")) if isinstance(crs, dict) else None
    if name not in _ACCEPTED_CRS:
        raise IngestError([f"unsupported CRS {name!r}; only WGS 84 (EPSG:4326) is accepted"])


def approx_area_m2(polygon: Polygon) -> float:
    """Area in m2 via a local equirectangular projection (accurate for small sites)."""
    lat0 = radians(polygon.centroid.y)
    scale_x = EARTH_RADIUS_M * cos(lat0)

    def project(xs, ys, zs=None):
        return (
            [radians(x) * scale_x for x in xs],
            [radians(y) * EARTH_RADIUS_M for y in ys],
        )

    return transform(project, polygon).area


def parse_source_date(value: Any) -> date:
    if not value:
        raise ValueError("missing source_date")
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"invalid source_date {value!r} (expected YYYY-MM-DD)") from None


def parse_number(value: Any, name: str, *, allow_zero: bool) -> float:
    if value is None:
        raise ValueError(f"missing {name}")
    if isinstance(value, bool):
        raise ValueError(f"invalid {name} {value!r}")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid {name} {value!r}") from None
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite {name}")
    if parsed < 0 or (parsed == 0 and not allow_zero):
        raise ValueError(f"{'negative' if parsed < 0 else 'zero'} {name}: {parsed}")
    return parsed


def parse_geometry(data: Any) -> Polygon:
    if not data:
        raise ValueError("missing geometry")
    try:
        geometry = shape(data)
    except Exception as error:  # shapely raises many types for malformed input
        raise ValueError(f"unreadable geometry ({error})") from None
    if geometry.geom_type != "Polygon":
        raise ValueError(f"geometry must be a Polygon, got {geometry.geom_type}")
    if geometry.is_empty:
        raise ValueError("empty geometry")
    if geometry.has_z:
        raise ValueError("3-D geometry is not supported")
    if not geometry.is_valid:
        raise ValueError(f"invalid geometry: {explain_validity(geometry)}")
    min_x, min_y, max_x, max_y = geometry.bounds
    if not (-180 <= min_x and max_x <= 180 and -90 <= min_y and max_y <= 90):
        raise ValueError(
            "coordinates outside lon/lat range; data is not WGS 84 (projected or axis-swapped?)"
        )
    return geometry


def _identity(feature: Any, position: int) -> tuple[str, str, str]:
    if not isinstance(feature, dict):
        raise ValueError(f"feature #{position} is not an object")
    props = feature.get("properties") or {}
    feature_id = str(feature.get("id") or props.get("id") or "").strip()
    label = feature_id or f"#{position}"
    if not feature_id:
        raise ValueError(f"feature {label} has no stable id")
    country = str(props.get("country_code") or "").strip()
    region = str(props.get("region_code") or "").strip()
    if not COUNTRY_CODE_RE.fullmatch(country):
        raise ValueError(f"feature {label} has missing/invalid country_code {country!r}")
    if not REGION_CODE_RE.fullmatch(region):
        raise ValueError(f"feature {label} has missing/invalid region_code {region!r}")
    return feature_id, country, region


def parse_features(collection: dict[str, Any], settings: Settings) -> ParseResult:
    check_crs(collection)
    errors: list[str] = []
    records: list[SiteRecord] = []
    seen_ids: set[str] = set()
    features = collection.get("features", [])
    skipped = 0

    for position, feature in enumerate(features, start=1):
        try:
            feature_id, country, region = _identity(feature, position)
        except ValueError as error:
            errors.append(str(error))
            continue
        if (country, region) != (settings.country_code, settings.region_code):
            skipped += 1
            continue
        if feature_id in seen_ids:
            errors.append(f"feature {feature_id} appears more than once")
            continue
        seen_ids.add(feature_id)
        props = feature.get("properties") or {}
        try:
            record = _build_record(feature_id, country, region, feature, props, settings)
        except ValueError as error:
            errors.append(f"feature {feature_id}: {error}")
            continue
        records.append(record)

    if not errors and not records:
        errors.append(
            "no features for configured scope "
            f"{settings.country_code}/{settings.region_code} "
            f"({len(features)} features in source)"
        )
    if errors:
        raise IngestError(errors)
    return ParseResult(records, len(features), skipped)


def _build_record(feature_id, country, region, feature, props, settings) -> SiteRecord:
    units = str(props.get("source_units") or "").strip()
    if units != "m2":
        raise ValueError(f"must declare source_units='m2', got {units!r}")
    source_date = parse_source_date(props.get("source_date"))
    area = parse_number(props.get("area_m2"), "area_m2", allow_zero=False)
    uncertainty = parse_number(props.get("uncertainty_m2"), "uncertainty_m2", allow_zero=True)
    geometry = parse_geometry(feature.get("geometry"))

    computed = approx_area_m2(geometry)
    allowed = max(uncertainty, area * settings.area_tolerance_pct / 100)
    if abs(computed - area) > allowed:
        raise ValueError(
            f"declared area_m2={area:g} disagrees with geometry (~{computed:.0f} m2, "
            f"allowed +/-{allowed:.0f}); wrong units or CRS?"
        )
    return SiteRecord(country, feature_id, region, source_date, area, uncertainty, geometry)


_UPSERT = """
INSERT INTO sites (country_code, id, region_code, source_endpoint, source_date,
                   area_m2, uncertainty_m2, geom)
VALUES (%s, %s, %s, %s, %s, %s, %s, ST_SetSRID(ST_GeomFromWKB(%s), 4326))
ON CONFLICT (country_code, id) DO UPDATE SET
    region_code     = EXCLUDED.region_code,
    source_endpoint = EXCLUDED.source_endpoint,
    source_date     = EXCLUDED.source_date,
    area_m2         = EXCLUDED.area_m2,
    uncertainty_m2  = EXCLUDED.uncertainty_m2,
    geom            = EXCLUDED.geom
"""


def upsert_sites(
    connection: psycopg.Connection, records: list[SiteRecord], source_endpoint: str
) -> int:
    """Idempotent upsert keyed on (country_code, id). Caller owns the transaction."""
    with connection.cursor() as cursor:
        cursor.executemany(
            _UPSERT,
            [
                (
                    r.country_code,
                    r.id,
                    r.region_code,
                    source_endpoint,
                    r.source_date,
                    r.area_m2,
                    r.uncertainty_m2,
                    r.geometry.wkb,
                )
                for r in records
            ],
        )
    return len(records)
