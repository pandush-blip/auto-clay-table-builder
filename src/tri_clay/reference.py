"""Load enum data from the saved Clay query reference."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REFERENCE_PATH = Path("docs/clay-query-reference.md")

# Values verified in the reference text (company_size field).
COMPANY_SIZE_BUCKETS = (
    "1",
    "2-10",
    "11-50",
    "51-200",
    "201-500",
    "501-1,000",
    "1,001-5,000",
    "5,001-10,000",
    "10,001+",
)

US_STATES = (
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
    "Delaware", "District of Columbia", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois",
    "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts",
    "Michigan", "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota",
    "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina",
    "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming",
)


def extract_industry_values(text: str) -> frozenset[str]:
    """The reference embeds the industry enum as one long comma-separated quoted run."""
    runs = re.findall(r'"(?:[^"\n]{2,90})"(?:\s*,\s*"(?:[^"\n]{2,90})"){25,}', text)
    for run in runs:
        values = re.findall(r'"([^"]+)"', run)
        if "Computer Hardware" in values:
            return frozenset(values)
    raise ValueError("could not find the industry enum in the query reference")


@dataclass(frozen=True)
class Reference:
    industry_values: frozenset[str]
    path: Path

    @classmethod
    def load(cls, path: Path | None = None) -> "Reference":
        ref_path = Path(path) if path else DEFAULT_REFERENCE_PATH
        text = ref_path.read_text(encoding="utf-8")
        return cls(industry_values=extract_industry_values(text), path=ref_path)

    def is_valid_industry(self, value: str) -> bool:
        return value in self.industry_values
