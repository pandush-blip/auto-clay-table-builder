import json

from webapp import clayops, crypto, db, worker


def test_crypto_roundtrip():
    token = crypto.encrypt("secret", "claysession=abc")
    assert token != "claysession=abc"
    assert crypto.decrypt("secret", token) == "claysession=abc"
    assert crypto.decrypt("other-secret", token) is None


def test_parse_minimal_csv():
    content = (
        "client,audience name,clay search people queries,target_people\n"
        'Acme,Founders of SaaS,"select from people where company.industry in (""Computer Software"")",500\n'
    ).encode()
    rows, warnings = clayops.parse_audience_csv(content)
    assert len(rows) == 1
    assert rows[0]["audience_name"] == "Founders of SaaS"
    assert "company.industry" in rows[0]["query"]
    assert rows[0]["target_people"] == 500


def test_parse_csv_requires_query():
    rows, warnings = clayops.parse_audience_csv(b"name,other\nA,B\n")
    assert rows == []
    assert warnings


def test_rule_repair_uses_api_options():
    error = 'Invalid value "Softwre Development" for field \'industry\'. Use one of: "Computer Hardware", "Computer Networking", "Software Development"'
    parsed = clayops.parse_value_error(error)
    assert parsed
    bad, field, options = parsed
    assert bad == "Softwre Development" and field == "industry" and len(options) == 3
    repaired = clayops.rule_repair(
        'select from people where company.industry in ("Softwre Development")', error
    )
    assert repaired
    assert 'company.industry in ("Software Development")' in repaired["query"]
    assert repaired["source"] == "rule"
    assert clayops.rule_repair(
        'select from people where company.industry in ("Computer Software")',
        'Invalid value "Computer Software" for field \'industry\'. Use one of: "Computer Hardware", "Computer Networking"',
    ) is None


def test_lint_repair_applies_the_did_you_mean_suggestion():
    query = (
        'select from people where experiences.any('
        'company.industry in ("Movies, Videos, and Sound"))'
    )
    error = (
        "invalid industry 'Movies, Videos, and Sound' - did you mean "
        "'Movies, Videos and Sound', 'Movies and Sound Recording'?"
    )
    fixed = clayops.lint_repair(query, error)
    assert fixed and fixed["source"] == "rule"
    assert '"Movies, Videos and Sound"' in fixed["query"]
    assert "and Sound" in fixed["explanation"]


def test_lint_repair_leaves_semantic_guesses_to_the_ai():
    # No close reference match for "Spas": a rule must not invent an industry.
    assert (
        clayops.lint_repair(
            'select from people where experiences.any(company.industry in ("Spas"))',
            "invalid industry 'Spas'",
        )
        is None
    )


def test_unknown_field_repair_drops_only_that_predicate():
    query = (
        "select from people where experiences.any("
        "is_current = true and jobs.posted_within_days <= 30)"
    )
    fixed = clayops.unknown_field_repair(
        query, "Unknown field 'posted_within_days' for entity 'jobs'"
    )
    assert fixed and "posted_within_days" not in fixed["query"]
    assert "is_current = true" in fixed["query"]
    assert "review the query" in fixed["explanation"]


def test_validate_with_retry_rides_out_concurrency_errors(monkeypatch):
    calls = {"n": 0}

    def fake_validate(con, audience, query=None):
        calls["n"] += 1
        if calls["n"] < 3:
            return {
                "ok": False,
                "errors": [
                    'POST /search/query-mode/x/run -> 429: {"message":"Too many concurrent '
                    'requests in your workspace. Please try again shortly."}'
                ],
            }
        return {"ok": True, "count": 42, "errors": [], "warnings": []}

    monkeypatch.setattr(clayops, "validate_audience", fake_validate)
    monkeypatch.setattr(clayops.time, "sleep", lambda seconds: None)
    result = clayops.validate_with_retry(None, {"id": 1})
    assert result["ok"] and result["retries"] == 2 and calls["n"] == 3
    assert any("retried" in w for w in result["warnings"])


def test_validate_and_repair_auto_fixes_and_revalidates(monkeypatch):
    bad = 'select from people where experiences.any(company.industry in ("Movies, Videos, and Sound"))'
    good = 'select from people where experiences.any(company.industry in ("Movies, Videos and Sound"))'

    def fake_validate(con, audience, query=None):
        if (query or bad) == good:
            return {"ok": True, "count": 812, "errors": [], "warnings": []}
        return {
            "ok": False,
            "errors": [
                "invalid industry 'Movies, Videos, and Sound' - did you mean "
                "'Movies, Videos and Sound', 'Movies and Sound Recording'?"
            ],
        }

    monkeypatch.setattr(clayops, "validate_audience", fake_validate)
    monkeypatch.setattr(clayops.time, "sleep", lambda seconds: None)
    result = clayops.validate_and_repair(None, {"query": bad, "edited_query": None})
    assert result["ok"], result
    assert result["repaired_query"] == good
    assert result["source"] == "rule"
    assert result["count"] == 812


def test_worker_progress_parsing():
    progress = worker._blank_progress()
    worker.apply_line(progress, "run_dir: runs/acme/2026")
    worker.apply_line(progress, "plan aud-1: parent 1200, effective 1200, 1 shard(s)")
    worker.apply_line(progress, "[progress] table=Audience A id=tbl_1 state=created")
    worker.apply_line(progress, "[progress] import table=tbl_1 rows=400 run=RUNNING")
    worker.apply_line(progress, "[progress] fields table=tbl_1 processed=399/400")
    worker.apply_line(progress, "  built Audience A: 400 rows, exported 400")
    assert progress["run_dir"] == "runs/acme/2026"
    assert progress["parent_count"] == 1200
    table = progress["tables"]["Audience A"]
    assert table["id"] == "tbl_1"
    assert table["exported"] == 400


def test_kv_roundtrip(tmp_path):
    path = tmp_path / "kv.db"
    db.init_db(path)
    con = db.connect(path)
    try:
        assert db.kv_get(con, "missing", "fallback") == "fallback"
        db.kv_set(con, "flag", "1")
        assert db.kv_get(con, "flag") == "1"
    finally:
        con.close()


def test_flask_login_and_pages():
    from webapp import app as app_module

    client = app_module.app.test_client()
    response = client.get("/healthz")
    assert response.status_code == 200
    client.get("/login")
    with client.session_transaction() as session:
        token = session.get("csrf")
    assert token
    response = client.post(
        "/login",
        data={"email": "admin@test.local", "password": "test-password", "_csrf": token},
    )
    assert response.status_code == 302
    assert client.get("/").status_code == 200
    assert client.get("/clients").status_code == 200
    assert client.get("/upload").status_code == 200
    assert client.get("/queue").status_code == 200


def test_client_upload_and_queue_guard():
    import io

    from webapp import app as app_module

    client = app_module.app.test_client()
    client.get("/login")
    with client.session_transaction() as session:
        token = session["csrf"]
    client.post(
        "/login",
        data={"email": "admin@test.local", "password": "test-password", "_csrf": token},
    )
    client.get("/")
    with client.session_transaction() as session:
        token = session["csrf"]
    client.post(
        "/clients/save",
        data={
            "name": "FlowTest",
            "tier": "Tier 1",
            "dedupe_scope": "client",
            "max_contacts_per_company": "2",
            "_csrf": token,
        },
    )
    con = db.connect()
    try:
        client_row = con.execute("SELECT * FROM clients WHERE name = 'FlowTest'").fetchone()
        assert client_row
    finally:
        con.close()
    csv_bytes = (
        "audience name,clay search people queries\n"
        'Flow audience,"select from people where location_country in (""United States"")"\n'
    ).encode()
    response = client.post(
        "/upload",
        data={
            "client_id": str(client_row["id"]),
            "file": (io.BytesIO(csv_bytes), "flow.csv"),
            "_csrf": token,
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "Imported 1 audiences" in response.get_data(as_text=True)
    con = db.connect()
    try:
        batch_row = con.execute(
            "SELECT * FROM batches WHERE client_id = ? ORDER BY id DESC", (client_row["id"],)
        ).fetchone()
        audience_row = con.execute(
            "SELECT * FROM audiences WHERE batch_id = ?", (batch_row["id"],)
        ).fetchone()
    finally:
        con.close()
    assert audience_row["audience_name"] == "Flow audience"
    assert audience_row["status"] == "new"
    response = client.post(
        f"/batches/{batch_row['id']}/queue", data={"_csrf": token}, follow_redirects=True
    )
    text = response.get_data(as_text=True)
    assert "not verified" in text or "No validated" in text


def test_queue_creates_job_when_connection_ok():
    from webapp import app as app_module

    client = app_module.app.test_client()
    client.get("/login")
    with client.session_transaction() as session:
        token = session["csrf"]
    client.post(
        "/login",
        data={"email": "admin@test.local", "password": "test-password", "_csrf": token},
    )
    client.get("/")
    with client.session_transaction() as session:
        token = session["csrf"]
    con = db.connect()
    try:
        db.kv_set(
            con,
            "doctor_json",
            json.dumps({"ok": True, "workspace_name": "Test", "workspace_id": "1"}),
        )
        db.kv_set(con, "queue_paused", "0")
        batch_row = con.execute("SELECT * FROM batches ORDER BY id DESC LIMIT 1").fetchone()
        audience_row = con.execute(
            "SELECT * FROM audiences WHERE batch_id = ?", (batch_row["id"],)
        ).fetchone()
        con.execute(
            "UPDATE audiences SET status = 'valid' WHERE id = ?", (audience_row["id"],)
        )
        con.commit()
    finally:
        con.close()
    response = client.post(
        f"/batches/{batch_row['id']}/queue", data={"_csrf": token}, follow_redirects=True
    )
    assert response.status_code == 200
    con = db.connect()
    try:
        job = con.execute(
            "SELECT * FROM jobs WHERE audience_row_id = ?", (audience_row["id"],)
        ).fetchone()
        assert job and job["status"] == "queued"
    finally:
        con.close()


def test_worker_pauses_queue_on_session_expiry(tmp_path):
    path = tmp_path / "jobs.db"
    db.init_db(path)
    con = db.connect(path)
    try:
        con.execute(
            "INSERT INTO clients(name, folder_id, defaults_json, created_at) VALUES('C', '', '{}', ?)",
            (db.utcnow(),),
        )
        client_id = con.execute("SELECT id FROM clients").fetchone()["id"]
        con.execute(
            "INSERT INTO batches(client_id, name, filename, created_at) VALUES(?, 'B', 'f.csv', ?)",
            (client_id, db.utcnow()),
        )
        batch_id = con.execute("SELECT id FROM batches").fetchone()["id"]
        con.execute(
            "INSERT INTO audiences(batch_id, client_id, audience_id, audience_name, query, status, created_at, updated_at) "
            "VALUES(?, ?, 'a1', 'A1', 'select from people', 'queued', ?, ?)",
            (batch_id, client_id, db.utcnow(), db.utcnow()),
        )
        audience_id = con.execute("SELECT id FROM audiences").fetchone()["id"]
        con.execute(
            "INSERT INTO jobs(audience_row_id, client_id, status, priority, created_at) VALUES(?, ?, 'running', 1, ?)",
            (audience_id, client_id, db.utcnow()),
        )
        job_id = con.execute("SELECT id FROM jobs").fetchone()["id"]
        con.commit()
        worker.finalize(
            con,
            job_id,
            worker._blank_progress(),
            1,
            "SessionExpiredError: web-app session rejected on POST /v3/... (401)",
        )
        job = con.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert job["status"] == "needs_session"
        assert clayops.queue_paused(con)[0] is True
    finally:
        con.close()


def test_worker_global_rate_limit(tmp_path):
    from datetime import datetime, timedelta, timezone

    path = tmp_path / "rate.db"
    db.init_db(path)
    con = db.connect(path)
    try:
        assert worker.rate_limit_delay(con) == 0.0
        now = datetime.now(timezone.utc)
        stamps = [(now - timedelta(minutes=30)).isoformat()] * 30
        db.kv_set(con, "tables_built", json.dumps(stamps))
        delay = worker.rate_limit_delay(con)
        assert 25 * 60 < delay <= 31 * 60
        db.kv_set(
            con,
            "tables_built",
            json.dumps([(now - timedelta(hours=2)).isoformat()] * 30),
        )
        assert worker.rate_limit_delay(con) == 0.0
    finally:
        con.close()


def test_batch_validate_endpoint_auto_fixes_and_reports(monkeypatch):
    """Validate-all must run server-side, auto-repair, and report per row."""
    import io
    import time

    from webapp import app as app_module

    client = app_module.app.test_client()
    client.get("/login")
    with client.session_transaction() as session:
        token = session["csrf"]
    client.post(
        "/login",
        data={"email": "admin@test.local", "password": "test-password", "_csrf": token},
    )
    client.get("/")
    with client.session_transaction() as session:
        token = session["csrf"]
    client.post(
        "/clients/save",
        data={
            "name": "BatchValidate",
            "tier": "Tier 1",
            "dedupe_scope": "client",
            "max_contacts_per_company": "2",
            "_csrf": token,
        },
    )
    con = db.connect()
    try:
        client_row = con.execute(
            "SELECT * FROM clients WHERE name = 'BatchValidate'"
        ).fetchone()
    finally:
        con.close()
    csv_bytes = (
        "audience name,clay search people queries\n"
        'Fixing row,"select from people where experiences.any(company.industry in (""Movies, Videos, and Sound""))"\n'
        'Clean row,"select from people where experiences.any(is_current = true)"\n'
    ).encode()
    client.post(
        "/upload",
        data={
            "client_id": str(client_row["id"]),
            "file": (io.BytesIO(csv_bytes), "batch.csv"),
            "_csrf": token,
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    con = db.connect()
    try:
        batch_row = con.execute(
            "SELECT * FROM batches WHERE client_id = ? ORDER BY id DESC", (client_row["id"],)
        ).fetchone()
        rows = con.execute(
            "SELECT * FROM audiences WHERE batch_id = ? ORDER BY id", (batch_row["id"],)
        ).fetchall()
    finally:
        con.close()
    assert len(rows) == 2

    def fake_validate_and_repair(con, audience, **kwargs):
        if "and Sound" in (audience["query"] or "") and "Videos," in audience["query"]:
            good = audience["query"].replace("Videos, and Sound", "Videos and Sound")
            return {
                "ok": True,
                "count": 700,
                "errors": [],
                "warnings": [],
                "repaired_query": good,
                "source": "rule",
                "explanation": "industry: replaced 'Movies, Videos, and Sound' with 'Movies, Videos and Sound'.",
            }
        return {"ok": True, "count": 123, "errors": [], "warnings": []}

    monkeypatch.setattr(clayops, "validate_and_repair", fake_validate_and_repair)
    started = client.post(
        f"/batches/{batch_row['id']}/validate", json={}, headers={"X-CSRF-Token": token}
    )
    assert started.status_code == 200 and started.get_json()["ok"]
    state = {}
    deadline = time.time() + 30
    while time.time() < deadline:
        state = client.get(f"/batches/{batch_row['id']}/validate").get_json()
        if not state.get("running"):
            break
        time.sleep(0.1)
    assert state.get("running") is False, state
    assert state["done"] == 2
    statuses = {entry["status"] for entry in state["results"].values()}
    assert statuses == {"valid"}
    assert any(entry.get("repaired") for entry in state["results"].values())

    con = db.connect()
    try:
        stored = con.execute(
            "SELECT status, repaired_query FROM audiences WHERE batch_id = ? ORDER BY id",
            (batch_row["id"],),
        ).fetchall()
    finally:
        con.close()
    assert all(row["status"] == "valid" for row in stored)
    assert any("Videos and Sound" in (row["repaired_query"] or "") for row in stored)
