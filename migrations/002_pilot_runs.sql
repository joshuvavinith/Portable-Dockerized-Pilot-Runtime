-- One row per worker run per country/region scope, so operators (and the
-- smoke test) can see whether the last run for a scope succeeded.
CREATE TABLE IF NOT EXISTS pilot_runs (
    country_code    TEXT NOT NULL CHECK (country_code ~ '^[A-Z]{2}$'),
    run_id          BIGINT GENERATED ALWAYS AS IDENTITY,
    region_code     TEXT NOT NULL CHECK (region_code ~ '^[A-Z0-9]{1,8}$'),
    source_endpoint TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
    features_seen   INTEGER NOT NULL DEFAULT 0 CHECK (features_seen >= 0),
    sites_ingested  INTEGER NOT NULL DEFAULT 0 CHECK (sites_ingested >= 0),
    error_message   TEXT,
    started_at      TIMESTAMPTZ NOT NULL,
    finished_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (country_code, run_id)
);

CREATE INDEX IF NOT EXISTS idx_pilot_runs_scope ON pilot_runs (country_code, region_code, finished_at DESC);
