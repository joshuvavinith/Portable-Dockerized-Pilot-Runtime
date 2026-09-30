# IRIS pilot runtime (IRIS-CAND-09)

This packages the IRIS pilot as a PostgreSQL/PostGIS database plus a small Python worker, run with Docker Compose. The point of the exercise is that nothing about a particular country, region, host or path lives in the code. I run `DE/NW` on my machine and `NL/LI` with a different env file, using the same image and the same source tree.

> Preliminary prospecting material. Figures, eco-point estimates and site suitability are indicative and based on available source data and commercial screening assumptions. The 8 eco-points/m2 factor is the current commercial baseline, not certified compensation. Ownership, planning, grid capacity, environmental eligibility and transferability remain subject to project-specific verification. No permit, reservation or construction readiness is represented.

## Running it

You need Docker with Compose v2 and an internet connection for the first build (base images and Python wheels). After that nothing needs the network, because the default data source is a fixture committed in `fixtures/`.

Linux / macOS / Git Bash:

```bash
cp .env.example .env
export IRIS_UID=$(id -u) IRIS_GID=$(id -g)      # Linux only, see "Host assumptions"
docker compose --profile smoke run --build --rm smoke
docker compose down -v
```

Windows PowerShell (skip the uid lines, Docker Desktop doesn't need them):

```powershell
Copy-Item .env.example .env
docker compose --profile smoke run --build --rm smoke
docker compose down -v
```

That one `run` command starts PostGIS, waits for it to be healthy, runs the worker to completion, then runs the smoke test. It should finish with `SMOKE TEST PASSED`, and the worker's result is in `output/iris_run_summary_DE_NW.json`.

### Running the second configuration

`.env.second-host.example` is the same stack pointed at `NL/LI`, with a different container output path, database name, project name and published DB port. Its password is left empty on purpose so there is no default credential in the repo. Set one for your shell session first:

```bash
export POSTGRES_PASSWORD="$(openssl rand -hex 16)"
docker compose --env-file .env.second-host.example --profile smoke run --build --rm smoke
docker compose --env-file .env.second-host.example --profile smoke --profile test down -v
```

```powershell
$env:POSTGRES_PASSWORD = [guid]::NewGuid().ToString("N")
docker compose --env-file .env.second-host.example --profile smoke run --build --rm smoke
docker compose --env-file .env.second-host.example --profile smoke --profile test down -v
```

Two things that tripped me up while testing. First, Postgres only reads the password when it creates the data volume, so if you change the password you have to `down -v` the matching project first. Second, `down` only cleans the project named by the env file you pass, so pass the same `--env-file` you started with.

### Other commands

| What | Command |
|---|---|
| Smoke test | `docker compose --profile smoke run --build --rm smoke` |
| Tests (unit and DB-backed, inside Docker) | `docker compose --profile test run --build --rm test` |
| Worker only | `docker compose up --build --abort-on-container-exit --exit-code-from worker worker` |
| Both configs back to back | `bash scripts/verify-portability.sh` |
| Find the DB's published port | `docker compose port db 5432` |

The Makefile has shortcuts for the same commands. They are optional.

To work without Docker, install `requirements.txt` and `requirements-test.txt`, `pip install -e .`, export the variables from `.env`, and run `iris-worker`, `iris-smoke` and `pytest`. The integration tests need `POSTGRES_HOST`, `POSTGRES_USER`, `POSTGRES_PASSWORD` and `POSTGRES_DB` pointing at a PostgreSQL 16 + PostGIS 3.4 superuser, and skip themselves otherwise.

## Host assumptions

- Docker Engine 24 or newer with Compose v2. I rely on `service_healthy` and `service_completed_successfully`.
- The reference platform is linux/amd64. The `postgis/postgis` tag may not be published for other architectures, so on those set `POSTGIS_IMAGE` to any PostgreSQL 16+ / PostGIS 3.4+ image.
- About 1 GB of disk. No PostgreSQL needs to be installed on the host.
- The DB port is published on `127.0.0.1` only, on a random free port unless you set `POSTGRES_PUBLISH_PORT`. I did that so a host that already runs Postgres on 5432 doesn't break the stack.
- The worker runs as an unprivileged user, not root. On Linux it needs to match the owner of the output directory, hence `IRIS_UID` and `IRIS_GID`. `IRIS_OUTPUT_HOST_DIR` must exist and be writable by that user. `output/.gitkeep` is committed so the default exists.
- Docker Desktop on Windows and macOS handles file ownership itself, so the uid settings can be ignored there.

## Configuration

Everything is an environment variable, read and validated once in `src/iris/config.py`. If several are wrong it reports all of them together and exits with code 2.

| Variable | Required | Meaning |
|---|---|---|
| `IRIS_COUNTRY_CODE` | yes | Upper-case ISO 3166-1 alpha-2, for example `DE` |
| `IRIS_REGION_CODE` | yes | 1 to 8 upper-case letters or digits, for example `NW` |
| `IRIS_SOURCE_ENDPOINT` | yes | `fixture://<file in fixtures/>` or an `http(s)://` GeoJSON URL |
| `IRIS_OUTPUT_PATH` | yes | Absolute path inside the container for the run summary |
| `IRIS_OUTPUT_HOST_DIR` | no | Host folder mounted at that path, default `./output` |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | yes | Database credentials |
| `POSTGRES_HOST`, `POSTGRES_PORT` | no | Compose sets `db` and `5432` |
| `IRIS_AREA_TOLERANCE_PCT` | no | Allowed gap between declared area and geometry, default 5 |
| `IRIS_DB_CONNECT_RETRIES`, `IRIS_DB_CONNECT_RETRY_DELAY_S` | no | Connection retry budget, default 30 tries, 1 s apart |
| `IRIS_MIGRATIONS_DIR`, `IRIS_FIXTURES_DIR` | no | Default to `/app/...` in the image |
| `IRIS_SOURCE_TIMEOUT_S`, `IRIS_SOURCE_MAX_BYTES` | no | Limits for HTTP sources |
| `POSTGIS_IMAGE`, `COMPOSE_PROJECT_NAME`, `IRIS_UID`, `IRIS_GID`, `IRIS_IMAGE`, `IRIS_LOG_LEVEL` | no | Compose and host plumbing |

The app also accepts `POSTGRES_PASSWORD_FILE` instead of `POSTGRES_PASSWORD`, which is how I would pass it with Docker or Kubernetes secrets.

## How it fits together

There are four services in `docker-compose.yml`: `db` (PostGIS), `worker` (runs once and exits), `smoke` (the smoke test, behind the `smoke` profile) and `test` (pytest, behind the `test` profile). The worker waits for `db` to be healthy, and `smoke` waits for the worker to finish successfully.

The worker does five things in order: apply migrations, load the source, validate every feature, upsert the sites, write a JSON summary. A row is also written to `pilot_runs` for every run, successful or not, so the last result for a country and region can be checked in the database.

Some decisions worth explaining:

**The database health check uses TCP.** It runs `pg_isready -h 127.0.0.1`. The official Postgres image starts a temporary server on a Unix socket during first-time setup. A plain `pg_isready` can report ready against that temporary server, and then the worker connects over the network a moment before the real server is up. Forcing TCP avoids that. The worker also retries its connection a limited number of times as a second line of defence, but it fails immediately on a wrong password or a missing database, since retrying those never helps.

**Migrations are plain SQL files applied in numeric order.** Each one runs in its own transaction and is recorded in `schema_migrations` with a SHA-256 checksum. A Postgres advisory lock keeps two workers from migrating at once. If a file that was already applied has been edited, the run stops with an error instead of carrying on with a schema nobody has reviewed. The DDL uses `IF NOT EXISTS` too, so running the raw SQL by hand twice does no harm. That doesn't mean a migration should be edited after it has been applied. Add a new one instead.

**No secrets in the image.** The Dockerfile copies only code, migrations and fixtures. `.dockerignore` and `.gitignore` keep `.env` files out. Credentials only arrive as environment variables at run time, and compose stops with a clear message if one is missing. The password never appears in `repr`, logs or the summary file, and source URLs are stored without credentials or query strings. The container runs as a non-root user with a read-only filesystem and all Linux capabilities dropped. The CI workflow checks that the built image contains no password.

## Data contracts

The brief says to treat completeness, CRS, units, source date and uncertainty as explicit contracts and never silently invent data. This is how each one is enforced:

- **Geometry** is stored in `sites.geom` as `geometry(Polygon, 4326)` with a `ST_IsValid` check.
- **Country** is a non-null `country_code` on every business table, with a format check. Keys are `(country_code, id)`, so `site-001` can exist in both DE and NL. There is a test for exactly that.
- **CRS**: only WGS 84 lon/lat is accepted. A feature collection that declares another CRS is rejected, and so are coordinates outside the lon/lat range, which is what mislabelled projected data looks like.
- **Units**: the source has to say `source_units: "m2"`.
- **Area**: the declared `area_m2` must agree with the geometry to within the declared uncertainty or `IRIS_AREA_TOLERANCE_PCT`, whichever is larger. This catches hectares entered as square metres. I had to fix my own fixture because its areas didn't match its geometry.
- **Source date and uncertainty** are required. Missing values are errors, never defaults.
- **Batches are all or nothing.** One bad in-scope feature rejects the run, every problem is listed, and the failure is recorded in `pilot_runs`. Features for other countries or regions are counted and skipped.

## Tests

There are 98 tests. Most are plain unit tests: config validation, each data contract above, the source adapters (including a path traversal attempt and HTTP size and error handling), and migration file rules. The rest run against a real PostgreSQL 16 with PostGIS 3.4 and are the ones I care most about. They cover migrations being idempotent and refusing edited files, a failed migration leaving nothing half-applied, running `DE/NW` and then `NL/LI` with no code change, a bad batch leaving the table empty, the database constraints themselves, the stored area matching PostGIS's own geography area, and the smoke test both passing and failing when it should.

## What the smoke test checks

It connects and confirms PostgreSQL is 16 or newer and PostGIS is 3.4 or newer. It then checks that all migrations are applied with matching checksums, that `sites.geom` has the expected type and SRID, and that `country_code` is `NOT NULL` on both tables. It requires rows for the configured country and region and valid stored geometries. Finally it checks that the latest run for that scope succeeded and that the summary file matches both the configuration and the database. A missing file or an empty scope fails the test.

## Simplifications I made on purpose

- Sources are a local fixture or a plain HTTP GeoJSON URL. A real deployment would have one adapter per country behind the same endpoint setting, with authentication.
- Only `Polygon` is accepted, not `MultiPolygon`, and the area check uses a local flat projection that is fine for small sites. Production should normalise multi-part geometries explicitly and use geodesic area in PostGIS.
- The database password is a plain environment variable, which keeps the stack portable. For production I would use secret files (already supported through `POSTGRES_PASSWORD_FILE`), TLS to the database, and a non-superuser application role with migrations run by a separate owner role.
- The worker runs once. In production it would run on a schedule, with metrics and alerting, and load into a staging table before data becomes visible.
- DB data lives in a named volume with no backups. Production would add backups and pin the image by digest rather than by tag.

## Repository layout

```
docker-compose.yml, Dockerfile, .env.example, .env.second-host.example
src/iris/       config, db, migrate, sources, ingest, worker, smoke
migrations/     001_init.sql, 002_pilot_runs.sql
fixtures/       sites.geojson (DE/NW x2, DE/BY x1, NL/LI x2)
tests/          unit and integration tests
scripts/        verify-portability.sh
.github/        ci.yml (runs the tests and both smoke paths from a fresh checkout)
```
