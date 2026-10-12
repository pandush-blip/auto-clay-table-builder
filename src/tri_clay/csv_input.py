"""Stage 1: load and validate the audiences CSV."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import InputError

REQUIRED_COLUMNS = (
    "client",
    "audience_id",
    "audience_name",
    "tier",
    "priority",
    "result_entity",
    "clay_query_draft",
    "max_contacts_per_company",
    "target_people",
    "pause_if_below",
    "pause_if_above",
    "dedupe_scope",
    "not_expressible",
    "proxy_applied",
    "run_notes",
)


def split_multi(value: str | None) -> list[str]:
    """Multi-value cells are separated by ' | '; empty cell = no value."""
    if not value:
        return []
    return [part.strip() for part in value.split("|") if part.strip()]


def _int_or_none(row: dict[str, str], column: str, row_number: int) -> int | None:
    raw = (row.get(column) or "").strip()
    if not raw:
        return None
    text = raw.replace(",", "")
    range_match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", text)
    if range_match:
        # Audience-builder matrices express targets as ranges; the max bound is the
        # "max rows to build" for this audience.
        return int(range_match.group(2))
    plus_match = re.fullmatch(r"(\d+)\s*\+", text)
    if plus_match:
        return int(plus_match.group(1))
    try:
        return int(text)
    except ValueError as exc:
        raise InputError(
            f"row {row_number}, column {column}: {raw!r} is not an integer"
        ) from exc


@dataclass
class Audience:
    client: str
    audience_id: str
    audience_name: str
    tier: str
    priority: int | None
    result_entity: str
    clay_query_draft: str
    max_contacts_per_company: int | None
    target_people: int | None
    pause_if_below: int | None
    pause_if_above: int | None
    dedupe_scope: str
    not_expressible: list[str] = field(default_factory=list)
    proxy_applied: list[str] = field(default_factory=list)
    run_notes: list[str] = field(default_factory=list)
    row_number: int = 0


@dataclass
class LoadResult:
    audiences: list[Audience]
    warnings: list[str]


def load_audiences(path: Path | str) -> LoadResult:
    csv_path = Path(path)
    if not csv_path.exists():
        raise InputError(f"audiences CSV not found: {csv_path}")
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames or []
        missing = [c for c in REQUIRED_COLUMNS if c not in columns]
        if missing:
            raise InputError(f"missing required column(s): {', '.join(missing)}")

        audiences: list[Audience] = []
        warnings: list[str] = []
        loose_scopes: dict[str, int] = {}
        seen_ids: set[str] = set()
        for index, row in enumerate(reader, start=2):
            audience_id = (row.get("audience_id") or "").strip()
            if not audience_id:
                raise InputError(f"row {index}, column audience_id: must not be empty")
            if audience_id in seen_ids:
                raise InputError(f"row {index}, column audience_id: duplicate id {audience_id!r}")
            seen_ids.add(audience_id)

            query = (row.get("clay_query_draft") or "").strip()
            if not query:
                raise InputError(f"row {index}, column clay_query_draft: must not be empty")

            result_entity = (row.get("result_entity") or "people").strip().lower()
            if result_entity and result_entity != "people":
                warnings.append(
                    f"row {index} ({audience_id}): result_entity={result_entity!r} skipped — v1 supports people only"
                )
                continue

            raw_scope = (row.get("dedupe_scope") or "").strip().lower()
            if raw_scope in ("", "client"):
                dedupe_scope = "client"
            elif raw_scope == "audience":
                dedupe_scope = "audience"
            else:
                dedupe_scope = "client"
                loose_scopes[raw_scope[:60]] = loose_scopes.get(raw_scope[:60], 0) + 1

            audiences.append(
                Audience(
                    client=(row.get("client") or "").strip(),
                    audience_id=audience_id,
                    audience_name=(row.get("audience_name") or "").strip(),
                    tier=(row.get("tier") or "").strip(),
                    priority=_int_or_none(row, "priority", index),
                    result_entity="people",
                    clay_query_draft=query,
                    max_contacts_per_company=_int_or_none(row, "max_contacts_per_company", index),
                    target_people=_int_or_none(row, "target_people", index),
                    pause_if_below=_int_or_none(row, "pause_if_below", index),
                    pause_if_above=_int_or_none(row, "pause_if_above", index),
                    dedupe_scope=dedupe_scope,
                    not_expressible=split_multi(row.get("not_expressible")),
                    proxy_applied=split_multi(row.get("proxy_applied")),
                    run_notes=split_multi(row.get("run_notes")),
                    row_number=index,
                )
            )
    if not audiences:
        raise InputError("no buildable audiences in the CSV (all rows skipped?)")
    for scope, count in loose_scopes.items():
        warnings.append(
            f"dedupe_scope {scope!r} (x{count}) is not 'audience'/'client' - "
            "defaulting to client-level dedupe"
        )
    return LoadResult(audiences=audiences, warnings=warnings)
