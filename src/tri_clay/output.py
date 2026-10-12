"""Stage 9: output schema and CSV writers."""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

OUTPUT_COLUMNS = (
    "client",
    "list_name",
    "audience_id",
    "tier",
    "priority",
    "first_name_clean",
    "first_name",
    "last_name",
    "full_name",
    "job_title",
    "linkedin_url",
    "person_location",
    "company_name",
    "company_name_normalized",
    "normalized_by",
    "company_domain",
    "company_linkedin_url",
    "company_size",
    "company_industry",
    "also_in_lists",
    "shard",
    "clay_table_id",
    "query_hash",
    "built_at",
)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:60] or "audience"


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(OUTPUT_COLUMNS), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in OUTPUT_COLUMNS})


def audience_filename(audience_id: str, audience_name: str) -> str:
    return f"{audience_id}__{slugify(audience_name)}.csv"
