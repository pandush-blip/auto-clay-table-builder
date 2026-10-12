"""tri-clay command line interface."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import httpx
import typer

from .allowlist import validate_request
from .builder import TableBuilder
from .checkpoint import Checkpoint
from .config import Settings
from .credits import CreditTripwire, OfficialApiClient
from .csv_input import load_audiences
from .errors import TriClayError
from .export import export_view
from .lint import lint_query
from .output import audience_filename
from .pipeline import AudienceOutcome, built_at_now, map_export_row, process_outputs
from .reference import Reference
from .report import write_run_report
from .samples import summarize_search_results
from .shard import plan_audience
from .webapp_client import WebAppClient

app = typer.Typer(
    name="tri-clay",
    help="Build Clay audience tables from Clay searches without spending credits.",
    no_args_is_help=True,
)


def _session_probe(settings: Settings) -> tuple[bool, str]:
    cookie = settings.require_build_session()
    path = f"/v3/workspaces/{settings.workspace_id}/peopleSearchLimit"
    validate_request("GET", path)
    try:
        response = httpx.get(
            settings.webapp_base + path,
            headers={"Cookie": cookie, "accept": "application/json"},
            timeout=30,
        )
    except httpx.HTTPError as exc:
        return False, f"web-app unreachable: {exc}"
    if response.status_code == 200:
        limit = response.json().get("peopleSearchLimit")
        return True, f"session ok, per-table row cap = {limit}"
    return False, (
        f"session rejected ({response.status_code}). Refresh with: "
        "python tools/browser_login.js, then update CLAY_SESSION_COOKIE in .env."
    )


@app.command()
def doctor() -> None:
    """Check config, API key, credits and the web-app session."""
    try:
        settings = Settings.load()
    except TriClayError as exc:
        typer.echo(f"config: FAIL - {exc}", err=True)
        raise typer.Exit(code=1)

    typer.echo("config loaded:")
    for key, value in settings.redacted().items():
        typer.echo(f"  {key}: {value}")

    with OfficialApiClient(settings.api_key, settings.api_base) as client:
        me = client.get_me()
        balance = client.get_balance()
    typer.echo(
        f"official api: ok - user {me['user']['id']}, workspace {me['workspace']['id']} "
        f"({me['workspace'].get('name')})"
    )
    typer.echo(
        f"credits: data={balance.balance} actions={balance.action_execution_balance}"
    )

    if not settings.session_cookie:
        typer.echo("web-app session: MISSING - run python tools/browser_login.js")
        raise typer.Exit(code=1)
    ok, message = _session_probe(settings)
    typer.echo(f"web-app session: {'ok' if ok else 'FAIL'} - {message}")
    if not ok:
        raise typer.Exit(code=1)


@app.command()
def lint(audiences_csv: Path) -> None:
    """Load an audiences CSV and lint every query (no API calls)."""
    try:
        loaded = load_audiences(audiences_csv)
        reference = Reference.load()
    except TriClayError as exc:
        typer.echo(f"FAIL - {exc}", err=True)
        raise typer.Exit(code=1)

    for warning in loaded.warnings:
        typer.echo(f"warn  {warning}")

    failed = 0
    for audience in loaded.audiences:
        result = lint_query(audience.clay_query_draft, reference)
        status = "ok" if result.ok else "FAIL"
        typer.echo(f"{status}  {audience.audience_id} ({audience.audience_name})")
        for error in result.errors:
            typer.echo(f"  error  {error}")
        for warning in result.warnings:
            typer.echo(f"  warn   {warning}")
        if not result.ok:
            failed += 1
    typer.echo(f"{len(loaded.audiences) - failed}/{len(loaded.audiences)} audiences pass lint")
    if failed:
        raise typer.Exit(code=1)


@app.command()
def validate(audiences_csv: Path) -> None:
    """Lint, validate against the official API and pull a 25-row sample per audience."""
    settings = Settings.load()
    loaded = load_audiences(audiences_csv)
    reference = Reference.load()
    stamp = datetime.now().strftime("%Y-%m-%dT%H%M%S")
    run_dir = Path("runs") / (loaded.audiences[0].client or "unknown") / stamp
    samples_dir = run_dir / "samples"
    samples_dir.mkdir(parents=True, exist_ok=True)

    for warning in loaded.warnings:
        typer.echo(f"warn  {warning}")

    blocked: list[str] = []
    with OfficialApiClient(settings.api_key, settings.api_base) as client:
        tripwire = CreditTripwire(client)
        before = tripwire.start()
        for audience in loaded.audiences:
            result = lint_query(audience.clay_query_draft, reference)
            if not result.ok:
                blocked.append(audience.audience_id)
                typer.echo(f"FAIL  {audience.audience_id} ({audience.audience_name}) - lint")
                for error in result.errors:
                    typer.echo(f"  error  {error}")
                continue
            for warning in result.warnings:
                typer.echo(f"  warn   {audience.audience_id}: {warning}")
            try:
                search = client.create_search(audience.clay_query_draft)
                payload = client.run_search_page(search["search_id"], 25)
            except TriClayError as exc:
                blocked.append(audience.audience_id)
                typer.echo(f"FAIL  {audience.audience_id} - {exc}")
                continue
            (samples_dir / f"{audience.audience_id}.json").write_text(
                __import__("json").dumps(payload, indent=1), encoding="utf-8"
            )
            summary = summarize_search_results(payload)
            top_titles = ", ".join(f"{t} x{c}" for t, c in summary["top_titles"][:3])
            top_companies = ", ".join(f"{c} x{n}" for c, n in summary["top_companies"][:3])
            typer.echo(
                f"ok    {audience.audience_id} ({audience.audience_name}): "
                f"{summary['records']} rows, linkedin {summary['linkedin_present']}"
            )
            typer.echo(f"      titles: {top_titles}")
            typer.echo(f"      companies: {top_companies}")
        after = tripwire.check()

    quota = after.as_dict()
    typer.echo(
        f"credits: data {before.balance} -> {after.balance}, "
        f"actions {before.action_execution_balance} -> {after.action_execution_balance} (unchanged)"
    )
    typer.echo(f"quota: {quota}")
    typer.echo(f"samples written to {samples_dir}")
    if blocked:
        typer.echo(f"blocked audiences: {', '.join(blocked)}", err=True)
        raise typer.Exit(code=1)


def _count_fn(client: WebAppClient):
    def count(query: str) -> int:
        return int(client.preview(query)["totalCount"])

    return count


def _selected(audiences, only: str | None):
    if not only:
        return audiences
    wanted = {part.strip() for part in only.split(",") if part.strip()}
    return [audience for audience in audiences if audience.audience_id in wanted]


@app.command()
def plan(audiences_csv: Path, only: str = typer.Option(None, help="comma-separated audience ids")):
    """Size each audience and print the shard plan (no writes)."""
    settings = Settings.load()
    loaded = load_audiences(audiences_csv)
    reference = Reference.load()
    with WebAppClient(settings) as client:
        count = _count_fn(client)
        for audience in _selected(loaded.audiences, only):
            lint = lint_query(audience.clay_query_draft, reference)
            if not lint.ok:
                typer.echo(f"FAIL  {audience.audience_id} - lint")
                for error in lint.errors:
                    typer.echo(f"  error  {error}")
                continue
            audience_plan = plan_audience(audience, count)
            typer.echo(
                f"{audience.audience_id} ({audience.audience_name}): parent {audience_plan.parent_count}, "
                f"effective {audience_plan.effective_size}, shards {len(audience_plan.shards)}"
            )
            for shard in audience_plan.shards:
                predicate = shard.predicate[:90] + ("…" if len(shard.predicate) > 90 else "")
                typer.echo(f"  {shard.index}/{shard.total}  {shard.count:>7}  {shard.table_name}")
                if predicate:
                    typer.echo(f"        where {predicate}")
            for note in audience_plan.notes:
                typer.echo(f"  note: {note}")


def _run_build(
    audiences_csv: Path,
    *,
    yes: bool,
    only: str | None,
    resume_dir: Path | None = None,
) -> None:
    settings = Settings.load()
    loaded = load_audiences(audiences_csv)
    audiences = _selected(loaded.audiences, only)
    if not audiences:
        raise TriClayError("no audiences selected")
    reference = Reference.load()

    stamp = datetime.now().strftime("%Y-%m-%dT%H%M%S")
    run_dir = resume_dir or (Path("runs") / (audiences[0].client or "unknown") / stamp)
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"run_dir: {run_dir}", flush=True)
    (run_dir / "audiences.csv").write_text(
        Path(audiences_csv).read_text(encoding="utf-8"), encoding="utf-8"
    )
    checkpoint = Checkpoint.load(run_dir / "checkpoint.json")

    with OfficialApiClient(settings.api_key, settings.api_base) as api, WebAppClient(settings) as client:
        tripwire = CreditTripwire(api)
        before = tripwire.start()
        count = _count_fn(client)

        blocking = []
        lints = {}
        for audience in audiences:
            lint = lint_query(audience.clay_query_draft, reference)
            if not lint.ok:
                blocking.append(audience.audience_id)
                typer.echo(f"FAIL  {audience.audience_id} - lint")
                for error in lint.errors:
                    typer.echo(f"  error  {error}")
                continue
            lints[audience.audience_id] = lint
        if blocking:
            typer.echo(f"blocked audiences: {', '.join(blocking)}", err=True)
            raise typer.Exit(code=1)

        plans: dict[str, object] = {}

        def get_plan(aud):
            if aud.audience_id not in plans:
                plans[aud.audience_id] = plan_audience(aud, count)
            return plans[aud.audience_id]

        if not yes:
            for audience in audiences:
                get_plan(audience)
            total_tables = sum(len(get_plan(a).shards) for a in audiences)
            typer.echo(f"plan: {len(audiences)} audiences, {total_tables} tables")
            for audience in audiences:
                audience_plan = get_plan(audience)
                typer.echo(
                    f"  {audience.audience_id}: parent {audience_plan.parent_count}, "
                    f"effective {audience_plan.effective_size}, {len(audience_plan.shards)} shard(s)"
                )
            if not typer.confirm(
                f"Build {total_tables} table(s) in workspace {settings.workspace_id}?"
            ):
                typer.echo("aborted")
                raise typer.Exit(code=1)

        workbook = TableBuilder(client, checkpoint).ensure_workbook(
            f"{audiences[0].client} | audiences | {stamp[:10]}"
        )
        builder = TableBuilder(client, checkpoint)
        rows_by_audience: dict[str, list[dict]] = {}
        built_at = built_at_now()
        audience_meta: dict[str, dict] = {}

        for audience in audiences:
            audience_plan = get_plan(audience)  # planned just-in-time when --yes was used
            lint = lints[audience.audience_id]
            typer.echo(
                f"plan {audience.audience_id}: parent {audience_plan.parent_count}, "
                f"effective {audience_plan.effective_size}, {len(audience_plan.shards)} shard(s)"
            )
            rows_by_audience.setdefault(audience.audience_id, [])
            shard_records = []
            for shard in audience_plan.shards:
                built = builder.build_table(audience, shard, workbook)
                destination = (
                    run_dir / "exports" / f"{audience.audience_id}__{shard.index}-{shard.total}.csv"
                )
                if destination.exists() and destination.stat().st_size > 0:
                    # Resume: reuse an export we already downloaded instead of re-exporting.
                    from .export import count_csv_rows

                    rows_exported = count_csv_rows(destination)
                    typer.echo(
                        f"  reused export {destination.name}: {rows_exported} rows"
                    )
                else:
                    result = export_view(
                        client,
                        table_id=built.table_id,
                        view_id=built.view_id,
                        destination=destination,
                    )
                    rows_exported = result.rows_exported
                import csv as _csv

                with destination.open(encoding="utf-8", newline="") as handle:
                    for raw in _csv.DictReader(handle):
                        rows_by_audience[audience.audience_id].append(
                            map_export_row(
                                raw,
                                audience=audience,
                                shard_index=shard.index,
                                shard_total=shard.total,
                                table_id=built.table_id,
                                built_at=built_at,
                            )
                        )
                typer.echo(
                    f"  built {shard.table_name}: {built.rows_imported} rows, "
                    f"exported {rows_exported}"
                )
                shard_records.append(
                    {
                        "index": shard.index,
                        "total": shard.total,
                        "table_name": shard.table_name,
                        "table_id": built.table_id,
                        "count": shard.count,
                        "rows_exported": rows_exported,
                    }
                )
            audience_meta[audience.audience_id] = {
                "audience_name": audience.audience_name,
                "parent_count": audience_plan.parent_count,
                "effective_size": audience_plan.effective_size,
                "shards": shard_records,
                "rows_exported": sum(record["rows_exported"] for record in shard_records),
                "lint_warnings": lint.warnings,
                "not_expressible": audience.not_expressible,
                "proxy_applied": audience.proxy_applied,
                "run_notes": audience.run_notes,
                "pause_flags": "",
            }

        after = tripwire.check()
        outcomes, merged_path = process_outputs(
            rows_by_audience,
            {audience.audience_id: audience for audience in audiences},
            run_dir / "outputs",
        )
        summary = {
            "built_at": built_at,
            "workspace_id": settings.workspace_id,
            "credits_before": before.as_dict(),
            "credits_after": after.as_dict(),
            "audiences": [
                {
                    **audience_meta[outcome.audience.audience_id],
                    "audience_id": outcome.audience.audience_id,
                    "rows_final": len(outcome.rows),
                    "removed_within": outcome.removed_within,
                    "removed_across": outcome.removed_across,
                    "cap_removed": outcome.cap_removed,
                    "domain_fill_rate": f"{outcome.domain_fill_rate}%",
                }
                for outcome in outcomes
            ],
        }
        write_run_report(run_dir / "run_report.md", client=audiences[0].client, summary=summary)
        typer.echo(f"outputs: {run_dir / 'outputs'}")
        typer.echo(f"merged:  {merged_path}")
        typer.echo(f"report:  {run_dir / 'run_report.md'}")
        typer.echo(
            f"credits: data {before.balance} -> {after.balance}, "
            f"actions {before.action_execution_balance} -> {after.action_execution_balance}"
        )


@app.command()
def build(
    audiences_csv: Path,
    yes: bool = typer.Option(False, "--yes", help="skip the runtime gate"),
    only: str = typer.Option(None, help="comma-separated audience ids"),
):
    """Build, export and output audiences (lint -> plan -> gate -> build)."""
    _run_build(audiences_csv, yes=yes, only=only)


@app.command()
def run(
    audiences_csv: Path,
    yes: bool = typer.Option(False, "--yes"),
    only: str = typer.Option(None),
):
    """Alias for build (validate + plan + gate + build)."""
    _run_build(audiences_csv, yes=yes, only=only)


@app.command()
def resume(run_dir: Path, yes: bool = typer.Option(False, "--yes")):
    """Resume a previous run from its checkpoint."""
    csv_path = run_dir / "audiences.csv"
    if not csv_path.exists():
        typer.echo(f"no audiences.csv in {run_dir}", err=True)
        raise typer.Exit(code=1)
    _run_build(csv_path, yes=yes, only=None, resume_dir=run_dir)


@app.command("run-batch")
def run_batch(folder: Path, yes: bool = typer.Option(False, "--yes")):
    """Build every audiences CSV in a folder, one client at a time."""
    files = sorted(folder.glob("*.csv"))
    if not files:
        typer.echo(f"no CSVs in {folder}", err=True)
        raise typer.Exit(code=1)
    failures = []
    for path in files:
        typer.echo(f"=== {path.name}")
        try:
            _run_build(path, yes=yes, only=None)
        except (TriClayError, typer.Exit) as exc:  # keep going with the next client
            failures.append(path.name)
            typer.echo(f"FAILED {path.name}: {exc}", err=True)
    typer.echo(f"batch done: {len(files) - len(failures)}/{len(files)} ok")
    if failures:
        raise typer.Exit(code=1)


@app.command()
def cleanup(
    client: str = typer.Option(..., help="client name (run folder)"),
    older_than_days: int = typer.Option(30, "--older-than-days"),
    confirm: bool = typer.Option(False, "--confirm"),
):
    """List tables this tool created (dry-run). Deleting needs the Clay app for now."""
    cutoff = datetime.now().timestamp() - older_than_days * 86400
    found: list[str] = []
    for checkpoint_path in Path("runs").glob(f"{client}/*/checkpoint.json"):
        if checkpoint_path.stat().st_mtime > cutoff and not confirm:
            continue
        data = __import__("json").loads(checkpoint_path.read_text(encoding="utf-8"))
        for key, table in (data.get("tables") or {}).items():
            found.append(f"{table.get('table_name')} ({table.get('table_id')}) from {checkpoint_path.parent.name}")
    for line in found:
        typer.echo(line)
    typer.echo(f"{len(found)} table(s) from {client} older than {older_than_days}d")
    if confirm:
        typer.echo(
            "Deletion is not automated yet: Clay's delete endpoint is not in the verified "
            "allowlist. Delete these tables in the Clay app (or record a HAR of a table "
            "deletion and I will wire it with --confirm-name)."
        )


if __name__ == "__main__":
    sys.exit(app())
