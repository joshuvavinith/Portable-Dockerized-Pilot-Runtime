#!/usr/bin/env bash
# Runs the documented smoke path with BOTH env templates (dev DE/NW and
# second-host NL/LI) against the same, unmodified code and images.
set -euo pipefail
cd "$(dirname "$0")/.."

export IRIS_UID="${IRIS_UID:-$(id -u)}" IRIS_GID="${IRIS_GID:-$(id -g)}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(openssl rand -hex 16)}"

for envfile in .env.example .env.second-host.example; do
  echo "=== smoke path with ${envfile} ==="
  compose=(docker compose --env-file "${envfile}")
  "${compose[@]}" --profile smoke --profile test down -v --remove-orphans >/dev/null 2>&1 || true
  "${compose[@]}" --profile smoke run --build --rm smoke
  "${compose[@]}" --profile smoke --profile test down -v --remove-orphans >/dev/null
done
echo "portability check passed for both configurations"
