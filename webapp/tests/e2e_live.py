"""Live end-to-end test against a deployed Clay Table Builder.

Creates a QA client, uploads a minimal one-row CSV, validates it (free check layer),
queues it, waits for the build worker, and downloads the produced CSV.

Usage:
  SMOKE_EMAIL=... SMOKE_PASSWORD=... python webapp/tests/e2e_live.py <base_url> <csv_path> [client_name]
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8600"
CSV_PATH = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(".secrets/e2e-wyoming.csv")
CLIENT_NAME = sys.argv[3] if len(sys.argv) > 3 else "TRI QA"
EMAIL = os.environ["SMOKE_EMAIL"]
PASSWORD = os.environ["SMOKE_PASSWORD"]
WAIT_SECONDS = int(os.environ.get("E2E_WAIT_SECONDS", "4200"))

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def get(path: str) -> tuple[int, str]:
    response = opener.open(BASE + path, timeout=60)
    return response.status, response.read().decode(errors="replace")


def post_form(path: str, fields: dict[str, str], csrf: str) -> tuple[int, str, str]:
    data = {"_csrf": csrf, **fields}
    encoded = urllib.parse.urlencode(data).encode()
    response = opener.open(BASE + path, encoded, timeout=120)
    return response.status, response.read().decode(errors="replace"), response.geturl()


def post_json(path: str, payload: dict, csrf: str) -> dict:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-CSRF-Token": csrf},
        method="POST",
    )
    response = opener.open(request, timeout=180)
    return json.loads(response.read().decode())


def post_multipart(
    path: str, fields: dict[str, str], filename: str, content: bytes, csrf: str
) -> tuple[int, str, str]:
    boundary = "----claybuilder" + secrets.token_hex(8)
    parts: list[bytes] = []
    for name, value in {**fields, "_csrf": csrf}.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: text/csv\r\n\r\n".encode()
    )
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    request = urllib.request.Request(
        BASE + path,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    response = opener.open(request, timeout=180)
    return response.status, response.read().decode(errors="replace"), response.geturl()


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf-token" content="([^"]+)"', html)
    if not match:
        raise SystemExit("FAIL: no CSRF token in page")
    return match.group(1)


def client_id_for(name: str) -> str:
    _, html = get("/clients")
    match = re.search(
        rf"<h2>{re.escape(name)}</h2>.*?name=\"id\" value=\"(\d+)\"", html, re.S
    )
    if match:
        return match.group(1)
    raise SystemExit(f"FAIL: could not find client id for {name}")


def main() -> None:
    _, login_html = get("/login")
    post_form("/login", {"email": EMAIL, "password": PASSWORD}, csrf_from(login_html))
    _, home = get("/")
    csrf = csrf_from(home)
    print(f"logged in to {BASE}")

    post_form("/connect/test", {}, csrf)
    _, connect_html = get("/connect")
    if "Not connected" in connect_html:
        raise SystemExit("FAIL: Clay connection check did not pass")
    print("Clay connection verified")

    post_form(
        "/clients/save",
        {
            "name": CLIENT_NAME,
            "tier": "Tier 1",
            "dedupe_scope": "client",
            "max_contacts_per_company": "2",
            "priority": "1",
        },
        csrf,
    )
    print(f"client {CLIENT_NAME!r} ensured")

    status, html, url = post_multipart(
        "/upload",
        {"client_id": client_id_for(CLIENT_NAME), "batch_name": "E2E live smoke"},
        CSV_PATH.name,
        CSV_PATH.read_bytes(),
        csrf,
    )
    batch_match = re.search(r"/batches/(\d+)", url)
    if not batch_match:
        raise SystemExit(f"FAIL: upload did not land on a batch page ({url}): {html[:300]}")
    batch_id = batch_match.group(1)
    audience_match = re.search(r'id="aud-(\d+)"', html)
    if not audience_match:
        raise SystemExit("FAIL: no audience row on the batch page")
    audience_row = audience_match.group(1)
    print(f"uploaded batch {batch_id}, audience row {audience_row}")

    result = post_json(f"/audiences/{audience_row}/validate", {}, csrf)
    if not result.get("ok"):
        raise SystemExit(f"FAIL: validation failed: {result}")
    print(
        f"validation OK: count={result.get('count')} "
        f"(sample {result.get('sample_records')} rows, linkedin {result.get('linkedin_present')})"
    )
    if not result.get("count"):
        raise SystemExit("FAIL: preview count is zero")

    post_form(f"/batches/{batch_id}/queue", {}, csrf)
    print("queued")

    job_id = None
    deadline = time.time() + 120
    while time.time() < deadline and job_id is None:
        jobs = json.loads(get("/api/jobs")[1])
        for job in jobs:
            if job.get("audience_row_id") == int(audience_row):
                job_id = job["id"]
                break
        if job_id is None:
            time.sleep(3)
    if job_id is None:
        raise SystemExit("FAIL: job did not appear in the queue")
    print(f"job {job_id} started")

    payload = None
    deadline = time.time() + WAIT_SECONDS
    last_phase = None
    while time.time() < deadline:
        payload = json.loads(get(f"/api/jobs/{job_id}")[1])
        phase = (payload.get("progress") or {}).get("phase")
        if payload["status"] != "running" or phase != last_phase:
            tables = len((payload.get("progress") or {}).get("tables", {}))
            print(f"  status={payload['status']} phase={phase} tables={tables}")
            last_phase = phase
        if payload["status"] in {"done", "failed", "needs_session", "canceled"}:
            break
        time.sleep(10)
    if not payload or payload["status"] == "running":
        raise SystemExit("FAIL: job did not finish before the timeout")
    if payload["status"] != "done":
        raise SystemExit(f"FAIL: job ended {payload['status']}: {payload.get('error')}")
    outputs = payload.get("outputs") or []
    print(f"job done. outputs: {[o['name'] for o in outputs]}")
    csv_outputs = [o for o in outputs if o["name"].endswith(".csv")]
    if not csv_outputs:
        raise SystemExit("FAIL: no CSV outputs")
    target = csv_outputs[0]
    status, body = get(f"/jobs/{job_id}/download?p={urllib.parse.quote(target['rel'])}")
    rows = max(0, body.count("\n") - 1)
    print(f"downloaded {target['name']}: {rows} data rows")
    if rows == 0:
        raise SystemExit("FAIL: downloaded CSV has no rows")
    print("E2E OK")


if __name__ == "__main__":
    main()
