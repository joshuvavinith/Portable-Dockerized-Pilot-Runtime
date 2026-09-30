from __future__ import annotations

import pytest

from iris.config import ConfigError, Settings, redact_endpoint


def test_loads_all_values(env_factory):
    s = Settings.from_env(env_factory())
    assert (s.country_code, s.region_code) == ("DE", "NW")
    assert s.source_endpoint == "fixture://sites.geojson"
    assert s.postgres_port == 5432


def test_switching_region_needs_only_env(env_factory):
    s = Settings.from_env(env_factory(IRIS_COUNTRY_CODE="NL", IRIS_REGION_CODE="LI"))
    assert (s.country_code, s.region_code) == ("NL", "LI")


def test_all_problems_reported_together():
    with pytest.raises(ConfigError) as exc:
        Settings.from_env({})
    text = str(exc.value)
    for name in ("IRIS_COUNTRY_CODE", "IRIS_REGION_CODE", "IRIS_SOURCE_ENDPOINT",
                 "IRIS_OUTPUT_PATH", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"):
        assert name in text


@pytest.mark.parametrize("bad", ["de", "DEU", "D", "1A", ""])
def test_country_code_must_be_iso_alpha2(env_factory, bad):
    with pytest.raises(ConfigError, match="IRIS_COUNTRY_CODE"):
        Settings.from_env(env_factory(IRIS_COUNTRY_CODE=bad))


@pytest.mark.parametrize("bad", ["nw", "TOOLONGREGION", "N W", "   "])
def test_region_code_is_validated(env_factory, bad):
    with pytest.raises(ConfigError, match="IRIS_REGION_CODE"):
        Settings.from_env(env_factory(IRIS_REGION_CODE=bad))


@pytest.mark.parametrize("bad", ["ftp://x/y", "file:///etc/passwd", "sites.geojson", "fixture://"])
def test_endpoint_scheme_is_restricted(env_factory, bad):
    with pytest.raises(ConfigError, match="IRIS_SOURCE_ENDPOINT"):
        Settings.from_env(env_factory(IRIS_SOURCE_ENDPOINT=bad))


@pytest.mark.parametrize("port", ["0", "70000", "abc"])
def test_port_is_validated(env_factory, port):
    with pytest.raises(ConfigError, match="POSTGRES_PORT"):
        Settings.from_env(env_factory(POSTGRES_PORT=port))


def test_password_never_in_repr(env_factory):
    s = Settings.from_env(env_factory(POSTGRES_PASSWORD="s3cret-value"))
    assert "s3cret-value" not in repr(s) and "s3cret-value" not in str(s)


def test_password_file_supported(env_factory, tmp_path):
    secret = tmp_path / "pw"
    secret.write_text("from-file\n")
    env = env_factory()
    del env["POSTGRES_PASSWORD"]
    env["POSTGRES_PASSWORD_FILE"] = str(secret)
    assert Settings.from_env(env).postgres_password == "from-file"


def test_password_and_file_together_rejected(env_factory, tmp_path):
    secret = tmp_path / "pw"
    secret.write_text("x")
    with pytest.raises(ConfigError, match="only one"):
        Settings.from_env(env_factory(POSTGRES_PASSWORD_FILE=str(secret)))


def test_missing_password_file_rejected(env_factory):
    env = env_factory()
    del env["POSTGRES_PASSWORD"]
    env["POSTGRES_PASSWORD_FILE"] = "/nonexistent/secret"
    with pytest.raises(ConfigError, match="POSTGRES_PASSWORD_FILE"):
        Settings.from_env(env)


def test_redact_endpoint_removes_credentials_and_query():
    assert (redact_endpoint("https://user:pw@example.com:8443/a/b.geojson?token=abc#x")
            == "https://example.com:8443/a/b.geojson")
    assert redact_endpoint("fixture://sites.geojson") == "fixture://sites.geojson"
