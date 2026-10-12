"""Stage 2: query lint (no API calls)."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from .reference import Reference

INDUSTRY_COMPARISON = re.compile(
    r"company\.industry\s*(?P<op>not_in|in|=|!=)\s*(?P<values>\([^)]*\)|\"[^\"]*\")",
    re.I,
)
PRODUCTS_COMPARISON = re.compile(
    r"products_and_services\s*(?P<op>not_in|in|=|!=|is_similar_to|contains)\s*(?P<values>\([^)]*\)|\"[^\"]*\")",
    re.I,
)
CONTAINS_VALUES = re.compile(r"contains\s*\((?P<values>[^)]*)\)", re.I)
EXPERIENCE_ARM = re.compile(r"experiences\.(?:any|count)\s*\(", re.I)
AI_BUSINESS = re.compile(r"ai_business_types", re.I)

STEM_WORDS = {
    "engineer",
    "manufactur",
    "technolog",
    "consult",
    "develop",
    "financ",
    "market",
    "comput",
    "scien",
    "operat",
    "manag",
    "analys",
}


@dataclass
class LintResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _values_in(text: str) -> list[str]:
    return [v.strip() for v in re.findall(r'"([^"]*)"', text)]


def _blocks(query: str) -> list[tuple[int, int, str]]:
    """Return top-level parenthesised groups with spans (balanced)."""
    blocks: list[tuple[int, int, str]] = []
    depth = 0
    start = 0
    for i, char in enumerate(query):
        if char == "(":
            if depth == 0:
                start = i
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                blocks.append((start, i + 1, query[start : i + 1]))
    return blocks


def _context_for(query: str, position: int) -> str:
    containing = [b for b in _blocks(query) if b[0] <= position < b[1]]
    if containing:
        first, last, _ = min(containing, key=lambda b: b[1] - b[0])
        return query[first:last]
    line_start = query.rfind("\n", 0, position) + 1
    line_end = query.find("\n", position)
    return query[line_start : line_end if line_end != -1 else len(query)]


def lint_query(query: str, reference: Reference) -> LintResult:
    result = LintResult()

    for match in INDUSTRY_COMPARISON.finditer(query):
        values = _values_in(match.group("values"))
        if match.group("op").lower() == "not_in":
            result.errors.append(
                "`not industry in (...)` is not allowed — use positive industry filters "
                "inside experiences.any(...)"
            )
        for value in values:
            if reference.is_valid_industry(value):
                continue
            close = difflib.get_close_matches(value, reference.industry_values, n=3, cutoff=0.5)
            hint = f" - did you mean {', '.join(repr(c) for c in close)}?" if close else ""
            result.errors.append(f"invalid industry {value!r}{hint}")

    for match in PRODUCTS_COMPARISON.finditer(query):
        values = _values_in(match.group("values"))
        if len(values) > 3:
            result.errors.append(
                f"products_and_services has {len(values)} values (max 3): {values[:4]}…"
            )

    if AI_BUSINESS.search(query):
        for match in AI_BUSINESS.finditer(query):
            context = _context_for(query, match.start())
            if "is_null" not in context.lower():
                result.errors.append(
                    "ai_business_types block is missing an `is_null true` fallback: "
                    f"{context.strip()[:90]}…"
                )

    arms = [m for m in EXPERIENCE_ARM.finditer(query)]
    if len(arms) > 1:
        arm_texts = [
            query[m.end() : query.find(")", m.end())] for m in arms
        ]
        company_arms = [text for text in arm_texts if "company." in text]
        if len(company_arms) > 1:
            result.warnings.append(
                f"{len(company_arms)} experiences arms contain company.* predicates — "
                "keep one role's company filters inside a single arm"
            )

    for match in CONTAINS_VALUES.finditer(query):
        for value in _values_in(match.group("values")):
            lowered = value.lower().strip()
            if lowered in STEM_WORDS:
                result.warnings.append(
                    f"`contains` is token-based; {value!r} will not match longer forms "
                    "(e.g. 'engineer' does not match 'engineering')"
                )

    return result
