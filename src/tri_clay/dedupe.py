"""Stage 8: dedupe and company contact caps."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def linkedin_key(row: dict[str, Any]) -> str | None:
    url = (row.get("linkedin_url") or "").strip().lower()
    if url:
        return "li:" + url
    first = (row.get("first_name_clean") or row.get("first_name") or "").strip().lower()
    last = (row.get("last_name") or "").strip().lower()
    domain = (row.get("company_domain") or "").strip().lower()
    if first and last and domain:
        return f"ncd:{first}|{last}|{domain}"
    return None


def priority_of(row: dict[str, Any]) -> int:
    try:
        return int(row.get("priority") or 9999)
    except (TypeError, ValueError):
        return 9999


@dataclass
class DedupeResult:
    kept: list[dict[str, Any]]
    removed_within: int = 0
    removed_across: int = 0
    cap_removed: int = 0
    also_in_lists: dict[str, list[str]] = field(default_factory=dict)


def dedupe_within(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    seen: dict[str, dict[str, Any]] = {}
    removed = 0
    for row in rows:
        key = linkedin_key(row)
        if key is None:
            seen[f"row:{len(seen)}:{id(row)}"] = row
            continue
        if key in seen:
            removed += 1
            continue
        seen[key] = row
    return list(seen.values()), removed


def dedupe_across(
    rows_by_audience: dict[str, list[dict[str, Any]]],
    *,
    scope: str = "client",
) -> tuple[dict[str, list[dict[str, Any]]], int, dict[str, list[str]]]:
    """Keep the best-priority row per key across audiences (scope client) or per audience."""
    if scope == "audience":
        return rows_by_audience, 0, {}
    best: dict[str, dict[str, Any]] = {}
    owner: dict[str, str] = {}
    also_in: dict[str, set[str]] = {}
    for audience_id, rows in rows_by_audience.items():
        for row in rows:
            key = linkedin_key(row)
            if key is None:
                continue
            if key not in best or priority_of(row) < priority_of(best[key]):
                best[key] = row
                owner[key] = audience_id
    removed = 0
    kept: dict[str, list[dict[str, Any]]] = {audience_id: [] for audience_id in rows_by_audience}
    for audience_id, rows in rows_by_audience.items():
        for row in rows:
            key = linkedin_key(row)
            if key is None or owner.get(key) == audience_id:
                kept[audience_id].append(row)
            else:
                removed += 1
                also_in.setdefault(key, set()).add(row.get("list_name") or audience_id)
    for audience_id, rows in kept.items():
        for row in rows:
            key = linkedin_key(row)
            others = sorted(also_in.get(key or "", set()) - {row.get("list_name") or audience_id})
            if others:
                row["also_in_lists"] = " | ".join(others)
    return kept, removed, {k: sorted(v) for k, v in also_in.items()}


def enforce_contact_cap(
    rows: list[dict[str, Any]], max_per_company: int | None
) -> tuple[list[dict[str, Any]], int]:
    if not max_per_company or max_per_company <= 0:
        return rows, 0
    counts: dict[str, int] = {}
    kept: list[dict[str, Any]] = []
    removed = 0
    for row in rows:
        key = (row.get("company_domain") or row.get("company_name_normalized") or "").lower()
        if not key:
            kept.append(row)
            continue
        if counts.get(key, 0) >= max_per_company:
            removed += 1
            continue
        counts[key] = counts.get(key, 0) + 1
        kept.append(row)
    return kept, removed
