# IRIS Pilot: Portable Dockerized Runtime (IRIS-CAND-09)

PostgreSQL/PostGIS + a Python worker, packaged so that **country, region, source
endpoint, DB credentials and output path are configuration, not code**. The same
unmodified image runs `DE/NW` on a dev machine and `NL/LI` on a second host.

> Preliminary prospecting material. Figures, eco-point estimates and site suitability are indicative and based on available source data and commercial screening assumptions. The 8 eco-points/m2 factor is the current commercial baseline, not certified compensation. Ownership, planning, grid capacity, environmental eligibility and transferability remain subject to project-specific verification. No permit, reservation or construction readiness is represented.

## Quick start (clean host, ~3 commands)

```bash
cp .env.example .env                                   # 1. dev config (DE/NW)
export IRIS_UID=$(id -u) IRIS_GID=$(id -g)             #    Linux: makes ./output writable
docker compose --profile smoke run --build --rm smoke  # 2. DB -> healthy -> worker -> smoke test
docker compose down -v                                 # 3. clean up (drops the DB volume)
```

Expected end of output: `SMOKE TEST PASSED`. The worker's result is in
`./output/iris_run_summary_DE_NW.json`.

### Second host (different country, region, paths, project)

```bash
export IRIS_UID=$(id -u) IRIS_GID=$(id -g)
export POSTGRES_PASSWORD="$(openssl rand -hex 16)"     # template leaves it empty on purpose
docker compose --env-file .env.second-host.example --profile smoke run --build --rm smoke
docker compose --env-file .env.second-host.example down -v
```

`scripts/verify-portability.sh` (or `make portability`) runs both configurations back to back.

## Commands

| Goal | Command |
|---|---|
| Run stack + worker once | `docker compose up --build --abort-on-container-exit --exit-code-from worker worker` |
| **Smoke test** | `docker compose --profile smoke run --build --rm smoke` |
| Full test suite (unit + DB integration, in Docker) | `docker compose --profile test run --build --rm test` |
| Use another config | add `--env-file <file>` to any command above |
| Stop, keep data / delete data | `docker compose down` / `docker compose down -v` |
| Find the published DB port | `docker compose port db 5432` |

`make init | up | smoke | test | reset | portability` are optional wrappers around the same commands.

Local (no Docker) development: `pip install -r requirements.txt -r requirements-test.txt && pip install -e .`,
export the variables from `.env`, then `iris-worker`, `iris-smoke`, `pytest`. Integration tests
need `POSTGRES_HOST/USER/PASSWORD/DB` pointing at a PostgreSQL 16 + PostGIS 3.4 superuser; without them they are skipped
(the Docker `test` service always provides them).

## Host assumptions

* Docker Engine 24+ with **Compose v2** (`docker compose`, needs `service_healthy` / `service_completed_successfully`).
* Outbound access to pull `postgis/postgis:16-3.4`, `python:3.12-slim` and PyPI wheels **at build time only**. The runtime needs no internet (default source is a local fixture).
* `linux/amd64` is the reference platform. On other architectures check that the chosen PostGIS tag is published for it, or set `POSTGIS_IMAGE` (any PostgreSQL 16+/PostGIS 3.4+ image).
* ~1 GB free disk; no host PostgreSQL required. The DB port is published on `127.0.0.1` with a **random** host port (set `POSTGRES_PUBLISH_PORT` to pin it), so an existing local PostgreSQL on 5432 does not conflict.
* `IRIS_OUTPUT_HOST_DIR` must exist and be writable by `IRIS_UID:IRIS_GID` (the worker runs as that unprivileged user with a read-only root filesystem, all capabilities dropped and `no-new-privileges`). `./output/.gitkeep` is committed so the default exists. On Docker Desktop (macOS/Windows) the uid setting is harmless.
* Windows: use WSL2 or Git Bash for the shell snippets; the compose commands are identical.

## Configuration reference

All values are environment variables (validated in `src/iris/config.py`; every problem is reported at once, the process exits `2`).

| Variable | Required | Meaning |
|---|---|---|
| `IRIS_COUNTRY_CODE` | yes | ISO 3166-1 alpha-2, upper case (`DE`) |
| `IRIS_REGION_CODE` | yes | 1-8 upper-case letters/digits (`NW`) |
| `IRIS_SOURCE_ENDPOINT` | yes | `fixture://<file in fixtures dir>` or `http(s)://...` GeoJSON |
| `IRIS_OUTPUT_PATH` | yes | absolute path **inside the container** for the run summary |
| `IRIS_OUTPUT_HOST_DIR` | no (`./output`) | host directory mounted at `IRIS_OUTPUT_PATH` |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | yes | DB credentials. `POSTGRES_PASSWORD_FILE` is supported by the app for secret files |
| `POSTGRES_HOST` / `POSTGRES_PORT` | no (`db` / `5432` in compose) | DB address |
| `IRIS_MIGRATIONS_DIR`, `IRIS_FIXTURES_DIR` | no | default `/app/...` in the image |
| `IRIS_DB_CONNECT_RETRIES` / `_RETRY_DELAY_S` / `_TIMEOUT_S` | no (30 / 1 / 5) | bounded connect retry (defence in depth behind the health check) |
| `IRIS_AREA_TOLERANCE_PCT` | no (5) | allowed disagreement between declared area and geometry |
| `IRIS_SOURCE_TIMEOUT_S`, `IRIS_SOURCE_MAX_BYTES` | no | limits for `http(s)` sources |
| `IRIS_UID`, `IRIS_GID`, `IRIS_IMAGE`, `POSTGIS_IMAGE`, `POSTGRES_PUBLISH_PORT`, `COMPOSE_PROJECT_NAME`, `IRIS_LOG_LEVEL` | no | host/compose plumbing |

## Architecture

```
docker compose
  db      postgis/postgis:16-3.4   healthcheck: pg_isready over TCP
   ▲ service_healthy
  worker  python -m iris.worker    migrate -> load source -> validate -> ingest -> summary   (one-shot)
   ▲ service_completed_successfully
  smoke   python -m iris.smoke     asserts DB versions, schema, data, run log, output file    (profile: smoke)
  test    pytest                   unit + real-PostGIS integration tests                       (profile: test)
```

* **Deterministic startup migrations** (DDL is also `IF NOT EXISTS`, so re-running the raw SQL by hand is harmless; never edit a migration that has been applied, add a new one): `migrations/NNN_name.sql` applied in numeric order, one transaction each, guarded by a Postgres advisory lock (two workers cannot race), recorded with a SHA-256 checksum. Editing an already-applied file, or a DB that is ahead of the code, is refused instead of ignored.
* **DB healthy before worker**: `depends_on: service_healthy`. The check uses `pg_isready -h 127.0.0.1` (TCP) on purpose: the official image runs a temporary socket-only server during first init, so a socket check can pass too early. The worker additionally retries connections a bounded number of times and **fails fast** on wrong credentials.
* **No secrets in images**: the Dockerfile copies only code, migrations and fixtures; `.env*` are excluded by `.dockerignore` and git-ignored; credentials arrive as run-time environment variables (compose refuses to start if one is missing). The password is excluded from `repr`, the summary and logs; source URLs are stored **redacted** (no userinfo/query). CI asserts the image contains no password.
* **Container hardening**: unprivileged user, read-only root filesystem, `cap_drop: ALL`, `no-new-privileges`, DB bound to loopback, pinned dependency versions.

## Data contracts (nothing is silently invented)

| Contract | Enforcement |
|---|---|
| Canonical geometry | column `sites.geom geometry(Polygon, 4326)`, `CHECK ST_IsValid` |
| `country_code` | `NOT NULL` + `^[A-Z]{2}$` check on every business table; keys are `(country_code, id)`, so `site-001` may exist in both `DE` and `NL` |
| CRS | only WGS 84 lon/lat accepted; declared foreign CRS is rejected; coordinates outside lon/lat range (projected data, mislabelled) are rejected |
| Units | source must declare `source_units = "m2"` |
| Area vs geometry | declared `area_m2` must match the geometry within `max(uncertainty_m2, IRIS_AREA_TOLERANCE_PCT %)` (catches ha/m2 and CRS mistakes) |
| Source date, uncertainty | required, `YYYY-MM-DD` / finite number; missing values are errors, never defaulted |
| Batch semantics | all-or-nothing per run; every validation error is reported; failures are logged in `pilot_runs` |
| Scope | only the configured `country/region` is ingested; other features are counted and skipped |

## Tests

`98` tests: config validation, every ingest contract above, source adapters (incl. path traversal, HTTP size/timeout errors),
migration ordering/checksums/atomicity, connect retry vs fail-fast classification, and **real PostgreSQL 16 / PostGIS 3.4
integration tests** (idempotent migrations, DE/NW then NL/LI on unchanged code, country-scoped identity, atomic rejection of a
bad batch, DB constraints, PostGIS geography area cross-check, smoke test passing and failing correctly).

## Acceptance criteria

| # | Criterion | How it is met / verified |
|---|---|---|
| 1 | Change DE/NW without code edits | Only env values differ between `.env.example` (DE/NW) and `.env.second-host.example` (NL/LI); `test_switching_de_nw_to_nl_li_needs_no_code_change`, `verify-portability.sh` |
| 2 | No secrets baked into images | see Architecture; CI step "No secrets baked into the image" |
| 3 | DB healthy before worker | TCP health check + `depends_on: service_healthy` + bounded retry |
| 4 | Clean host runs documented smoke path | Quick start; CI job `clean-host-smoke` runs it from a fresh checkout on both configs |

## Deliberate simplifications and production evolution

* Source adapters: local fixture and plain `http(s)` GeoJSON only. Production: per-country adapters behind the same `IRIS_SOURCE_ENDPOINT` contract, auth via secret files, conditional fetches.
* Polygon only (no MultiPolygon) and a local equirectangular area check; production would normalise multipart geometries explicitly and use geodesic area in PostGIS.
* Postgres password is a compose environment variable (simple, portable). Production: Docker/K8s secrets via `POSTGRES_PASSWORD_FILE` (already supported by the app), TLS to the DB, a non-superuser application role with migrations run by a separate owner role.
* One-shot worker; production would run it on a schedule (cron/Kubernetes CronJob) with metrics, and promote data through staging tables before it becomes visible (controlled promotion).
* Named volume for DB data; production adds backups and a pinned image digest instead of a tag.

## Layout

```
docker-compose.yml  Dockerfile  .env.example  .env.second-host.example  Makefile
src/iris/           config.py db.py migrate.py sources.py ingest.py worker.py smoke.py
migrations/         001_init.sql  002_pilot_runs.sql
fixtures/           sites.geojson   (5 features: DE/NW x2, DE/BY, NL/LI x2; areas consistent with geometry)
tests/              unit + integration
scripts/            verify-portability.sh
.github/workflows/  ci.yml
```
