"""Official Clay Public API client (check layer).

Used for identity, credit balances, the query reference and 25-row samples.
Search results from this API are free (verified live: balance unchanged) but the
workspace quota is capped at 1 M results / rolling 30 days, so it never carries
bulk volume.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from .errors import OfficialApiError, SessionExpiredError


@dataclass(frozen=True)
class Balance:
    balance: float
    action_execution_balance: float | None = None

    def as_dict(self) -> dict[str, float | None]:
        return {
            "balance": self.balance,
            "action_execution_balance": self.action_execution_balance,
        }


class OfficialApiClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.clay.com/public/v0",
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 5,
        backoff_base: float = 3.0,
    ) -> None:
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"clay-api-key": api_key, "accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OfficialApiClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        import random

        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._client.request(method, path, json=payload)
            except httpx.HTTPError as exc:
                if attempt <= self._max_retries:
                    time.sleep(min(self._backoff_base**attempt, 60) * random.uniform(0.8, 1.2))
                    continue
                raise OfficialApiError(f"{method} {path} failed: {exc}") from exc
            retryable = response.status_code == 429 or 500 <= response.status_code < 600
            if retryable and attempt <= self._max_retries:
                delay = min(self._backoff_base**attempt, 60) * random.uniform(0.8, 1.2)
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = max(delay, min(float(retry_after), 90))
                if "concurrent" in response.text.lower():
                    # Clay's workspace concurrency limit clears after the in-flight
                    # requests finish; the exponential delays above are the fix.
                    delay = max(delay, 5.0)
                time.sleep(delay)
                continue
            break
        if response.status_code == 401:
            raise SessionExpiredError(
                f"Clay Public API rejected the key on {method} {path} (401). "
                "Check CLAY_API_KEY; create a new one with `clay api-keys create`."
            )
        if response.status_code >= 400:
            message = response.text[:400]
            raise OfficialApiError(
                f"{method} {path} -> {response.status_code}: {message}",
                status=response.status_code,
            )
        return response.json()

    def get_me(self) -> dict:
        return self._request("GET", "/me")

    def get_balance(self) -> Balance:
        data = self._request("GET", "/credits/balance")
        return Balance(
            balance=float(data["balance"]),
            action_execution_balance=(
                float(data["action_execution_balance"])
                if data.get("action_execution_balance") is not None
                else None
            ),
        )

    def get_query_reference(self) -> str:
        return self._request("GET", "/search/query-mode/reference")["reference"]

    def create_search(self, query: str) -> dict:
        """POST /search/query-mode -> {search_id, source_type}. 400 on bad queries."""
        return self._request("POST", "/search/query-mode", {"query": query})

    def run_search_page(self, search_id: str, limit: int = 25) -> dict:
        if not 1 <= limit <= 500:
            raise OfficialApiError("limit must be between 1 and 500")
        return self._request("POST", f"/search/query-mode/{search_id}/run", {"limit": limit})
