"""Live test: a query with an invalid enum must fail validation and be repaired."""

from __future__ import annotations

import http.cookiejar
import csv
import io
import json
import os
import re
import secrets
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8600"
EMAIL = os.environ["SMOKE_EMAIL"]
PASSWORD = os.environ["SMOKE_PASSWORD"]
BAD_QUERY = 'select from people where company.industry in ("Computer Software")'

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def get(path: str) -> str:
    return opener.open(BASE + path, timeout=60).read().decode(errors="replace")


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def post_form(path: str, fields: dict[str, str], csrf: str) -> str:
    data = urllib.parse.urlencode({"_csrf": csrf, **fields}).encode()
    return opener.open(BASE + path, data, timeout=180).read().decode(errors="replace")


def post_json(path: str, csrf: str) -> dict:
    request = urllib.request.Request(
        BASE + path,
        data=b"{}",
        headers={"Content-Type": "application/json", "X-CSRF-Token": csrf},
        method="POST",
    )
    return json.loads(opener.open(request, timeout=300).read().decode())


def post_multipart(fields: dict[str, str], content: bytes, csrf: str) -> str:
    boundary = "----repair" + secrets.token_hex(8)
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in {**fields, "_csrf": csrf}.items()
    ]
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="repair.csv"\r\n'
        "Content-Type: text/csv\r\n\r\n".encode()
    )
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        BASE + "/upload",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    response = opener.open(request, timeout=180)
    html = response.read().decode(errors="replace")
    return html


def main() -> None:
    login_html = get("/login")
    post_form("/login", {"email": EMAIL, "password": PASSWORD}, csrf_of(login_html))
    csrf = csrf_of(get("/"))
    clients_html = get("/clients")
    match = re.search(r"<h2>TRI QA</h2>.*?name=\"id\" value=\"(\d+)\"", clients_html, re.S)
    if not match:
        post_form(
            "/clients/save",
            {"name": "TRI QA", "tier": "Tier 1", "dedupe_scope": "client", "max_contacts_per_company": "2"},
            csrf,
        )
        clients_html = get("/clients")
        match = re.search(r"<h2>TRI QA</h2>.*?name=\"id\" value=\"(\d+)\"", clients_html, re.S)
    client_id = match.group(1)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["audience name", "clay search people queries"])
    writer.writerow(["TRI QA repair smoke", BAD_QUERY])
    csv_bytes = buffer.getvalue().encode()
    html = post_multipart({"client_id": client_id, "batch_name": "E2E repair smoke"}, csv_bytes, csrf)
    audience_row = re.search(r'id="aud-(\d+)"', html).group(1)
    print(f"uploaded repair audience row {audience_row}")
    result = post_json(f"/audiences/{audience_row}/validate", csrf)
    print("validation:", json.dumps({k: result.get(k) for k in ("ok", "stage", "errors", "warnings")})[:600])
    if result.get("ok"):
        raise SystemExit("FAIL: invalid enum unexpectedly validated")
    fixed = post_json(f"/audiences/{audience_row}/fix", csrf)
    print("repair:", json.dumps({k: fixed.get(k) for k in ("ok", "source", "query", "explanation", "message")})[:900])
    if not fixed.get("ok"):
        raise SystemExit("FAIL: repair did not produce a valid query")
    if "Computer Software" in fixed.get("query", ""):
        raise SystemExit("FAIL: repaired query still contains the invalid value")
    print("REPAIR OK:", fixed["query"][:300])


if __name__ == "__main__":
    main()
