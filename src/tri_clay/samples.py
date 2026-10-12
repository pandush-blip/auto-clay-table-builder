"""Summarize official-API search results without leaking personal data."""

from __future__ import annotations

import collections
from typing import Any


def _experience(row: dict[str, Any]) -> dict[str, Any]:
    experiences = row.get("matched_experiences") or []
    first = experiences[0] if experiences else None
    return first if isinstance(first, dict) else {}


def summarize_search_results(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") or []
    titles: collections.Counter[str] = collections.Counter()
    companies: collections.Counter[str] = collections.Counter()
    linkedin = 0
    for row in data:
        exp = _experience(row)
        title = row.get("job_title") or exp.get("title")
        company = exp.get("company")
        if isinstance(company, dict):
            company = company.get("name")
        if title:
            titles[str(title)] += 1
        if company:
            companies[str(company)] += 1
        if row.get("linkedin_url"):
            linkedin += 1
    return {
        "records": len(data),
        "has_more": payload.get("has_more"),
        "period_quota": payload.get("period_quota"),
        "linkedin_present": linkedin,
        "top_titles": titles.most_common(5),
        "top_companies": companies.most_common(5),
        "row_keys": sorted(data[0].keys()) if data else [],
    }
