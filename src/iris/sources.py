"""Source adapters: turn IRIS_SOURCE_ENDPOINT into a GeoJSON FeatureCollection.

fixture://<file>   committed local fixture (default; no network needed)
http(s)://<url>    remote GeoJSON, bounded by timeout and size
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from iris.config import Settings

_FIXTURE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class SourceError(RuntimeError):
    pass


def load_source(settings: Settings) -> dict[str, Any]:
    scheme = urlsplit(settings.source_endpoint).scheme
    if scheme == "fixture":
        payload = _read_fixture(settings)
    elif scheme in ("http", "https"):
        payload = _read_http(settings)
    else:  # pragma: no cover - config validation already rejects this
        raise SourceError(f"unsupported source scheme: {scheme!r}")
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise SourceError("source is not a GeoJSON FeatureCollection")
    if not isinstance(payload.get("features"), list):
        raise SourceError("FeatureCollection has no 'features' list")
    return payload


def _read_fixture(settings: Settings) -> Any:
    parts = urlsplit(settings.source_endpoint)
    name = (parts.netloc + parts.path).strip()
    if not _FIXTURE_NAME_RE.fullmatch(name):
        raise SourceError(f"invalid fixture name {name!r} (no paths allowed)")
    base = settings.fixtures_dir.resolve()
    path = (base / name).resolve()
    if path.parent != base:
        raise SourceError(f"fixture escapes fixtures directory: {name!r}")
    if not path.is_file():
        raise SourceError(f"fixture not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SourceError(f"fixture is not valid JSON: {error}") from error


def _read_http(settings: Settings) -> Any:
    request = urllib.request.Request(
        settings.source_endpoint,
        headers={"Accept": "application/geo+json, application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.source_timeout_s) as response:
            body = response.read(settings.source_max_bytes + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise SourceError(
            f"could not fetch {settings.redacted_endpoint}: {error}"
        ) from error
    if len(body) > settings.source_max_bytes:
        raise SourceError(f"source exceeds {settings.source_max_bytes} bytes")
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise SourceError(f"source is not valid JSON: {error}") from error
