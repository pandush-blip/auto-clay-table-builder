"""Load webapp/.env into os.environ (without overriding real env vars)."""

from __future__ import annotations

import os
from pathlib import Path

from tri_clay.config import parse_env_file

ENV_PATH = Path(__file__).resolve().parent / ".env"


def load_env() -> None:
    for key, value in parse_env_file(ENV_PATH).items():
        os.environ.setdefault(key, value)
