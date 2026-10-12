"""Build-layer HTTP client: session cookie, pacing, retries, allowlist enforcement."""

from __future__ import annotations

import random
import time
from collections import deque
from typing import Any

import httpx

from .allowlist import validate_request
from .config import Settings
from .errors import SessionExpiredError, TriClayError

WRITE_METHODS = {"POST", "PATCH", "PUT", "DELETE"}
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class RateLimiter:
    def __init__(self, max_per_hour: int) -> None:
        self._max = max_per_hour
        self._events: deque[float] = deque()

    def check(self) -> None:
        now = time.monotonic()
        while self._events and now - self._events[0] > 3600:
            self._events.popleft()
        if len(self._events) >= self._max:
            wait = 3600 - (now - self._events[0]) + 1
            raise TriClayError(
                f"table rate limit reached ({self._max}/hour); retry in {wait:.0f}s"
            )

    def record(self) -> None:
        self._events.append(time.monotonic())


class WebAppClient:
    def __init__(
        self,
        settings: Settings,
        timeout: float = 180.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        cookie = settings.require_build_session()
        self.settings = settings
        self._client = httpx.Client(
            base_url=settings.webapp_base,
            headers={
                "accept": "application/json",
                "x-clay-frontend-version": "1.0.0",
            },
            timeout=timeout,
            transport=transport,
        )
        for pair in cookie.split(";"):
            pair = pair.strip()
            if "=" in pair:
                name, _, value = pair.partition("=")
                self._client.cookies.set(name.strip(), value.strip(), domain=".clay.com")
        self._limiter = RateLimiter(settings.max_tables_per_hour)
        self._last_write = 0.0

    # ------------------------------------------------------------------ helpers
    def _pace(self) -> None:
        base = self.settings.pace_seconds
        if base <= 0:
            return
        delay = base * random.uniform(0.6, 1.4)
        elapsed = time.monotonic() - self._last_write
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_write = time.monotonic()

    def _persist_cookie(self) -> None:
        pairs = [f"{c.name}={c.value}" for c in self._client.cookies.jar if "clay.com" in (c.domain or "")]
        if not pairs:
            return
        header = "; ".join(pairs)
        lines = [
            line
            for line in self.settings.env_path.read_text(encoding="utf-8").splitlines()
            if not line.startswith("CLAY_SESSION_COOKIE=")
        ]
        lines.append("CLAY_SESSION_COOKIE=" + header)
        self.settings.env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def request(self, method: str, path: str, json: Any = None) -> dict:
        validate_request(method, path, json)
        if method.upper() in WRITE_METHODS:
            self._pace()
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._client.request(method, path, json=json)
            except httpx.HTTPError as exc:
                # DNS/TLS hiccups on this machine are transient; ride them out.
                if attempt <= 5:
                    time.sleep(min(2 ** attempt, 30))
                    continue
                raise TriClayError(f"{method} {path} failed: {exc}") from exc
            if response.status_code in RETRYABLE_STATUS and attempt <= 3:
                time.sleep(2 ** attempt)
                continue
            break
        if response.status_code in (401, 403):
            self._persist_cookie()
            raise SessionExpiredError(
                f"web-app session rejected on {method} {path} ({response.status_code}).\n"
                "Refresh it: python tools/browser_login.js (sign in once), then copy the\n"
                "claysession cookie from runs/phase0/browser-cookies.json into .env."
            )
        if response.status_code >= 400:
            raise TriClayError(
                f"{method} {path} -> {response.status_code}: {response.text[:400]}"
            )
        self._persist_cookie()
        return response.json() if response.content else {}

    def get(self, path: str) -> dict:
        return self.request("GET", path)

    def post(self, path: str, payload: dict) -> dict:
        return self.request("POST", path, payload)

    def patch(self, path: str, payload: dict) -> dict:
        return self.request("PATCH", path, payload)

    def people_search_limit(self) -> int:
        return int(
            self.get(f"/v3/workspaces/{self.settings.workspace_id}/peopleSearchLimit")[
                "peopleSearchLimit"
            ]
        )

    def preview(self, query: str) -> dict:
        """Preview a query; returns rows/totalCount/resolvedResources."""
        return self.post(
            f"/v3/workspaces/{self.settings.workspace_id}/search-dsl/query",
            {
                "query": query,
                "workspaceId": str(self.settings.workspace_id),
                "selectMode": "preview",
                "userQueryHasLimit": False,
            },
        )

    def close(self) -> None:
        self._persist_cookie()
        self._client.close()

    def __enter__(self) -> "WebAppClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
