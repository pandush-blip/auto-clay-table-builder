from __future__ import annotations

from pathlib import Path

import pytest

from tri_clay.csv_input import load_audiences, split_multi
from tri_clay.errors import InputError

FIXTURE = Path(__file__).parent / "fixtures" / "audiences_sample.csv"


def test_split_multi():
    assert split_multi("a | b | ") == ["a", "b"]
    assert split_multi("") == []
    assert split_multi(None) == []


def test_load_fixture():
    result = load_audiences(FIXTURE)
    assert [a.audience_id for a in result.audiences] == ["aud-001", "aud-002"]
    first = result.audiences[0]
    assert first.client == "Acme"
    assert first.priority == 1
    assert first.max_contacts_per_company == 3
    assert first.target_people == 50000
    assert first.dedupe_scope == "client"
    assert result.audiences[1].dedupe_scope == "audience"
    assert result.audiences[1].run_notes == ["second pass", "watch domain fill"]


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "audiences.csv"
    path.write_text(text, encoding="utf-8")
    return path


HEADER = (
    "client,audience_id,audience_name,tier,priority,result_entity,clay_query_draft,"
    "max_contacts_per_company,target_people,pause_if_below,pause_if_above,dedupe_scope,"
    "not_expressible,proxy_applied,run_notes\n"
)


def test_missing_column_names_it(tmp_path):
    path = _write(tmp_path, "client,audience_id\nx,1\n")
    with pytest.raises(InputError) as excinfo:
        load_audiences(path)
    assert "clay_query_draft" in str(excinfo.value)


def test_duplicate_audience_id(tmp_path):
    row = 'c,aud,Name,T1,1,people,select from people,2,1000,,,client,,,\n'
    path = _write(tmp_path, HEADER + row + row)
    with pytest.raises(InputError) as excinfo:
        load_audiences(path)
    assert "duplicate" in str(excinfo.value)


def test_empty_query(tmp_path):
    row = "c,aud,Name,T1,1,people,,2,1000,,,client,,,\n"
    path = _write(tmp_path, HEADER + row)
    with pytest.raises(InputError) as excinfo:
        load_audiences(path)
    assert "clay_query_draft" in str(excinfo.value)
    assert "row 2" in str(excinfo.value)


def test_bad_integer_names_row_and_column(tmp_path):
    row = "c,aud,Name,T1,abc,people,select from people,2,1000,,,client,,,\n"
    path = _write(tmp_path, HEADER + row)
    with pytest.raises(InputError) as excinfo:
        load_audiences(path)
    assert "priority" in str(excinfo.value)
    assert "row 2" in str(excinfo.value)


def test_non_people_entity_is_skipped_with_warning(tmp_path):
    good = "c,aud1,Name,T1,1,people,select from people,2,1000,,,client,,,\n"
    company = "c,aud2,Companies,T1,1,companies,select from companies,2,1000,,,client,,,\n"
    path = _write(tmp_path, HEADER + good + company)
    result = load_audiences(path)
    assert [a.audience_id for a in result.audiences] == ["aud1"]
    assert any("companies" in w for w in result.warnings)


def test_bad_dedupe_scope(tmp_path):
    row = "c,aud,Name,T1,1,people,select from people,2,1000,,,everything,,,\n"
    path = _write(tmp_path, HEADER + row)
    result = load_audiences(path)
    assert result.audiences[0].dedupe_scope == "client"
    assert any("defaulting to client" in warning for warning in result.warnings)


def test_range_targets_use_upper_bound(tmp_path):
    row = 'c,aud,Name,T1,1,people,select from people,3,"5,000-10,000",3000,15000,client,"note",,"x"\n'
    path = _write(tmp_path, HEADER + row)
    audience = load_audiences(path).audiences[0]
    assert audience.target_people == 10000
    assert audience.pause_if_below == 3000
    assert audience.pause_if_above == 15000
    assert audience.max_contacts_per_company == 3
