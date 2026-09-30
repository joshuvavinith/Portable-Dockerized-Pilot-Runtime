"""Runtime configuration.

Everything host-, country- or region-specific comes from environment
variables. Nothing in this module (or anywhere else in the package) hard-codes
a country, region, host name, path or credential.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

COUNTRY_CODE_RE = re.compile(r"[A-Z]{2}")
REGION_CODE_RE = re.compile(r"[A-Z0-9]{1,8}")
SUPPORTED_SCHEMES = ("fixture", "http", "https")


class ConfigError(ValueError):
    """Raised with *all* configuration problems at once."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("invalid configuration: " + "; ".join(problems))


def redact_endpoint(endpoint: str) -> str:
    """Drop credentials, query string and fragment before persisting/logging."""
    parts = urlsplit(endpoint)
    host = parts.hostname or parts.netloc
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


@dataclass(frozen=True)
class Settings:
    country_code: str
    region_code: str
    source_endpoint: str
    output_path: Path
    postgres_db: str
    postgres_user: str
    postgres_password: str = field(repr=False)
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    migrations_dir: Path = Path("migrations")
    fixtures_dir: Path = Path("fixtures")
    db_connect_timeout_s: int = 5
    db_connect_retries: int = 30
    db_connect_retry_delay_s: float = 1.0
    area_tolerance_pct: float = 5.0
    source_timeout_s: float = 15.0
    source_max_bytes: int = 10_000_000
    log_level: str = "INFO"

    @property
    def redacted_endpoint(self) -> str:
        return redact_endpoint(self.source_endpoint)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        problems: list[str] = []

        def text(name: str, default: str | None = None) -> str:
            raw = env.get(name)
            if raw is None or not raw.strip():
                if default is None:
                    problems.append(f"{name} is required and must not be empty")
                    return ""
                return default
            return raw.strip()

        def number(name, default, cast, minimum, maximum=None):
            raw = env.get(name)
            if raw is None or not raw.strip():
                return default
            try:
                value = cast(raw.strip())
            except ValueError:
                problems.append(f"{name} must be a number, got {raw!r}")
                return default
            if value < minimum or (maximum is not None and value > maximum):
                bound = f">= {minimum}" if maximum is None else f"in [{minimum}, {maximum}]"
                problems.append(f"{name} must be {bound}, got {value}")
                return default
            return value

        country = text("IRIS_COUNTRY_CODE")
        if country and not COUNTRY_CODE_RE.fullmatch(country):
            problems.append(
                "IRIS_COUNTRY_CODE must be an upper-case ISO 3166-1 alpha-2 "
                f"code such as DE, got {country!r}"
            )
        region = text("IRIS_REGION_CODE")
        if region and not REGION_CODE_RE.fullmatch(region):
            problems.append(
                "IRIS_REGION_CODE must be 1-8 upper-case letters/digits "
                f"such as NW, got {region!r}"
            )

        endpoint = text("IRIS_SOURCE_ENDPOINT")
        if endpoint:
            parts = urlsplit(endpoint)
            if parts.scheme not in SUPPORTED_SCHEMES:
                problems.append(
                    "IRIS_SOURCE_ENDPOINT scheme must be one of "
                    f"{', '.join(SUPPORTED_SCHEMES)}"
                )
            elif not (parts.netloc + parts.path).strip("/"):
                problems.append("IRIS_SOURCE_ENDPOINT has no target after the scheme")

        password = _read_password(env, problems)

        settings = cls(
            country_code=country,
            region_code=region,
            source_endpoint=endpoint,
            output_path=Path(text("IRIS_OUTPUT_PATH")),
            postgres_db=text("POSTGRES_DB"),
            postgres_user=text("POSTGRES_USER"),
            postgres_password=password,
            postgres_host=text("POSTGRES_HOST", "localhost"),
            postgres_port=number("POSTGRES_PORT", 5432, int, 1, 65535),
            migrations_dir=Path(text("IRIS_MIGRATIONS_DIR", "migrations")),
            fixtures_dir=Path(text("IRIS_FIXTURES_DIR", "fixtures")),
            db_connect_timeout_s=number("IRIS_DB_CONNECT_TIMEOUT_S", 5, int, 1, 300),
            db_connect_retries=number("IRIS_DB_CONNECT_RETRIES", 30, int, 0, 1000),
            db_connect_retry_delay_s=number(
                "IRIS_DB_CONNECT_RETRY_DELAY_S", 1.0, float, 0.0, 60.0
            ),
            area_tolerance_pct=number("IRIS_AREA_TOLERANCE_PCT", 5.0, float, 0.0, 100.0),
            source_timeout_s=number("IRIS_SOURCE_TIMEOUT_S", 15.0, float, 0.1, 300.0),
            source_max_bytes=number("IRIS_SOURCE_MAX_BYTES", 10_000_000, int, 1),
            log_level=text("IRIS_LOG_LEVEL", "INFO").upper(),
        )
        if settings.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            problems.append("IRIS_LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR")
        if problems:
            raise ConfigError(problems)
        return settings


def _read_password(env: Mapping[str, str], problems: list[str]) -> str:
    """POSTGRES_PASSWORD, or POSTGRES_PASSWORD_FILE (Docker/K8s secrets)."""
    direct = env.get("POSTGRES_PASSWORD")
    file_ref = env.get("POSTGRES_PASSWORD_FILE")
    if direct and file_ref:
        problems.append("set only one of POSTGRES_PASSWORD and POSTGRES_PASSWORD_FILE")
        return ""
    if file_ref:
        try:
            value = Path(file_ref).read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as error:
            problems.append(f"POSTGRES_PASSWORD_FILE is unreadable: {error.strerror}")
            return ""
    else:
        value = direct or ""
    if not value:
        problems.append("POSTGRES_PASSWORD (or POSTGRES_PASSWORD_FILE) is required")
    return value
