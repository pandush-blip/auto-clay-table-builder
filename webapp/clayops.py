"""Clay integration for the web app: connection checks, validation, repair."""

from __future__ import annotations

import csv
import difflib
import io
import json
import os
import random
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx

from tri_clay.config import Settings, parse_env_file
from tri_clay.credits import CreditTripwire
from tri_clay.errors import SessionExpiredError, TriClayError
from tri_clay.lint import lint_query
from tri_clay.official_api import OfficialApiClient
from tri_clay.reference import Reference
from tri_clay.samples import summarize_search_results
from tri_clay.webapp_client import WebAppClient

from . import crypto, db

REPO_ROOT = Path(__file__).resolve().parent.parent
REFERENCE_PATH = REPO_ROOT / "docs" / "clay-query-reference.md"
TMP_DIR = Path(__file__).resolve().parent / "data" / "tmp"
DEFAULT_LLM_MODEL = "gpt-4o-mini"


# --------------------------------------------------------------------- helpers
def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return slug or "audience"


def env_default(name: str, fallback: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value else fallback


def secret_key() -> str:
    return env_default("SECRET_KEY", "dev-secret-change-me") or "dev-secret-change-me"


def llm_configured() -> bool:
    return bool(env_default("OPENAI_API_KEY"))


def llm_model(con: sqlite3.Connection) -> str:
    return db.kv_get(con, "llm_model") or env_default("OPENAI_MODEL", DEFAULT_LLM_MODEL) or DEFAULT_LLM_MODEL


def get_cookie(con: sqlite3.Connection) -> str | None:
    token = db.kv_get(con, "clay_cookie_enc")
    return crypto.decrypt(secret_key(), token)


def set_cookie(con: sqlite3.Connection, cookie: str) -> None:
    db.kv_set(con, "clay_cookie_enc", crypto.encrypt(secret_key(), cookie))
    db.kv_set(con, "clay_cookie_updated", db.utcnow())


def seed_cookie_from_env(con: sqlite3.Connection) -> None:
    """First boot: adopt CLAY_SESSION_COOKIE from webapp/.env if the DB has none."""
    if get_cookie(con):
        return
    cookie = env_default("CLAY_SESSION_COOKIE")
    if cookie:
        set_cookie(con, cookie)


def cookie_updated_at(con: sqlite3.Connection) -> str | None:
    return db.kv_get(con, "clay_cookie_updated")


def _tmp_env_path() -> Path:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    path = TMP_DIR / "session.env"
    if not path.exists():
        path.write_text("", encoding="utf-8")
    return path


def read_env_cookie(path: Path) -> str | None:
    values = parse_env_file(path)
    return values.get("CLAY_SESSION_COOKIE")


def build_settings(
    cookie: str | None,
    *,
    folder_id: str | None = None,
    env_path: Path | None = None,
    pace_seconds: float | None = None,
) -> Settings:
    api_key = env_default("CLAY_API_KEY")
    workspace_id = env_default("CLAY_WORKSPACE_ID")
    if not api_key or not workspace_id:
        raise TriClayError("CLAY_API_KEY / CLAY_WORKSPACE_ID are not configured on the server")
    pace = pace_seconds
    if pace is None:
        try:
            pace = float(env_default("TRI_CLAY_PACE_SECONDS", "3") or 3)
        except ValueError:
            pace = 3.0
    try:
        max_tables = int(env_default("TRI_CLAY_MAX_TABLES_PER_HOUR", "30") or 30)
    except ValueError:
        max_tables = 30
    return Settings(
        api_key=api_key,
        workspace_id=str(workspace_id),
        api_base=env_default("CLAY_API_BASE", "https://api.clay.com/public/v0") or "",
        webapp_base=env_default("CLAY_WEBAPP_BASE", "https://api.clay.com") or "",
        session_cookie=cookie,
        webapp_token=env_default("CLAY_WEBAPP_TOKEN"),
        target_folder_id=(folder_id or env_default("CLAY_TARGET_FOLDER_ID") or None),
        pace_seconds=pace,
        max_tables_per_hour=max_tables,
        env_path=env_path or _tmp_env_path(),
    )


def _persist_rotated_cookie(con: sqlite3.Connection, env_path: Path) -> None:
    rotated = read_env_cookie(env_path)
    if rotated and rotated != get_cookie(con):
        set_cookie(con, rotated)


# ------------------------------------------------------------------- doctor
def doctor(con: sqlite3.Connection, *, store: bool = True) -> dict[str, Any]:
    cookie = get_cookie(con)
    result: dict[str, Any] = {
        "ok": False,
        "checked_at": db.utcnow(),
        "cookie_updated_at": cookie_updated_at(con),
        "error": None,
    }
    if not cookie:
        result["error"] = "No Clay session connected yet."
        if store:
            db.kv_set(con, "doctor_json", json.dumps(result))
        return result

    env_path = _tmp_env_path()
    try:
        settings = build_settings(cookie, env_path=env_path)
    except TriClayError as exc:
        result["error"] = str(exc)
        if store:
            db.kv_set(con, "doctor_json", json.dumps(result))
        return result

    try:
        with OfficialApiClient(settings.api_key, settings.api_base) as api:
            me = api.get_me()
            balance = api.get_balance()
        result.update(
            {
                "workspace_id": str((me.get("workspace") or {}).get("id") or settings.workspace_id),
                "workspace_name": (me.get("workspace") or {}).get("name"),
                "user_id": (me.get("user") or {}).get("id"),
                "credits": balance.balance,
                "action_credits": balance.action_execution_balance,
            }
        )
    except TriClayError as exc:
        result["error"] = f"Official API rejected the key: {exc}"
        if store:
            db.kv_set(con, "doctor_json", json.dumps(result))
        return result

    try:
        with WebAppClient(settings) as client:
            limit = client.people_search_limit()
        result["row_cap"] = limit
        result["ok"] = True
        _persist_rotated_cookie(con, env_path)
    except SessionExpiredError:
        result["error"] = "Clay web-app session expired. Reconnect with a fresh cookie."
    except TriClayError as exc:
        result["error"] = f"Web-app session check failed: {exc}"

    if result["ok"]:
        db.kv_set(con, "queue_paused", "0")
        db.kv_set(con, "queue_paused_reason", "")
    if store:
        db.kv_set(con, "doctor_json", json.dumps(result))
    return result


def set_queue_paused(con: sqlite3.Connection, reason: str) -> None:
    db.kv_set(con, "queue_paused", "1")
    db.kv_set(con, "queue_paused_reason", reason)


def queue_paused(con: sqlite3.Connection) -> tuple[bool, str]:
    paused = db.kv_get(con, "queue_paused", "0") == "1"
    return paused, db.kv_get(con, "queue_paused_reason", "") or ""


# ------------------------------------------------------------------ validation
PREVIEW_CACHE_KEY = "preview_cache_v1"
PREVIEW_CACHE_TTL = float(os.environ.get("TRI_CLAY_PREVIEW_CACHE_TTL", "900") or 900)

TRANSIENT_ERROR = re.compile(
    r"\b429\b|too many concurrent|too many requests|rate limit|"
    r"timed out|timeout|gateway time-out|\b50[234]\b|temporarily unavailable",
    re.I,
)


def is_transient(result: dict[str, Any]) -> bool:
    """True when the failure is Clay being busy - retry, never mark invalid."""
    return any(
        TRANSIENT_ERROR.search(str(error)) for error in (result.get("errors") or [])
    )


def _cached_preview(con: sqlite3.Connection, query: str) -> int | None:
    cache = db.kv_json(con, PREVIEW_CACHE_KEY, {}) or {}
    item = cache.get(query)
    if not isinstance(item, dict):
        return None
    try:
        age = time.time() - float(item.get("at") or 0)
    except (TypeError, ValueError):
        return None
    if age > PREVIEW_CACHE_TTL:
        return None
    count = item.get("count")
    return int(count) if count is not None else None


def _store_preview(con: sqlite3.Connection, query: str, count: int) -> None:
    cache = db.kv_json(con, PREVIEW_CACHE_KEY, {}) or {}
    cache[query] = {"count": int(count), "at": time.time()}
    if len(cache) > 500:
        newest = sorted(cache.items(), key=lambda kv: kv[1].get("at", 0), reverse=True)[:400]
        cache = dict(newest)
    db.kv_set(con, PREVIEW_CACHE_KEY, json.dumps(cache))


def _reference() -> Reference:
    return Reference.load(REFERENCE_PATH)


def effective_query(audience: sqlite3.Row | dict[str, Any]) -> str:
    """The query the build will actually run: manual edit first, then auto-repair, then import.

    Everything that validates, displays or builds a query must use this same precedence,
    otherwise a query can validate green and still build the broken original (or vice versa).
    """

    def field(name: str) -> str:
        try:
            return audience[name] or ""
        except (IndexError, KeyError):
            return ""

    return (field("edited_query") or field("repaired_query") or field("query")).strip()


def validate_audience(
    con: sqlite3.Connection,
    audience: sqlite3.Row | dict[str, Any],
    *,
    query: str | None = None,
) -> dict[str, Any]:
    """Lint + official-API sample + web-app preview count. Free (check layer)."""
    q = (query if query is not None else effective_query(audience)).strip()
    warnings: list[str] = []
    if re.search(r"\blimit\s+\d+(?!\s+by\b)", q, re.I):
        warnings.append(
            "query contains a `limit` clause - remove it or the build will only import that many rows"
        )
    try:
        lint = lint_query(q, _reference())
    except Exception as exc:  # noqa: BLE001 - reference problems must be visible
        return {"ok": False, "stage": "lint", "errors": [f"lint failed: {exc}"], "warnings": warnings}
    warnings.extend(lint.warnings)
    if not lint.ok:
        return {"ok": False, "stage": "lint", "errors": lint.errors, "warnings": warnings}

    cached_count = _cached_preview(con, q)
    cookie = get_cookie(con)
    if not cookie:
        return {"ok": False, "stage": "session", "errors": ["Clay session not connected"], "warnings": warnings}
    env_path = _tmp_env_path()
    try:
        settings = build_settings(cookie, env_path=env_path)
        with OfficialApiClient(settings.api_key, settings.api_base) as api:
            tripwire = CreditTripwire(api)
            tripwire.start()
            search = api.create_search(q)
            payload = api.run_search_page(search["search_id"], 25)
            if cached_count is None:
                with WebAppClient(settings) as web:
                    count = int(web.preview(q)["totalCount"])
                _store_preview(con, q, count)
            else:
                count = cached_count
                warnings.append("preview count served from cache (fresh result)")
            tripwire.check()
        summary = summarize_search_results(payload)
        _persist_rotated_cookie(con, env_path)
    except SessionExpiredError as exc:
        set_queue_paused(con, "Clay session expired during validation")
        return {"ok": False, "stage": "session", "errors": [str(exc)], "warnings": warnings}
    except TriClayError as exc:
        return {"ok": False, "stage": "api", "errors": [str(exc)], "warnings": warnings}

    return {
        "ok": True,
        "stage": "done",
        "errors": [],
        "warnings": warnings,
        "count": count,
        "sample_records": summary["records"],
        "linkedin_present": summary["linkedin_present"],
        "top_titles": [f"{t} x{c}" for t, c in summary["top_titles"][:5]],
        "top_companies": [f"{c} x{n}" for c, n in summary["top_companies"][:5]],
        "period_quota": summary.get("period_quota"),
    }


def validate_with_retry(
    con: sqlite3.Connection,
    audience: sqlite3.Row | dict[str, Any],
    *,
    query: str | None = None,
    attempts: int = 5,
    base_delay: float | None = None,
) -> dict[str, Any]:
    """Validate, retrying transient Clay failures (429 concurrency, 5xx, timeouts).

    A row is never marked invalid just because Clay's workspace was busy: it backs off
    (exponential, jittered, capped at 60s) and tries again.
    """
    if base_delay is None:
        try:
            base_delay = float(env_default("TRI_CLAY_VALIDATE_RETRY_BASE", "4") or 4)
        except ValueError:
            base_delay = 4.0
    result = validate_audience(con, audience, query=query)
    tries = 0
    while not result.get("ok") and is_transient(result) and tries < attempts:
        tries += 1
        wait = min(base_delay * (2 ** (tries - 1)), 60) * random.uniform(0.8, 1.2)
        time.sleep(wait)
        result = validate_audience(con, audience, query=query)
    if tries:
        result.setdefault("warnings", []).append(
            f"Clay was busy - retried {tries}x with backoff before this result"
        )
        result["retries"] = tries
    return result


# ---------------------------------------------------------------------- repair
API_VALUE_ERROR = re.compile(
    r'Invalid value "?(?P<bad>[^"\n]+?)"? for field \'(?P<field>[^\']+)\'\.\s*Use one of:\s*(?P<options>.+?)(?:\n|$)',
    re.S,
)


def parse_value_error(error: str) -> tuple[str, str, list[str]] | None:
    match = API_VALUE_ERROR.search(error or "")
    if not match:
        return None
    options = re.findall(r'"([^"]+)"', match.group("options"))
    if not options:
        options = [part.strip() for part in match.group("options").split(",") if part.strip()]
    return match.group("bad"), match.group("field"), options


def rule_repair(query: str, error: str) -> dict[str, Any] | None:
    parsed = parse_value_error(error)
    if not parsed:
        return None
    bad, field, options = parsed
    if not options:
        return None
    good = None
    for option in options:
        if option.lower() == bad.lower():
            good = option
            break
    if not good:
        # High cutoff only: typo-level fixes go through the rule; anything fuzzier is a
        # semantic guess and must go to AI + validation instead (e.g. "Computer Software"
        # must never silently become "Computer Hardware").
        matches = difflib.get_close_matches(bad, options, n=1, cutoff=0.85)
        if not matches:
            return None
        good = matches[0]
    fixed = re.sub(r'"\s*' + re.escape(bad) + r'\s*"', f'"{good}"', query)
    if fixed == query:
        fixed = re.sub(r'(?<![\w"])' + re.escape(bad) + r'(?![\w"])', good, query, count=1)
    if fixed == query:
        return None
    return {
        "query": fixed,
        "source": "rule",
        "explanation": f"{field}: replaced {bad!r} with {good!r} from the API's valid values.",
    }


LINT_INDUSTRY_ERROR = re.compile(
    r"invalid industry\s+(?P<quote>['\"])(?P<bad>.+?)(?P=quote)"
    r"(?:\s*-\s*did you mean\s+(?P<suggestions>.+?)\?)?",
    re.I | re.S,
)
UNKNOWN_FIELD_ERROR = re.compile(
    r"Unknown field\s+['\"](?P<field>[^'\"]+)['\"]\s+for entity\s+['\"](?P<entity>[^'\"]+)['\"]",
    re.I,
)


def lint_repair(query: str, error: str) -> dict[str, Any] | None:
    """Apply the lint's own 'did you mean' suggestion (punctuation/case typos)."""
    match = LINT_INDUSTRY_ERROR.search(error or "")
    if not match:
        return None
    bad = match.group("bad").strip()
    suggestions = re.findall(r"['\"]([^'\"]+)['\"]", match.group("suggestions") or "")
    good: str | None = None
    if suggestions:
        best = suggestions[0]
        ratio = difflib.SequenceMatcher(None, bad.lower(), best.lower()).ratio()
        if ratio >= 0.7:
            good = best
    if good is None:
        try:
            matches = difflib.get_close_matches(
                bad, list(_reference().industry_values), n=1, cutoff=0.72
            )
        except Exception:  # noqa: BLE001
            matches = []
        if matches:
            good = matches[0]
    if not good:
        return None
    fixed = query.replace(f'"{bad}"', f'"{good}"').replace(f"'{bad}'", f"'{good}'")
    if fixed == query:
        fixed = re.sub(
            r'(?<![\w"])' + re.escape(bad) + r'(?![\w"])', good, query, count=1
        )
    if fixed == query:
        return None
    return {
        "query": fixed,
        "source": "rule",
        "explanation": f"industry: replaced {bad!r} with {good!r} from the reference list.",
    }


def unknown_field_repair(query: str, error: str) -> dict[str, Any] | None:
    """Last resort: drop the predicate that uses a field Clay does not know."""
    match = UNKNOWN_FIELD_ERROR.search(error or "")
    if not match:
        return None
    field = match.group("field")
    predicate = re.compile(
        r"(?P<lead>\b(?:and|or)\b)?\s*[\w.]*\b"
        + re.escape(field)
        + r"\b\s*(?:=|!=|<=|>=|<|>|in\b|not_in\b|contains\b|is_null\b|is_not_null\b)"
        r"[^()\n]*",
        re.I,
    )
    fixed, count = predicate.subn("", query, count=1)
    if not count:
        return None
    # Tidy the leftovers of removing a predicate.
    fixed = re.sub(r"\(\s*(?:and|or)\b", "(", fixed, flags=re.I)
    fixed = re.sub(r"\b(?:and|or)\s*(?=\))", "", fixed, flags=re.I)
    fixed = re.sub(r"\b(and|or)\s+\1\b", r"\1", fixed, flags=re.I)
    fixed = re.sub(r"[ \t]{2,}", " ", fixed).strip()
    if fixed == query:
        return None
    return {
        "query": fixed,
        "source": "rule",
        "explanation": (
            f"removed the unsupported filter {field!r} ({match.group('entity')} entity) - "
            "review the query to confirm the targeting is still what you want."
        ),
    }


SYSTEM_PROMPT = """You repair Clay people-search DSL queries for an internal tool.

You receive: the current query, the exact error returned by Clay's API, and a reference excerpt.
Return strict JSON only: {"fixed_query": "...", "explanation": "..."}.

Rules:
- Keep the query's intent, structure and fields. Change as little as possible.
- Only use fields and enum values that appear in the error message or the reference excerpt.
- Never invent filters, fields or values.
- If you cannot fix it with certainty, return the original query and explain what is unclear.
"""


def _reference_excerpt() -> str:
    text = REFERENCE_PATH.read_text(encoding="utf-8")
    excerpt = text[:18000]
    try:
        industries = sorted(_reference().industry_values)
        excerpt += "\n\nVALID company.industry VALUES:\n" + json.dumps(industries)
    except Exception:  # noqa: BLE001
        pass
    return excerpt


def ai_repair(con: sqlite3.Connection, query: str, error: str, history: list[str] | None = None) -> dict[str, Any] | None:
    api_key = env_default("OPENAI_API_KEY")
    if not api_key:
        return None
    model = llm_model(con)
    user_payload = {
        "query": query,
        "error": (error or "")[:2500],
        "previous_attempt_errors": history or [],
        "reference_excerpt": _reference_excerpt(),
    }
    try:
        response = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(user_payload)},
                ],
            },
            timeout=90,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
        fixed = (data.get("fixed_query") or "").strip()
        if not fixed or fixed == query:
            return None
        return {
            "query": fixed,
            "source": "ai",
            "explanation": str(data.get("explanation") or "AI repair")[:600],
            "model": model,
        }
    except Exception as exc:  # noqa: BLE001 - AI failure must never break the flow
        return {"error": f"AI repair failed: {exc}"}


def attempt_repair(
    con: sqlite3.Connection,
    audience: sqlite3.Row | dict[str, Any],
    *,
    query: str | None = None,
    error: str | None = None,
    history: list[str] | None = None,
) -> dict[str, Any]:
    """Try rule-based then AI repair; every candidate must pass full validation."""
    current = (query if query is not None else effective_query(audience)).strip()
    if error is None:
        api_json = audience["api_json"] or "{}"
        try:
            stored = json.loads(api_json)
        except json.JSONDecodeError:
            stored = {}
        error = " ".join(stored.get("errors") or [])
        if not error:
            lint = validate_with_retry(con, audience)
            stored = lint
            error = " ".join(lint.get("errors") or [])
    if not error:
        return {"ok": False, "message": "No recorded error to repair - validate the query first."}

    attempts: list[dict[str, Any]] = []
    for candidate in (
        rule_repair(current, error),
        lint_repair(current, error),
        ai_repair(con, current, error, history=history),
    ):
        if candidate and candidate.get("query"):
            attempts.append(candidate)
    # A dropped predicate changes targeting, so it is only tried after AI (which keeps
    # the intent when it can) and only when nothing else worked.
    dropped = unknown_field_repair(current, error)
    if dropped:
        attempts.append(dropped)

    failed_history: list[str] = list(history or [])
    for candidate in attempts:
        outcome = validate_with_retry(con, audience, query=candidate["query"])
        if outcome.get("ok"):
            return {"ok": True, **candidate, "validation": outcome}
        failed_history.append(" ".join(outcome.get("errors") or []))

    if llm_configured():
        for _ in range(2):
            ai = ai_repair(con, current, error, history=failed_history)
            if not ai:
                break
            if ai.get("error"):
                return {"ok": False, "message": ai["error"]}
            outcome = validate_with_retry(con, audience, query=ai["query"])
            if outcome.get("ok"):
                return {"ok": True, **ai, "validation": outcome}
            failed_history.append(" ".join(outcome.get("errors") or []))

    return {
        "ok": False,
        "message": "Could not repair automatically. Edit the query manually.",
        "attempts": attempts,
        "errors": failed_history[-1:] or [error],
    }


def validate_and_repair(
    con: sqlite3.Connection,
    audience: sqlite3.Row | dict[str, Any],
    *,
    max_rounds: int = 3,
) -> dict[str, Any]:
    """Validate; on a fixable error auto-repair and re-validate, until valid or stuck.

    Returns the same shape as `validate_audience`, plus `repaired_query`, `source`,
    `explanation` and `fixes` when a repair was applied.
    """

    def field(name: str) -> str:
        try:
            return audience[name] or ""
        except (IndexError, KeyError):
            return ""

    current = (field("edited_query") or field("query")).strip() or effective_query(audience)
    result = validate_with_retry(con, audience, query=current)
    if result.get("ok"):
        return result
    fixes: list[dict[str, Any]] = []
    problem_history: list[str] = []
    for _ in range(max_rounds):
        error = " ".join(result.get("errors") or [])
        if not error or is_transient(result):
            break
        problem_history.append(error)
        fix = attempt_repair(
            con, audience, query=current, error=error, history=problem_history[:-1]
        )
        if not fix.get("ok"):
            fixes.append({"ok": False, "message": fix.get("message")})
            break
        current = fix["query"]
        result = validate_with_retry(con, audience, query=current)
        fixes.append(
            {
                "ok": bool(result.get("ok")),
                "source": fix.get("source"),
                "explanation": fix.get("explanation"),
            }
        )
        if result.get("ok"):
            result.update(
                {
                    "repaired_query": current,
                    "source": fix.get("source"),
                    "explanation": fix.get("explanation"),
                    "fixes": fixes,
                }
            )
            return result
    if fixes:
        result["fixes"] = fixes
    return result


# ------------------------------------------------------------------ CSV input
QUERY_HEADERS = {
    "clay query draft", "clay query", "query", "search query", "clay search query",
    "clay search people query", "clay search people queries", "clay people search query",
    "people search query", "clay people search queries",
}
NAME_HEADERS = {"audience name", "audience", "list name", "list", "segment name", "segment", "name"}
ID_HEADERS = {"audience id", "id"}
CLIENT_HEADERS = {"client", "client name"}
TARGET_HEADERS = {"target people", "target", "max rows", "target rows"}
TIER_HEADERS = {"tier"}
PRIORITY_HEADERS = {"priority"}
DEDUPE_HEADERS = {"dedupe scope"}
CAP_HEADERS = {"max contacts per company", "contact cap", "contacts per company"}


def _norm_header(value: str) -> str:
    return re.sub(r"[\s_]+", " ", (value or "").strip().lower())


def parse_audience_csv(content: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], ["CSV has no header row"]
    mapping: dict[str, str] = {}
    for header in reader.fieldnames:
        norm = _norm_header(header)
        for field, options in (
            ("query", QUERY_HEADERS),
            ("audience_name", NAME_HEADERS),
            ("audience_id", ID_HEADERS),
            ("client", CLIENT_HEADERS),
            ("target_people", TARGET_HEADERS),
            ("tier", TIER_HEADERS),
            ("priority", PRIORITY_HEADERS),
            ("dedupe_scope", DEDUPE_HEADERS),
            ("max_contacts_per_company", CAP_HEADERS),
        ):
            if field not in mapping and norm in options:
                mapping[field] = header
    warnings: list[str] = []
    if "query" not in mapping:
        return [], [
            "No query column found. Name it `clay_query_draft` (or `clay search people queries` / `query`)."
        ]
    if "audience_name" not in mapping:
        warnings.append("No audience name column found - using row numbers as names.")

    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(reader, start=1):
        query = (raw.get(mapping.get("query", "")) or "").strip()
        if not query:
            continue
        name = (raw.get(mapping.get("audience_name", "")) or "").strip() if "audience_name" in mapping else ""
        row = {
            "row": index,
            "query": query,
            "audience_name": name or f"Audience {index}",
            "audience_id": (raw.get(mapping["audience_id"]) or "").strip() if "audience_id" in mapping else "",
            "client": (raw.get(mapping["client"]) or "").strip() if "client" in mapping else "",
        }
        for field in ("target_people", "priority", "max_contacts_per_company"):
            if field in mapping:
                raw_value = (raw.get(mapping[field]) or "").strip().replace(",", "")
                if raw_value.isdigit():
                    row[field] = int(raw_value)
        if "tier" in mapping:
            row["tier"] = (raw.get(mapping["tier"]) or "").strip()
        if "dedupe_scope" in mapping:
            scope = (raw.get(mapping["dedupe_scope"]) or "").strip().lower()
            if scope in ("audience", "client"):
                row["dedupe_scope"] = scope
        rows.append(row)
    if not rows:
        warnings.append("No non-empty query rows found.")
    return rows, warnings
