from __future__ import annotations

import pytest

from tri_clay.allowlist import (
    EndpointNotAllowed,
    PayloadNotAllowed,
    assert_download_url,
    assert_free_schema,
    validate_request,
)


def test_allowed_read_endpoint_passes():
    validate_request("GET", "/v3/workspaces/4242424/tables/t_1/count")
    validate_request("GET", "/v3/workspaces/4242424/sources/s_1/runs")


def test_ai_chat_endpoint_is_not_allowed():
    with pytest.raises(EndpointNotAllowed):
        validate_request("POST", "/v3/workspaces/4242424/ai-generation/chat-conversation", {})


def test_wrong_method_rejected():
    with pytest.raises(EndpointNotAllowed):
        validate_request("DELETE", "/v3/workspaces/4242424/tables/t_1/fields/f_1")


def test_download_host_allowlist():
    assert_download_url("https://clay-base-export-prod.s3.us-east-1.amazonaws.com/x?y=1")
    with pytest.raises(EndpointNotAllowed):
        assert_download_url("https://evil.example.com/x")


def test_action_field_is_rejected():
    payload = {
        "type": "action",
        "name": "Find email",
        "typeSettings": {"actionKey": "email-finder"},
    }
    with pytest.raises(PayloadNotAllowed):
        validate_request("POST", "/v3/workspaces/1/tables/t_1/fields", payload)


def test_formula_with_action_marker_is_rejected():
    payload = {
        "type": "formula",
        "name": "X",
        "typeSettings": {
            "dataTypeSettings": {"type": "text"},
            "formulaType": "text",
            "actionKey": "claygent-v2",
        },
    }
    with pytest.raises(PayloadNotAllowed):
        validate_request("POST", "/v3/workspaces/1/tables/t_1/fields", payload)


def test_domain_formula_passes():
    payload = {
        "type": "formula",
        "name": "Domain",
        "typeSettings": {
            "dataTypeSettings": {"type": "url"},
            "formula": "{{f_people_search}}?.matched_experience?.company?.domain",
            "formulaText": "{{f_people_search}}?.matched_experience?.company?.domain",
            "mappedResultPath": ["matched_experience", "company", "domain"],
            "formulaType": "text",
        },
        "activeViewId": "gv_1",
        "attributionData": {"created_from": "object_mapper"},
    }
    validate_request("POST", "/v3/workspaces/1/tables/t_1/fields", payload)


def test_list_name_single_post_passes():
    payload = {
        "type": "formula",
        "name": "List Name",
        "typeSettings": {
            "dataTypeSettings": {"type": "text"},
            "formulaType": "text",
            "formula": '"My list"',
            "formulaText": '"My list"',
        },
        "activeViewId": "gv_1",
    }
    validate_request("POST", "/v3/workspaces/1/tables/t_1/fields", payload)


def test_patch_name_only_passes():
    validate_request("PATCH", "/v3/workspaces/1/tables/t_1/fields/f_1", {"name": "List Name"})


def test_patch_cannot_smuggle_action():
    payload = {"type": "action", "typeSettings": {"actionKey": "x"}}
    with pytest.raises(PayloadNotAllowed):
        validate_request("PATCH", "/v3/workspaces/1/tables/t_1/fields/f_1", payload)


def test_export_payload_must_be_empty():
    validate_request("POST", "/v3/workspaces/1/tables/t_1/views/v_1/export", {})
    with pytest.raises(PayloadNotAllowed):
        validate_request("POST", "/v3/workspaces/1/tables/t_1/views/v_1/export", {"format": "csv"})


def test_send_to_table_rejects_unexpected_keys():
    payload = {
        "dslQuery": "select from people",
        "workspaceId": "1",
        "workbookId": "wb_1",
        "enrich": True,
    }
    with pytest.raises(PayloadNotAllowed):
        validate_request("POST", "/v3/workspaces/1/search-dsl/send-to-table", payload)


def test_send_to_table_accepts_table_name():
    payload = {
        "dslQuery": "select from people",
        "workspaceId": "1",
        "workbookId": "wb_1",
        "tableName": "client | list | 1/1 | 2026-10-02",
        "resolvedResourceValues": {},
        "previewEntityIds": ["a"],
    }
    validate_request("POST", "/v3/workspaces/1/search-dsl/send-to-table", payload)


def test_schema_check_rejects_action_columns():
    assert_free_schema([{"name": "Domain", "type": "basic"}])
    with pytest.raises(PayloadNotAllowed):
        assert_free_schema([{"name": "Email", "type": "action"}])
