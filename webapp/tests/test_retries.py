"""Automatic retries: table import/export level and job level."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from tri_clay import builder as builder_module
from tri_clay import export as export_module
from tri_clay.checkpoint import Checkpoint
from tri_clay.errors import TriClayError, is_transient_error, summarize_clay_error
from webapp import db, worker


class _Audience:
    audience_id = "a1"
    audience_name = "Audience One"


class _Shard:
    index = 1
    total = 1
    table_name = "Audience One"
    query = "select from people"
    count = 10


def test_error_summary_and_transient_detection():
    raw = (
        "{'id': 'x', 'status': 'ERROR', 'statusMessage': 'Too many concurrent requests in "
        "your workspace. Please try again shortly.'}"
    )
    assert summarize_clay_error(raw) == (
        "Too many concurrent requests in your workspace. Please try again shortly."
    )
    assert is_transient_error(raw)
    assert not is_transient_error("invalid industry 'Spas'")


def test_build_table_retries_transient_failures(tmp_path, monkeypatch):
    checkpoint = Checkpoint.load(tmp_path / "checkpoint.json")
    builder = builder_module.TableBuilder.__new__(builder_module.TableBuilder)
    builder.checkpoint = checkpoint
    calls = {"n": 0}

    def fake_once(audience, shard, workbook_id):
        calls["n"] += 1
        checkpoint.set_table("a1:1", {"table_id": f"t{calls['n']}"})
        if calls["n"] == 1:
            raise TriClayError(
                "source run failed: Too many concurrent requests in your workspace. "
                "Please try again shortly. (status=ERROR)"
            )
        return "built"

    builder._build_table_once = fake_once
    monkeypatch.setattr(builder_module, "TABLE_RETRY_DELAY", 5.0)
    monkeypatch.setattr(builder_module.time, "sleep", lambda seconds: None)

    assert builder.build_table(_Audience(), _Shard(), "wb") == "built"
    assert calls["n"] == 2
    assert checkpoint.get_table("a1:1") == {"table_id": "t2"}  # fresh table on retry


def test_build_table_does_not_retry_real_errors(tmp_path, monkeypatch):
    checkpoint = Checkpoint.load(tmp_path / "checkpoint.json")
    builder = builder_module.TableBuilder.__new__(builder_module.TableBuilder)
    builder.checkpoint = checkpoint
    calls = {"n": 0}

    def fake_once(audience, shard, workbook_id):
        calls["n"] += 1
        raise TriClayError("invalid industry 'Spas' in the query")

    builder._build_table_once = fake_once
    monkeypatch.setattr(builder_module.time, "sleep", lambda seconds: None)
    try:
        builder.build_table(_Audience(), _Shard(), "wb")
        raise AssertionError("should have raised")
    except TriClayError as exc:
        assert "invalid industry" in str(exc)
    assert calls["n"] == 1


def test_export_retries_transient_failures(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_once(client, *, table_id, view_id, destination, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TriClayError(
                "export ex1 failed: {'status': 'ERROR', 'statusMessage': "
                "'Too many concurrent requests in your workspace.'}"
            )
        return "exported"

    monkeypatch.setattr(export_module, "_export_view_once", fake_once)
    monkeypatch.setattr(export_module, "EXPORT_RETRY_DELAY", 5.0)
    monkeypatch.setattr(export_module.time, "sleep", lambda seconds: None)

    result = export_module.export_view(
        None, table_id="t1", view_id="v1", destination=tmp_path / "out.csv"
    )
    assert result == "exported" and calls["n"] == 2


def _seed_job(con, *, status: str = "running", resume_of=None, created_at=None, tag: str = "1"):
    con.execute(
        "INSERT INTO clients(name, folder_id, defaults_json, created_at) "
        "VALUES(?, '', '{}', ?)",
        (f"RetryClient{tag}", db.utcnow()),
    )
    client_id = con.execute("SELECT id FROM clients ORDER BY id DESC").fetchone()["id"]
    con.execute(
        "INSERT INTO batches(client_id, name, filename, created_at) VALUES(?, 'B', 'f.csv', ?)",
        (client_id, db.utcnow()),
    )
    batch_id = con.execute("SELECT id FROM batches ORDER BY id DESC").fetchone()["id"]
    con.execute(
        "INSERT INTO audiences(batch_id, client_id, audience_id, audience_name, query, status, "
        "created_at, updated_at) VALUES(?, ?, 'a1', 'A1', 'select from people', 'running', ?, ?)",
        (batch_id, client_id, db.utcnow(), db.utcnow()),
    )
    audience_row = con.execute("SELECT id FROM audiences ORDER BY id DESC").fetchone()["id"]
    con.execute(
        "INSERT INTO jobs(audience_row_id, client_id, status, priority, created_at, resume_of) "
        "VALUES(?, ?, ?, 1, ?, ?)",
        (audience_row, client_id, status, created_at or db.utcnow(), resume_of),
    )
    job_id = con.execute("SELECT id FROM jobs ORDER BY id DESC").fetchone()["id"]
    con.commit()
    return job_id, audience_row


def test_finalize_schedules_automatic_retry_for_transient_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("TRI_CLAY_JOB_RETRIES", "2")
    path = tmp_path / "retry.db"
    db.init_db(path)
    con = db.connect(path)
    try:
        job_id, audience_row = _seed_job(con)
        progress = {"run_dir": "runs/acme/2026", "phase": "building"}
        worker.finalize(
            con,
            job_id,
            progress,
            1,
            "TriClayError: source run failed: Too many concurrent requests in your "
            "workspace. Please try again shortly. (status=ERROR)",
        )
        failed_job = con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert failed_job["status"] == "retrying"
        assert "retry 1/2" in (failed_job["error"] or "")
        retry = con.execute("SELECT * FROM jobs WHERE resume_of = ?", (job_id,)).fetchone()
        assert retry and retry["status"] == "queued"
        audience = con.execute("SELECT status FROM audiences WHERE id = ?", (audience_row,)).fetchone()
        assert audience["status"] == "queued"
        event = con.execute(
            "SELECT kind FROM events WHERE job_id = ? ORDER BY id DESC", (job_id,)
        ).fetchone()
        assert event["kind"] == "retrying"
    finally:
        con.close()


def test_finalize_marks_real_failures_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("TRI_CLAY_JOB_RETRIES", "2")
    path = tmp_path / "real.db"
    db.init_db(path)
    con = db.connect(path)
    try:
        job_id, audience_row = _seed_job(con)
        worker.finalize(
            con, job_id, {"phase": "building"}, 1,
            "TriClayError: invalid industry 'Spas' - cannot build",
        )
        job = con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert job["status"] == "failed"
        assert not con.execute("SELECT 1 FROM jobs WHERE resume_of = ?", (job_id,)).fetchone()
        audience = con.execute("SELECT status FROM audiences WHERE id = ?", (audience_row,)).fetchone()
        assert audience["status"] == "failed"
    finally:
        con.close()


def test_first_attempt_is_claimable_immediately(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "RETRY_COOLDOWN_SECONDS", 90)
    path = tmp_path / "cooldown.db"
    db.init_db(path)
    con = db.connect(path)
    try:
        job_id, _ = _seed_job(con, status="queued")
        claimed = worker.claim(con)
        assert claimed and claimed["id"] == job_id
    finally:
        con.close()


def test_claim_waits_for_the_retry_cooldown(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "RETRY_COOLDOWN_SECONDS", 90)
    path = tmp_path / "cooldown2.db"
    db.init_db(path)
    con = db.connect(path)
    try:
        parent, _ = _seed_job(con, status="running", tag="p")
        young_retry, _ = _seed_job(con, status="queued", resume_of=parent, tag="r")
        con.execute("UPDATE jobs SET created_at = ? WHERE id = ?", (db.utcnow(), young_retry))
        con.commit()
        assert worker.claim(con) is None  # retry still cooling down
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        con.execute("UPDATE jobs SET created_at = ? WHERE id = ?", (old, young_retry))
        con.commit()
        claimed = worker.claim(con)
        assert claimed and claimed["id"] == young_retry
    finally:
        con.close()
