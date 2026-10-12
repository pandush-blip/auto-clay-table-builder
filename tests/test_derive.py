from __future__ import annotations

import pytest

from tri_clay.derive import (
    canonical_linkedin,
    clean_domain,
    clean_first_name,
    normalize_company_name,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Acme Inc.", "Acme"),
        ("Acme Inc", "Acme"),
        ("Acme, Inc.", "Acme"),
        ("Acme LLC", "Acme"),
        ("Acme L.L.C.", "Acme"),
        ("Acme Ltd", "Acme"),
        ("Acme Ltd.", "Acme"),
        ("Acme Limited", "Acme"),
        ("Acme GmbH", "Acme"),
        ("Acme AG", "Acme"),
        ("Acme S.A.", "Acme"),
        ("Acme S.A", "Acme"),
        ("Acme SRL", "Acme"),
        ("Acme S.R.L.", "Acme"),
        ("Acme B.V.", "Acme"),
        ("Acme BV", "Acme"),
        ("Acme N.V.", "Acme"),
        ("Acme PLLC", "Acme"),
        ("Acme P.C.", "Acme"),
        ("Acme PC", "Acme"),
        ("Acme Corp", "Acme"),
        ("Acme Corp.", "Acme"),
        ("Acme Corporation", "Acme"),
        ("Acme Co", "Acme"),
        ("Acme Co.", "Acme"),
        ("Acme Company", "Acme"),
        ("Acme Pty Ltd", "Acme"),
        ("Acme Pty. Ltd.", "Acme"),
        ("Acme PLC", "Acme"),
        ("Acme LLP", "Acme"),
        ("Acme LP", "Acme"),
        ("Acme Oy", "Acme"),
        ("Acme AB", "Acme"),
        ("Acme A/S", "Acme"),
        ("Acme Kft", "Acme"),
        ("Acme d.o.o.", "Acme"),
        ("ACME INDUSTRIES", "ACME Industries"),
        ("IBM", "IBM"),
        ("acme technologies", "Acme Technologies"),
        ("IBM", "IBM"),
        ("NASA", "NASA"),
        ("HubSpot", "HubSpot"),
        ("eBay", "eBay"),
        ("Acme (formerly Foo)", "Acme"),
        ("  Acme   Inc.  ", "Acme"),
        ("", ""),
    ],
)
def test_normalize_company_name(raw, expected):
    assert normalize_company_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.Acme.com/path?x=1", "acme.com"),
        ("http://acme.com:8080/x", "acme.com"),
        ("www.acme.com.", "acme.com"),
        ("acme.com/", "acme.com"),
        ("HTTPS://WWW.ACME.COM", "acme.com"),
        ("", ""),
    ],
)
def test_clean_domain(raw, expected):
    assert clean_domain(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Dr. Jane", "Jane"),
        ("Mr. John Smith", "John Smith"),
        ("Jane Doe MBA", "Jane Doe"),
        ("Jane (she/her)", "Jane"),
        ("Jane 🚀 Doe", "Jane Doe"),
        ("JANE", "JANE"),
        ("", ""),
    ],
)
def test_clean_first_name(raw, expected):
    assert clean_first_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.linkedin.com/in/jane-doe/", "https://www.linkedin.com/in/jane-doe"),
        ("linkedin.com/in/jane-doe", "https://www.linkedin.com/in/jane-doe"),
        ("http://de.linkedin.com/in/jane", "https://www.linkedin.com/in/jane"),
        ("https://www.linkedin.com/in/jane?trk=x", "https://www.linkedin.com/in/jane"),
        ("", ""),
    ],
)
def test_canonical_linkedin(raw, expected):
    assert canonical_linkedin(raw) == expected
