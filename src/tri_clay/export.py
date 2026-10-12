"""Stage 6: trigger exports, poll them and download the CSVs."""

from __future__ import annotations

import csv
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .allowlist import assert_download_url
from .errors import TriClayError, is_transient_error, summarize_clay_error
from .webapp_client import WebAppClient

EXPORT_RETRIES = max(0, int(os.environ.get("TRI_CLAY_EXPORT_RETRIES", "2") or 2))
EXPORT_RETRY_DELAY = max(5.0, float(os.environ.get("TRI_CLAY_EXPORT_RETRY_DELAY", "30") or 30))


@dataclass
class ExportResult:
    export_id: str
    file_path: Path
    rows_exported: int
    total_in_view: int


def count_csv_rows(path: Path) -> int:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        return sum(1 for _ in reader)


def export_view(
    client: WebAppClient,
    *,
    table_id: str,
    view_id: str,
    destination: Path,
    timeout: float = 20 * 60,
) -> ExportResult:
    """Export a view, retrying transient failures with fresh export jobs."""
    attempt = 0
    while True:
        try:
            return _export_view_once(
                client, table_id=table_id, view_id=view_id, destination=destination, timeout=timeout
            )
        except TriClayError as exc:
            attempt += 1
            if attempt > EXPORT_RETRIES or not is_transient_error(str(exc)):
                raise
            delay = min(EXPORT_RETRY_DELAY * attempt, 300)
            print(
                f"[progress] export table={table_id} state=retrying attempt={attempt}"
                f"/{EXPORT_RETRIES} wait={delay:.0f}s reason={summarize_clay_error(exc)[:120]}",
                flush=True,
            )
            time.sleep(delay)


def _export_view_once(
    client: WebAppClient,
    *,
    table_id: str,
    view_id: str,
    destination: Path,
    timeout: float = 20 * 60,
) -> ExportResult:
    workspace = client.settings.workspace_id
    created = client.post(
        f"/v3/workspaces/{workspace}/tables/{table_id}/views/{view_id}/export", {}
    )
    export_id = created["id"]
    deadline = time.monotonic() + timeout
    export: dict = created
    last_status = None
    while time.monotonic() < deadline:
        export = client.get(f"/v3/workspaces/{workspace}/exports/{export_id}")
        status = export.get("status")
        if status != last_status:
            print(f"[progress] export table={table_id} status={status or '?'}", flush=True)
            last_status = status
        if export.get("status") == "FINISHED":
            break
        if export.get("status") in {"FAILED", "ERROR"}:
            raise TriClayError(f"export {export_id} failed: {export}")
        time.sleep(5)
    else:
        raise TriClayError(f"export {export_id} did not finish within {timeout}s")

    url = export.get("downloadUrl")
    if not url:
        raise TriClayError(f"export {export_id} is FINISHED but has no downloadUrl")
    assert_download_url(url)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, timeout=600, follow_redirects=True) as response:
        response.raise_for_status()
        with destination.open("wb") as handle:
            for chunk in response.iter_bytes(1 << 20):
                handle.write(chunk)
    rows = count_csv_rows(destination)
    total = int(export.get("totalRecordsInViewCount") or rows)
    if rows != total:
        raise TriClayError(
            f"export row count mismatch: csv {rows} vs export metadata {total}"
        )
    return ExportResult(export_id=export_id, file_path=destination, rows_exported=rows, total_in_view=total)
