"""Clay Table Builder web app."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from webapp import envload

envload.load_env()

from webapp import clayops, db  # noqa: E402

APP_DIR = Path(__file__).resolve().parent
VALID_STATUSES = {"valid", "repaired"}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(16)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("CLAYBUILDER_INSECURE_COOKIES", "0") != "1",
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,
)


# ------------------------------------------------------------------ bootstrap
def ensure_admin() -> None:
    con = db.connect()
    try:
        if con.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]:
            return
        email = os.environ.get("ADMIN_EMAIL") or "admin@example.com"
        password = os.environ.get("ADMIN_PASSWORD")
        generated = password is None
        password = password or secrets.token_urlsafe(12)
        try:
            con.execute(
                "INSERT INTO users(email, password_hash, is_admin, created_at) VALUES(?, ?, 1, ?)",
                (email, generate_password_hash(password), db.utcnow()),
            )
            con.commit()
        except Exception:  # noqa: BLE001 - another gunicorn worker won the race
            return
        if generated:
            creds = APP_DIR / "data" / "INITIAL_CREDENTIALS.txt"
            creds.parent.mkdir(parents=True, exist_ok=True)
            creds.write_text(f"email: {email}\npassword: {password}\n", encoding="utf-8")
            try:
                creds.chmod(0o600)
            except OSError:
                pass
    finally:
        con.close()


db.init_db()
ensure_admin()
_seed_con = db.connect()
try:
    clayops.seed_cookie_from_env(_seed_con)
finally:
    _seed_con.close()


# --------------------------------------------------------------------- helpers
def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    con = db.connect()
    try:
        return con.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    finally:
        con.close()


# --- shared sign-on ----------------------------------------------------------
# One signed cookie on the parent domain (`tri_sso`) authenticates every Revenue Inbox app.
# Payload is {"email": ..., "exp": ...}, signed with TRI_SSO_SECRET (HMAC-SHA256, base64url).

SSO_COOKIE = "tri_sso"
SSO_TTL_SECONDS = 60 * 60 * 24 * 30


def _sso_secret() -> str:
    return os.environ.get("TRI_SSO_SECRET", "")


def sso_email() -> str:
    """Returns the signed-in email from the shared cookie, or an empty string."""
    secret = _sso_secret()
    if not secret:
        return ""
    raw = request.cookies.get(SSO_COOKIE, "")
    if "." not in raw:
        return ""
    payload, _, signature = raw.rpartition(".")
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    try:
        supplied = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    except Exception:  # noqa: BLE001 - malformed cookie means "not signed in"
        return ""
    if not hmac.compare_digest(expected, supplied):
        return ""
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:  # noqa: BLE001
        return ""
    email = str(data.get("email") or "")
    exp = int(data.get("exp") or 0)
    if not email or exp * 1000 < time.time() * 1000:
        return ""
    return email


def sso_token(email: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"email": email, "exp": int(time.time()) + SSO_TTL_SECONDS}).encode()
    ).decode().rstrip("=")
    signature = base64.urlsafe_b64encode(
        hmac.new(_sso_secret().encode(), payload.encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")
    return f"{payload}.{signature}"


def sso_credentials_match(email: str, password: str) -> bool:
    want_email = os.environ.get("TRI_SSO_EMAIL", "admin@example.com").strip().lower()
    want_password = os.environ.get("TRI_SSO_PASSWORD", "")
    return (
        bool(want_password)
        and email.strip().lower() == want_email
        and secrets.compare_digest(password, want_password)
    )


def user_for_email(email: str):
    """Local user row for a signed-in email; the shared login creates it on first use."""
    wanted = email.strip().lower()
    con = db.connect()
    try:
        row = con.execute("SELECT * FROM users WHERE lower(email) = ?", (wanted,)).fetchone()
        if row:
            return row
        con.execute(
            "INSERT INTO users(email, password_hash, is_admin, created_at) VALUES(?, ?, 1, ?)",
            (wanted, generate_password_hash(secrets.token_urlsafe(24)), db.utcnow()),
        )
        con.commit()
        return con.execute("SELECT * FROM users WHERE lower(email) = ?", (wanted,)).fetchone()
    finally:
        con.close()


def adopt_shared_session() -> bool:
    """A valid `tri_sso` cookie counts as a local session - one login for every app."""
    if session.get("user_id"):
        return True
    email = sso_email()
    if not email:
        return False
    user = user_for_email(email)
    if not user:
        return False
    session["user_id"] = user["id"]
    session.permanent = True
    return True


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id") and not adopt_shared_session():
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user or not user["is_admin"]:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        session["csrf"] = token
    return token


@app.before_request
def adopt_shared_cookie():
    """Any request may arrive with the shared cookie - adopt it before the CSRF check runs."""
    adopt_shared_session()
    return None


@app.before_request
def csrf_protect():
    if request.method != "POST" or request.path == "/healthz":
        return None
    sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
    if not sent or sent != session.get("csrf"):
        abort(400, "CSRF check failed - reload the page and try again")
    return None


@app.context_processor
def inject_globals():
    return {"csrf_token": csrf_token, "user": current_user()}


def is_admin() -> bool:
    user = current_user()
    return bool(user and user["is_admin"])


app.jinja_env.globals["is_admin"] = is_admin


def conn():
    return db.connect()


def slug_unique(con, batch_id: int, base: str) -> str:
    slug = clayops.slugify(base)
    candidate = slug
    index = 2
    while con.execute(
        "SELECT 1 FROM audiences WHERE batch_id = ? AND audience_id = ?", (batch_id, candidate)
    ).fetchone():
        candidate = f"{slug}-{index}"
        index += 1
    return candidate


def parse_json(raw: str | None, fallback):
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback


def job_payload(con, job) -> dict:
    audience = con.execute("SELECT * FROM audiences WHERE id = ?", (job["audience_row_id"],)).fetchone()
    client = con.execute("SELECT * FROM clients WHERE id = ?", (job["client_id"],)).fetchone()
    progress = parse_json(job["progress_json"], {})
    outputs = []
    # Repaired/recovered jobs can carry the run dir only inside progress_json.
    run_dir_value = job["run_dir"] or (progress or {}).get("run_dir")
    if run_dir_value:
        run_dir = Path(run_dir_value)
        for pattern in ("outputs/*.csv", "exports/*.csv", "run_report.md"):
            for path in sorted(run_dir.glob(pattern)):
                outputs.append(
                    {
                        "name": path.name,
                        "rel": str(path.relative_to(run_dir)),
                        "size": path.stat().st_size if path.exists() else 0,
                    }
                )
    return {
        "id": job["id"],
        "status": job["status"],
        "priority": job["priority"],
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "error": job["error"],
        "run_dir": run_dir_value,
        "client": client["name"] if client else "",
        "client_id": job["client_id"],
        "audience_name": audience["audience_name"] if audience else "",
        "audience_id": audience["audience_id"] if audience else "",
        "audience_row_id": job["audience_row_id"],
        "progress": progress,
        "outputs": outputs,
    }


# ---------------------------------------------------------------------- routes
@app.get("/healthz")
def healthz():
    return "ok", 200


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        con = conn()
        try:
            user = con.execute("SELECT * FROM users WHERE lower(email) = ?", (email,)).fetchone()
        finally:
            con.close()
        local_ok = bool(user) and check_password_hash(user["password_hash"], password)
        shared_ok = sso_credentials_match(email, password)
        if local_ok or shared_ok:
            if not user and shared_ok:
                user = user_for_email(email)
            session.clear()
            session["user_id"] = user["id"]
            session.permanent = True
            response = redirect(request.args.get("next") or url_for("dashboard"))
            if shared_ok and _sso_secret():
                response.set_cookie(
                    SSO_COOKIE,
                    sso_token(email),
                    max_age=SSO_TTL_SECONDS,
                    httponly=True,
                    secure=request.is_secure or request.headers.get("x-forwarded-proto") == "https",
                    samesite="Lax",
                    domain=os.environ.get("TRI_SSO_DOMAIN") or None,
                )
            return response
        flash("Wrong email or password.", "error")
    return render_template("login.html")


@app.post("/logout")
@login_required
def logout():
    session.clear()
    response = redirect(url_for("login"))
    # Clear the shared cookie too, otherwise another app signs the user straight back in.
    response.delete_cookie(SSO_COOKIE, domain=os.environ.get("TRI_SSO_DOMAIN") or None)
    return response


@app.get("/")
@login_required
def dashboard():
    con = conn()
    try:
        stats = {
            "queued": con.execute("SELECT COUNT(*) c FROM jobs WHERE status = 'queued'").fetchone()["c"],
            "running": con.execute("SELECT COUNT(*) c FROM jobs WHERE status = 'running'").fetchone()["c"],
            "done": con.execute("SELECT COUNT(*) c FROM jobs WHERE status = 'done'").fetchone()["c"],
            "failed": con.execute(
                "SELECT COUNT(*) c FROM jobs WHERE status IN ('failed', 'needs_session')"
            ).fetchone()["c"],
        }
        recent = db.dicts(
            con.execute(
                "SELECT j.*, a.audience_name, c.name AS client_name "
                "FROM jobs j JOIN audiences a ON a.id = j.audience_row_id "
                "JOIN clients c ON c.id = j.client_id ORDER BY j.id DESC LIMIT 12"
            )
        )
        batches = db.dicts(
            con.execute(
                "SELECT b.*, c.name AS client_name, "
                "(SELECT COUNT(*) FROM audiences a WHERE a.batch_id = b.id) AS audiences "
                "FROM batches b JOIN clients c ON c.id = b.client_id ORDER BY b.id DESC LIMIT 10"
            )
        )
    finally:
        con.close()
    doctor_state = None
    con = conn()
    try:
        doctor_state = db.kv_json(con, "doctor_json")
        paused, reason = clayops.queue_paused(con)
    finally:
        con.close()
    return render_template(
        "dashboard.html",
        stats=stats,
        recent=recent,
        batches=batches,
        doctor=doctor_state,
        paused=paused,
        pause_reason=reason,
    )


@app.get("/connect")
@login_required
def connect():
    con = conn()
    try:
        doctor_state = db.kv_json(con, "doctor_json")
        updated = clayops.cookie_updated_at(con)
        has_cookie = bool(clayops.get_cookie(con))
        paused, reason = clayops.queue_paused(con)
    finally:
        con.close()
    return render_template(
        "connect.html",
        doctor=doctor_state,
        cookie_updated=updated,
        has_cookie=has_cookie,
        paused=paused,
        pause_reason=reason,
    )


@app.post("/connect/test")
@login_required
def connect_test():
    con = conn()
    try:
        result = clayops.doctor(con)
    finally:
        con.close()
    if result.get("ok"):
        flash(
            f"Clay connection OK - {result.get('workspace_name')} "
            f"(row cap {result.get('row_cap')}, credits {result.get('credits')}).",
            "ok",
        )
    else:
        flash(f"Clay connection failed: {result.get('error')}", "error")
    return redirect(url_for("connect"))


@app.post("/connect/cookie")
@login_required
def connect_cookie():
    raw = (request.form.get("cookie") or "").strip().replace("\n", " ").replace("\r", " ")
    raw = re.sub(r"\s+", " ", raw)
    if not raw:
        flash("Paste the claysession cookie first.", "error")
        return redirect(url_for("connect"))
    if "claysession=" not in raw:
        raw = f"claysession={raw}"
    con = conn()
    try:
        clayops.set_cookie(con, raw)
        result = clayops.doctor(con)
    finally:
        con.close()
    if result.get("ok"):
        flash("Cookie saved and verified. Queue resumed.", "ok")
    else:
        flash(f"Cookie saved but the check failed: {result.get('error')}", "error")
    return redirect(url_for("connect"))


@app.get("/clients")
@login_required
def clients():
    con = conn()
    try:
        rows = db.dicts(con.execute("SELECT * FROM clients ORDER BY name"))
        for row in rows:
            row["defaults"] = parse_json(row["defaults_json"], {})
    finally:
        con.close()
    return render_template("clients.html", clients=rows)


@app.post("/clients/save")
@login_required
def clients_save():
    client_id = request.form.get("id") or ""
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Client name is required.", "error")
        return redirect(url_for("clients"))
    defaults = {
        "tier": (request.form.get("tier") or "").strip() or "Tier 1",
        "dedupe_scope": (request.form.get("dedupe_scope") or "client").strip(),
        "max_contacts_per_company": int(request.form.get("max_contacts_per_company") or 2),
        "target_people": int(request.form["target_people"]) if (request.form.get("target_people") or "").strip() else None,
        "priority": int(request.form["priority"]) if (request.form.get("priority") or "").strip() else 100,
        "pace_seconds": float(request.form["pace_seconds"]) if (request.form.get("pace_seconds") or "").strip() else None,
    }
    con = conn()
    try:
        if client_id:
            con.execute(
                "UPDATE clients SET name = ?, folder_id = ?, defaults_json = ? WHERE id = ?",
                (name, (request.form.get("folder_id") or "").strip(), json.dumps(defaults), client_id),
            )
        else:
            con.execute(
                "INSERT INTO clients(name, folder_id, defaults_json, created_at) VALUES(?, ?, ?, ?)",
                (name, (request.form.get("folder_id") or "").strip(), json.dumps(defaults), db.utcnow()),
            )
        con.commit()
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not save client: {exc}", "error")
    finally:
        con.close()
    return redirect(url_for("clients"))


@app.get("/upload")
@login_required
def upload():
    con = conn()
    try:
        client_rows = db.dicts(con.execute("SELECT * FROM clients ORDER BY name"))
    finally:
        con.close()
    return render_template("upload.html", clients=client_rows)


@app.post("/upload")
@login_required
def upload_post():
    uploaded = request.files.get("file")
    client_id = request.form.get("client_id")
    if not uploaded or not uploaded.filename:
        flash("Choose a CSV file first.", "error")
        return redirect(url_for("upload"))
    if not client_id:
        flash("Choose a client (create one under Clients first).", "error")
        return redirect(url_for("upload"))
    rows, warnings = clayops.parse_audience_csv(uploaded.read())
    if not rows:
        flash("; ".join(warnings) or "No usable rows in the CSV.", "error")
        return redirect(url_for("upload"))
    con = conn()
    try:
        client_row = con.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        if not client_row:
            flash("Unknown client.", "error")
            return redirect(url_for("upload"))
        defaults = parse_json(client_row["defaults_json"], {})
        batch_name = (request.form.get("batch_name") or "").strip() or (
            f"{client_row['name']} - {uploaded.filename}"
        )
        cursor = con.execute(
            "INSERT INTO batches(client_id, name, filename, created_at, status) VALUES(?, ?, ?, ?, 'review')",
            (client_row["id"], batch_name, uploaded.filename, db.utcnow()),
        )
        batch_id = cursor.lastrowid
        for row in rows:
            base = row.get("audience_id") or row["audience_name"]
            audience_id = slug_unique(con, batch_id, base)
            con.execute(
                "INSERT INTO audiences(batch_id, client_id, audience_id, audience_name, query, tier, "
                "priority, target_people, dedupe_scope, max_contacts_per_company, status, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)",
                (
                    batch_id,
                    client_row["id"],
                    audience_id,
                    row["audience_name"],
                    row["query"],
                    row.get("tier") or defaults.get("tier") or "Tier 1",
                    row.get("priority", defaults.get("priority", 1)),
                    row.get("target_people", defaults.get("target_people")),
                    row.get("dedupe_scope") or defaults.get("dedupe_scope", "client"),
                    row.get("max_contacts_per_company", defaults.get("max_contacts_per_company", 2)),
                    db.utcnow(),
                    db.utcnow(),
                ),
            )
        con.commit()
    finally:
        con.close()
    for warning in warnings:
        flash(warning, "warn")
    flash(f"Imported {len(rows)} audiences. Review and validate them now.", "ok")
    return redirect(url_for("batch", batch_id=batch_id))


@app.get("/batches/<int:batch_id>")
@login_required
def batch(batch_id: int):
    con = conn()
    try:
        batch_row = con.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if not batch_row:
            abort(404)
        client = con.execute("SELECT * FROM clients WHERE id = ?", (batch_row["client_id"],)).fetchone()
        audiences = db.dicts(
            con.execute("SELECT * FROM audiences WHERE batch_id = ? ORDER BY id", (batch_id,))
        )
    finally:
        con.close()
    for row in audiences:
        row["api"] = parse_json(row["api_json"], {})
        row["lint"] = parse_json(row["lint_json"], {})
        row["repair"] = parse_json(row["repair_json"], {})
    queued = sum(1 for row in audiences if row["status"] in ("queued", "building"))
    valid = sum(1 for row in audiences if row["status"] in VALID_STATUSES)
    return render_template(
        "batch.html",
        batch=batch_row,
        client=client,
        audiences=audiences,
        valid_count=valid,
        queued_count=queued,
        llm=clayops.llm_configured(),
    )


@app.post("/audiences/<int:audience_id>/validate")
@login_required
def audience_validate(audience_id: int):
    con = conn()
    try:
        row = con.execute("SELECT * FROM audiences WHERE id = ?", (audience_id,)).fetchone()
        if not row:
            return jsonify({"ok": False, "errors": ["unknown audience"]}), 404
        result = clayops.validate_and_repair(con, row)
        if result.get("ok"):
            status = "valid"
            if result.get("count") == 0:
                status = "invalid"
                result["errors"] = ["preview count is 0 - nothing to build"]
                result["ok"] = False
        else:
            status = "invalid"
        repaired = result.get("repaired_query")
        con.execute(
            "UPDATE audiences SET status = ?, lint_json = ?, api_json = ?, preview_count = ?, "
            "repaired_query = COALESCE(?, repaired_query), "
            "repair_json = COALESCE(?, repair_json), updated_at = ? WHERE id = ?",
            (
                status,
                json.dumps({"errors": [] if result.get("stage") != "lint" else result.get("errors", []),
                            "warnings": result.get("warnings", [])}),
                json.dumps(result),
                result.get("count"),
                repaired,
                json.dumps(
                    {
                        "source": result.get("source"),
                        "explanation": result.get("explanation"),
                        "fixes": result.get("fixes") or [],
                    }
                )
                if repaired
                else None,
                db.utcnow(),
                audience_id,
            ),
        )
        con.commit()
    finally:
        con.close()
    return jsonify(result)


@app.post("/audiences/<int:audience_id>/fix")
@login_required
def audience_fix(audience_id: int):
    con = conn()
    try:
        row = con.execute("SELECT * FROM audiences WHERE id = ?", (audience_id,)).fetchone()
        if not row:
            return jsonify({"ok": False, "message": "unknown audience"}), 404
        result = clayops.attempt_repair(con, row)
        if result.get("ok"):
            con.execute(
                "UPDATE audiences SET repaired_query = ?, repair_json = ?, status = 'valid', "
                "api_json = ?, preview_count = ?, updated_at = ? WHERE id = ?",
                (
                    result["query"],
                    json.dumps(result),
                    json.dumps(result.get("validation", {})),
                    (result.get("validation") or {}).get("count"),
                    db.utcnow(),
                    audience_id,
                ),
            )
        else:
            con.execute(
                "UPDATE audiences SET repair_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(result), db.utcnow(), audience_id),
            )
        con.commit()
    finally:
        con.close()
    return jsonify(result)


# ---------------------------------------------------------- batch validation
BATCH_VALIDATE_WORKERS = max(
    1, min(4, int(os.environ.get("TRI_CLAY_VALIDATE_WORKERS", "2") or 2))
)


def _batch_state_key(batch_id: int) -> str:
    return f"batch_validate_{batch_id}"


def _persist_validation(con, audience_id: int, result: dict) -> str:
    """Write one row's validation outcome; return the status written."""
    status = "valid" if result.get("ok") else "invalid"
    if result.get("ok") and result.get("count") == 0:
        status = "invalid"
        result["errors"] = ["preview count is 0 - nothing to build"]
        result["ok"] = False
    repaired = result.get("repaired_query")
    con.execute(
        "UPDATE audiences SET status = ?, lint_json = ?, api_json = ?, preview_count = ?, "
        "repaired_query = COALESCE(?, repaired_query), "
        "repair_json = COALESCE(?, repair_json), updated_at = ? WHERE id = ?",
        (
            status,
            json.dumps(
                {
                    "errors": [] if result.get("stage") != "lint" else result.get("errors", []),
                    "warnings": result.get("warnings", []),
                }
            ),
            json.dumps(result),
            result.get("count"),
            repaired,
            json.dumps(
                {
                    "source": result.get("source"),
                    "explanation": result.get("explanation"),
                    "fixes": result.get("fixes") or [],
                }
            )
            if repaired
            else None,
            db.utcnow(),
            audience_id,
        ),
    )
    con.commit()
    return status


def _batch_validate_worker(batch_id: int) -> None:
    """Validate every row in a batch with bounded concurrency + dedupe + auto-repair."""
    key = _batch_state_key(batch_id)
    con = db.connect()
    try:
        rows = con.execute(
            "SELECT id, query, edited_query, repaired_query FROM audiences WHERE batch_id = ? ORDER BY id",
            (batch_id,),
        ).fetchall()
        total = len(rows)
        state = db.kv_json(con, key, {}) or {}
        state.update({"running": True, "total": total, "done": 0, "results": state.get("results") or {}})
        db.kv_set(con, key, json.dumps(state))
        lock = threading.Lock()
        seen: dict[str, dict] = {}
        counts = {"valid": 0, "invalid": 0, "repaired": 0}

        def work(row) -> None:
            task_con = db.connect()
            try:
                audience = task_con.execute(
                    "SELECT * FROM audiences WHERE id = ?", (row["id"],)
                ).fetchone()
                query_key = clayops.effective_query(audience)
                with lock:
                    cached = seen.get(query_key)
                if cached is not None:
                    result = dict(cached)
                    result["warnings"] = list(result.get("warnings") or []) + [
                        "identical query already validated in this run"
                    ]
                else:
                    result = clayops.validate_and_repair(task_con, audience)
                    with lock:
                        seen[query_key] = dict(result)
                status = _persist_validation(task_con, row["id"], result)
                entry = {
                    "ok": bool(result.get("ok")),
                    "status": status,
                    "count": result.get("count"),
                    "errors": (result.get("errors") or [])[:3],
                    "warnings": (result.get("warnings") or [])[:3],
                    "repaired": bool(result.get("repaired_query")),
                    "explanation": result.get("explanation"),
                    "source": result.get("source"),
                }
                with lock:
                    counts[status if status in counts else "invalid"] += 1
                    if result.get("repaired_query"):
                        counts["repaired"] += 1
                    state = db.kv_json(task_con, key, {}) or {}
                    state["done"] = int(state.get("done", 0)) + 1
                    state.setdefault("results", {})[str(row["id"])] = entry
                    db.kv_set(task_con, key, json.dumps(state))
            except Exception as exc:  # noqa: BLE001 - one bad row must not kill the run
                with lock:
                    counts["invalid"] += 1
                    state = db.kv_json(task_con, key, {}) or {}
                    state["done"] = int(state.get("done", 0)) + 1
                    state.setdefault("results", {})[str(row["id"])] = {
                        "ok": False,
                        "status": "invalid",
                        "errors": [f"{type(exc).__name__}: {exc}"],
                    }
                    db.kv_set(task_con, key, json.dumps(state))
            finally:
                task_con.close()

        with ThreadPoolExecutor(max_workers=BATCH_VALIDATE_WORKERS) as pool:
            list(pool.map(work, rows))

        state = db.kv_json(con, key, {}) or {}
        state.update(
            {
                "running": False,
                "done": total,
                "summary": counts,
                "finished_at": db.utcnow(),
            }
        )
        db.kv_set(con, key, json.dumps(state))
    finally:
        con.close()


@app.post("/batches/<int:batch_id>/validate")
@login_required
def batch_validate(batch_id: int):
    con = conn()
    try:
        batch_row = con.execute("SELECT id FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if not batch_row:
            return jsonify({"ok": False, "errors": ["unknown batch"]}), 404
        key = _batch_state_key(batch_id)
        state = db.kv_json(con, key, {}) or {}
        if state.get("running"):
            return jsonify({"ok": True, "running": True, "state": state})
        total = con.execute(
            "SELECT COUNT(*) AS n FROM audiences WHERE batch_id = ?", (batch_id,)
        ).fetchone()["n"]
        if not total:
            return jsonify({"ok": False, "errors": ["no audiences in this batch"]}), 400
        state = {
            "running": True,
            "total": total,
            "done": 0,
            "results": {},
            "started_at": db.utcnow(),
            "workers": BATCH_VALIDATE_WORKERS,
        }
        db.kv_set(con, key, json.dumps(state))
    finally:
        con.close()
    threading.Thread(target=_batch_validate_worker, args=(batch_id,), daemon=True).start()
    return jsonify({"ok": True, "running": True, "state": state})


@app.get("/batches/<int:batch_id>/validate")
@login_required
def batch_validate_status(batch_id: int):
    con = conn()
    try:
        state = db.kv_json(con, _batch_state_key(batch_id), {}) or {}
    finally:
        con.close()
    return jsonify({"ok": True, **state})


@app.post("/audiences/<int:audience_id>/query")
@login_required
def audience_query(audience_id: int):
    payload = request.get_json(silent=True) or {}
    query = (payload.get("query") or "").strip()
    if not query:
        return jsonify({"ok": False, "errors": ["query is empty"]}), 400
    con = conn()
    try:
        con.execute(
            "UPDATE audiences SET query = ?, edited_query = NULL, repaired_query = NULL, status = 'new', updated_at = ? WHERE id = ?",
            (query, db.utcnow(), audience_id),
        )
        con.commit()
        row = con.execute("SELECT * FROM audiences WHERE id = ?", (audience_id,)).fetchone()
        result = clayops.validate_with_retry(con, row)
        status = "valid" if result.get("ok") and result.get("count") else "invalid"
        con.execute(
            "UPDATE audiences SET status = ?, api_json = ?, preview_count = ?, updated_at = ? WHERE id = ?",
            (status, json.dumps(result), result.get("count"), db.utcnow(), audience_id),
        )
        con.commit()
    finally:
        con.close()
    return jsonify(result)


@app.post("/batches/<int:batch_id>/queue")
@login_required
def batch_queue(batch_id: int):
    con = conn()
    try:
        paused, reason = clayops.queue_paused(con)
        if paused:
            flash(f"Queue is paused: {reason}. Reconnect Clay first.", "error")
            return redirect(url_for("batch", batch_id=batch_id))
        doctor_state = db.kv_json(con, "doctor_json") or {}
        if not doctor_state.get("ok"):
            flash("Clay connection is not verified. Open Connection and test it first.", "error")
            return redirect(url_for("batch", batch_id=batch_id))
        rows = con.execute(
            "SELECT * FROM audiences WHERE batch_id = ? AND status IN ('valid', 'repaired') ORDER BY id",
            (batch_id,),
        ).fetchall()
        if not rows:
            flash("No validated audiences to queue.", "error")
            return redirect(url_for("batch", batch_id=batch_id))
        queued = 0
        for row in rows:
            if con.execute(
                "SELECT 1 FROM jobs WHERE audience_row_id = ? AND status IN ('queued', 'running')",
                (row["id"],),
            ).fetchone():
                continue
            con.execute(
                "INSERT INTO jobs(audience_row_id, client_id, status, priority, created_at) VALUES(?, ?, 'queued', ?, ?)",
                (row["id"], row["client_id"], row["priority"] or 100, db.utcnow()),
            )
            con.execute(
                "UPDATE audiences SET status = 'queued', updated_at = ? WHERE id = ?",
                (db.utcnow(), row["id"]),
            )
            queued += 1
        con.commit()
    finally:
        con.close()
    flash(f"Queued {queued} job(s).", "ok" if queued else "warn")
    return redirect(url_for("queue_page"))


@app.get("/queue")
@login_required
def queue_page():
    con = conn()
    try:
        paused, reason = clayops.queue_paused(con)
    finally:
        con.close()
    return render_template("queue.html", paused=paused, pause_reason=reason)


@app.get("/jobs/<int:job_id>")
@login_required
def job_page(job_id: int):
    con = conn()
    try:
        job = con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            abort(404)
        payload = job_payload(con, job)
    finally:
        con.close()
    return render_template("job.html", job=payload)


@app.post("/jobs/<int:job_id>/cancel")
@login_required
def job_cancel(job_id: int):
    con = conn()
    try:
        job = con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            abort(404)
        if job["status"] == "queued":
            con.execute("UPDATE jobs SET status = 'canceled', finished_at = ? WHERE id = ?", (db.utcnow(), job_id))
            con.execute("UPDATE audiences SET status = 'valid' WHERE id = ?", (job["audience_row_id"],))
        elif job["status"] == "running":
            con.execute("UPDATE jobs SET status = 'canceling' WHERE id = ?", (job_id,))
        con.commit()
    finally:
        con.close()
    return redirect(url_for("job_page", job_id=job_id))


@app.post("/jobs/<int:job_id>/retry")
@login_required
def job_retry(job_id: int):
    con = conn()
    try:
        job = con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            abort(404)
        resume_of = job_id if job["run_dir"] and (Path(job["run_dir"]) / "checkpoint.json").exists() else None
        con.execute(
            "INSERT INTO jobs(audience_row_id, client_id, status, priority, created_at, resume_of) "
            "VALUES(?, ?, 'queued', ?, ?, ?)",
            (job["audience_row_id"], job["client_id"], job["priority"], db.utcnow(), resume_of),
        )
        con.execute(
            "UPDATE audiences SET status = 'queued', updated_at = ? WHERE id = ?",
            (db.utcnow(), job["audience_row_id"]),
        )
        con.commit()
    finally:
        con.close()
    flash("Job queued again." + (" Resuming from the existing checkpoint." if resume_of else ""), "ok")
    return redirect(url_for("queue_page"))


@app.get("/jobs/<int:job_id>/download")
@login_required
def job_download(job_id: int):
    rel = request.args.get("p") or ""
    con = conn()
    try:
        job = con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        con.close()
    if not job or not job["run_dir"] or not rel:
        abort(404)
    run_dir = Path(job["run_dir"]).resolve()
    target = (run_dir / rel).resolve()
    if not str(target).startswith(str(run_dir)) or not target.is_file():
        abort(404)
    return send_file(target, as_attachment=True, download_name=target.name)


@app.get("/api/jobs")
@login_required
def api_jobs():
    con = conn()
    try:
        rows = con.execute(
            "SELECT j.*, a.audience_name, a.audience_id, c.name AS client_name "
            "FROM jobs j JOIN audiences a ON a.id = j.audience_row_id "
            "JOIN clients c ON c.id = j.client_id ORDER BY j.id DESC LIMIT 150"
        ).fetchall()
    finally:
        con.close()
    return jsonify(
        [
            {
                "id": row["id"],
                "status": row["status"],
                "priority": row["priority"],
                "client": row["client_name"],
                "audience_name": row["audience_name"],
                "audience_id": row["audience_id"],
                "audience_row_id": row["audience_row_id"],
                "created_at": row["created_at"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "error": row["error"],
                "resume_of": row["resume_of"],
                "phase": (parse_json(row["progress_json"], {}) or {}).get("phase"),
            }
            for row in rows
        ]
    )


@app.get("/api/jobs/<int:job_id>")
@login_required
def api_job(job_id: int):
    con = conn()
    try:
        job = con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            abort(404)
        payload = job_payload(con, job)
        events = db.dicts(
            con.execute(
                "SELECT ts, kind, message FROM events WHERE job_id = ? ORDER BY id DESC LIMIT 40", (job_id,)
            )
        )
        events.reverse()
    finally:
        con.close()
    log_tail = ""
    if job["log_path"]:
        try:
            text = Path(job["log_path"]).read_text(encoding="utf-8", errors="replace")
            log_tail = "\n".join(text.splitlines()[-200:])
        except OSError:
            log_tail = ""
    payload["events"] = events
    payload["log_tail"] = log_tail
    return jsonify(payload)


@app.get("/users")
@admin_required
def users():
    con = conn()
    try:
        rows = db.dicts(con.execute("SELECT id, email, is_admin, created_at FROM users ORDER BY email"))
    finally:
        con.close()
    return render_template("users.html", users=rows)


@app.post("/users/save")
@admin_required
def users_save():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    is_admin_flag = 1 if request.form.get("is_admin") else 0
    if not email or not password:
        flash("Email and password are required.", "error")
        return redirect(url_for("users"))
    con = conn()
    try:
        con.execute(
            "INSERT INTO users(email, password_hash, is_admin, created_at) VALUES(?, ?, ?, ?)",
            (email, generate_password_hash(password), is_admin_flag, db.utcnow()),
        )
        con.commit()
        flash(f"User {email} created.", "ok")
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not create user: {exc}", "error")
    finally:
        con.close()
    return redirect(url_for("users"))


@app.post("/users/<int:user_id>/delete")
@admin_required
def users_delete(user_id: int):
    me = current_user()
    if me and me["id"] == user_id:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("users"))
    con = conn()
    try:
        con.execute("DELETE FROM users WHERE id = ?", (user_id,))
        con.commit()
    finally:
        con.close()
    return redirect(url_for("users"))


@app.get("/settings")
@admin_required
def settings_page():
    con = conn()
    try:
        paused, reason = clayops.queue_paused(con)
        doctor_state = db.kv_json(con, "doctor_json")
        model = clayops.llm_model(con)
        cookie_updated = clayops.cookie_updated_at(con)
    finally:
        con.close()
    return render_template(
        "settings.html",
        llm=clayops.llm_configured(),
        model=model,
        doctor=doctor_state,
        paused=paused,
        pause_reason=reason,
        cookie_updated=cookie_updated,
    )


@app.post("/settings/save")
@admin_required
def settings_save():
    model = (request.form.get("llm_model") or "").strip()
    con = conn()
    try:
        if model:
            db.kv_set(con, "llm_model", model)
    finally:
        con.close()
    flash("Settings saved.", "ok")
    return redirect(url_for("settings_page"))


@app.post("/settings/resume")
@admin_required
def settings_resume():
    con = conn()
    try:
        result = clayops.doctor(con)
    finally:
        con.close()
    if result.get("ok"):
        flash("Connection verified, queue resumed.", "ok")
    else:
        flash(f"Cannot resume: {result.get('error')}", "error")
    return redirect(url_for("settings_page"))
