"""Configuration loading for tri-clay.

Reads `.env` (gitignored) and lets real environment variables override it.
Secret values are never included in `Settings.redacted()`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

DEFAULT_ENV_PATH = Path(".env")


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


@dataclass(frozen=True)
class Settings:
    api_key: str
    workspace_id: str
    api_base: str = "https://api.clay.com/public/v0"
    webapp_base: str = "https://api.clay.com"
    session_cookie: str | None = None
    webapp_token: str | None = None
    target_folder_id: str | None = None
    pace_seconds: float = 3.0
    max_tables_per_hour: int = 30
    env_path: Path = field(default=DEFAULT_ENV_PATH, repr=False)

    @classmethod
    def load(cls, env_path: Path | None = None) -> "Settings":
        path = Path(env_path) if env_path else DEFAULT_ENV_PATH
        file_values = parse_env_file(path)

        def get(key: str, default: str | None = None) -> str | None:
            if key in os.environ and os.environ[key] != "":
                return os.environ[key]
            value = file_values.get(key)
            if value:
                return value
            return default

        api_key = get("CLAY_API_KEY")
        if not api_key:
            raise ConfigError(
                "CLAY_API_KEY is missing. Create one with `clay api-keys create` "
                "and put it in .env (see .env.example)."
            )
        workspace_id = get("CLAY_WORKSPACE_ID")
        if not workspace_id:
            raise ConfigError("CLAY_WORKSPACE_ID is missing. Example: CLAY_WORKSPACE_ID=4242424")

        pace_raw = get("TRI_CLAY_PACE_SECONDS", "3")
        try:
            pace = float(pace_raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"TRI_CLAY_PACE_SECONDS must be a number, got {pace_raw!r}") from exc
        if not 0 <= pace <= 60:
            raise ConfigError("TRI_CLAY_PACE_SECONDS must be between 0 and 60")

        rate_raw = get("TRI_CLAY_MAX_TABLES_PER_HOUR", "30")
        try:
            max_tables = int(rate_raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"TRI_CLAY_MAX_TABLES_PER_HOUR must be an integer, got {rate_raw!r}"
            ) from exc
        if max_tables < 1:
            raise ConfigError("TRI_CLAY_MAX_TABLES_PER_HOUR must be at least 1")

        return cls(
            api_key=api_key,
            workspace_id=str(workspace_id),
            api_base=(get("CLAY_API_BASE") or "https://api.clay.com/public/v0").rstrip("/"),
            webapp_base=(get("CLAY_WEBAPP_BASE") or "https://api.clay.com").rstrip("/"),
            session_cookie=get("CLAY_SESSION_COOKIE"),
            webapp_token=get("CLAY_WEBAPP_TOKEN"),
            target_folder_id=get("CLAY_TARGET_FOLDER_ID"),
            pace_seconds=pace,
            max_tables_per_hour=max_tables,
            env_path=path,
        )

    def require_build_session(self) -> str:
        if not self.session_cookie:
            raise ConfigError(
                "CLAY_SESSION_COOKIE is missing. The build layer needs the web-app session.\n"
                "Refresh it with:  python tools/browser_login.js  (opens Chrome, sign in once)\n"
                "then copy the claysession cookie from runs/phase0/browser-cookies.json into .env."
            )
        return self.session_cookie

    def redacted(self) -> dict[str, object]:
        return {
            "api_base": self.api_base,
            "webapp_base": self.webapp_base,
            "workspace_id": self.workspace_id,
            "target_folder_id": self.target_folder_id,
            "api_key": "<set>" if self.api_key else "<missing>",
            "session_cookie": "<set>" if self.session_cookie else "<missing>",
            "webapp_token": "<set>" if self.webapp_token else "<missing>",
            "pace_seconds": self.pace_seconds,
            "max_tables_per_hour": self.max_tables_per_hour,
            "env_path": str(self.env_path),
        }
