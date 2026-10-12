"""Stage 4: sizing and shard planning."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from .csv_input import Audience
from .errors import TriClayError
from .reference import COMPANY_SIZE_BUCKETS, US_STATES

DEFAULT_SHARD_LIMIT = 48_000
LIMIT_BY_CLAUSE = re.compile(r"(\s+limit\s+\d+\s+by\s+[A-Za-z0-9_.]+\s*)$", re.I)
TRAILING_LIMIT = re.compile(r"\s+limit\s+\d+\s*$", re.I)


class PlanningError(TriClayError):
    """The audience cannot be sharded safely."""


def strip_preview_limit(query: str) -> str:
    """Remove a UI-appended ` limit N` but keep `limit N by field` clauses."""
    return TRAILING_LIMIT.sub("", query.rstrip())


def with_preview_limit(query: str, limit: int = 50) -> str:
    return strip_preview_limit(query) + f" limit {limit}"


def add_predicate(query: str, predicate: str) -> str:
    match = LIMIT_BY_CLAUSE.search(query)
    if match:
        return query[: match.start()].rstrip() + "\n  and " + predicate + match.group(1)
    return query.rstrip() + "\n  and " + predicate


@dataclass
class Shard:
    index: int
    total: int
    predicate: str
    count: int
    table_name: str = ""
    query: str = ""


@dataclass
class AudiencePlan:
    audience: Audience
    parent_count: int
    effective_size: int
    shards: list[Shard] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def effective_size(count: int, target_people: int | None, pause_if_above: int | None) -> int:
    size = count
    if target_people:
        size = min(size, target_people)
    if pause_if_above:
        size = min(size, pause_if_above)
    return size


def table_name(client: str, audience_name: str, index: int, total: int, day: date) -> str:
    return f"{client} | {audience_name} | {index}/{total} | {day.isoformat()}"


def _pack(
    values: list[tuple[str, int]],
    predicate_fn: Callable[[str], str],
    limit: int,
) -> tuple[list[tuple[str, int]], list[str]]:
    """Greedy-pack (value, count) pairs into groups <= limit. Returns groups + notes."""
    groups: list[tuple[list[str], int]] = []
    current: list[str] = []
    current_count = 0
    notes: list[str] = []
    for value, count in values:
        if count == 0:
            continue
        if count > limit:
            notes.append(f"value {value!r} alone has {count} rows (> {limit})")
            continue
        if current and current_count + count > limit:
            groups.append((current, current_count))
            current, current_count = [], 0
        current.append(value)
        current_count += count
    if current:
        groups.append((current, current_count))
    packed = [
        (predicate_fn(",".join(f'"{v}"' for v in group)), group_count)
        for group, group_count in groups
    ]
    return packed, notes


def plan_audience(
    audience: Audience,
    count_fn: Callable[[str], int],
    *,
    limit: int = DEFAULT_SHARD_LIMIT,
    day: date | None = None,
) -> AudiencePlan:
    day = day or date.today()
    query = strip_preview_limit(audience.clay_query_draft)
    parent = count_fn(query)
    size = effective_size(parent, audience.target_people, audience.pause_if_above)
    plan = AudiencePlan(audience=audience, parent_count=parent, effective_size=size)
    if size <= limit:
        plan.shards.append(
            Shard(
                1,
                1,
                "",
                min(size, limit),
                table_name(audience.client, audience.audience_name, 1, 1, day),
                query,
            )
        )
        return plan

    strategies: list[tuple[str, tuple[str, ...], str]] = [
        ("person location_state", US_STATES, "location_state"),
        ("company company_size", COMPANY_SIZE_BUCKETS, "company.company_size"),
    ]
    industries = re.findall(r'company\.industry\s+in\s*\(([^)]*)\)', query, re.I)
    if industries:
        values = tuple(v.strip() for v in re.findall(r'"([^"]+)"', industries[0]))
        strategies.append(("company industry", values, "company.industry"))

    best: list[Shard] | None = None
    notes: list[str] = []
    for label, candidates, field_name in strategies:
        counts: list[tuple[str, int]] = []
        for value in candidates:
            predicate = f'{field_name} in ("{value}")'
            counts.append((value, count_fn(add_predicate(query, predicate))))
        packed, pack_notes = _pack(
            counts, lambda group: f"{field_name} in ({group})", limit
        )
        notes.extend(f"{label}: {n}" for n in pack_notes)
        covered = sum(count for _, count in counts)
        covered_values = [v for v, c in counts if c > 0]
        residual = parent - covered
        if covered_values and residual > 0:
            residual_predicate = (
                f"{field_name} not_in (" + ",".join(f'"{v}"' for v in covered_values) + ")"
            )
            residual_count = count_fn(add_predicate(query, residual_predicate))
            if residual_count != residual:
                notes.append(
                    f"{label}: residual mismatch (computed {residual}, queried {residual_count})"
                )
            if residual_count > limit:
                notes.append(
                    f"{label}: residual shard of {residual_count} rows exceeds {limit}; "
                    "trying next strategy"
                )
                continue
            packed.append((residual_predicate, residual_count))
        if not packed:
            continue
        shards: list[Shard] = []
        total = len(packed)
        for index, (predicate, _) in enumerate(packed, start=1):
            shard_query = add_predicate(query, predicate)
            shard_count = count_fn(shard_query)
            shards.append(
                Shard(
                    index,
                    total,
                    predicate,
                    shard_count,
                    table_name(audience.client, audience.audience_name, index, total, day),
                    shard_query,
                )
            )
        counted = sum(s.count for s in shards)
        if counted != parent:
            notes.append(
                f"{label}: shard counts sum to {counted} but parent count is {parent}"
            )
        best = shards
        plan.notes = notes
        break

    if best is None:
        raise PlanningError(
            f"audience {audience.audience_id}: cannot split {parent} rows into shards "
            f"<= {limit} with the available strategies"
        )
    plan.shards = best
    plan.notes = notes
    return plan
