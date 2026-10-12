from __future__ import annotations

import httpx
import pytest

from tri_clay.errors import OfficialApiError
from tri_clay.official_api import OfficialApiClient
from tri_clay.samples import summarize_search_results


def test_create_search_and_run_shape():
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/search/query-mode"):
            return httpx.Response(200, json={"search_id": "search_1", "source_type": "people"})
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "name": "Jane Doe",
                        "linkedin_url": "https://www.linkedin.com/in/x",
                        "matched_experiences": [
                            {"company": "Acme", "title": "Co-Founder"}
                        ],
                    }
                ],
                "has_more": True,
                "period_quota": {"limit": 1000000, "used": 1, "remaining": 999999},
            },
        )

    client = OfficialApiClient("key", transport=httpx.MockTransport(handler))
    search = client.create_search("select from people")
    assert search["search_id"] == "search_1"
    payload = client.run_search_page(search["search_id"], 25)
    summary = summarize_search_results(payload)
    assert summary["records"] == 1
    assert summary["linkedin_present"] == 1
    assert summary["top_titles"] == [("Co-Founder", 1)]
    assert summary["top_companies"] == [("Acme", 1)]
    assert calls[0][1].endswith("/search/query-mode")
    assert calls[1][1].endswith("/search/query-mode/search_1/run")


def test_limit_bounds():
    client = OfficialApiClient("key", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with pytest.raises(OfficialApiError):
        client.run_search_page("search_1", 501)
    with pytest.raises(OfficialApiError):
        client.run_search_page("search_1", 0)


def test_400_raises_official_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "Invalid value \"Computer Software\""})

    client = OfficialApiClient("key", transport=httpx.MockTransport(handler))
    with pytest.raises(OfficialApiError) as excinfo:
        client.create_search("bad query")
    assert "Invalid value" in str(excinfo.value)


def test_504_is_retried():
    responses = [
        httpx.Response(504, json={"title": "Gateway time-out"}),
        httpx.Response(200, json={"search_id": "search_9", "source_type": "people"}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    client = OfficialApiClient(
        "key", transport=httpx.MockTransport(handler), backoff_base=0
    )
    assert client.create_search("select from people")["search_id"] == "search_9"
