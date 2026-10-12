"""Wire stages 7-9 together: map export rows, dedupe, cap, write outputs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .builder import BuiltTable, TableBuilder
from .csv_input import Audience
from .dedupe import dedupe_across, dedupe_within, enforce_contact_cap
from .derive import (
    canonical_linkedin,
    clean_domain,
    clean_first_name,
    normalize_company_name,
)
from .output import audience_filename, write_csv


def query_hash(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:12]


def map_export_row(
    raw: dict[str, str],
    *,
    audience: Audience,
    shard_index: int,
    shard_total: int,
    table_id: str,
    built_at: str,
) -> dict[str, Any]:
    company = (raw.get("Company") or "").strip()
    city = (raw.get("City") or "").strip()
    state = (raw.get("State or Province") or "").strip()
    country = (raw.get("Country") or "").strip()
    location = ", ".join(part for part in (city, state, country) if part)
    return {
        "client": audience.client,
        "list_name": (raw.get("List Name") or audience.audience_name).strip(),
        "audience_id": audience.audience_id,
        "tier": audience.tier,
        "priority": audience.priority if audience.priority is not None else "",
        "first_name_clean": clean_first_name(raw.get("First Name")),
        "first_name": (raw.get("First Name") or "").strip(),
        "last_name": (raw.get("Last Name") or "").strip(),
        "full_name": (raw.get("Full Name") or "").strip(),
        "job_title": (raw.get("Job Title") or "").strip(),
        "linkedin_url": canonical_linkedin(raw.get("LinkedIn Profile")),
        "person_location": location,
        "company_name": company,
        "company_name_normalized": normalize_company_name(company),
        "normalized_by": "local",
        "company_domain": clean_domain(raw.get("Domain")),
        "company_linkedin_url": canonical_linkedin(raw.get("Company LinkedIn")),
        "company_size": "",
        "company_industry": "",
        "also_in_lists": "",
        "shard": f"{shard_index}/{shard_total}",
        "clay_table_id": table_id,
        "query_hash": query_hash(audience.clay_query_draft),
        "built_at": built_at,
    }


def trim_to_target(rows: list[dict[str, Any]], target: int | None) -> list[dict[str, Any]]:
    if target and len(rows) > target:
        return rows[:target]
    return rows


@dataclass
class AudienceOutcome:
    audience: Audience
    rows: list[dict[str, Any]]
    removed_within: int
    removed_across: int
    cap_removed: int

    @property
    def domain_fill_rate(self) -> float:
        if not self.rows:
            return 0.0
        filled = sum(1 for row in self.rows if row.get("company_domain"))
        return round(100 * filled / len(self.rows), 1)


def process_outputs(
    rows_by_audience: dict[str, list[dict[str, Any]]],
    audiences: dict[str, Audience],
    output_dir: Path,
) -> tuple[list[AudienceOutcome], Path]:
    outcomes: list[AudienceOutcome] = []
    within: dict[str, list[dict[str, Any]]] = {}
    removed_within: dict[str, int] = {}
    for audience_id, rows in rows_by_audience.items():
        kept, removed = dedupe_within(rows)
        within[audience_id] = kept
        removed_within[audience_id] = removed

    by_scope: dict[str, list[str]] = {}
    for audience_id, audience in audiences.items():
        by_scope.setdefault(audience.dedupe_scope, []).append(audience_id)

    across_kept: dict[str, list[dict[str, Any]]] = {
        audience_id: [] for audience_id in rows_by_audience
    }
    across_removed: dict[str, int] = {audience_id: 0 for audience_id in rows_by_audience}
    for scope, ids in by_scope.items():
        subset = {audience_id: within[audience_id] for audience_id in ids}
        kept, removed, _ = dedupe_across(subset, scope="audience" if scope == "audience" else "client")
        for audience_id, rows in kept.items():
            across_kept[audience_id] = rows
        if len(ids) > 1:
            per = removed // max(len(ids), 1)
            for audience_id in ids:
                across_removed[audience_id] = per

    for audience_id, rows in across_kept.items():
        audience = audiences[audience_id]
        capped, cap_removed = enforce_contact_cap(rows, audience.max_contacts_per_company)
        outcomes.append(
            AudienceOutcome(
                audience=audience,
                rows=trim_to_target(capped, audience.target_people),
                removed_within=removed_within[audience_id],
                removed_across=across_removed[audience_id],
                cap_removed=cap_removed,
            )
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    merged: list[dict[str, Any]] = []
    for outcome in outcomes:
        write_csv(
            output_dir / audience_filename(outcome.audience.audience_id, outcome.audience.audience_name),
            outcome.rows,
        )
        merged.extend(outcome.rows)
    merged_path = output_dir / f"{outcomes[0].audience.client}__merged.csv" if outcomes else output_dir / "merged.csv"
    write_csv(merged_path, merged)
    return outcomes, merged_path


def built_at_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
