from __future__ import annotations

import csv

from tri_clay.dedupe import dedupe_across, dedupe_within, enforce_contact_cap
from tri_clay.output import OUTPUT_COLUMNS, write_csv


def row(li="https://www.linkedin.com/in/x", priority=1, domain="acme.com", list_name="A"):
    return {
        "linkedin_url": li,
        "first_name_clean": "Jane",
        "last_name": "Doe",
        "company_domain": domain,
        "company_name_normalized": "Acme",
        "priority": priority,
        "list_name": list_name,
    }


def test_dedupe_within_removes_duplicate_linkedin():
    kept, removed = dedupe_within([row(), row()])
    assert len(kept) == 1
    assert removed == 1


def test_dedupe_within_uses_name_company_fallback():
    a = row(li="", domain="acme.com")
    b = row(li="", domain="acme.com")
    kept, removed = dedupe_within([a, b])
    assert len(kept) == 1 and removed == 1


def test_dedupe_across_keeps_best_priority():
    rows = {"aud1": [row(priority=3, list_name="A")], "aud2": [row(priority=1, list_name="B")]}
    kept, removed, also = dedupe_across(rows, scope="client")
    assert removed == 1
    assert kept["aud1"] == []
    assert len(kept["aud2"]) == 1
    assert also  # the removed row is recorded as also-in


def test_dedupe_across_audience_scope_is_noop():
    rows = {"aud1": [row()], "aud2": [row()]}
    kept, removed, _ = dedupe_across(rows, scope="audience")
    assert kept == rows and removed == 0


def test_contact_cap():
    rows = [row(li=f"https://www.linkedin.com/in/{i}") for i in range(5)]
    kept, removed = enforce_contact_cap(rows, 2)
    assert len(kept) == 2 and removed == 3


def test_output_columns_and_writer(tmp_path):
    path = tmp_path / "out.csv"
    write_csv(path, [{"client": "X", "list_name": "L", "unknown": "ignored"}])
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        body = next(reader)
    assert header == list(OUTPUT_COLUMNS)
    assert body[0] == "X" and body[1] == "L"
    assert len(body) == len(OUTPUT_COLUMNS)
