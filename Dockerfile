# syntax=docker/dockerfile:1
# No secrets, credentials or host paths are baked in: everything comes from the
# environment at run time (see docker-compose.yml / .env templates).

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-deps .

COPY migrations ./migrations
COPY fixtures ./fixtures
ENV IRIS_MIGRATIONS_DIR=/app/migrations \
    IRIS_FIXTURES_DIR=/app/fixtures

# Test image: adds pytest + tests. Never used for the pilot runtime.
FROM base AS test
COPY requirements-test.txt ./
RUN pip install -r requirements-test.txt
COPY tests ./tests
RUN useradd --system --uid 10001 --no-create-home iris
USER 10001
CMD ["pytest", "-ra", "-p", "no:cacheprovider"]

# Runtime image (default target; must stay last). Runs as an unprivileged user;
# compose overrides the uid/gid so the bind-mounted output dir stays writable.
FROM base AS runtime
RUN useradd --system --uid 10001 --no-create-home iris
USER 10001
CMD ["python", "-m", "iris.worker"]
