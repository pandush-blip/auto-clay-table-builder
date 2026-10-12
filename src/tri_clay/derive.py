"""Local, free cleaning and normalization (stage 7)."""

from __future__ import annotations

import re
import unicodedata

LEGAL_SUFFIXES = (
    "Pty. Ltd.", "Pty Ltd", "Pte Ltd", "Pte. Ltd.", "Sdn Bhd", "S.A.", "S.A", "S.A.S.", "SAS", "SARL",
    "S.p.A.", "SpA", "S.R.L.", "SRL", "GmbH", "mbH", "AG", "B.V.", "BV", "N.V.", "NV",
    "A/S", "Oy", "AB", "Kft", "d.o.o.", "OÜ", "LLC", "L.L.C.", "PLLC", "PLC", "LLP", "LP",
    "Inc.", "Inc", "Incorporated", "Ltd.", "Ltd", "Limited", "Corp.", "Corp", "Corporation",
    "Co.", "Co", "Company", "P.C.", "PC", "Bhd",
)

TITLES = ("mr", "mrs", "ms", "miss", "dr", "prof", "professor", "sir", "madam", "rev")
CREDENTIALS = (
    "mba", "phd", "ph.d.", "cpa", "md", "pmp", "cfa", "esq", "esq.", "jd", "dds", "rn",
    "cfa", "cissp", "pe", "cma",
)
EMOJI_RE = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F1E6-\U0001F1FF" "]+"
)
BRACKET_RE = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")


def clean_domain(value: str | None) -> str:
    if not value:
        return ""
    domain = value.strip().lower()
    domain = re.sub(r"^[a-z]+://", "", domain)
    domain = domain.split("/")[0]
    domain = domain.split("?")[0]
    domain = re.sub(r"^www\.", "", domain)
    domain = domain.split("@")[-1] if "@" in domain else domain
    domain = domain.split(":")[0]
    return domain.strip(".")


def normalize_company_name(value: str | None) -> str:
    if not value:
        return ""
    name = re.sub(r"\s+", " ", value).strip()
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()
    changed = True
    while changed:
        changed = False
        for suffix in sorted(LEGAL_SUFFIXES, key=len, reverse=True):
            base = suffix.rstrip(".")
            pattern = re.compile(rf"[,\s]+{re.escape(base)}\.?$", re.I)
            new = pattern.sub("", name).strip()
            if new and new != name:
                name, changed = new, True
    name = name.strip(" ,.")
    if not name:
        return ""
    if name.isupper() and len(name) > 3 or name.islower():
        name = " ".join(
            word if (word.isupper() and len(word) <= 4) else word.capitalize()
            for word in name.split(" ")
        )
    return name


def clean_first_name(value: str | None) -> str:
    if not value:
        return ""
    name = EMOJI_RE.sub(" ", value)
    name = BRACKET_RE.sub(" ", name)
    name = name.replace("|", " ")
    parts = []
    for token in name.split():
        plain = token.strip(".,").lower()
        if plain in TITLES or plain in CREDENTIALS:
            continue
        if re.fullmatch(r"[A-Za-z\'\-\.]+", token) is None:
            continue
        parts.append(token)
    return " ".join(parts).strip(" .")


def canonical_linkedin(value: str | None) -> str:
    if not value:
        return ""
    url = value.strip()
    url = re.sub(r"^https?://", "", url, flags=re.I)
    url = re.sub(r"^(www\.|([a-z]{2,3}\.)?)linkedin\.com/", "", url, flags=re.I)
    url = url.split("?")[0].rstrip("/")
    slug = url.split("/")[-1] if "/" in url else url
    return f"https://www.linkedin.com/in/{slug}" if slug else ""


def normalized_by(clay_value: str | None, local_value: str) -> str:
    return "clay" if (clay_value or "").strip() else "local"


def ascii_fold(value: str) -> str:
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
