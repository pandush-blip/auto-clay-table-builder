"""Run report (markdown)."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def write_run_report(path: Path, *, client: str, summary: dict[str, Any]) -> None:
    lines = [
        f"# Run report — {client}",
        "",
        f"- Built at: {summary.get('built_at')}",
        f"- Workspace: {summary.get('workspace_id')}",
        f"- Credits before: data {summary.get('credits_before', {}).get('balance')}, "
        f"actions {summary.get('credits_before', {}).get('action_execution_balance')}",
        f"- Credits after: data {summary.get('credits_after', {}).get('balance')}, "
        f"actions {summary.get('credits_after', {}).get('action_execution_balance')}",
        "",
        "## Audiences",
        "",
    ]
    for audience in summary.get("audiences", []):
        lines.extend(
            [
                f"### {audience['audience_id']} — {audience['audience_name']}",
                "",
                f"- Parent count: {audience['parent_count']}",
                f"- Effective size: {audience['effective_size']}",
                f"- Shards: {len(audience['shards'])}",
                f"- Rows exported: {audience.get('rows_exported', 0)}",
                f"- Rows after dedupe: {audience.get('rows_final', 0)}",
                f"- Dupes removed (within/across): "
                f"{audience.get('removed_within', 0)}/{audience.get('removed_across', 0)}",
                f"- Contact-cap removals: {audience.get('cap_removed', 0)}",
                f"- Domain fill rate: {audience.get('domain_fill_rate', 'n/a')}",
                f"- Pause flags: {audience.get('pause_flags', '') or 'none'}",
                f"- Lint warnings: {'; '.join(audience.get('lint_warnings', [])) or 'none'}",
                f"- Not expressible: {'; '.join(audience.get('not_expressible', [])) or 'none'}",
                f"- Proxy applied: {'; '.join(audience.get('proxy_applied', [])) or 'none'}",
                f"- Run notes: {'; '.join(audience.get('run_notes', [])) or 'none'}",
                "",
            ]
        )
        for shard in audience["shards"]:
            lines.append(
                f"  - shard {shard['index']}/{shard['total']}: {shard['table_name']} "
                f"({shard.get('table_id', 'pending')}), rows {shard.get('count')}"
            )
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
