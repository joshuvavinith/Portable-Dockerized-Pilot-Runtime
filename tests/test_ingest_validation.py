from __future__ import annotations

import copy
import json

import pytest

from iris.config import Settings
from iris.ingest import (
    IngestError,
    approx_area_m2,
    parse_features,
    parse_geometry,
    parse_number,
    parse_source_date,
)
from tests.conftest import ROOT


@pytest.fixture()
def collection():
    return json.loads((ROOT / "fixtures" / "sites.geojson").read_text())


def settings(env_factory, country="DE", region="NW", **extra):
    return Settings.from_env(
        env_factory(IRIS_COUNTRY_CODE=country, IRIS_REGION_CODE=region, **extra)
    )


def first_de_nw(collection):
    return next(f for f in collection["features"] if f["properties"]["region_code"] == "NW")


def test_fixture_parses_for_both_configured_scopes(env_factory, collection):
    de = parse_features(collection, settings(env_factory))
    nl = parse_features(collection, settings(env_factory, "NL", "LI"))
    assert (len(de.records), de.skipped_out_of_scope) == (2, 3)
    assert (len(nl.records), nl.skipped_out_of_scope) == (2, 3)


def test_same_id_in_two_countries_is_allowed(env_factory, collection):
    de = parse_features(collection, settings(env_factory))
    nl = parse_features(collection, settings(env_factory, "NL", "LI"))
    assert {r.id for r in de.records} & {r.id for r in nl.records} == {"site-001", "site-002"}
    assert all(r.country_code == "DE" for r in de.records)
    assert all(r.country_code == "NL" for r in nl.records)


def test_region_scoping_excludes_other_regions(env_factory, collection):
    by = parse_features(collection, settings(env_factory, "DE", "BY"))
    assert [r.id for r in by.records] == ["site-003"]


def test_scope_with_no_features_is_an_error(env_factory, collection):
    with pytest.raises(IngestError, match="no features for configured scope FR/IDF"):
        parse_features(collection, settings(env_factory, "FR", "IDF"))


def test_out_of_scope_garbage_does_not_block_in_scope(env_factory, collection):
    collection["features"].append(
        {"type": "Feature", "id": "bad", "properties": {"country_code": "AT", "region_code": "W"},
         "geometry": None}
    )
    assert len(parse_features(collection, settings(env_factory)).records) == 2


@pytest.mark.parametrize("field", ["country_code", "region_code"])
def test_missing_scope_fields_rejected(env_factory, collection, field):
    del first_de_nw(collection)["properties"][field]
    with pytest.raises(IngestError, match=field):
        parse_features(collection, settings(env_factory))


def test_missing_id_rejected(env_factory, collection):
    del first_de_nw(collection)["id"]
    with pytest.raises(IngestError, match="no stable id"):
        parse_features(collection, settings(env_factory))


def test_duplicate_id_in_scope_rejected(env_factory, collection):
    collection["features"].append(copy.deepcopy(first_de_nw(collection)))
    with pytest.raises(IngestError, match="more than once"):
        parse_features(collection, settings(env_factory))


@pytest.mark.parametrize("crs", ["EPSG:3857", "EPSG:25832", None])
def test_foreign_crs_rejected(env_factory, collection, crs):
    collection["crs"] = {"type": "name", "properties": {"name": crs}}
    with pytest.raises(IngestError, match="unsupported CRS"):
        parse_features(collection, settings(env_factory))


def test_missing_crs_means_wgs84(env_factory, collection):
    del collection["crs"]
    assert len(parse_features(collection, settings(env_factory)).records) == 2


def test_projected_coordinates_mislabelled_as_wgs84_rejected(env_factory, collection):
    first_de_nw(collection)["geometry"]["coordinates"] = [
        [[500000, 5600000], [500100, 5600000], [500100, 5600100], [500000, 5600100],
         [500000, 5600000]]
    ]
    with pytest.raises(IngestError, match="outside lon/lat range"):
        parse_features(collection, settings(env_factory))


def test_self_intersecting_polygon_rejected(env_factory, collection):
    first_de_nw(collection)["geometry"]["coordinates"] = [
        [[6.9, 50.9], [6.901, 50.901], [6.901, 50.9], [6.9, 50.901], [6.9, 50.9]]
    ]
    with pytest.raises(IngestError, match="invalid geometry"):
        parse_features(collection, settings(env_factory))


def test_non_polygon_rejected(env_factory, collection):
    first_de_nw(collection)["geometry"] = {"type": "Point", "coordinates": [6.9, 50.9]}
    with pytest.raises(IngestError, match="must be a Polygon"):
        parse_features(collection, settings(env_factory))


def test_3d_geometry_rejected():
    with pytest.raises(ValueError, match="3-D"):
        parse_geometry({"type": "Polygon",
                        "coordinates": [[[6.9, 50.9, 1], [6.91, 50.9, 1], [6.91, 50.91, 1],
                                         [6.9, 50.9, 1]]]})


def test_missing_geometry_rejected(env_factory, collection):
    first_de_nw(collection)["geometry"] = None
    with pytest.raises(IngestError, match="missing geometry"):
        parse_features(collection, settings(env_factory))


@pytest.mark.parametrize("units", [None, "ha", "km2", "M2"])
def test_units_must_be_declared_as_m2(env_factory, collection, units):
    props = first_de_nw(collection)["properties"]
    if units is None:
        del props["source_units"]
    else:
        props["source_units"] = units
    with pytest.raises(IngestError, match="source_units"):
        parse_features(collection, settings(env_factory))


def test_area_that_contradicts_geometry_rejected(env_factory, collection):
    first_de_nw(collection)["properties"]["area_m2"] = 1.0  # e.g. hectares mistaken for m2
    with pytest.raises(IngestError, match="disagrees with geometry"):
        parse_features(collection, settings(env_factory))


def test_area_tolerance_is_configurable(env_factory, collection):
    first_de_nw(collection)["properties"]["area_m2"] = 10600.0  # +6%
    first_de_nw(collection)["properties"]["uncertainty_m2"] = 0.0
    with pytest.raises(IngestError):
        parse_features(collection, settings(env_factory))
    assert parse_features(collection, settings(env_factory, IRIS_AREA_TOLERANCE_PCT="10"))


@pytest.mark.parametrize("field", ["area_m2", "uncertainty_m2", "source_date"])
def test_missing_required_attribute_never_invented(env_factory, collection, field):
    del first_de_nw(collection)["properties"][field]
    with pytest.raises(IngestError, match=f"missing {field}"):
        parse_features(collection, settings(env_factory))


def test_all_errors_are_reported_not_just_the_first(env_factory, collection):
    for feature in collection["features"]:
        if feature["properties"]["region_code"] == "NW":
            del feature["properties"]["source_date"]
    with pytest.raises(IngestError) as exc:
        parse_features(collection, settings(env_factory))
    assert len(exc.value.errors) == 2


def test_source_date_parsing():
    assert parse_source_date("2026-09-29").isoformat() == "2026-09-29"
    for bad in ("29.09.2026", "20260929", "2026-13-01", "", None):
        with pytest.raises(ValueError):
            parse_source_date(bad)


@pytest.mark.parametrize("bad", [None, "abc", float("nan"), float("inf"), -1, True])
def test_parse_number_rejects_bad_values(bad):
    with pytest.raises(ValueError):
        parse_number(bad, "x", allow_zero=True)


def test_parse_number_zero_rules():
    assert parse_number(0, "x", allow_zero=True) == 0
    with pytest.raises(ValueError, match="zero"):
        parse_number(0, "x", allow_zero=False)


def test_approx_area_close_to_declared(collection):
    from shapely.geometry import shape

    for feature in collection["features"]:
        declared = feature["properties"]["area_m2"]
        assert approx_area_m2(shape(feature["geometry"])) == pytest.approx(declared, rel=0.01)
