"""Queue worker: runs tri-clay builds one at a time and records live progress."""

from __future__ import annotations

import csv
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from webapp import clayops, db, envload
from tri_clay.errors import is_transient_error, summarize_clay_error

envload.load_env()

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent
JOBS_DIR = APP_DIR / "data" / "jobs"
POLL_SECONDS = 2.0
MAX_TABLES_PER_HOUR = int(os.environ.get("TRI_CLAY_MAX_TABLES_PER_HOUR", "30") or 30)
JOB_RETRIES = max(0, int(os.environ.get("TRI_CLAY_JOB_RETRIES", "2") or 2))
RETRY_COOLDOWN_SECONDS = max(10, int(os.environ.get("TRI_CLAY_RETRY_COOLDOWN", "90") or 90))
CSV_COLUMNS = (
    "client",
    "audience_id",
    "audience_name",
    "tier",
    "priority",
    "result_entity",
    "clay_query_draft",
    "max_contacts_per_company",
    "target_people",
    "pause_if_below",
    "pause_if_above",
    "dedupe_scope",
    "not_expressible",
    "proxy_applied",
    "run_notes",
)


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def tri_clay_command() -> list[str]:
    name = "tri-clay.exe" if os.name == "nt" else "tri-clay"
    script = Path(sys.executable).parent / name
    if script.exists():
        return [str(script)]
    return [sys.executable, "-m", "tri_clay.cli"]


def insert_event(con, job_id: int, kind: str, message: str) -> None:
    con.execute(
        "INSERT INTO events(job_id, ts, kind, message) VALUES(?, ?, ?, ?)",
        (job_id, db.utcnow(), kind, message[:2000]),
    )
    con.commit()


def claim(con) -> Any:
    con.execute("BEGIN IMMEDIATE")
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=RETRY_COOLDOWN_SECONDS)).isoformat(
            timespec="seconds"
        )
        row = con.execute(
            "SELECT * FROM jobs WHERE status = 'queued' "
            "AND (resume_of IS NULL OR created_at <= ?) "
            "ORDER BY priority, id LIMIT 1",
            (cutoff,),
        ).fetchone()
        if not row:
            con.commit()
            return None
        cursor = con.execute(
            "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'",
            (db.utcnow(), row["id"]),
        )
        con.commit()
        return row if cursor.rowcount == 1 else None
    except Exception:
        con.rollback()
        raise


def build_job_csv(job, audience, client) -> Path:
    work_dir = JOBS_DIR / str(job["id"])
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        defaults = json.loads(client["defaults_json"] or "{}")
    except json.JSONDecodeError:
        defaults = {}
    row = {
        "client": client["name"],
        "audience_id": audience["audience_id"],
        "audience_name": audience["audience_name"],
        "tier": audience["tier"] or defaults.get("tier") or "Tier 1",
        "priority": audience["priority"] if audience["priority"] is not None else defaults.get("priority", 1),
        "result_entity": "people",
        "clay_query_draft": clayops.effective_query(audience),
        "max_contacts_per_company": (
            audience["max_contacts_per_company"]
            if audience["max_contacts_per_company"] is not None
            else defaults.get("max_contacts_per_company", 2)
        ),
        "target_people": (
            audience["target_people"]
            if audience["target_people"] is not None
            else defaults.get("target_people", "")
        ),
        "pause_if_below": defaults.get("pause_if_below", ""),
        "pause_if_above": defaults.get("pause_if_above", ""),
        "dedupe_scope": audience["dedupe_scope"] or defaults.get("dedupe_scope", "client"),
        "not_expressible": "",
        "proxy_applied": "",
        "run_notes": f"webapp job {job['id']}",
    }
    path = work_dir / "audience.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerow(row)
    return path


def _blank_progress() -> dict[str, Any]:
    return {
        "phase": "starting",
        "run_dir": None,
        "parent_count": None,
        "effective": None,
        "shards_total": None,
        "tables": {},
        "outputs": {},
    }


def _table_by_id(progress: dict[str, Any], table_id: str) -> dict[str, Any]:
    for table in progress["tables"].values():
        if table.get("id") == table_id:
            return table
    table = {"name": f"table {table_id}", "id": table_id, "state": "created"}
    progress["tables"][table_id] = table
    return table


def apply_line(progress: dict[str, Any], line: str) -> str | None:
    """Update progress from one stdout line; return an event kind if noteworthy."""
    if match := re.match(r"run_dir: (.+)$", line):
        progress["run_dir"] = match.group(1).strip()
        progress["phase"] = "planning"
        return "planning"
    if match := re.match(r"plan ([^:]+): parent (\d+), effective (\d+), (\d+) shard", line):
        progress["parent_count"] = int(match.group(2))
        progress["effective"] = int(match.group(3))
        progress["shards_total"] = int(match.group(4))
        progress["phase"] = "building"
        return "planned"
    if match := re.match(r"\[progress\] table=(.+?) id=(\S+) state=(\w+)", line):
        table = progress["tables"].setdefault(match.group(1).strip(), {})
        table.update({"name": match.group(1).strip(), "id": match.group(2), "state": match.group(3)})
        return None
    if match := re.match(r"\[progress\] import table=(\S+) rows=(\d+) run=(\S+)", line):
        table = _table_by_id(progress, match.group(1))
        table.update({"rows": int(match.group(2)), "run_status": match.group(3), "state": "importing"})
        return None
    if match := re.match(r"\[progress\] fields table=(\S+) processed=(\d+)/(\d+)", line):
        table = _table_by_id(progress, match.group(1))
        table.update(
            {
                "fields_done": int(match.group(2)),
                "fields_total": int(match.group(3)),
                "state": "columns",
            }
        )
        return None
    if match := re.match(r"\[progress\] export table=(\S+) status=(\w+)", line):
        table = _table_by_id(progress, match.group(1))
        table.update({"export_status": match.group(2), "state": "exporting"})
        return None
    if match := re.match(r"  built (.+?): (\d+) rows, exported (\d+)", line):
        table = progress["tables"].setdefault(match.group(1).strip(), {"name": match.group(1).strip()})
        table.update(
            {"rows": int(match.group(2)), "exported": int(match.group(3)), "state": "exported"}
        )
        return "table_done"
    if match := re.match(r"outputs: (.+)$", line):
        progress["outputs"]["dir"] = match.group(1).strip()
        progress["phase"] = "outputs"
        return "outputs"
    if match := re.match(r"merged:  (.+)$", line):
        progress["outputs"]["merged"] = match.group(1).strip()
        return None
    if match := re.match(r"report:  (.+)$", line):
        progress["outputs"]["report"] = match.group(1).strip()
        return None
    if line.startswith("credits:"):
        progress["credits"] = line.strip()
        return None
    if "error" in line.lower() or line.startswith("FAIL"):
        progress.setdefault("warnings", []).append(line.strip()[:300])
        return "warning"
    return None


def read_checkpoint(run_dir: str | None) -> dict[str, Any]:
    if not run_dir:
        return {}
    path = Path(run_dir) / "checkpoint.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _error_excerpt(log_text: str) -> str:
    """Last exception line plus its continuation, instead of a rich-wrapped tail fragment."""
    lines = [line.rstrip() for line in log_text.splitlines() if line.strip()]
    for index in range(len(lines) - 1, -1, -1):
        if "error" in lines[index].lower():
            excerpt = " ".join(part.strip() for part in lines[index : index + 4])
            return excerpt[:1000]
    return lines[-1][:1000] if lines else "unknown error"


def attempt_number(con, job_id: int) -> int:
    """1 for the original job, 2 for its first retry, ... (walks the resume_of chain)."""
    depth = 1
    current = job_id
    for _ in range(20):
        row = con.execute("SELECT resume_of FROM jobs WHERE id = ?", (current,)).fetchone()
        parent = row["resume_of"] if row else None
        if not parent:
            break
        depth += 1
        current = int(parent)
    return depth


def schedule_retry(con, job, progress: dict[str, Any], message: str) -> int | None:
    """Create a resume job for a transient failure; returns the new job id."""
    attempt = attempt_number(con, int(job["id"]))
    if attempt > JOB_RETRIES:
        return None
    run_dir = progress.get("run_dir")
    cursor = con.execute(
        "INSERT INTO jobs(audience_row_id, client_id, status, priority, created_at, resume_of, "
        "progress_json) VALUES(?, ?, 'queued', ?, ?, ?, ?)",
        (
            job["audience_row_id"],
            job["client_id"],
            job["priority"],
            db.utcnow(),
            job["id"],
            json.dumps({"phase": "retry-queued", "run_dir": run_dir}),
        ),
    )
    retry_id = cursor.lastrowid
    con.execute(
        "UPDATE audiences SET status = 'queued', updated_at = ? WHERE id = ?",
        (db.utcnow(), job["audience_row_id"]),
    )
    con.commit()
    insert_event(
        con,
        job["id"],
        "retrying",
        f"transient failure - retry {attempt}/{JOB_RETRIES} scheduled as job {retry_id} "
        f"in ~{RETRY_COOLDOWN_SECONDS}s: {message[:300]}",
    )
    insert_event(
        con,
        retry_id,
        "info",
        f"auto-retry {attempt}/{JOB_RETRIES} of job {job['id']} (resumes finished tables)",
    )
    return int(retry_id) if retry_id is not None else None


def record_table_built(con) -> None:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=1)
    stamps = []
    for raw in db.kv_json(con, "tables_built", []) or []:
        try:
            moment = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if moment > cutoff:
            stamps.append(moment.isoformat())
    stamps.append(now.isoformat())
    db.kv_set(con, "tables_built", json.dumps(stamps))


def rate_limit_delay(con) -> float:
    """Seconds to wait before a new job: the 30-tables/hour cap is global, not per job."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=1)
    moments = []
    for raw in db.kv_json(con, "tables_built", []) or []:
        try:
            moment = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if moment > cutoff:
            moments.append(moment)
    if len(moments) < MAX_TABLES_PER_HOUR:
        return 0.0
    return max(0.0, (min(moments) + timedelta(hours=1) - now).total_seconds())
    path = Path(run_dir) / "checkpoint.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def finalize(con, job_id: int, progress: dict[str, Any], exit_code: int, log_text: str) -> None:
    run_dir = progress.get("run_dir")
    checkpoint = read_checkpoint(run_dir) or {}
    tables = checkpoint.get("tables")
    if isinstance(tables, dict):
        for key, table in tables.items():
            if not isinstance(table, dict):
                continue
            progress["tables"].setdefault(
                table.get("table_name") or key,
                {"name": table.get("table_name") or key, "id": table.get("table_id"), "state": "built"},
            )
    status = "done"
    error = None
    lowered = log_text[-60000:].lower()
    retry_scheduled = False
    if exit_code != 0:
        if "web-app session rejected" in lowered or "clay_session_cookie is missing" in lowered:
            status = "needs_session"
            error = "Clay session expired during the build. Reconnect, then resume this job."
            clayops.set_queue_paused(con, "Clay session expired during a build")
        else:
            raw = _error_excerpt(log_text) or f"exit code {exit_code}"
            error = summarize_clay_error(raw) or raw
            job = con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is not None and is_transient_error(f"{raw} {log_text[-20000:]}"):
                retry_id = schedule_retry(con, job, progress, error)
                if retry_id is not None:
                    retry_scheduled = True
                    status = "retrying"
                    attempt = attempt_number(con, job_id)
                    error = (
                        f"{error} - Clay was busy; retry {attempt}/{JOB_RETRIES} queued as "
                        f"job {retry_id} (finished tables are reused)."
                    )
            if not retry_scheduled:
                status = "failed"
    progress["phase"] = "finished" if status == "done" else status
    con.execute(
        "UPDATE jobs SET status = ?, finished_at = ?, run_dir = ?, error = ?, progress_json = ? WHERE id = ?",
        (status, db.utcnow(), run_dir, error, json.dumps(progress), job_id),
    )
    job = con.execute("SELECT audience_row_id FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not retry_scheduled:
        audience_status = {"done": "done", "needs_session": "failed", "failed": "failed"}.get(
            status, status
        )
        con.execute(
            "UPDATE audiences SET status = ?, updated_at = ? WHERE id = ?",
            (audience_status, db.utcnow(), job["audience_row_id"]),
        )
    con.commit()
    insert_event(con, job_id, "retrying" if retry_scheduled else status, error or f"build {status}")

    rotated = clayops.read_env_cookie(REPO_ROOT / ".env")
    if rotated and rotated != clayops.get_cookie(con):
        clayops.set_cookie(con, rotated)


def run_job(con, job) -> None:
    audience = con.execute("SELECT * FROM audiences WHERE id = ?", (job["audience_row_id"],)).fetchone()
    client = con.execute("SELECT * FROM clients WHERE id = ?", (job["client_id"],)).fetchone()
    work_dir = JOBS_DIR / str(job["id"])
    work_dir.mkdir(parents=True, exist_ok=True)
    log_path = work_dir / "job.log"
    csv_path = build_job_csv(job, audience, client)

    resume_dir = None
    if job["resume_of"]:
        previous = con.execute("SELECT run_dir FROM jobs WHERE id = ?", (job["resume_of"],)).fetchone()
        if previous and previous["run_dir"] and (Path(previous["run_dir"]) / "checkpoint.json").exists():
            resume_dir = previous["run_dir"]

    command = tri_clay_command()
    if resume_dir:
        command += ["resume", str(resume_dir), "--yes"]
        insert_event(con, job["id"], "info", f"resuming from {resume_dir}")
    else:
        command += ["build", str(csv_path), "--yes"]

    cookie = clayops.get_cookie(con)
    if not cookie:
        finalize(con, job["id"], _blank_progress(), 2, "web-app session rejected: no cookie")
        return
    env = os.environ.copy()
    env["CLAY_SESSION_COOKIE"] = cookie
    try:
        defaults = json.loads(client["defaults_json"] or "{}")
    except json.JSONDecodeError:
        defaults = {}
    folder = client["folder_id"] or ""
    env["CLAY_TARGET_FOLDER_ID"] = folder
    if defaults.get("pace_seconds"):
        env["TRI_CLAY_PACE_SECONDS"] = str(defaults["pace_seconds"])

    con.execute(
        "UPDATE jobs SET work_dir = ?, log_path = ?, progress_json = ? WHERE id = ?",
        (str(work_dir), str(log_path), json.dumps(_blank_progress()), job["id"]),
    )
    con.commit()
    insert_event(con, job["id"], "info", f"started: {' '.join(command[-3:])}")

    stream = queue.Queue()
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )

    def reader() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            stream.put(line)
        stream.put(None)

    threading.Thread(target=reader, daemon=True).start()

    progress = _blank_progress()
    lines: list[str] = []
    last_persist = 0.0
    last_phase = None
    cancel_checked = 0.0
    while True:
        try:
            line = stream.get(timeout=0.5)
        except queue.Empty:
            line = ""
        if line is not None and line:
            lines.append(line)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
            kind = apply_line(progress, line)
            if kind and (kind != last_phase or kind == "table_done"):
                insert_event(con, job["id"], kind, line.strip()[:500])
                last_phase = kind
            if kind == "table_done":
                record_table_built(con)
            now = time.monotonic()
            if now - last_persist > 2:
                con.execute(
                    "UPDATE jobs SET progress_json = ? WHERE id = ?",
                    (json.dumps(progress), job["id"]),
                )
                con.commit()
                last_persist = now
        if line is None and process.poll() is not None:
            break
        if time.monotonic() - cancel_checked > 2:
            cancel_checked = time.monotonic()
            row = con.execute("SELECT status FROM jobs WHERE id = ?", (job["id"],)).fetchone()
            if row and row["status"] == "canceling":
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                con.execute(
                    "UPDATE jobs SET status = 'canceled', finished_at = ?, error = ? WHERE id = ?",
                    (db.utcnow(), "canceled by user", job["id"]),
                )
                con.commit()
                insert_event(con, job["id"], "canceled", "canceled by user")
                return

    exit_code = process.wait()
    finalize(con, job["id"], progress, exit_code, "".join(lines))


def mark_job_failed(con, job_id: int, error: Exception) -> None:
    """A crash must never leave a job (or its audience) stuck on 'running'."""
    message = f"{type(error).__name__}: {error}"[:1000]
    row = con.execute("SELECT audience_row_id, progress_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return
    progress = {}
    try:
        progress = json.loads(row["progress_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        progress = {}
    progress["phase"] = "failed"
    con.execute(
        "UPDATE jobs SET status = 'failed', finished_at = ?, error = ?, progress_json = ? WHERE id = ? AND status IN ('running', 'canceling')",
        (db.utcnow(), message, json.dumps(progress), job_id),
    )
    con.execute(
        "UPDATE audiences SET status = 'failed', updated_at = ? WHERE id = ? AND status NOT IN ('done', 'failed')",
        (db.utcnow(), row["audience_row_id"]),
    )
    con.commit()
    insert_event(con, job_id, "failed", message)


def main() -> None:
    db.init_db()
    con = db.connect()
    clayops.seed_cookie_from_env(con)
    log("claybuilder worker started")
    while True:
        try:
            paused, reason = clayops.queue_paused(con)
            if paused:
                time.sleep(POLL_SECONDS)
                continue
            delay = rate_limit_delay(con)
            if delay > 0:
                log(f"global table rate limit reached; sleeping {delay:.0f}s")
                time.sleep(min(delay, 600))
                continue
            job = claim(con)
            if job:
                log(f"running job {job['id']}")
                try:
                    run_job(con, job)
                    log(f"job {job['id']} finished")
                except Exception as exc:  # noqa: BLE001 - never leave a job stuck on running
                    traceback.print_exc()
                    log(f"job {job['id']} crashed: {exc!r}")
                    mark_job_failed(con, job["id"], exc)
                continue
        except Exception as exc:  # noqa: BLE001 - worker must survive anything
            log(f"worker error: {exc!r}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
