"""Stage 5: build Clay tables from planned shards (free columns only)."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from .allowlist import assert_free_schema
from .checkpoint import Checkpoint
from .csv_input import Audience
from .errors import TriClayError, is_transient_error, summarize_clay_error
from .shard import Shard, with_preview_limit
from .webapp_client import WebAppClient

FREE_COLUMNS: tuple[tuple[str, str, list[str], str], ...] = (
    ("Domain", "matched_experience.company.domain", ["matched_experience", "company", "domain"], "url"),
    (
        "Company LinkedIn",
        "matched_experience.company.social_urls.linkedin",
        ["matched_experience", "company", "social_urls", "linkedin"],
        "url",
    ),
    (
        "Clay Company ID",
        "matched_experience.company.clay_company_id",
        ["matched_experience", "company", "clay_company_id"],
        "text",
    ),
    ("Clay Profile ID", "clay_metadata.clay_profile_id", ["clay_metadata", "clay_profile_id"], "text"),
)

IMPORT_TIMEOUT = 60 * 60
FIELD_TIMEOUT = 15 * 60
STALL_GRACE_FRACTION = 0.95
TABLE_RETRIES = max(0, int(os.environ.get("TRI_CLAY_TABLE_RETRIES", "2") or 2))
TABLE_RETRY_DELAY = max(5.0, float(os.environ.get("TRI_CLAY_TABLE_RETRY_DELAY", "45") or 45))


@dataclass
class BuiltTable:
    audience_id: str
    shard_index: int
    shard_total: int
    table_name: str
    table_id: str
    view_id: str
    source_id: str
    workbook_id: str
    rows_imported: int = 0
    fields: dict[str, str] = field(default_factory=dict)


class TableBuilder:
    def __init__(self, client: WebAppClient, checkpoint: Checkpoint) -> None:
        self.client = client
        self.checkpoint = checkpoint
        self.workspace_id = client.settings.workspace_id

    # ------------------------------------------------------------------ workbook
    def ensure_workbook(self, name: str) -> str:
        existing = self.checkpoint.data.get("workbook_id")
        if existing:
            return str(existing)
        payload: dict[str, Any] = {"name": name, "workspaceId": self.workspace_id}
        if self.client.settings.target_folder_id:
            payload["parentFolderId"] = self.client.settings.target_folder_id
        workbook = self.client.post(f"/v3/workspaces/{self.workspace_id}/workbooks", payload)
        self.checkpoint.data["workbook_id"] = workbook["id"]
        self.checkpoint.save()
        return str(workbook["id"])

    # ------------------------------------------------------------------- columns
    def _existing_field_names(self, table_id: str) -> set[str]:
        details = self.client.get(
            f"/v3/workspaces/{self.workspace_id}/tables/{table_id}/details"
        )
        fields = (details.get("table") or {}).get("fields") or []
        return {field.get("name") for field in fields}

    def _ensure_columns(self, table_id: str, view_id: str, list_name: str) -> dict[str, str]:
        existing = self._existing_field_names(table_id)
        base = f"/v3/workspaces/{self.workspace_id}/tables/{table_id}/fields"
        field_ids: dict[str, str] = {}
        for name, path, mapped, data_type in FREE_COLUMNS:
            if name in existing:
                continue
            formula = "{{f_people_search}}?." + "?.".join(mapped)
            payload = {
                "type": "formula",
                "name": name,
                "typeSettings": {
                    "dataTypeSettings": {"type": data_type},
                    "formula": formula,
                    "formulaText": formula,
                    "mappedResultPath": mapped,
                    "formulaType": "text",
                },
                "activeViewId": view_id,
                "attributionData": {"created_from": "object_mapper"},
            }
            response = self.client.post(base, payload)
            field_ids[name] = response["field"]["id"]
        if "List Name" in existing:
            return field_ids
        import json as _json

        constant = _json.dumps(list_name)
        payload = {
            "type": "formula",
            "name": "List Name",
            "typeSettings": {
                "dataTypeSettings": {"type": "text"},
                "formulaType": "text",
                "formula": constant,
                "formulaText": constant,
            },
            "activeViewId": view_id,
        }
        response = self.client.post(base, payload)
        field_ids["List Name"] = response["field"]["id"]
        return field_ids

    # -------------------------------------------------------------------- import
    def _source_run(self, source_id: str) -> dict[str, Any] | None:
        runs = self.client.get(
            f"/v3/workspaces/{self.workspace_id}/sources/{source_id}/runs?limit=1"
        ).get("runs") or []
        return runs[0] if runs else None

    def _wait_for_import(self, table_id: str, source_id: str) -> int:
        deadline = time.monotonic() + IMPORT_TIMEOUT
        count = 0
        last_report = 0.0
        while time.monotonic() < deadline:
            count = int(
                self.client.get(
                    f"/v3/workspaces/{self.workspace_id}/tables/{table_id}/count"
                )["tableTotalRecordsCount"]
            )
            run = self._source_run(source_id)
            if run and run.get("status") in {"SUCCESS", "FAILED", "ERROR"}:
                if run["status"] == "SUCCESS":
                    return int(run.get("numberOfRowsAdded") or count)
                raise TriClayError(
                    "source run failed: "
                    f"{summarize_clay_error(run)} (status={run.get('status')})"
                )
            if time.monotonic() - last_report > 120:
                last_report = time.monotonic()
            status = (run or {}).get("status", "?")
            rows = (run or {}).get("numberOfRowsAdded")
            print(
                f"[progress] import table={table_id} rows={count} run={status}"
                + (f" added={rows}" if rows else ""),
                flush=True,
            )
            time.sleep(10)
        raise TriClayError(f"import timed out after {IMPORT_TIMEOUT}s (rows so far: {count})")

    def _wait_for_fields(self, table_id: str, rows: int) -> None:
        deadline = time.monotonic() + FIELD_TIMEOUT
        last_report = 0.0
        last_min: int | None = None
        stall_polls = 0
        while time.monotonic() < deadline:
            counts = self.client.get(
                f"/v3/workspaces/{self.workspace_id}/tables/{table_id}/fields/runstatus"
            ).get("statusCountsByField") or {}
            pending = False
            min_success: int | None = None
            for entries in counts.values():
                if not entries:
                    continue
                for entry in entries:
                    status = (entry.get("status") or "").upper()
                    if status not in {"SUCCESS", "ERROR", "FAILED", "SKIPPED"}:
                        pending = True
                success = sum(
                    int(entry.get("count") or 0)
                    for entry in entries
                    if (entry.get("status") or "").upper() == "SUCCESS"
                )
                min_success = success if min_success is None else min(min_success, success)
            # Field counts can exceed the source run's numberOfRowsAdded (the table
            # keeps ingesting past the reported batch), so >= is the correct check.
            if not pending and min_success is not None and min_success >= rows:
                return
            if not pending and min_success is not None and min_success >= rows * STALL_GRACE_FRACTION:
                # Clay's field runner occasionally leaves a handful of rows behind.
                # Accept a >=95% complete column after a minute without movement and
                # let the export fill-rate report surface it.
                if min_success == last_min:
                    stall_polls += 1
                else:
                    stall_polls = 0
                    last_min = min_success
                if stall_polls >= 6:
                    print(
                        f"    warning: formula fields at {min_success}/{rows} rows and stalled; "
                        "accepting (export fill rate will show the gap)",
                        flush=True,
                    )
                    return
            if time.monotonic() - last_report > 120:
                last_report = time.monotonic()
            print(
                f"[progress] fields table={table_id} "
                f"processed={min_success if min_success is not None else 0}/{rows}"
                f"{' pending' if pending else ''}",
                flush=True,
            )
            time.sleep(10)
        raise TriClayError("field runs did not finish in time")

    # --------------------------------------------------------------------- build
    def build_table(self, audience: Audience, shard: Shard, workbook_id: str) -> BuiltTable:
        """Build one shard's table, retrying transient Clay failures automatically.

        A retry drops the failed shard's checkpoint entry and builds a fresh table (the
        web-app API has no delete-table endpoint), so a "Too many concurrent requests"
        error during the import or a 5xx during column creation heals by itself.
        """
        key = f"{audience.audience_id}:{shard.index}"
        attempt = 0
        while True:
            try:
                return self._build_table_once(audience, shard, workbook_id)
            except TriClayError as exc:
                attempt += 1
                reason = summarize_clay_error(exc)
                if attempt > TABLE_RETRIES or not is_transient_error(str(exc)):
                    raise
                delay = min(TABLE_RETRY_DELAY * attempt, 300)
                print(
                    f"[progress] table={shard.table_name} state=retrying attempt={attempt}"
                    f"/{TABLE_RETRIES} wait={delay:.0f}s reason={reason[:120]}",
                    flush=True,
                )
                self.checkpoint.clear_table(key)
                time.sleep(delay)

    def _build_table_once(self, audience: Audience, shard: Shard, workbook_id: str) -> BuiltTable:
        key = f"{audience.audience_id}:{shard.index}"
        saved = self.checkpoint.get_table(key)
        if saved:
            resumed = BuiltTable(**saved)
            print(
                f"[progress] table={resumed.table_name} id={resumed.table_id} state=resumed",
                flush=True,
            )
            resumed.fields = self._ensure_columns(
                resumed.table_id, resumed.view_id, audience.audience_name
            )
            if not resumed.rows_imported:
                resumed.rows_imported = self._wait_for_import(
                    resumed.table_id, resumed.source_id
                )
                self._wait_for_fields(resumed.table_id, resumed.rows_imported)
                self.checkpoint.set_table(key, resumed.__dict__)
            return resumed

        preview = self.client.preview(with_preview_limit(shard.query))
        preview_ids = [row["id"] for row in preview.get("rows") or []]
        table = self.client.post(
            f"/v3/workspaces/{self.workspace_id}/search-dsl/send-to-table",
            {
                "dslQuery": shard.query,
                "workspaceId": self.workspace_id,
                "workbookId": workbook_id,
                "resolvedResourceValues": preview.get("resolvedResources") or {},
                "previewEntityIds": preview_ids,
                "tableName": shard.table_name,
            },
        )
        built = BuiltTable(
            audience_id=audience.audience_id,
            shard_index=shard.index,
            shard_total=shard.total,
            table_name=shard.table_name,
            table_id=table["tableId"],
            view_id=table["viewId"],
            source_id=table["sourceId"],
            workbook_id=table.get("workbookId") or workbook_id,
        )
        self.checkpoint.set_table(key, built.__dict__)
        print(f"[progress] table={built.table_name} id={built.table_id} state=created", flush=True)

        built.fields = self._ensure_columns(built.table_id, built.view_id, audience.audience_name)
        print(f"[progress] table={built.table_name} id={built.table_id} state=columns", flush=True)
        built.rows_imported = self._wait_for_import(built.table_id, built.source_id)
        print(
            f"[progress] table={built.table_name} id={built.table_id} "
            f"state=imported rows={built.rows_imported}",
            flush=True,
        )
        self._wait_for_fields(built.table_id, built.rows_imported)
        print(f"[progress] table={built.table_name} id={built.table_id} state=ready", flush=True)

        details = self.client.get(
            f"/v3/workspaces/{self.workspace_id}/tables/{built.table_id}/details"
        )
        fields = (details.get("table") or {}).get("fields") or []
        assert_free_schema(fields)
        self.checkpoint.set_table(key, built.__dict__)
        return built

    def readback_sample(self, table_id: str, view_id: str) -> dict[str, Any]:
        return self.client.get(
            f"/v3/workspaces/{self.workspace_id}/tables/{table_id}/views/{view_id}/table-schema-v2"
        )

    def table_url(self, table_id: str) -> str:
        return (
            f"https://app.clay.com/workspaces/{self.workspace_id}/tables/{quote(table_id)}"
        )
