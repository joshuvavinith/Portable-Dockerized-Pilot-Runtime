CREATE EXTENSION IF NOT EXISTS postgis;

-- Canonical site table. Identity is country-scoped: (country_code, id).
CREATE TABLE IF NOT EXISTS sites (
    country_code    TEXT NOT NULL CHECK (country_code ~ '^[A-Z]{2}$'),
    id              TEXT NOT NULL CHECK (length(id) > 0),
    region_code     TEXT NOT NULL CHECK (region_code ~ '^[A-Z0-9]{1,8}$'),
    source_endpoint TEXT NOT NULL,
    source_date     DATE NOT NULL,
    area_m2         DOUBLE PRECISION NOT NULL CHECK (area_m2 > 0),
    uncertainty_m2  DOUBLE PRECISION NOT NULL CHECK (uncertainty_m2 >= 0),
    geom            geometry(Polygon, 4326) NOT NULL CHECK (ST_IsValid(geom)),
    PRIMARY KEY (country_code, id)
);

COMMENT ON COLUMN sites.area_m2 IS 'Source-declared area in square metres (never derived silently).';
COMMENT ON COLUMN sites.uncertainty_m2 IS 'Source-declared absolute area uncertainty in square metres.';
COMMENT ON COLUMN sites.source_date IS 'Date of the source data (not the ingest date).';
COMMENT ON COLUMN sites.geom IS 'WGS 84 (EPSG:4326) polygon, lon/lat order.';

CREATE INDEX IF NOT EXISTS idx_sites_country_region ON sites (country_code, region_code);
CREATE INDEX IF NOT EXISTS idx_sites_geom ON sites USING GIST (geom);
