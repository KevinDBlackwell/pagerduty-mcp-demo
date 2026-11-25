"""Tests that go through the MCP layer."""

import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from pd_mcp import server
from pd_mcp.client import PagerDutyClient
from pd_mcp.fake_api import FakePagerDuty


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    fake = FakePagerDuty()
    monkeypatch.setattr(server, "_client", PagerDutyClient("test-token", transport=fake.transport(), sleep=lambda s: None))
    return fake


def run(coro):
    return asyncio.run(coro)


WRITE_TOOLS = {"acknowledge_incident"}


def test_tools_are_listed_with_descriptions_and_only_ack_writes():
    listed = {t.name: t for t in run(server.mcp.list_tools())}
    assert {"who_is_on_call", "list_open_incidents", "get_incident_context", "find_services",
            "acknowledge_incident"} <= set(listed)
    for name, t in listed.items():
        assert t.annotations.read_only_hint is (name not in WRITE_TOOLS)
        assert len(t.description) > 40


def test_acknowledge_schema_defaults_to_dry_run():
    ack = next(t for t in run(server.mcp.list_tools()) if t.name == "acknowledge_incident")
    props = ack.input_schema["properties"]
    assert props["dry_run"]["default"] is True
    assert "ask the user" in props["requester_email"]["description"].lower()


def test_acknowledge_via_mcp_is_dry_run_unless_told_otherwise(fake_client):
    result = run(server.mcp.call_tool("acknowledge_incident",
                                      {"incident": 1001, "requester_email": "ana@northwind.example"}))
    assert json.loads(result.content[0].text)["status"] == "dry_run"
    assert fake_client.writes == []
    result = run(server.mcp.call_tool("acknowledge_incident",
                                      {"incident": "#1001", "requester_email": "ana@northwind.example",
                                       "dry_run": False}))
    assert json.loads(result.content[0].text)["status"] == "acknowledged"
    assert len(fake_client.writes) == 1


def test_acknowledge_api_failure_becomes_tool_error(fake_client):
    fake_client.fail_next = [(403, {})]
    with pytest.raises(ToolError):
        run(server.mcp.call_tool("acknowledge_incident",
                                 {"incident": 1001, "requester_email": "ana@northwind.example"}))


def test_call_tool_returns_structured_content():
    result = run(server.mcp.call_tool("who_is_on_call", {"service": "Payments"}))
    assert result.is_error is False
    data = json.loads(result.content[0].text)  # dict results are returned as JSON text
    assert data["on_call"][0]["people"][0]["last_name"] == "de la Cruz"


def test_invalid_enum_is_rejected_by_schema():
    with pytest.raises(ToolError):
        run(server.mcp.call_tool("list_open_incidents", {"urgency": "urgent"}))


def test_api_failure_becomes_tool_error_with_hint(fake_client):
    fake_client.fail_next = [(401, {})]
    with pytest.raises(ToolError) as e:
        run(server.mcp.call_tool("find_services", {"query": "x"}))
    assert "token" in str(e.value) and "Do not retry" in str(e.value)


def test_missing_token_becomes_tool_error_with_hint(monkeypatch):
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.delenv("PD_MCP_FAKE", raising=False)
    monkeypatch.setenv("PAGERDUTY_API_TOKEN", "")
    with pytest.raises(ToolError) as e:
        run(server.mcp.call_tool("list_open_incidents", {"urgency": "high"}))
    assert "PAGERDUTY_API_TOKEN" in str(e.value) and "Do not retry" in str(e.value)
