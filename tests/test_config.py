from __future__ import annotations

import pytest

from tri_clay.config import Settings, parse_env_file
from tri_clay.errors import ConfigError


def write_env(tmp_path, text: str):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_env_file_handles_quotes_and_comments(tmp_path):
    path = write_env(
        tmp_path,
        "# comment\nCLAY_API_KEY=\"abc\"\nCLAY_WORKSPACE_ID=123\n\nEMPTY=\n",
    )
    values = parse_env_file(path)
    assert values == {"CLAY_API_KEY": "abc", "CLAY_WORKSPACE_ID": "123", "EMPTY": ""}


def test_load_reads_file(tmp_path):
    path = write_env(
        tmp_path,
        "CLAY_API_KEY=key\nCLAY_WORKSPACE_ID=4242424\nCLAY_SESSION_COOKIE=claysession=x\n"
        "TRI_CLAY_PACE_SECONDS=2.5\nTRI_CLAY_MAX_TABLES_PER_HOUR=12\n",
    )
    settings = Settings.load(path)
    assert settings.api_key == "key"
    assert settings.workspace_id == "4242424"
    assert settings.session_cookie == "claysession=x"
    assert settings.pace_seconds == 2.5
    assert settings.max_tables_per_hour == 12
    assert settings.require_build_session() == "claysession=x"


def test_env_var_overrides_file(tmp_path, monkeypatch):
    path = write_env(tmp_path, "CLAY_API_KEY=file-key\nCLAY_WORKSPACE_ID=1\n")
    monkeypatch.setenv("CLAY_API_KEY", "env-key")
    assert Settings.load(path).api_key == "env-key"


def test_missing_api_key_raises(tmp_path):
    path = write_env(tmp_path, "CLAY_WORKSPACE_ID=1\n")
    with pytest.raises(ConfigError):
        Settings.load(path)


def test_bad_pace_raises(tmp_path):
    path = write_env(
        tmp_path,
        "CLAY_API_KEY=k\nCLAY_WORKSPACE_ID=1\nTRI_CLAY_PACE_SECONDS=fast\n",
    )
    with pytest.raises(ConfigError):
        Settings.load(path)


def test_missing_session_has_refresh_instructions(tmp_path):
    path = write_env(tmp_path, "CLAY_API_KEY=k\nCLAY_WORKSPACE_ID=1\n")
    settings = Settings.load(path)
    with pytest.raises(ConfigError) as excinfo:
        settings.require_build_session()
    assert "browser_login.js" in str(excinfo.value)


def test_redacted_never_contains_secrets(tmp_path):
    path = write_env(
        tmp_path,
        "CLAY_API_KEY=super-secret-key\nCLAY_WORKSPACE_ID=1\nCLAY_SESSION_COOKIE=claysession=abc\n",
    )
    redacted = Settings.load(path).redacted()
    blob = str(redacted)
    assert "super-secret-key" not in blob
    assert "claysession=abc" not in blob
    assert redacted["api_key"] == "<set>"
