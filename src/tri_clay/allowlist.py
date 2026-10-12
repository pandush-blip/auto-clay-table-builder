"""Endpoint and payload allowlist — the zero-credit guarantee.

Every build-layer request must pass `validate_request` before it is sent.
Anything not explicitly allowed raises, and field payloads are checked so only
free source/formula/text columns can be created.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlparse

from .errors import EndpointNotAllowed, PayloadNotAllowed

ALLOWED_DOWNLOAD_HOSTS = {"clay-base-export-prod.s3.us-east-1.amazonaws.com"}

# (method, path template) — placeholders match a single path segment.
ENDPOINT_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("POST", "/v3/workspaces/{ws}/workbooks"),
    ("PATCH", "/v3/workspaces/{ws}/workbooks/{wb}"),
    ("GET", "/v3/workspaces/{ws}/workbooks/{wb}"),
    ("POST", "/v3/workspaces/{ws}/search-dsl/query"),
    ("POST", "/v3/workspaces/{ws}/search-dsl/send-to-table"),
    ("GET", "/v3/workspaces/{ws}/tables/{t}/count"),
    ("GET", "/v3/workspaces/{ws}/tables/{t}/details"),
    ("GET", "/v3/workspaces/{ws}/tables/{t}/fields/runstatus"),
    ("GET", "/v3/workspaces/{ws}/tables/{t}/views/{v}/table-schema-v2"),
    ("POST", "/v3/workspaces/{ws}/tables/{t}/fields"),
    ("PATCH", "/v3/workspaces/{ws}/tables/{t}/fields/{f}"),
    ("POST", "/v3/workspaces/{ws}/tables/{t}/views/{v}/export"),
    ("GET", "/v3/workspaces/{ws}/exports/{ej}"),
    ("GET", "/v3/workspaces/{ws}/sources/{s}"),
    ("GET", "/v3/workspaces/{ws}/sources/{s}/runs"),
    ("GET", "/v3/workspaces/{ws}/peopleSearchLimit"),
)

FREE_FIELD_TYPES = {"formula", "text"}
FREE_DATA_TYPES = {"text", "url"}
FORBIDDEN_PAYLOAD_MARKERS = (
    "actionkey",
    "actionversion",
    "actionpackageid",
    "enrichment",
    "claygent",
    "waterfall",
    "ai_",
    "use-ai",
    "useai",
    "http-api",
    "smartlead",
    "hubspot",
    "slack",
)


@dataclass(frozen=True)
class Route:
    method: str
    template: str
    regex: re.Pattern[str]


def _compile(template: str) -> re.Pattern[str]:
    pattern = re.escape(template)
    pattern = re.sub(r"\\\{[a-z]+\\\}", r"[^/]+", pattern)
    return re.compile("^" + pattern + "$")


ROUTES: tuple[Route, ...] = tuple(
    Route(method, template, _compile(template)) for method, template in ENDPOINT_TEMPLATES
)


def match_route(method: str, path: str) -> Route | None:
    for route in ROUTES:
        if route.method == method.upper() and route.regex.match(path):
            return route
    return None


def assert_endpoint(method: str, path: str) -> Route:
    path_only = path.split("?", 1)[0]
    route = match_route(method, path_only)
    if route is None:
        raise EndpointNotAllowed(
            f"{method.upper()} {path} is not on the build-layer allowlist. "
            "Add it to src/tri_clay/allowlist.py only with a documented reason in docs/decisions.md."
        )
    return route


def assert_download_url(url: str) -> None:
    host = urlparse(url).netloc.lower()
    if host not in ALLOWED_DOWNLOAD_HOSTS:
        raise EndpointNotAllowed(f"download host {host!r} is not allowed")


def _walk_keys(node: Any) -> Iterable[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key)
            yield from _walk_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_keys(item)


def assert_no_paid_markers(payload: Any, where: str) -> None:
    lowered = {key.lower() for key in _walk_keys(payload)}
    for marker in FORBIDDEN_PAYLOAD_MARKERS:
        if any(marker in key for key in lowered):
            raise PayloadNotAllowed(
                f"{where}: payload contains credit-risk marker {marker!r}. "
                "Only free source/formula/text columns are allowed."
            )


def assert_field_payload(payload: dict[str, Any], *, patch: bool = False) -> None:
    allowed_keys = {"name", "type", "typeSettings", "activeViewId", "attributionData", "options"}
    if patch:
        allowed_keys = {"name", "type", "typeSettings"}
    extra = set(payload) - allowed_keys
    if extra:
        raise PayloadNotAllowed(f"field payload has unexpected keys: {sorted(extra)}")
    if "type" in payload:
        field_type = payload["type"]
        if field_type not in FREE_FIELD_TYPES:
            raise PayloadNotAllowed(
                f"field type {field_type!r} is not free (allowed: {sorted(FREE_FIELD_TYPES)})"
            )
    if payload.get("type") == "formula" or "typeSettings" in payload and payload.get("type") is None:
        type_settings = payload.get("typeSettings") or {}
        data_type = ((type_settings.get("dataTypeSettings") or {}).get("type"))
        if data_type not in FREE_DATA_TYPES:
            raise PayloadNotAllowed(
                f"formula data type {data_type!r} is not free (allowed: {sorted(FREE_DATA_TYPES)})"
            )
        formula_type = type_settings.get("formulaType")
        if formula_type != "text":
            raise PayloadNotAllowed(f"formulaType {formula_type!r} is not free")
    assert_no_paid_markers(payload, "field payload")


PAYLOAD_RULES: dict[str, dict[str, set[str]]] = {
    "POST /v3/workspaces/{ws}/workbooks": {
        "required": {"name", "workspaceId"},
        "optional": {"parentFolderId"},
    },
    "PATCH /v3/workspaces/{ws}/workbooks/{wb}": {
        "required": set(),
        "optional": {"name", "parentFolderId"},
    },
    "POST /v3/workspaces/{ws}/search-dsl/query": {
        "required": {"query", "workspaceId"},
        "optional": {"selectMode", "userQueryHasLimit", "conversationId"},
    },
    "POST /v3/workspaces/{ws}/search-dsl/send-to-table": {
        "required": {"dslQuery", "workspaceId", "workbookId"},
        "optional": {
            "resolvedResourceValues",
            "previewEntityIds",
            "conversationId",
            "tableName",
        },
    },
    "POST /v3/workspaces/{ws}/tables/{t}/views/{v}/export": {"required": set(), "optional": set()},
}


def assert_payload(route: Route, payload: Any) -> None:
    key = f"{route.method} {route.template}"
    if route.method == "POST" and route.template.endswith("/fields"):
        if not isinstance(payload, dict):
            raise PayloadNotAllowed("field payload must be an object")
        assert_field_payload(payload)
        return
    if route.method == "PATCH" and "/fields/" in route.template:
        if not isinstance(payload, dict):
            raise PayloadNotAllowed("field patch payload must be an object")
        assert_field_payload(payload, patch=True)
        return
    rule = PAYLOAD_RULES.get(key)
    if rule is None:
        if payload not in (None, {}):
            raise PayloadNotAllowed(f"{key} does not accept a payload")
        return
    if not isinstance(payload, dict):
        raise PayloadNotAllowed(f"{key} payload must be an object")
    keys = set(payload)
    missing = rule["required"] - keys
    if missing:
        raise PayloadNotAllowed(f"{key} is missing required keys: {sorted(missing)}")
    unexpected = keys - rule["required"] - rule["optional"]
    if unexpected:
        raise PayloadNotAllowed(f"{key} has unexpected keys: {sorted(unexpected)}")
    if route.template.endswith("/export") and payload:
        raise PayloadNotAllowed("export payload must be empty")


def validate_request(method: str, path: str, payload: Any = None) -> Route:
    route = assert_endpoint(method, path)
    assert_payload(route, payload)
    return route


SCHEMA_FORBIDDEN_TYPES = {"action", "enrichment", "claygent", "http", "ai", "waterfall"}


def assert_free_schema(fields: Iterable[dict[str, Any]]) -> None:
    """Check a table readback only contains free field types."""
    for field in fields:
        field_type = (field.get("type") or "").lower()
        if field_type in SCHEMA_FORBIDDEN_TYPES:
            raise PayloadNotAllowed(
                f"table contains a non-free column {field.get('name')!r} of type {field_type!r}"
            )
