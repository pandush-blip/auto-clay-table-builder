from __future__ import annotations

import re
from datetime import date

import pytest

from tri_clay.csv_input import Audience
from tri_clay.shard import (
    PlanningError,
    add_predicate,
    effective_size,
    plan_audience,
    strip_preview_limit,
    with_preview_limit,
)


def audience(**overrides) -> Audience:
    base = dict(
        client="Acme",
        audience_id="aud-1",
        audience_name="Founders",
        tier="T1",
        priority=1,
        result_entity="people",
        clay_query_draft='select from people\nwhere location_country in ("United States")\nlimit 3 by clay_company_id',
        max_contacts_per_company=3,
        target_people=None,
        pause_if_below=None,
        pause_if_above=None,
        dedupe_scope="client",
        row_number=2,
    )
    base.update(overrides)
    return Audience(**base)


def test_effective_size():
    assert effective_size(100, None, None) == 100
    assert effective_size(100, 40, None) == 40
    assert effective_size(100, 40, 30) == 30
    assert effective_size(100, 40, 300) == 40


def test_preview_limit_helpers():
    q = 'select from people where x = 1 limit 3 by clay_company_id'
    assert strip_preview_limit(q + " limit 50") == q
    assert strip_preview_limit(q) == q
    assert with_preview_limit(q) == q + " limit 50"


def test_add_predicate_keeps_limit_by_clause():
    q = 'select from people\nwhere x = 1\nlimit 3 by clay_company_id'
    out = add_predicate(q, 'location_state in ("Texas")')
    assert out.endswith("limit 3 by clay_company_id")
    assert 'and location_state in ("Texas")' in out


def state_count(parent: int, state_counts: dict[str, int]):
    def count(query: str) -> int:
        m = re.search(r'location_state\s+(not_in|in)\s*\(([^)]*)\)', query)
        if not m:
            return parent
        values = re.findall(r'"([^"]+)"', m.group(2))
        total = sum(state_counts.get(v, 0) for v in values)
        return parent - total if m.group(1) == "not_in" else total

    return count


def test_single_shard_when_small():
    plan = plan_audience(audience(), lambda q: 1000, day=date(2026, 10, 2))
    assert len(plan.shards) == 1
    assert plan.shards[0].table_name == "Acme | Founders | 1/1 | 2026-10-02"


def test_packing_is_exclusive_and_exhaustive():
    counts = {"California": 30000, "Texas": 20000, "New York": 10000}
    plan = plan_audience(
        audience(), state_count(60000, counts), day=date(2026, 10, 2)
    )
    assert len(plan.shards) == 2
    assert sum(shard.count for shard in plan.shards) == 60000
    predicates = [shard.predicate for shard in plan.shards]
    assert any("California" in p and "New York" in p for p in predicates)
    assert any("Texas" in p for p in predicates)


def test_residual_shard_is_added_and_checked():
    counts = {"California": 30000, "Texas": 20000, "New York": 5000}
    plan = plan_audience(audience(), state_count(60000, counts), day=date(2026, 10, 2))
    assert sum(shard.count for shard in plan.shards) == 60000
    assert any("not_in" in shard.predicate for shard in plan.shards)


def test_unshardable_value_raises():
    counts = {"California": 60000}
    with pytest.raises(PlanningError):
        plan_audience(audience(), state_count(60000, counts))


def test_industry_strategy_used_when_states_fail():
    query = (
        'select from people where company.industry in ("Computer Hardware","Computer Networking")\n'
        "limit 3 by clay_company_id"
    )
    counts = {"Computer Hardware": 30000, "Computer Networking": 25000}

    def count(q: str) -> int:
        if "location_state" in q:
            return 0
        if "company_size" in q:
            return 0
        matches = re.findall(r'company\.industry\s+(not_in|in)\s*\(([^)]*)\)', q)
        if not matches:
            return 55000
        m = matches[-1]
        values = re.findall(r'"([^"]+)"', m[1])
        total = sum(counts.get(v, 0) for v in values)
        return 55000 - total if m[0] == "not_in" else total

    plan = plan_audience(audience(clay_query_draft=query), count, day=date(2026, 10, 2))
    assert sum(shard.count for shard in plan.shards) == 55000
