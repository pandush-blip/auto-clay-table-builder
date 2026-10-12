"""HTTP smoke test: log in and fetch every page.

Usage: SMOKE_EMAIL=... SMOKE_PASSWORD=... python webapp/tests/smoke_http.py [base_url]
"""

from __future__ import annotations

import http.cookiejar
import os
import re
import sys
import urllib.parse
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SMOKE_BASE", "http://127.0.0.1:8765")).rstrip("/")
EMAIL = os.environ.get("SMOKE_EMAIL", "admin@test.local")
PASSWORD = os.environ.get("SMOKE_PASSWORD", "test-password")

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

html = opener.open(f"{BASE}/login").read().decode()
match = re.search(r'name="_csrf" value="([^"]+)"', html)
if not match:
    raise SystemExit("FAIL: no CSRF token on the login page")
csrf = match.group(1)
payload = urllib.parse.urlencode({"email": EMAIL, "password": PASSWORD, "_csrf": csrf}).encode()
landing = opener.open(f"{BASE}/login", payload)
if landing.geturl().endswith("/login"):
    raise SystemExit("FAIL: login rejected")
print(f"login -> {landing.status} {landing.geturl()}")

failures = []
for path in ["/", "/clients", "/upload", "/queue", "/connect", "/users", "/settings", "/api/jobs", "/healthz"]:
    try:
        response = opener.open(f"{BASE}{path}")
        body = response.read()
        print(f"{path} -> {response.status} ({len(body)} bytes)")
        if response.status != 200:
            failures.append(path)
    except Exception as exc:  # noqa: BLE001
        print(f"{path} -> FAIL {exc}")
        failures.append(path)
if failures:
    raise SystemExit("FAIL: " + ", ".join(failures))
print("SMOKE OK")
