from __future__ import annotations

from pathlib import Path

import pytest

from tri_clay.lint import lint_query
from tri_clay.reference import Reference

REFERENCE = Reference.load(Path("docs/clay-query-reference.md"))


def test_reference_has_industry_enum():
    assert "Computer Hardware" in REFERENCE.industry_values
    assert "Computer Software" not in REFERENCE.industry_values


def test_valid_query_has_no_errors():
    query = (
        "select from people\n"
        'where location_country in ("United States")\n'
        "  and experiences.any(\n"
        "    is_current = true\n"
        '    and job_title is_similar_to ("Founder", "CTO")\n'
        '    and company.industry in ("Computer Hardware", "Computer Networking")\n'
        '    and company.company_size in ("11-50")\n'
        "  )\n"
        "limit 3 by clay_company_id"
    )
    result = lint_query(query, REFERENCE)
    assert result.errors == []


def test_invalid_industry_blocks_with_suggestion():
    query = (
        "select from people where experiences.any(is_current = true and "
        'company.industry in ("Computer Software"))'
    )
    result = lint_query(query, REFERENCE)
    assert not result.ok
    assert any("Computer Software" in error for error in result.errors)
    assert any("did you mean" in error for error in result.errors)


def test_not_industry_in_is_an_error():
    query = (
        "select from people where experiences.any(is_current = true and "
        'company.industry not_in ("Computer Hardware"))'
    )
    result = lint_query(query, REFERENCE)
    assert any("not industry in" in error for error in result.errors)


def test_products_and_services_max_three():
    query = (
        "select from people where experiences.any(is_current = true and "
        'products_and_services is_similar_to ("a", "b", "c", "d"))'
    )
    result = lint_query(query, REFERENCE)
    assert any("max 3" in error for error in result.errors)


def test_ai_business_types_requires_is_null_fallback():
    bad = (
        "select from people where experiences.any(is_current = true and "
        'company.ai_business_types in ("b2b"))'
    )
    good = (
        "select from people where experiences.any(is_current = true and "
        '(company.ai_business_types in ("b2b") or company.ai_business_types is_null true))'
    )
    assert any("is_null" in error for error in lint_query(bad, REFERENCE).errors)
    assert lint_query(good, REFERENCE).errors == []


def test_multiple_company_arms_warn():
    query = (
        "select from people where "
        'experiences.any(is_current = true and company.company_size in ("11-50")) or '
        'experiences.any(is_current = true and company.industry in ("Computer Hardware"))'
    )
    result = lint_query(query, REFERENCE)
    assert any("arms" in warning for warning in result.warnings)


def test_stem_contains_warns():
    query = (
        "select from people where experiences.any(is_current = true and "
        'job_title contains ("engineer"))'
    )
    result = lint_query(query, REFERENCE)
    assert any("token-based" in warning for warning in result.warnings)


def test_complete_phrases_do_not_warn():
    query = (
        "select from people where experiences.any(is_current = true and "
        'not job_title contains ("development officer", "marketing agency"))'
    )
    result = lint_query(query, REFERENCE)
    assert result.warnings == []
